import asyncio,json,time,threading,sqlite3
from contextlib import closing
import pytest
import main,config,database as db,monitor_service as ms,position_monitor as pm,priority_exit as pe,exit_wait_v65 as wait
from test_primary_integrity import fresh
from test_parallel_paper_v54 import opened as base_opened

def opened(c):
    __import__('wallet_costs').ensure(c)
    tid=base_opened(c)
    c.execute('UPDATE positions SET opened_ts=? WHERE trade_id=?',(time.time()-100,tid))
    return tid

def test_signal_bypasses_cadence_once_and_preserves_readonly_source(fresh):
    tid=opened(fresh);now=time.time();before=fresh.execute('select count(*) from trades').fetchone()[0]
    with closing(ms.connect()) as c:
        old=pm.next_job(c,now-1);pm.finish(c,old,{'status':'REQUEST_ERROR'},now=now-.5)
        assert pm.next_job(c,now) is None
        pe.observe(c,now);pe.observe(c,now+.01);job=pe.dispatch(c,now+.02)
        assert job['trade_id']==tid and job['scheduled_ts']>=fresh.execute("select signal_ts from paper_orders_v0720 where side='SELL'").fetchone()[0]
        assert job['priority_exit']['queue_wait_ms']==pytest.approx(20,abs=.001)
        pe.worker_started(c,job,now+.03);pm.finish(c,job,{'status':'REQUEST_ERROR'},now=now+.3)
        assert pe.dispatch(c,now+1) is None
        report=pe.summary(c)['rows'][0];assert report['status']=='REQUEST_ERROR' and report['worker_ms']==pytest.approx(270,abs=.001)
        assert c.execute('select count(*) from priority_exit_requests_v65').fetchone()[0]==1
    assert fresh.execute('select count(*) from trades').fetchone()[0]==before

def test_pre_signal_running_result_is_not_reused(fresh):
    tid=opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        old=pm.next_job(c,now-20);pe.observe(c,now)
        assert pe.dispatch(c,now) is None
        assert pe.summary(c)['rows'][0]['blocker']=='CHECK_ALREADY_RUNNING'
        pm.finish(c,old,{'status':'SUPERSEDED_BY_EXIT_SIGNAL'},now=now)
        job=pe.dispatch(c,now+.1);assert job['id']!=old['id']
        from test_route_exit_v59 import candidate
        import route_branch
        row,j,r,inv=candidate(tid,now=now+.3);j['scheduled_ts']=now-20
        assert route_branch.assess(fresh,row,j,r,now-10,inv,now+.3)[0]=='STALE_OR_PRE_SIGNAL'

def test_technical_backoff_is_not_bypassed(fresh):
    tid=opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        for i in range(3):
            old=pm.next_job(c,now-31+i*10);assert old
            pm.finish(c,old,{'status':'REQUEST_ERROR'},now=now-30+i*10)
        pe.observe(c,now);assert pe.dispatch(c,now) is None
        assert pe.summary(c)['rows'][0]['blocker']=='TECHNICAL_BACKOFF'
        assert pe.dispatch(c,now+21)

def test_close_and_restart_do_not_duplicate_priority_request(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        pe.observe(c,now);job=pe.dispatch(c,now);pm.recover(c);pe.recover(c);pe.observe(c,now+1)
        assert pe.dispatch(c,now+1) is None
        assert pe.summary(c)['rows'][0]['status']=='INTERRUPTED_RESTART'
    # A queued request whose position closed is retired without any simulation.
    fresh.execute("update paper_orders_v0720 set order_id='exit2'")
    with closing(ms.connect()) as c:
        pe.observe(c,now+2);fresh.execute('delete from positions');pe.observe(c,now+3)
        assert pe.summary(c)['rows'][-1]['status']=='CLOSED_BEFORE_DISPATCH'

def test_no_mint_event_does_not_mean_dead_or_executable(fresh):
    opened(fresh);now=time.time();db.setm('trade_last_market_ts',now)
    assert wait.rows(fresh,now)[0]['state']=='NO_MINT_EVENTS_CAUSE_UNPROVEN'
    assert wait.rows(fresh,now+30)[0]['state']=='MARKET_STREAM_NOT_FRESH'
    fresh.execute("insert into exit_diagnostics_v0722(order_id,raw_count,other_pool,last_code) values('exit',1,1,'OTHER_POOL')")
    assert wait.rows(fresh,now)[0]['state']=='MINT_EVENTS_OTHER_POOL'
    assert fresh.execute("select count(*) from trades where side='SELL'").fetchone()[0]==0

def test_break_even_preserves_setup_costs():
    report=wait.economics({'rows':[dict(role='route',trade_id='t',mint='m',size=3,gross=.5,net=.15,entry_cost=.05,exit_cost=.3,settlement={'route':'PUMP_CURVE','components':{'token_account_setup':.15,'volume_account_setup':.14}})]})
    row=report['rows'][0];assert row['break_even_gross']==pytest.approx(.35)
    assert row['break_even_gross_pct']==pytest.approx(100*.35/3)
    assert row['token_setup']==.15 and report['count']==1

def test_scheduler_preempts_pre_signal_check_then_starts_one_fresh_check(fresh,monkeypatch):
    # Run the actual scheduler with controllable workers; no network and no tick.
    tid=opened(fresh);fresh.execute("delete from paper_orders_v0720")
    stop=threading.Event();events=[];active=0;peak=0
    async def worker(job,connection=None):
        nonlocal active,peak
        active+=1;peak=max(peak,active);events.append(('START',job))
        try:
            if not job.get('priority_exit'):
                await asyncio.sleep(.1)
                fresh.execute("insert into paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,size,reason,trade_id) values('real-signal','m','SELL','PENDING',?,1,3,'TP2',?)",(time.time(),tid))
                await asyncio.sleep(3)
            else:
                pe.worker_started(connection,job,time.time());await asyncio.sleep(.02)
                pm.finish(connection,job,{'status':'OFFLINE_PRIORITY_TEST'});stop.set()
        except asyncio.CancelledError:
            pm.finish(connection,job,{'status':job.get('cancel_status','INTERRUPTED_STOP')});events.append(('CLEANED',job['id']));raise
        finally:active-=1
    monkeypatch.setattr(pm,'run_job',worker)
    asyncio.run(asyncio.wait_for(ms.run(config.DB,0,stop_event=stop),timeout=4))
    starts=[x[1] for x in events if x[0]=='START'];assert len(starts)==2 and peak<=2
    assert starts[1].get('priority_exit') and starts[1]['scheduled_ts']>=starts[1]['priority_exit']['signal_ts']
    with closing(ms.connect(readonly=True)) as c:
        rows=pe.summary(c)['rows'];assert len(rows)==1 and rows[0]['status']=='OFFLINE_PRIORITY_TEST'
        assert rows[0]['queue_wait_ms']<1000
        assert c.execute("select status from position_checks_v0730 where id=?",(starts[0]['id'],)).fetchone()[0]=='SUPERSEDED_BY_EXIT_SIGNAL'
    assert ms.read_state()['status']=='STOPPED' and active==0

def test_preempted_real_subprocess_is_reaped_before_replacement(fresh,monkeypatch,tmp_path):
    import sys
    opened(fresh);now=time.time();procs=[]
    real=asyncio.create_subprocess_exec
    async def sleeper(*args,**kwargs):
        proc=await real(sys.executable,'-c','import time; time.sleep(20)',**kwargs);procs.append(proc);return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',sleeper)
    async def case(c):
        job=pm.next_job(c,now);task=asyncio.create_task(pm.run_job(job,c))
        deadline=time.monotonic()+2
        while not procs and time.monotonic()<deadline:await asyncio.sleep(.01)
        assert procs
        pe.observe(c,time.time());pe.preempt(c,{'regular':(task,job)})
        with pytest.raises(asyncio.CancelledError):await task
        assert procs[0].returncode is not None
        assert c.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()[0]=='SUPERSEDED_BY_EXIT_SIGNAL'
        assert pe.dispatch(c,time.time())
    monkeypatch.setattr(pm,'ROOT',tmp_path/'evidence')
    with closing(ms.connect()) as c:asyncio.run(case(c))

def test_priority_result_still_requires_fresh_valid_exact_amount_before_settlement(fresh,monkeypatch):
    import route_branch as route,paper_capital as cap
    from test_route_exit_v59 import candidate
    tid=opened(fresh);fresh.executescript(route.SCHEMA)
    row,j,r,inv=candidate(tid);fresh.execute('INSERT OR REPLACE INTO quote_inventory_v0723(trade_id,mint,size_usdc,sol_usdc,token_units,entry_sol_price,raw_amount,decimals) VALUES(?,?,?,?,?,?,?,?)',tuple(inv[k] for k in ('trade_id','mint','size_usdc','sol_usdc','token_units','entry_sol_price','raw_amount','decimals')))
    before=cap.cash(fresh)
    with closing(ms.connect()) as c:
        now=time.time();pe.observe(c,now);job=pe.dispatch(c,now);pe.worker_started(c,job,time.time())
        r.update(scheduled_ts=job['scheduled_ts'],snapshot_received_ts=time.time(),net_status='NET_BELOW_FLOOR',minimum_net_lamports=30000000)
        pm.finish(c,job,r,now=time.time())
    route.poll(main);route.poll(main)
    assert not main.P and cap.cash(fresh)==pytest.approx(before+2.5)
    assert fresh.execute("SELECT count(*) FROM trades WHERE side='SELL'").fetchone()[0]==1
    assert __import__('primary_integrity').summarize(fresh)['status']=='OK'

def test_stop_cannot_leave_a_live_priority_queue(fresh):
    opened(fresh)
    with closing(ms.connect()) as c:
        pe.observe(c,time.time());pe.stop(c,time.time());pe.recover(c)
        assert pe.summary(c)['rows'][0]['status']=='INTERRUPTED_STOP'
        assert not pe.waiting(c)

def test_preemption_never_cancels_another_priority_worker(fresh):
    tid=opened(fresh)
    class Task:
        cancelled=False
        def done(self):return False
        def cancel(self):self.cancelled=True
    a,b=Task(),Task()
    with closing(ms.connect()) as c:
        pe.observe(c,time.time())
        pe.preempt(c,{'exit0':(a,dict(id='a',trade_id='other',priority_exit={'order_id':'other'})), 'regular':(b,dict(id='b',trade_id=tid))})
        assert not a.cancelled and b.cancelled

def test_cleanup_failure_is_not_reported_as_normal_stop(fresh,monkeypatch):
    opened(fresh)
    async def fail(*a,**kw):raise RuntimeError('SIMULATION_CHILD_CLEANUP_UNCONFIRMED')
    monkeypatch.setattr(pm,'run_job',fail)
    with pytest.raises(RuntimeError,match='SIMULATION_CHILD_CLEANUP_UNCONFIRMED'):
        asyncio.run(ms.run(config.DB,0,stop_event=threading.Event()))
    assert ms.read_state()['status']=='FAILED'
    with closing(ms.connect(readonly=True)) as c:assert pe.summary(c)['rows'][0]['status']=='CLEANUP_FAILED'
