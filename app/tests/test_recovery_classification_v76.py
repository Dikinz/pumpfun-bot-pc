"""Controlled offline PAPER checks; no OS suspension, clock writes or RPC."""
import asyncio,json,os,time
from pathlib import Path
import pytest
import recovery_gate as rg,position_monitor as pm,monitor_service as ms
import config,main,database as db,execution_model as ex,paper_capital as capital
from test_primary_integrity import fresh
from test_execution_model import env
from test_monitor_v31 import open_case,checked
from test_recovery_gate_v75 import Clock,tick,report,recovered

@pytest.fixture(autouse=True)
def isolate(monkeypatch):monkeypatch.setattr(rg,'ACTIVE',None)

@pytest.mark.parametrize('outcome',['SUPERSEDED_BY_EXIT_SIGNAL','INTERRUPTED_STOP','REQUEST_TIMEOUT','REQUEST_ERROR','WORKER_ERROR','CLEANUP_FAILED'])
@pytest.mark.parametrize('gate_state',['READY','RECOVERING','STOPPED','STALE_CLIENT'])
def test_no_result_reason_survives_closed_or_open_admission(fresh,monkeypatch,outcome,gate_state):
    tid=open_case(fresh);before=capital.summary(fresh)
    clock=Clock();g=rg.Coordinator(config.DB,'run',clock);monkeypatch.setattr(rg,'ACTIVE',g)
    with ms.connect() as c:
        job=pm.next_job(c,time.time())
        if gate_state=='RECOVERING':clock.step(230);g.status()
        elif gate_state=='STOPPED':g.close()
        elif gate_state=='STALE_CLIENT':
            client_clock=Clock();client=rg.Client(config.DB,client_clock)
            for _ in range(4):client_clock.step(1);client.status()
            assert g.data['generation']==0 and g.data['state']=='READY'
            monkeypatch.setattr(rg,'ACTIVE',client)
        pm.finish(c,job,dict(status=outcome),now=time.time())
        row=c.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
        detail=json.loads(row['result_json']);admit=detail['recovery_admission_v76']
        assert row['status']==detail['status']==admit['classified_status']==outcome
        assert admit['allowed']==(gate_state=='READY')
        if gate_state=='STALE_CLIENT':assert admit['reason']=='PRIMARY_GATE_PULSE_STALE'
        assert 'recovery_invalidated_v75' not in detail
        pm.finish(c,job,checked(time.time()),now=time.time()) # immutable terminal row
        assert c.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()[0]==outcome
        assert not c.execute('SELECT 1 FROM price_trial_exits_v0731').fetchone()
    assert capital.summary(fresh)==before and tid==main.P['monitor-test-mint']['trade_id']

def test_cancelled_worker_keeps_sell_preemption_reason_after_pause(fresh,monkeypatch,tmp_path):
    open_case(fresh);clock=Clock();g=rg.Coordinator(config.DB,'run',clock);monkeypatch.setattr(rg,'ACTIVE',g)
    monkeypatch.setattr(pm,'ROOT',tmp_path)
    async def scenario(c,job):
        started=asyncio.Event()
        async def child(*args,**kwargs):started.set();await asyncio.Future()
        monkeypatch.setattr(pm,'launch_check',child)
        task=asyncio.create_task(pm.run_job(job,c));await started.wait()
        clock.step(230);g.status();job['cancel_status']='SUPERSEDED_BY_EXIT_SIGNAL';task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    with ms.connect() as c:
        job=pm.next_job(c,time.time());asyncio.run(scenario(c,job))
        row=c.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
        assert row['status']=='SUPERSEDED_BY_EXIT_SIGNAL'
        assert json.loads(row['result_json'])['recovery_admission_v76']['reason']=='LOOP_GAP'

def test_old_sale_stays_invalidated_with_explicit_epoch_reason(fresh,monkeypatch):
    open_case(fresh);clock=Clock();g=rg.Coordinator(config.DB,'run',clock);monkeypatch.setattr(rg,'ACTIVE',g)
    with ms.connect() as c:
        job=pm.next_job(c,time.time());clock.step(230);g.status();recovered(g,clock)
        now=time.time();pm.finish(c,job,checked(now),now=now)
        row=c.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
        r=json.loads(row['result_json']);assert row['status']=='RECOVERY_INVALIDATED'
        assert r['status']=='CHECKED' and r['recovery_admission_v76']['reason']=='EPOCH_MISMATCH'
        assert r['recovery_admission_v76']['classified_status']=='NET_BELOW_FLOOR'

def test_controlled_recovery_immediate_buy_then_authorized_buy(env,monkeypatch):
    c,clock,do_tick,entry=env;sample=lambda:(clock['t'],clock['m'])
    g=rg.Coordinator(config.DB,'controlled-paper-v76',sample);monkeypatch.setattr(rg,'ACTIVE',g)
    assert entry()==(True,'ENTRY_PENDING');old=ex.pending(c,'a','BUY');old_epoch=g.data['epoch']
    cash=db.getm('cash');reserved=ex.reserve(c)
    clock['m']+=230;clock['t']+=225.741
    immediate=main.buy('b',1,s=main.S['a']);assert immediate==(False,'RECOVERY_BLOCKED')
    assert not main.P and db.getm('cash')==cash and ex.reserve(c)==reserved
    assert not ex.eligible(old,main.S['a']['cost_evidence'])
    g.time_result(old_epoch,report(sample));assert not rg.allowed() # late PASS from prior epoch
    g.connection();g.market(tick(sample));assert not rg.allowed() # missing new time diagnostic
    g.time_result(g.data['epoch'],report(sample,'OFFSET_DETECTED'));assert not rg.allowed()
    g.time_result(g.data['epoch'],report(sample));assert rg.allowed()
    assert not ex.eligible(old,main.S['a']['cost_evidence'])
    ex.expire(c);assert ex.reserve(c)==(0,0)
    new_entry=entry();assert new_entry==(True,'ENTRY_PENDING');do_tick();assert 'a' in main.P
    assert c.execute("SELECT count(*) FROM trades WHERE side='BUY'").fetchone()[0]==1
    assert __import__('primary_integrity').summarize(c)['status']=='OK'
    evidence=dict(schema='CONTROLLED_PAPER_RECOVERY_V76',pause_mono_s=230,wall_gap_s=225.741,
      immediate_buy=list(immediate),cash_unchanged_while_blocked=True,reserve_unchanged_while_blocked=True,
      old_order_rejected_after_ready=True,old_time_pass_rejected=True,missing_time_pass_blocked=True,
      failed_time_check_blocked=True,new_buy=list(new_entry),new_buy_fills=1,financial_integrity='OK',
      generation=g.data['generation'],events=g.data['events'],os_clock_modified=False,
      external_time_probe='INJECTED_TEST_REPORT',scope='OFFLINE_CLOCK_INJECTION_NOT_WINDOWS_SUSPEND')
    if os.getenv('RECOVERY_CHECK_EVIDENCE'):
        Path(os.environ['RECOVERY_CHECK_EVIDENCE']).write_text(json.dumps(evidence,indent=2),encoding='utf-8')

def test_open_position_and_sell_intent_survive_recovery(env,monkeypatch):
    c,clock,do_tick,entry=env;sample=lambda:(clock['t'],clock['m'])
    g=rg.Coordinator(config.DB,'paper-with-position',sample);monkeypatch.setattr(rg,'ACTIVE',g)
    entry();do_tick();assert 'a' in main.P
    main.sell('a',1,'TP2');sell=ex.pending(c,'a','SELL');cash=db.getm('cash')
    clock['m']+=230;clock['t']+=225.741;assert not rg.allowed()
    do_tick(p=.9);assert 'a' in main.P and db.getm('cash')==cash
    assert ex.pending(c,'a','SELL')['order_id']==sell['order_id']
    recovered(g,sample);do_tick(p=.9)
    assert 'a' not in main.P and ex.pending(c,'a','SELL') is None
    assert c.execute("select reason from trades where side='SELL'").fetchone()[0]=='TP2'
