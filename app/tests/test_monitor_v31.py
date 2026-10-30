import asyncio,json,sqlite3,time,subprocess,sys,os
from pathlib import Path
from contextlib import closing
import pytest
import config,main,database as db,wallet_costs as wc,position_monitor as pm,monitor_service as ms,price_exit_trial as trial,paper_capital as cap
from test_primary_integrity import fresh

def open_case(c):
    wc.ensure(c);assert main.buy('monitor-test-mint',1)[0]
    tid=main.P['monitor-test-mint']['trade_id']
    cap.record(c,tid,'BUY',3,{'pool_fee_rate':.0125})
    return tid

def checked(now,net=20000000):
    return dict(status='CHECKED',scheduled_ts=now-1,snapshot_received_ts=now-.2,
      reserves={'status':'RESERVES_COVER_GROSS','real_sol_lamports':1000000000},
      sale={'status':'LOCAL_SWAP_EXECUTED','seller_net_sol_delta_lamports':net,'synthetic_token_rent_lamports':1488440},
      zero_token_control_ok=True,net_status='NET_BELOW_FLOOR',minimum_net_lamports=25000000)

def test_monitor_database_cannot_write_primary_and_survives_primary_write_lock(fresh):
    open_case(fresh);before=cap.summary(fresh)
    with closing(ms.connect()) as c:
        with pytest.raises(sqlite3.OperationalError):c.execute("UPDATE paper.meta SET v='0' WHERE k='cash'")
        # Main connection holds an uncommitted write: independent private journal still works.
        fresh.execute('BEGIN IMMEDIATE')
        try:
            c.execute('BEGIN');job=pm.next_job(c,time.time());c.commit()
            assert job
            ms.journal('TEST_LOCK',{},c=c)
        finally:fresh.rollback()
        pm.finish(c,job,{'status':'REQUEST_TIMEOUT'})
        assert c.execute('SELECT COUNT(*) FROM position_checks_v0730').fetchone()[0]==1
    assert fresh.execute('SELECT COUNT(*) FROM position_checks_v0730').fetchone()[0]==0
    assert cap.summary(fresh)==before

def test_after_close_never_becomes_trial_or_first_problem(fresh):
    tid=open_case(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job=pm.next_job(c,now);job['inventory']={'sol_usdc':'100','size_usdc':'3'}
        main.sell('monitor-test-mint',.9,'TEST')
        ready=time.time()+.01;pm.finish(c,job,checked(ready),now=ready)
        assert trial.summary(c)['decisions']=={'AFTER_CLOSE':1}
        assert not trial.summary(c)['rows']
        assert pm.summary(c)['after_close_counts']=={'NET_BELOW_FLOOR':1}
        h=c.execute('SELECT first_problem_ts FROM position_health_v0730 WHERE trade_id=?',(tid,)).fetchone()
        assert h[0] is None

def test_first_fresh_price_exit_is_immutable_and_capital_unchanged(fresh):
    tid=open_case(fresh);before=cap.summary(fresh)
    with closing(ms.connect()) as c:
        now=time.time();a=pm.next_job(c,now);a['inventory']={'sol_usdc':'100','size_usdc':'3'}
        pm.finish(c,a,checked(now),now=now)
        b=pm.next_job(c,now+11);b['inventory']=a['inventory']
        pm.finish(c,b,checked(now+11,net=22000000),now=now+11)
        report=trial.summary(c)
        assert report['decisions']=={'ALREADY_SELECTED':1,'SELECTED':1}
        assert len(report['rows'])==1 and report['rows'][0]['check_id']==a['id']
        assert cap.summary(fresh)==before
        main.sell('monitor-test-mint',.8,'TEST')
        report=trial.summary(c)
        assert report['paired']==1 and report['late_excluded']==0
        assert float(report['rows'][0]['receipt_units'])==pytest.approx(1.851156)

def test_late_commit_reconciles_selection_out_of_comparison(fresh):
    tid=open_case(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job=pm.next_job(c,now);job['inventory']={'sol_usdc':'100','size_usdc':'3'}
        pm.finish(c,job,checked(now),now=now)
        main.sell('monitor-test-mint',.9,'TEST')
        # Simulate a primary fill timestamp committed only after the observation read.
        fresh.execute("UPDATE trades SET ts=? WHERE trade_id=? AND side='SELL'",(now-.01,tid))
        r=trial.summary(c)
        assert r['paired']==0 and r['late_excluded']==1
        assert pm.summary(c)['timing_counts']['AFTER_CLOSE']==1

def test_stale_or_failed_checks_cannot_trigger_price_rule(fresh):
    open_case(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job=pm.next_job(c,now);r=checked(now);r['snapshot_received_ts']=now-6
        pm.finish(c,job,r,now=now)
        assert not trial.summary(c)['rows']
        assert trial.summary(c)['decisions']=={'UNUSABLE_STALE_RESULT':1}

def test_deadline_warning_and_heartbeat_staleness(fresh,tmp_path):
    tid=open_case(fresh);now=time.time()
    with closing(ms.connect()) as c:
        job=pm.next_job(c,now);pm.finish(c,job,{'status':'REQUEST_TIMEOUT'},now=now)
        assert not ms.overdue(c,now+11)
        assert ms.overdue(c,now+13)[0]['late_s']==13-pm.INTERVAL
    ms.publish({'status':'RUNNING','heartbeat_ts':now,'pid':123})
    assert ms.service_status(now=now+16)['status']=='NO_HEARTBEAT'

def test_independent_process_runs_with_stale_primary_heartbeat(fresh,tmp_path):
    open_case(fresh);fresh.execute("INSERT OR REPLACE INTO meta VALUES('heartbeat_ts',?)",(str(time.time()-100),))
    gate=__import__('recovery_gate').Coordinator(config.DB,'offline-primary')
    script=tmp_path/'offline_monitor_process.py'
    script.write_text("""import sys,asyncio,time,threading
sys.path.insert(0,sys.argv[1])
import monitor_service as ms,position_monitor as pm
async def offline_job(job,connection=None):
    await asyncio.sleep(.1)
    pm.finish(connection,job,{'status':'OFFLINE_PROCESS_TEST'})
pm.run_job=offline_job
asyncio.run(ms.run_with_stdin(sys.argv[2],0))
""")
    proc=subprocess.Popen([sys.executable,str(script),str(Path.cwd()),config.DB],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+8;state={}
        while time.monotonic()<deadline:
            gate.save() # recovery publisher is independent of stale legacy heartbeat
            state=ms.read_state()
            if state.get('primary_stale') and state.get('heartbeat_ts') and ms.paths()['db'].exists():
                with closing(ms.connect(readonly=True)) as c:
                    if c.execute("SELECT COUNT(*) FROM position_checks_v0730 WHERE status='OFFLINE_PROCESS_TEST'").fetchone()[0]:break
            time.sleep(.1)
        else:raise AssertionError('Independent monitor did not finish check: '+str(state))
        assert state['pid']!=os.getpid() and state['primary_stale']
        proc.stdin.close();proc.wait(timeout=8)
        assert proc.returncode==0,proc.stderr.read().decode()
        assert ms.read_state()['status']=='STOPPED'
        assert 'HEARTBEAT' in ms.paths()['journal'].read_text()
    finally:
        for p in (proc,):
            if p.poll() is None:p.terminate();p.wait()


def test_export_contains_separate_database_timing_and_journal(fresh,tmp_path):
    import zipfile
    open_case(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job=pm.next_job(c,now);pm.finish(c,job,{'status':'REQUEST_TIMEOUT'},now=now)
        ms.journal('TEST',{},c=c)
    archive=ms.export(config.DB,tmp_path)
    with zipfile.ZipFile(archive) as z:
        assert 'monitor_timing_v0731.json' in z.namelist()
        assert any(n.endswith('_monitor.db') for n in z.namelist())
        assert any(n.endswith('_monitor_activity.jsonl') for n in z.namelist())
