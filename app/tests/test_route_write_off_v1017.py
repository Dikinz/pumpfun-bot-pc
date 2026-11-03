"""v0.7.101.7: a ROUTE position on a drained curve is written off instead of holding its slot forever.

03.11.2026: two creators emptied their curves (7.1 SOL -> 41 lamports) seconds after entry; ROUTE kept both
positions open for 7-9 h, both slots stayed taken and 29 PRIMARY entries were skipped."""
import json,time
from pathlib import Path
import pytest
import config,main,paper_capital as cap,route_branch as route,monitor_service,exit_cost_review as review,campaign_view,primary_integrity
from test_primary_integrity import fresh
from test_parallel_paper_v54 import opened
from test_route_exit_v59 import candidate,save_check

def drained(tid):
    row,j,r,inv=candidate(tid);row['status']='INSUFFICIENT_RESERVES'
    r.update(status='CURVE_RESERVE_PROBE_INSUFFICIENT',sale=None,
        reserves=dict(status='INSUFFICIENT_REAL_RESERVES',real_sol_lamports=41,spendable_lamports=41,sell_gross_upper_lamports=21938669))
    return row,j,r,inv

def history(c,tid,signal_age,decisions):
    now=time.time()
    c.execute("UPDATE paper_orders_v0720 SET signal_ts=? WHERE trade_id=? AND side='SELL'",(now-signal_age,tid))
    for i,(age,decision) in enumerate(decisions):
        c.execute('INSERT INTO route_exit_decisions_v59 VALUES(?,?,?,?,?,?)',('old%d'%i,tid,'m',now-age,decision,'PUMP_CURVE'))

def test_drained_curve_is_written_off_with_zero_receipt_and_frees_the_slot(fresh):
    tid=opened(fresh);fresh.executescript(route.SCHEMA);before=cap.cash(fresh)
    history(fresh,tid,1000,[(900,'INSUFFICIENT_LIQUIDITY_OPEN'),(500,'STALE_OR_PRE_SIGNAL'),(300,'INSUFFICIENT_LIQUIDITY_OPEN')])
    row,j,r,inv=drained(tid);save_check(fresh,row,j,r,inv);route.poll(main)
    assert fresh.execute("SELECT decision FROM route_exit_decisions_v59 WHERE check_id='check'").fetchone()[0]=='WRITTEN_OFF'
    assert 'm' not in main.P and fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0
    assert cap.cash(fresh)==pytest.approx(before) and cap.summary(fresh)['status']=='OK'  # nothing comes back
    gross,receipt,details=fresh.execute('SELECT gross,receipt,details FROM branch_amm_settlements').fetchone();details=json.loads(details)
    entry=fresh.execute("SELECT total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(tid,)).fetchone()[0]
    assert receipt==0 and gross==pytest.approx(41*100/1e9) and details['route']==route.WRITE_OFF
    a=details['accounting'];assert a['cost_model']==route.WRITE_OFF_COST_MODEL and a['drained_checks']==3
    assert float(a['pnl_units'])==pytest.approx(-3-entry)
    assert fresh.execute("SELECT reason FROM trades WHERE side='SELL'").fetchone()[0]=='CURVE_WRITE_OFF'
    assert fresh.execute("SELECT status,note FROM paper_orders_v0720 WHERE side='SELL'").fetchone()[:]==('FILLED','DRAINED_CURVE_WRITE_OFF_NO_SALE')
    assert fresh.execute("SELECT cash_delta,rate_source FROM capital_ledger_v0721 WHERE side='SELL'").fetchone()[:]==(0,route.WRITE_OFF)
    assert primary_integrity.summarize(fresh)['status']=='OK'
    route.poll(main);assert fresh.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==1

@pytest.mark.parametrize('case',['too_short','too_few','broken','before_signal','sellable_again'])
def test_write_off_needs_ten_unbroken_minutes_of_empty_curve_after_the_signal(fresh,case):
    tid=opened(fresh);fresh.executescript(route.SCHEMA);before=cap.cash(fresh)
    history(fresh,tid,*{'too_short':(1000,[(500,'INSUFFICIENT_LIQUIDITY_OPEN'),(300,'INSUFFICIENT_LIQUIDITY_OPEN')]),
        'too_few':(1000,[(900,'INSUFFICIENT_LIQUIDITY_OPEN')]),
        'broken':(1000,[(900,'INSUFFICIENT_LIQUIDITY_OPEN'),(700,'INSUFFICIENT_LIQUIDITY_OPEN'),(600,'NET_BELOW_FLOOR'),(300,'INSUFFICIENT_LIQUIDITY_OPEN')]),
        'before_signal':(400,[(900,'INSUFFICIENT_LIQUIDITY_OPEN'),(800,'INSUFFICIENT_LIQUIDITY_OPEN'),(300,'INSUFFICIENT_LIQUIDITY_OPEN')]),
        'sellable_again':(1000,[(900,'INSUFFICIENT_LIQUIDITY_OPEN'),(700,'INSUFFICIENT_LIQUIDITY_OPEN')])}[case])
    row,j,r,inv=drained(tid)
    if case=='sellable_again':row,j,r,inv=candidate(tid)  # the curve refilled: a real sale, never a write-off
    save_check(fresh,row,j,r,inv);route.poll(main)
    decision=fresh.execute("SELECT decision FROM route_exit_decisions_v59 WHERE check_id='check'").fetchone()[0]
    if case=='sellable_again':
        assert decision=='SETTLED' and json.loads(fresh.execute('SELECT details FROM branch_amm_settlements').fetchone()[0])['route']=='PUMP_CURVE';return
    assert decision=='INSUFFICIENT_LIQUIDITY_OPEN' and 'm' in main.P and cap.cash(fresh)==before
    assert not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()
    assert fresh.execute("SELECT status FROM paper_orders_v0720 WHERE side='SELL'").fetchone()[0]=='PENDING'

def test_write_off_refuses_foreign_inventory(fresh):
    tid=opened(fresh);fresh.executescript(route.SCHEMA)
    history(fresh,tid,1000,[(900,'INSUFFICIENT_LIQUIDITY_OPEN'),(300,'INSUFFICIENT_LIQUIDITY_OPEN')])
    row,j,r,inv=drained(tid);j['inventory']=dict(inv,trade_id='other');save_check(fresh,row,j,r,inv);route.poll(main)
    assert 'm' in main.P and not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()

def test_write_off_is_reported_apart_from_verified_sales(fresh):
    tid=opened(fresh);fresh.executescript(route.SCHEMA)
    history(fresh,tid,1000,[(900,'INSUFFICIENT_LIQUIDITY_OPEN'),(300,'INSUFFICIENT_LIQUIDITY_OPEN')])
    row,j,r,inv=drained(tid);save_check(fresh,row,j,r,inv);route.poll(main)
    x=review.build({'route':(config.DB,monitor_service.paths(config.DB)['db'])})
    row=x['rows'][0];assert row['setup_status']=='WRITTEN_OFF_DRAINED_CURVE' and row['cost_model']==route.WRITE_OFF_COST_MODEL
    assert x['totals']['route']['setup_reconciled']==1
    fresh.execute("INSERT INTO meta(k,v) VALUES('heartbeat_ts',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(str(time.time()),))
    view=campaign_view.branch(fresh,Path('missing.db'),'route',time.time())
    assert view['write_offs_v1017']==1 and view['verified_exits']==0 and view['curve_exits']==0 and view['unknown_route_exits']==0
    assert view['persistent_wallet_model_v100']['cost_models_v1016'][route.WRITE_OFF_COST_MODEL]['closed']==1
