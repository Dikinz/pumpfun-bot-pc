import asyncio,json,sqlite3,time,uuid
import pytest
import recovery_gate as rg,main,database as db,config,execution_model as ex,preentry,route_branch as route,monitor_service as ms,position_monitor as pm
from test_primary_integrity import fresh
from test_execution_model import env
from test_monitor_v31 import open_case,checked

class Clock:
    def __init__(self):self.w=1792927000.;self.m=100.
    def __call__(self):return self.w,self.m
    def step(self,m,w=None):self.m+=m;self.w+=m if w is None else w

def tick(clock,**kw):
    w,m=clock();d=dict(source='pumpapi_free',pool='pump',quote_mint='So11111111111111111111111111111111111111112',price_basis='provider_price',signature='fresh',clock_status='OK',provider_lag_ms=10,received_mono=m);d.update(kw);return d

def report(clock,status='PASS'):return dict(id=uuid.uuid4().hex,created_ts=clock()[0],reference=dict(status=status))
def recovered(g,clock):
    g.connection();g.time_result(g.data['epoch'],report(clock));g.market(tick(clock));assert g.status()['state']=='READY'

@pytest.fixture(autouse=True)
def isolate(monkeypatch):monkeypatch.setattr(rg,'ACTIVE',None)

def test_pause_230s_invalidates_old_token_and_needs_all_three_conditions(tmp_path,monkeypatch):
    c=Clock();g=rg.Coordinator(tmp_path/'p.db','run',c);monkeypatch.setattr(rg,'ACTIVE',g);old=rg.stamp({});epoch=old['recovery_epoch_v75']
    c.step(230,225.741);assert not rg.allowed() and g.data['epoch']!=epoch
    g.market(tick(c));assert not rg.allowed() # no new connection or external time check
    g.connection();g.market(tick(c));assert not rg.allowed()
    g.time_result(g.data['epoch'],report(c,'OFFSET_DETECTED'));assert not rg.allowed()
    g.time_result(epoch,report(c));assert not rg.allowed() # stale generation report
    g.time_result(g.data['epoch'],report(c));assert rg.allowed()
    assert not rg.allowed(old) and rg.allowed(rg.stamp({}))

def test_time_pass_without_fresh_market_stays_blocked(tmp_path,monkeypatch):
    c=Clock();g=rg.Coordinator(tmp_path/'p.db','run',c);monkeypatch.setattr(rg,'ACTIVE',g);c.step(230);g.status();g.connection()
    g.time_result(g.data['epoch'],report(c));g.market(tick(c,provider_lag_ms=1001));assert not rg.allowed()
    g.market(tick(c,quote_mint='other'));assert not rg.allowed()
    g.market(tick(c));assert rg.allowed()

def test_expired_tick_and_old_time_report_do_not_unlock(tmp_path):
    c=Clock();g=rg.Coordinator(tmp_path/'p.db','run',c);c.step(230);g.status();g.connection();old=report(c);old['created_ts']-=1
    g.time_result(g.data['epoch'],old);g.market(tick(c));assert g.status()['state']=='RECOVERING'
    c.step(1.1);g.time_result(g.data['epoch'],report(c));assert g.status()['state']=='RECOVERING'
    g.market(tick(c));assert g.status()['state']=='READY'

def test_new_clock_jump_restarts_recovery(tmp_path):
    c=Clock();g=rg.Coordinator(tmp_path/'p.db','run',c);c.step(230);g.status();epoch=g.data['epoch'];g.connection()
    c.step(.1,-4.159);assert g.status()['epoch']!=epoch
    assert not g.time_result(epoch,report(c));recovered(g,c)

def test_controlled_paper_immediate_entry_blocked_old_order_cannot_fill(env,monkeypatch):
    c,clock,do_tick,entry=env;sample=lambda:(clock['t'],clock['m']);g=rg.Coordinator(config.DB,'paper',sample);monkeypatch.setattr(rg,'ACTIVE',g)
    assert entry()==(True,'ENTRY_PENDING');old=ex.pending(c,'a','BUY');cash=db.getm('cash')
    clock['m']+=230;clock['t']+=225.741
    assert entry()==(False,'RECOVERY_BLOCKED') and not main.P and db.getm('cash')==cash
    g.connection();g.time_result(g.data['epoch'],report(sample));g.market(tick(sample))
    assert rg.allowed() and not ex.eligible(old,main.S['a']['cost_evidence'])
    ex.expire(c);assert ex.reserve(c)==(0,0)
    assert entry()==(True,'ENTRY_PENDING');do_tick();assert 'a' in main.P
    assert c.execute("select count(*) from trades where side='BUY'").fetchone()[0]==1
    assert __import__('primary_integrity').summarize(c)['status']=='OK'

def test_old_preflight_cannot_become_ready_after_recovery(fresh,monkeypatch):
    clock=Clock();g=rg.Coordinator(config.DB,'run',clock);monkeypatch.setattr(rg,'ACTIVE',g)
    inp=rg.stamp(dict(size_units='3',fx_rate='100',signal_price='1'))
    fresh.execute("insert into preentry_v0729(job_id,mint,created_ts,input_json,status) values('job','mint',?,?,'RUNNING')",(clock.w,json.dumps(inp)))
    job=dict(fresh.execute("select * from preentry_v0729 where job_id='job'").fetchone())
    clock.step(230);g.status();recovered(g,clock)
    preentry.complete(fresh,job,dict(status='ROUNDTRIP_EXECUTED',reserve_gate='PASS',net_gate='PASS',zero_token_control=dict(status='FAILED',error_code=6023),snapshot_received_ts=clock.w),now=clock.w)
    assert fresh.execute("select status from preentry_v0729 where job_id='job'").fetchone()[0]=='RECOVERY_INVALIDATED'

def test_old_exit_result_not_eligible_and_monitor_marks_invalidated(fresh,monkeypatch):
    tid=open_case(fresh);clock=Clock();g=rg.Coordinator(config.DB,'run',clock);monkeypatch.setattr(rg,'ACTIVE',g)
    with ms.connect() as c:
        job=pm.next_job(c,time.time());old=job['recovery_epoch_v75'];clock.step(230);g.status();recovered(g,clock)
        result=checked(time.time());pm.finish(c,job,result)
        row=dict(c.execute('select * from position_checks_v0730 where id=?',(job['id'],)).fetchone())
        assert row['status']=='RECOVERY_INVALIDATED'
        assert route.assess(fresh,row,job,result,job['scheduled_ts'],{},time.time())[0]=='RECOVERY_INVALIDATED'
        assert g.data['epoch']!=old and not rg.allowed(job)
        new=rg.stamp(dict(job));assert rg.allowed(new)
    assert fresh.execute('select count(*) from positions').fetchone()[0]==1

def test_client_pause_requires_primary_recheck_and_same_epoch(tmp_path,monkeypatch):
    root_clock=Clock();client_clock=Clock();g=rg.Coordinator(tmp_path/'p.db','run',root_clock);client=rg.Client(tmp_path/'p.db',client_clock)
    for _ in range(8):root_clock.step(.5);g.observe();g.save()
    client_clock.step(4);assert client.status()['state']=='RECOVERING'
    assert rg.request_path(tmp_path/'p.db').exists();g.remote_request();assert g.status()['state']=='RECOVERING'
    recovered(g,root_clock);assert client.status()['state']=='READY'
    rg.path(tmp_path/'p.db').unlink();assert client.status()['state']=='RECOVERING'

def test_unavailable_write_is_fail_closed(tmp_path,monkeypatch):
    from pathlib import Path
    def denied(*a,**kw):raise PermissionError('test')
    monkeypatch.setattr(Path,'replace',denied);g=rg.Coordinator(tmp_path/'p.db','run');monkeypatch.setattr(rg,'ACTIVE',g)
    assert not rg.allowed() and g.data['write_error']=='PermissionError'

def test_external_diagnostic_is_async_and_same_existing_collector(tmp_path,monkeypatch):
    import time_check
    clock=Clock();g=rg.Coordinator(tmp_path/'p.db','run',clock);clock.step(230);g.status();calls=[]
    async def collect():calls.append(1);return report(clock)
    monkeypatch.setattr(time_check,'collect',collect);monkeypatch.setattr(time_check,'save',lambda r:None)
    async def run():
        task=asyncio.create_task(g.run());await asyncio.sleep(.25);g.connection();g.market(tick(clock));task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run());assert calls==[1] and g.status()['state']=='READY'


def test_new_preflight_after_verified_recovery_can_authorize_entry(fresh,monkeypatch):
    import exit_quotes
    clock=Clock();g=rg.Coordinator(config.DB,'run',clock);monkeypatch.setattr(rg,'ACTIVE',g)
    clock.step(230);assert not rg.allowed();recovered(g,clock)
    monkeypatch.setattr(exit_quotes,'FX',dict(rate=100,ts=clock.w))
    assert preentry.request(fresh,'new',1,3,clock.w)=='PENDING'
    j=dict(fresh.execute("select * from preentry_v0729 where mint='new'").fetchone())
    result=dict(status='ROUNDTRIP_EXECUTED',reserve_gate='PASS',net_gate='PASS',zero_token_control=dict(status='FAILED',error_code=6023),snapshot_received_ts=clock.w)
    preentry.complete(fresh,j,result,now=clock.w)
    assert preentry._allow(fresh,'new',1,3,clock.w)==(True,'PREFLIGHT_READY')

def test_wall_valid_ready_permission_still_rejected_after_epoch_change(fresh,monkeypatch):
    import exit_quotes
    clock=Clock();g=rg.Coordinator(config.DB,'run',clock);monkeypatch.setattr(rg,'ACTIVE',g)
    monkeypatch.setattr(exit_quotes,'FX',dict(rate=100,ts=clock.w))
    preentry.request(fresh,'cached',1,3,clock.w)
    j=dict(fresh.execute("select * from preentry_v0729 where mint='cached'").fetchone())
    result=dict(status='ROUNDTRIP_EXECUTED',reserve_gate='PASS',net_gate='PASS',zero_token_control=dict(status='FAILED',error_code=6023),snapshot_received_ts=clock.w)
    preentry.complete(fresh,j,result,now=clock.w)
    assert preentry._allow(fresh,'cached',1,3,clock.w)==(True,'PREFLIGHT_READY')
    # 50 ms wall-vs-monotonic discontinuity; old approval remains within its TTL.
    clock.step(.1,.05);assert not rg.allowed();recovered(g,clock)
    assert preentry._allow(fresh,'cached',1,3,clock.w)==(False,'PREFLIGHT_RECOVERY_INVALIDATED')

def test_current_buy_rejects_still_fresh_tick_from_prior_epoch(env,monkeypatch):
    c,clock,do_tick,entry=env;sample=lambda:(clock['t'],clock['m']);g=rg.Coordinator(config.DB,'paper',sample);monkeypatch.setattr(rg,'ACTIVE',g)
    do_tick();evidence=rg.stamp(dict(main.S['a']['cost_evidence']))
    clock['m']+=.1;clock['t']+=.05;assert not rg.allowed();recovered(g,sample)
    assert entry()==(True,'ENTRY_PENDING');order=ex.pending(c,'a','BUY')
    clock['m']+=.3;clock['t']+=.3
    evidence.update(received_mono=clock['m'],provider_ts=clock['t'],signature='new-event')
    assert ex.fresh(evidence) # old epoch cannot be hidden by otherwise fresh timestamps
    assert not ex.eligible(order,evidence)
    rg.stamp(evidence);assert ex.eligible(order,evidence)
