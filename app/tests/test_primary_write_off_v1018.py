"""v0.7.101.8: PRIMARY writes off a SELL on a curve that stays drained, by the same rule as ROUTE.

03.11.2026: FPtv waited 1 h 51 min for a tick with 3.00 frozen; HUNT filled after 22 min at a price the empty curve
could not pay. ROUTE wrote both off."""
import json,time
from pathlib import Path
import pytest
import config,main,paper_capital as cap,primary_write_off as pw,route_branch as route,monitor_service,exit_cost_review as review,campaign_view,primary_integrity
from test_primary_integrity import fresh
from test_parallel_paper_v54 import opened
from test_route_exit_v59 import candidate,save_check
from test_route_write_off_v1017 import drained

def history(c,tid,signal_age,decisions):
    now=time.time()
    c.execute("UPDATE paper_orders_v0720 SET signal_ts=? WHERE trade_id=? AND side='SELL'",(now-signal_age,tid))
    for i,(age,decision) in enumerate(decisions):
        c.execute('INSERT INTO '+pw.TABLE+' VALUES(?,?,?,?,?,?)',('old%d'%i,tid,'m',now-age,decision,'PUMP_CURVE'))

def test_schema_is_created_with_the_primary_database(fresh):
    for table in (pw.TABLE,'branch_amm_settlements'):
        assert fresh.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone()
    assert not fresh.execute("SELECT 1 FROM sqlite_master WHERE name='branch_info'").fetchone()

def test_drained_curve_is_written_off_with_zero_receipt_and_frees_the_slot(fresh):
    tid=opened(fresh);before=cap.cash(fresh)
    history(fresh,tid,1000,[(900,pw.DRAINED),(500,'STALE_OR_PRE_SIGNAL'),(300,pw.DRAINED)])
    row,j,r,inv=drained(tid);save_check(fresh,row,j,r,inv);pw.poll(main)
    assert fresh.execute('SELECT decision FROM '+pw.TABLE+" WHERE check_id='check'").fetchone()[0]=='WRITTEN_OFF'
    assert 'm' not in main.P and fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0
    assert cap.cash(fresh)==pytest.approx(before) and cap.summary(fresh)['status']=='OK'  # nothing comes back
    receipt,details=fresh.execute('SELECT receipt,details FROM branch_amm_settlements').fetchone();details=json.loads(details)
    entry=fresh.execute("SELECT total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(tid,)).fetchone()[0]
    a=details['accounting'];assert receipt==0 and details['route']==route.WRITE_OFF and details['scenario']=='PRIMARY'
    assert a['cost_model']==pw.COST_MODEL and a['drained_checks']==3 and float(a['pnl_units'])==pytest.approx(-3-entry)
    assert fresh.execute("SELECT reason FROM trades WHERE side='SELL'").fetchone()[0]=='CURVE_WRITE_OFF'
    assert fresh.execute("SELECT status,note FROM paper_orders_v0720 WHERE side='SELL'").fetchone()[:]==('FILLED','DRAINED_CURVE_WRITE_OFF_NO_SALE')
    assert primary_integrity.summarize(fresh)['status']=='OK'
    pw.poll(main);assert fresh.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==1

@pytest.mark.parametrize('case',['too_short','too_few','broken','before_signal','sellable'])
def test_write_off_needs_ten_unbroken_minutes_of_empty_curve_after_the_signal(fresh,case):
    tid=opened(fresh);before=cap.cash(fresh)
    history(fresh,tid,*{'too_short':(1000,[(500,pw.DRAINED),(300,pw.DRAINED)]),
        'too_few':(1000,[(900,pw.DRAINED)]),
        'broken':(1000,[(900,pw.DRAINED),(700,pw.DRAINED),(600,'CURVE_NOT_DRAINED'),(300,pw.DRAINED)]),
        'before_signal':(400,[(900,pw.DRAINED),(800,pw.DRAINED),(300,pw.DRAINED)]),
        'sellable':(1000,[(900,pw.DRAINED),(700,pw.DRAINED)])}[case])
    row,j,r,inv=candidate(tid) if case=='sellable' else drained(tid)
    save_check(fresh,row,j,r,inv);pw.poll(main)
    decision=fresh.execute('SELECT decision FROM '+pw.TABLE+" WHERE check_id='check'").fetchone()[0]
    # PRIMARY never sells from the monitor: a refilled curve only breaks the streak, the tick still decides.
    assert decision==('CURVE_NOT_DRAINED' if case=='sellable' else pw.DRAINED)
    assert 'm' in main.P and cap.cash(fresh)==before and not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()
    assert fresh.execute("SELECT status FROM paper_orders_v0720 WHERE side='SELL'").fetchone()[0]=='PENDING'

def test_stale_checks_are_recorded_without_evidence(fresh):
    tid=opened(fresh);row,j,r,inv=drained(tid);r['snapshot_received_ts']=time.time()-60
    save_check(fresh,row,j,r,inv);pw.poll(main)
    assert fresh.execute('SELECT decision FROM '+pw.TABLE).fetchone()[0]=='STALE_OR_PRE_SIGNAL' and 'm' in main.P

def test_write_off_refuses_foreign_inventory(fresh):
    tid=opened(fresh);history(fresh,tid,1000,[(900,pw.DRAINED),(300,pw.DRAINED)])
    row,j,r,inv=drained(tid);j['inventory']=dict(inv,trade_id='other');save_check(fresh,row,j,r,inv);pw.poll(main)
    assert 'm' in main.P and not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()

def test_worker_survives_an_error_and_records_it_once(fresh,monkeypatch):
    import asyncio,system_health
    calls=[];errors=[]
    def boom(m):
        calls.append(1)
        if len(calls)>=3:raise asyncio.CancelledError
        raise ValueError('x')
    monkeypatch.setattr(pw,'poll',boom);monkeypatch.setattr(system_health,'failure',lambda *a:errors.append(a[1]))
    async def fast(_):pass
    monkeypatch.setattr(pw.asyncio,'sleep',fast)
    with pytest.raises(asyncio.CancelledError):asyncio.run(pw.worker(main,config.DB))
    assert len(calls)==3 and errors==['primary_write_off']

def test_reports_show_the_primary_write_off_apart_from_ticks(fresh):
    tid=opened(fresh);history(fresh,tid,1000,[(900,pw.DRAINED),(300,pw.DRAINED)])
    row,j,r,inv=drained(tid);save_check(fresh,row,j,r,inv);pw.poll(main)
    x=review.build({'primary':(config.DB,monitor_service.paths(config.DB)['db'])})
    assert x['rows'][0]['setup_status']=='WRITTEN_OFF_DRAINED_CURVE' and x['totals']['primary']['written_off_v1017']==1
    fresh.execute("INSERT INTO meta(k,v) VALUES('heartbeat_ts',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(str(time.time()),))
    view=campaign_view.branch(fresh,Path(config.DB),'primary',time.time())
    assert view['write_offs_v1017']==1 and view['verified_exits']==0

def test_open_position_shows_the_empty_curve_while_waiting(fresh):
    tid=opened(fresh);history(fresh,tid,100,[(60,pw.DRAINED),(30,'STALE_OR_PRE_SIGNAL')])
    fresh.execute("INSERT INTO meta(k,v) VALUES('heartbeat_ts',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(str(time.time()),))
    view=campaign_view.branch(fresh,Path(config.DB),'primary',time.time())
    assert view['positions'][0]['exit_decision']==pw.DRAINED
