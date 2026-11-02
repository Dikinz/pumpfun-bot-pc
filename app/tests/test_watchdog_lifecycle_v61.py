import asyncio,json,time,sqlite3
import pytest
import runtime_control as rt,main,config,database as db,aggregate_export
from test_primary_integrity import fresh

def sample(**kw):return dict(ts=100.,session='current',pid=7,state='RUNNING',loop_pulse_age_s=.1,**kw)

def test_transient_read_failure_does_not_kill_and_retains_trigger():
    w=rt.ChildWatchdog('current',7)
    assert not w.observe({'read_error':'PermissionError','errno':13},100,0)
    assert not w.observe(sample(),101,1)
    assert w.snapshot()['state']=='HEALTHY'
    assert w.events[-1]['state']=='RECOVERED'
    assert w.events[-1]['trigger']['snapshot']=={'read_error':'PermissionError','errno':13}

def test_stall_needs_repeat_and_recovery_cancels_suspicion():
    w=rt.ChildWatchdog('current',7);s=sample();s['loop_pulse_age_s']=16
    assert not w.observe(s,100,0)
    s['loop_pulse_age_s']=0
    assert w.pending['sample']['snapshot']['loop_pulse_age_s']==16
    assert not w.observe(s,101,1) and w.pending is None
    s['loop_pulse_age_s']=17;s['ts']=102
    assert not w.observe(s,102,2)
    assert w.observe(s,104,4)
    assert w.confirmed['trigger']['snapshot_age_s']==0
    assert w.confirmed['repeat']['snapshot_age_s']==2

@pytest.mark.parametrize('bad',[{},dict(read_error='JSONDecodeError'),dict(session='old',pid=7,ts=100),dict(session='current',pid=8,ts=100)])
def test_unreadable_or_foreign_probe_requires_persistent_failure(bad):
    w=rt.ChildWatchdog('current',7)
    assert not w.observe(bad,100,0)
    assert not w.observe(bad,102,2)
    assert w.observe(bad,116,16)

def test_corrupt_file_captures_read_error(tmp_path):
    p=tmp_path/'x.db';rt.status_path(p).write_text('{')
    assert rt.watchdog_read(p)['read_error']=='JSONDecodeError'

def test_market_task_cancel_skips_fifteen_second_reconnect_grace(fresh,monkeypatch):
    async def run():
        received=asyncio.Event()
        class WS:
            async def recv(self):received.set();await asyncio.Event().wait()
            async def close(self):pass
        async def connect(*a,**kw):return WS()
        monkeypatch.setattr(main.websockets,'connect',connect)
        monkeypatch.setattr(config,'PUMPAPI_CLOSE_GRACE',15)
        task=asyncio.create_task(main.pumpapi_trades(asyncio.Queue()))
        await asyncio.wait_for(received.wait(),1)
        started=time.monotonic()
        assert await rt.cancel_tasks([('trade_stream',task)],config.DB,timeout=.5)
        assert time.monotonic()-started<.5 and task.cancelled()
    asyncio.run(run())

def test_stuck_cleanup_is_named_and_second_cancel_can_finish(fresh,monkeypatch):
    events=[];monkeypatch.setattr(main.system_health,'event',lambda *a,**kw:events.append((a,kw)))
    async def run():
        started=asyncio.Event()
        async def worker():
            try:started.set();await asyncio.Event().wait()
            finally:await asyncio.Event().wait()
        task=asyncio.create_task(worker());await started.wait()
        assert await rt.cancel_tasks([('stuck_test',task)],config.DB,timeout=.01)
    asyncio.run(run())
    assert events[0][1]['pending'][0]['task']=='stuck_test'

def test_aggregate_payoff_reconciles_and_does_not_invent_rejected_profit(fresh):
    import paper_capital
    main.buy('one',1);tid=main.P['one']['trade_id']
    paper_capital.record(fresh,tid,'BUY',3,{'pool_fee_rate':.0125})
    main.sell('one',1.1,'TEST');paper_capital.record(fresh,tid,'SELL',3.3,{'pool_fee_rate':.0125})
    report=aggregate_export.summary(fresh,'primary')
    assert report['payoff']['closed_with_complete_costs']==1
    assert report['trades'][0]['net']==pytest.approx(report['payoff']['net'])
    assert report['payoff']['payoff_ratio'] is None
    assert report['scope']=='WHOLE_CAMPAIGN'

def test_gap_blocking_has_distinct_reason(fresh):
    db.setm('branch_role','ROUTE_PAPER');db.setm('branch_input_gap',1)
    assert main.transport_block_reason()=='BRANCH_PERSISTED_INPUT_GAP'
    db.setm('branch_input_gap',0);db.setm('branch_input_seq',0)
    assert main.transport_block_reason()=='BRANCH_NO_INPUT_YET'

def test_two_long_waiting_exits_release_both_slots_only_on_fresh_checks(fresh,monkeypatch):
    import route_branch,paper_capital
    from test_route_exit_v59 import candidate,save_check,WALLET_RECEIPT
    fresh.executescript(route_branch.SCHEMA)
    tids=[]
    for n in range(2):
        mint='waiting_'+str(n);monkeypatch.setattr(config,'EXECUTION_MODEL_ENABLED',False)
        assert main.buy(mint,1)[0]
        tid=main.P[mint]['trade_id'];tids.append(tid);main.S.setdefault(mint,main.state())
        paper_capital.record(fresh,tid,'BUY',3,{'pool_fee_rate':.0125})
        fresh.execute("INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,size,reason,trade_id) VALUES(?,?,'SELL','PENDING',?,1,3,'TP2',?)",('exit'+str(n),mint,time.time()-4800,tid))
        row,j,r,inv=candidate(tid,time.time()-60)
        row.update(id='old'+str(n),mint=mint);j['mint']=mint;inv['mint']=mint;r['sale']['mint']=mint
        save_check(fresh,row,j,r,inv)
    monkeypatch.setattr(config,'EXECUTION_MODEL_ENABLED',True)
    before=paper_capital.cash(fresh);route_branch.poll(main)
    assert fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==2
    assert paper_capital.cash(fresh)==before
    for n,tid in enumerate(tids):
        mint='waiting_'+str(n);row,j,r,inv=candidate(tid)
        row.update(id='fresh'+str(n),mint=mint);j['mint']=mint;inv['mint']=mint;r['sale']['mint']=mint
        save_check(fresh,row,j,r,inv)
    route_branch.poll(main);route_branch.poll(main)
    assert fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0 and not main.P
    assert fresh.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==2
    assert fresh.execute("SELECT COUNT(*) FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING'").fetchone()[0]==0
    assert paper_capital.cash(fresh)==pytest.approx(before+2*WALLET_RECEIPT)
