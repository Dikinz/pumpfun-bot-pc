import asyncio, json, sqlite3, subprocess, sys, time
from pathlib import Path
from contextlib import contextmanager
import pytest
import config, database as db, main, paper_capital, system_health as sh
import strategy_diagnostics as sd
from test_primary_integrity import fresh

def snapshot(c):
    return dict(capital=paper_capital.summary(c),positions=[],runtime={'state':'STOPPED'},monitor={})

def closed_with_ledger(c,m,price):
    assert main.buy(m,1)[0]
    tid=main.P[m]['trade_id'];size=main.P[m]['size']
    paper_capital.record(c,tid,'BUY',size,{'pool_fee_rate':.0125})
    main.sell(m,price,'TEST')
    paper_capital.record(c,tid,'SELL',size*price,{'pool_fee_rate':.0125})

def test_empty_sample_is_not_profitability(fresh):
    r=sd.summary(fresh,snapshot(fresh),config.DB)
    assert r['strategy']['closed']==0 and r['strategy']['mean_net'] is None
    assert r['strategy']['profit_factor'] is None and not r['recorder_available']

def test_full_cohort_not_dashboard_last_100(fresh):
    for i in range(105):
        closed_with_ledger(fresh,'m'+str(i),1.01)
    capital=paper_capital.summary(fresh)
    r=sd.summary(fresh,snapshot(fresh),config.DB)
    assert r['strategy']['accounted']==105
    assert r['strategy']['net']==pytest.approx(capital['closed_net'])
    assert r['strategy']['by_reason'][0]['count']==105
    assert not any(x['code']=='SMALL_SAMPLE' for x in r['issues'])

def test_missing_costs_never_treated_as_zero(fresh):
    main.buy('a',1);main.sell('a',1.1,'TEST')
    fresh.execute("DELETE FROM capital_ledger_v0721 WHERE side='SELL'")
    r=sd.summary(fresh,snapshot(fresh),config.DB)
    assert r['status']=='CRITICAL' and r['strategy']['accounted']==0
    assert {x['code'] for x in r['issues']} >= {'CAPITAL_RECONCILIATION','MISSING_OR_INVALID_CLOSED_COSTS'}

def test_ledger_count_joins_and_drawdown(fresh):
    for m,p in [('win',1.2),('loss',.9)]:closed_with_ledger(fresh,m,p)
    r=sd.summary(fresh,snapshot(fresh),config.DB)['strategy']
    assert r['wins']==1 and r['losses']==1
    assert r['cost']==pytest.approx(paper_capital.summary(fresh)['total_cost'])
    assert r['max_closed_drawdown']>0

def test_failure_flags_and_gap_warnings(fresh):
    snap=snapshot(fresh);snap.update(positions=[{'mint':'a'}],runtime={'state':'FAILED'})
    snap['monitor']=dict(service={'status':'NO_HEARTBEAT'},connection={'warning':True,'gaps':{'total_position_seconds':4}},health={'technical_errors':3})
    r=sd.summary(fresh,snap,config.DB)
    assert r['status']=='CRITICAL'
    assert {x['code'] for x in r['issues']} >= {'MONITOR_ERROR_STREAK','PRIMARY_HEARTBEAT_UNKNOWN','OPEN_POSITIONS_WITHOUT_MONITOR','MONITOR_COVERAGE_GAPS'}

def test_error_record_never_contains_exception_secret(tmp_path):
    primary=tmp_path/'test.db'
    try:raise ValueError('https://secret.example/?api-key=TOPSECRET')
    except ValueError as exc:assert sh.failure(primary,'test',exc)
    text=sh.paths(primary)['journal'].read_text()
    assert 'TOPSECRET' not in text and 'secret.example' not in text
    record=json.loads(text)
    assert record['error_type']=='ValueError' and record['frames'][-1]['function']=='test_error_record_never_contains_exception_secret'

def test_bounded_journal_and_partial_line(tmp_path,monkeypatch):
    monkeypatch.setattr(sh,'MAX_LOG_BYTES',100)
    primary=tmp_path/'test.db'
    for i in range(30):assert sh.event(primary,'TEST',n=i)
    p=sh.paths(primary)['journal']
    assert len(list(tmp_path.glob('*jsonl*')))==3
    with p.open('a') as f:f.write('{partial')
    assert sh.history(primary)[-1]['n']==29

def test_corrupt_state_does_not_break_report(fresh):
    sh.paths(config.DB)['state'].write_text('{partial')
    assert not sd.summary(fresh,snapshot(fresh),config.DB)['recorder_available']

def test_stale_recorder_and_failed_stop(fresh):
    sh.write_state(config.DB,dict(status='RUNNING',heartbeat_ts=time.time()-60,alerts=[]))
    r=sd.summary(fresh,snapshot(fresh),config.DB)
    assert 'RECORDER_HEARTBEAT_INVALID' in {x['code'] for x in r['issues']}
    rec=sh.Recorder(config.DB);rec.stop(1,'COMPONENT_OR_STARTUP_FAILURE')
    assert sd.summary(fresh,snapshot(fresh),config.DB)['status']=='CRITICAL'

def test_independent_sampler_real_child(fresh):
    p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    try:
        rec=sh.Recorder(config.DB);r=rec.sample({'bot':p},force=True)
        assert r['processes'] and r['processes'][0]['pid']==p.pid, r
        assert r['total_rss_mb']>=0
        assert sh.read_state(config.DB)['database_identity']==sh.identity(config.DB)
        assert rec.sample({'bot':p}) is r
    finally:p.terminate();p.wait(timeout=5)
    r=rec.sample({'bot':p},force=True)
    assert 'BOT_EXITED' in r['alerts']
    rec.stop(0,'USER_STOP')
    assert sh.read_state(config.DB)['status']=='STOPPED'

def test_background_failure_is_recorded_and_propagated(tmp_path):
    async def fail():raise LookupError('private message')
    with pytest.raises(LookupError):asyncio.run(sh.supervise('worker',fail(),tmp_path/'test.db'))
    assert sh.history(tmp_path/'test.db')[-1]['component']=='worker'

def test_required_task_return_vs_reference_refusal(tmp_path):
    async def done():return
    with pytest.raises(RuntimeError):asyncio.run(sh.supervise('heartbeat',done(),tmp_path/'test.db'))
    asyncio.run(sh.supervise('clock_reference',done(),tmp_path/'test.db',allow_return=True))

def test_background_cancellation_not_failure(tmp_path):
    async def cancel():raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):asyncio.run(sh.supervise('worker',cancel(),tmp_path/'test.db'))
    assert not sh.paths(tmp_path/'test.db')['journal'].exists()

def test_atomic_telemetry_rollback_and_single_commit(fresh):
    trace=[];fresh.set_trace_callback(trace.append)
    db.hot_trade_tick(123)
    assert sum(x=='BEGIN IMMEDIATE' for x in trace)==1 and sum(x=='COMMIT' for x in trace)==1
    assert db.getm('last_trade_ts')=='123' and db.getm('trade_stream_state')=='LIVE'
    with pytest.raises(RuntimeError):
        with db.atomic():
            db.setmany({'last_trade_ts':999,'test_new_key':1});raise RuntimeError('rollback')
    assert db.getm('last_trade_ts')=='123' and db.getm('test_new_key','missing')=='missing'
    fresh.set_trace_callback(None)

def test_dashboard_requires_readiness_and_identity(monkeypatch):
    import launcher
    class Child:
        pid=123
        def poll(self):return None
    @contextmanager
    def response(*a,**k):
        import io
        yield io.BytesIO(json.dumps(dict(version=config.VERSION,database_identity='WRONG')).encode())
    monkeypatch.setattr('urllib.request.urlopen',response)
    with pytest.raises(RuntimeError,match='identity mismatch'):launcher.wait_dashboard(Child(),timeout=.01)
    monkeypatch.setattr(launcher.time,'sleep',lambda _:None)
    @contextmanager
    def failed(*a,**k):
        raise OSError('offline');yield
    monkeypatch.setattr('urllib.request.urlopen',failed)
    with pytest.raises(RuntimeError,match='timeout'):launcher.wait_dashboard(Child(),timeout=.001)

def test_dashboard_dead_process_fails_readiness():
    import launcher
    class Dead:
        def poll(self):return 1
    with pytest.raises(RuntimeError,match='exited'):launcher.wait_dashboard(Dead())

def test_export_recorder_no_env(tmp_path):
    import zipfile
    primary=tmp_path/'test.db';sh.event(primary,'TEST');sh.write_state(primary,{'status':'STOPPED'})
    sh.export(primary,tmp_path)
    with zipfile.ZipFile(tmp_path/'system_flight_recorder.zip') as z:
        assert len(z.namelist())==2 and not any('.env' in n for n in z.namelist())

def test_launcher_monitor_failure_stops_all_children(fresh,monkeypatch):
    import launcher
    import release_guard
    monkeypatch.setattr(release_guard,'local_database',lambda *a:None)
    monkeypatch.setattr(release_guard,'available',lambda *a:None)
    class Child:
        pid=99999999
        stdin=None
        def __init__(self,code=None):self.returncode=code
        def poll(self):return self.returncode
        def send_signal(self,_):self.returncode=0
        def terminate(self):self.returncode=-15
        def kill(self):self.returncode=-9
        def wait(self,timeout=None):return self.returncode
    dash=Child();bot=Child();monitor=Child(7);children=iter([dash,bot,monitor])
    monkeypatch.setattr(launcher,'call',lambda *a:0)
    monkeypatch.setattr(launcher,'setup_env',lambda:None)
    monkeypatch.setattr(launcher,'wait_dashboard',lambda *a,**k:None)
    monkeypatch.setattr(launcher.subprocess,'Popen',lambda *a,**k:next(children))
    monkeypatch.setattr(launcher.webbrowser,'open',lambda *a:True)
    assert launcher.start()==1
    assert bot.poll() is not None and dash.poll() is not None
    assert sh.read_state(config.DB)['status']=='FAILED'
    assert any(r['kind']=='REQUIRED_PROCESS_EXIT' and r['component']=='monitor' for r in sh.history(config.DB))

def test_corrupt_database_still_exports_emergency_journal(fresh,monkeypatch,tmp_path):
    import launcher,zipfile
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['launcher.py','results'])
    def broken():raise sqlite3.DatabaseError('a secret URL must not be exported')
    monkeypatch.setattr(launcher,'results',broken)
    assert launcher.main()==1
    report=next((tmp_path/'results').glob('*.zip'))
    with zipfile.ZipFile(report) as z:
        assert json.loads(z.read('INCOMPLETE.json'))['database_included'] is False
        assert 'system_flight_recorder.zip' in z.namelist()
    assert 'secret URL' not in sh.paths(config.DB)['journal'].read_text()

def test_results_active_guard_not_bypassed(monkeypatch):
    import launcher
    monkeypatch.setattr(sys,'argv',['launcher.py','results'])
    def active():raise RuntimeError('Stop the bot')
    monkeypatch.setattr(launcher,'results',active)
    with pytest.raises(RuntimeError,match='Stop'):launcher.main()
