"""v0.7.101.10 (user's choice 05.11.2026): a drained curve is closed at the value a real position would get.

04-05.11.2026: 8 write-offs (3 PRIMARY, 5 ROUTE) booked -3.05 each. A PAPER position is not in the curve, a real
one would be: the curve below is the 07a3 snapshot (back at the initial state, 19 lamports of real SOL), where our
1 041 385.13 tokens sell for 29 144 365 lamports with our own buy included, about -0.34 instead of -3.05."""
import json,time
from pathlib import Path
import pytest
import config,main,paper_capital as cap,route_branch as route,primary_write_off as pw,monitor_service,exit_cost_review as review,campaign_view,primary_integrity
import curve_floor_v10110 as floor,position_monitor as pm,local_simulation as lab,curve_overflow_replay as replay
from test_primary_integrity import fresh
from test_parallel_paper_v54 import opened
from test_route_exit_v59 import candidate,save_check

VS,VT,RT,T=30000000019,1073000000000000,793100000000000,1041385130080
GROSS=29144365

def drained(tid,**reserves):
    row,j,r,inv=candidate(tid);row['status']='INSUFFICIENT_RESERVES'
    inv=dict(inv,raw_amount=str(T),token_units='1041385.13008',sol_usdc='94.712757');j['inventory']=dict(inv)
    upper=(T*VS+VT+T-1)//(VT+T)
    data=dict(status='INSUFFICIENT_REAL_RESERVES',real_sol_lamports=19,spendable_lamports=19,sell_gross_upper_lamports=upper,
        virtual_sol_lamports=VS,virtual_token_raw=VT,real_token_raw=RT,amount_raw=T,paper_inventory_gross_lamports=GROSS)
    data.update(reserves)
    r.update(status='CURVE_RESERVE_PROBE_INSUFFICIENT',sale=None,reserves=data)
    return row,j,r,inv

def expected(c,tid):
    entry=c.execute("SELECT total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(tid,)).fetchone()[0]
    value=GROSS*94.712757/1e9;receipt=value-value*(.0125+.0025)-.005
    return receipt,receipt-3-entry

def test_model_value_on_the_drained_night_curve():
    assert floor.gross_lamports(VS,VT,RT,T)==GROSS
    # With our tokens in the curve the sale is never below the plain virtual-reserve sale.
    assert GROSS>=(T*VS)//(VT+T)
    assert floor.gross_lamports(VS,VT,T-1,T) is None  # the curve could not have sold us these tokens
    assert floor.gross_lamports(VS,VT,RT,0) is None and floor.gross_lamports(VS,VT,RT,str(T)) is None

def test_monitor_records_curve_state_and_model_value():
    bank=replay.load_fixture()[0]
    curve=lab.v.decode_curve(bank['snapshot']['accounts'][bank['account_names']['bonding_curve']])
    r=pm.reserve_evidence(bank);amount=int(bank['amount'])
    assert (r['virtual_sol_lamports'],r['virtual_token_raw'],r['real_token_raw'],r['amount_raw'])==(curve['virtual_sol'],curve['virtual_token'],curve['real_token'],amount)
    assert r['paper_inventory_gross_lamports']==floor.gross_lamports(curve['virtual_sol'],curve['virtual_token'],curve['real_token'],amount)
    assert r['paper_inventory_gross_lamports']>=r['sell_gross_upper_lamports']-1

@pytest.mark.parametrize('role',['route','primary'])
def test_drained_curve_closes_at_the_model_value_on_the_first_check(fresh,role):
    tid=opened(fresh);fresh.executescript(route.SCHEMA);before=cap.cash(fresh)
    fresh.execute("UPDATE paper_orders_v0720 SET signal_ts=? WHERE trade_id=? AND side='SELL'",(time.time()-30,tid))
    row,j,r,inv=drained(tid);save_check(fresh,row,j,r,inv)
    (route if role=='route' else pw).poll(main)
    table='route_exit_decisions_v59' if role=='route' else pw.TABLE
    assert fresh.execute('SELECT decision FROM '+table+" WHERE check_id='check'").fetchone()[0]=='FLOOR_SETTLED'
    assert 'm' not in main.P and fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0
    receipt,pnl=expected(fresh,tid)
    assert cap.cash(fresh)==pytest.approx(before+receipt) and cap.summary(fresh)['status']=='OK'
    gross,booked,details=fresh.execute('SELECT gross,receipt,details FROM branch_amm_settlements').fetchone();details=json.loads(details)
    assert gross==pytest.approx(GROSS*94.712757/1e9) and booked==pytest.approx(receipt) and details['route']==floor.ROUTE
    a=details['accounting'];assert a['cost_model']==floor.COST_MODEL and float(a['pnl_units'])==pytest.approx(pnl)
    assert pnl==pytest.approx(-0.336,abs=.005)
    sell=fresh.execute("SELECT reason,pnl FROM trades WHERE side='SELL'").fetchone()
    entry=fresh.execute("SELECT total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(tid,)).fetchone()[0]
    exit_cost=fresh.execute("SELECT total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='SELL'",(tid,)).fetchone()[0]
    assert sell[0]==floor.REASON and sell[1]-entry-exit_cost==pytest.approx(pnl)
    assert exit_cost==pytest.approx(gross*(.0125+.0025)+.005)  # only fees count as costs, never the curve's missing SOL
    assert fresh.execute("SELECT status,note FROM paper_orders_v0720 WHERE side='SELL'").fetchone()[:]==('FILLED','PAPER_INVENTORY_CURVE_FLOOR_NO_SALE')
    assert fresh.execute("SELECT rate_source FROM capital_ledger_v0721 WHERE side='SELL'").fetchone()[0]==floor.ROUTE
    assert primary_integrity.summarize(fresh)['status']=='OK'
    (route if role=='route' else pw).poll(main);assert fresh.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==1

@pytest.mark.parametrize('change',[dict(paper_inventory_gross_lamports=None),dict(paper_inventory_gross_lamports=GROSS+1),
    dict(real_token_raw=T-1),dict(amount_raw=T+1),dict(virtual_sol_lamports=None)])
def test_no_model_without_consistent_curve_state(fresh,change):
    tid=opened(fresh);fresh.executescript(route.SCHEMA);before=cap.cash(fresh)
    row,j,r,inv=drained(tid,**change);save_check(fresh,row,j,r,inv);route.poll(main)
    # Old results and inconsistent state keep the v0.7.101.7 rule: wait for 10 drained minutes, then write off.
    assert fresh.execute("SELECT decision FROM route_exit_decisions_v59 WHERE check_id='check'").fetchone()[0]=='INSUFFICIENT_LIQUIDITY_OPEN'
    assert 'm' in main.P and cap.cash(fresh)==before and not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()

@pytest.mark.parametrize('role',['route','primary'])
def test_executed_simulation_at_the_rounding_boundary_is_never_modelled(fresh,role):
    tid=opened(fresh);fresh.executescript(route.SCHEMA);before=cap.cash(fresh)
    row,j,r,inv=drained(tid);r.update(status='CHECKED',sale=candidate(tid)[2]['sale'])  # the ceil flag says short, the sale executed
    save_check(fresh,row,j,r,inv);(route if role=='route' else pw).poll(main)
    table='route_exit_decisions_v59' if role=='route' else pw.TABLE
    assert fresh.execute('SELECT decision FROM '+table+" WHERE check_id='check'").fetchone()[0]=='INSUFFICIENT_LIQUIDITY_OPEN'
    assert 'm' in main.P and cap.cash(fresh)==before and not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()

def test_write_off_still_applies_to_old_results(fresh):
    from test_route_write_off_v1017 import drained as old,history
    tid=opened(fresh);fresh.executescript(route.SCHEMA)
    history(fresh,tid,1000,[(900,'INSUFFICIENT_LIQUIDITY_OPEN'),(300,'INSUFFICIENT_LIQUIDITY_OPEN')])
    row,j,r,inv=old(tid);save_check(fresh,row,j,r,inv);route.poll(main)
    assert fresh.execute("SELECT decision FROM route_exit_decisions_v59 WHERE check_id='check'").fetchone()[0]=='WRITTEN_OFF'

def test_stale_or_foreign_check_never_settles(fresh):
    tid=opened(fresh);fresh.executescript(route.SCHEMA)
    row,j,r,inv=drained(tid);r['snapshot_received_ts']=time.time()-60;save_check(fresh,row,j,r,inv)
    row2,j2,r2,inv2=drained(tid);row2['id']='foreign';j2['inventory']=dict(inv2,trade_id='other');save_check(fresh,row2,j2,r2,inv2)
    route.poll(main)
    assert dict(fresh.execute('SELECT check_id,decision FROM route_exit_decisions_v59'))=={'check':'STALE_OR_PRE_SIGNAL','foreign':'INSUFFICIENT_LIQUIDITY_OPEN'}
    assert 'm' in main.P and not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()

def test_reports_keep_model_exits_apart_from_verified_sales(fresh):
    tid=opened(fresh);fresh.executescript(route.SCHEMA)
    row,j,r,inv=drained(tid);save_check(fresh,row,j,r,inv);pw.poll(main)
    x=review.build({'primary':(config.DB,monitor_service.paths(config.DB)['db'])})
    assert x['rows'][0]['setup_status']=='CURVE_FLOOR_MODELED' and x['totals']['primary']['curve_floor_v10110']==1
    assert x['totals']['primary']['written_off_v1017']==0
    fresh.execute("INSERT INTO meta(k,v) VALUES('heartbeat_ts',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(str(time.time()),))
    view=campaign_view.branch(fresh,Path(config.DB),'primary',time.time())
    assert view['curve_floor_exits_v10110']==1 and view['verified_exits']==0 and view['unknown_route_exits']==0 and view['write_offs_v1017']==0
