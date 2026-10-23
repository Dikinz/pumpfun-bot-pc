import copy,json,time
from decimal import Decimal
import pytest
import amm_exit_scenario as amm,system_health as sh,strategy_diagnostics as sd,paper_capital as cap,main,config
from test_primary_integrity import fresh


def payload():
    inv=dict(trade_id='t',mint='m',size_usdc='3',sol_usdc='100',token_units='10',entry_sol_price='.003',raw_amount='10000000',decimals=6)
    row=dict(trade_id='t',mint='m',status='AMM_SALE_EXECUTED',started_ts=101.,finished_ts=104.)
    job=dict(trade_id='t',mint='m',route='PUMP_AMM',scheduled_ts=101.,inventory=dict(inv))
    sale=dict(status='AMM_SALE_EXECUTED',err=None,raw_amount='10000000',tokens_debited='10000000',pool_tokens_credited='10000000',
       account_scenario='ISOLATED_FIRST_AMM_SELL',broadcast=False,quote_received_lamports=40000000,wallet_sol_delta_lamports=-1005000,
       base_account_rent_lamports=1000000,quote_account_rent_lamports=1000000,net_after_setup_lamports=36995000,
       local_fee_lamports=5000,volume_rent_delta_lamports=1000000,pool_rent_topup_lamports=0)
    result=dict(status='AMM_SALE_EXECUTED',route='PUMP_AMM',snapshot_received_ts=103.,amount_decimals=6,
       zero_token_control_ok=True,address_status='CANONICAL_POOL_VERIFIED',pool='pool',reserves=dict(pool='pool',status='AMM_RESERVES_AVAILABLE'),
       broadcast=False,paper_mutated=False,raw_amount='10000000',sale=sale)
    return row,job,result,inv

def ledger(c):
    c.execute("INSERT INTO capital_ledger_v0721(trade_id,side,notional,total_cost) VALUES('t','BUY',3,.05)")

def test_amm_costs_counted_once(fresh):
    ledger(fresh);row,job,r,inv=payload();code,a=amm.assess(fresh,row,job,r,100.,None,inv)
    assert code=='ELIGIBLE' and Decimal(a['receipt_units'])==Decimal('3.6995')
    assert Decimal(a['pnl_units'])==Decimal('.6495')

@pytest.mark.parametrize('case,expected',[
 ('late','SNAPSHOT_NOT_TIMELY'),('before','STARTED_BEFORE_SIGNAL'),('closed','AFTER_OR_AT_TICK_CLOSE'),
 ('control','NO_VERIFIED_SALE'),('amount','AMOUNT_MISMATCH'),('decimals','AMOUNT_MISMATCH'),
 ('net','ACCOUNTING_MISMATCH'),('delta','ACCOUNTING_MISMATCH'),('pool','POOL_NOT_CONFIRMED'),
 ('fx','INVENTORY_MISMATCH'),('missing','MISSING_ACCOUNTING'),('wrong_route','OTHER_ROUTE')])
def test_bad_amm_candidates_stay_unresolved(fresh,case,expected):
    ledger(fresh);row,job,r,inv=payload();closed=None
    if case=='late':r['snapshot_received_ts']=98.
    if case=='before':row['started_ts']=99.
    if case=='closed':closed=104.
    if case=='control':r['zero_token_control_ok']=False
    if case=='amount':r['sale']['tokens_debited']='1'
    if case=='decimals':r['amount_decimals']=5
    if case=='net':r['sale']['net_after_setup_lamports']+=1
    if case=='delta':r['sale']['volume_rent_delta_lamports']+=1
    if case=='pool':r['reserves']['pool']='other'
    if case=='fx':job['inventory']['sol_usdc']='101'
    if case=='missing':r['sale'].pop('quote_account_rent_lamports')
    if case=='wrong_route':r['route']='PUMP_CURVE'
    assert amm.assess(fresh,row,job,r,100.,closed,inv)==(expected,None)

def test_stopped_positions_critical_even_without_recorder(fresh):
    snap=dict(runtime={'state':'STOPPED'},positions=[{'mint':'m'}],monitor={'service':{'status':'STOPPED'}})
    r=sd.summary(fresh,snap,config.DB)
    assert 'STOPPED_WITH_OPEN_POSITIONS' in {x['code'] for x in r['issues']}

def test_shutdown_heartbeat_three_seconds_after_launcher(fresh):
    now=time.time();sh.write_state(config.DB,dict(status='STOPPED',exit_code=0,stop_reason='USER_STOP',heartbeat_ts=now-5,database_identity=sh.identity(config.DB)))
    snap=dict(runtime=dict(state='STOPPED',started_ts=now-100,updated_ts=now-2),positions=[{'mint':'m'}],monitor={'service':{'status':'STOPPED'}})
    r=sd.summary(fresh,snap,config.DB);codes={x['code'] for x in r['issues']}
    assert {'USER_STOP','STOPPED_WITH_OPEN_POSITIONS'}<=codes
    assert 'PRIMARY_HEARTBEAT_UNKNOWN' not in codes

def test_stale_marks_never_become_capital_or_full_valuation(fresh,monkeypatch):
    main.buy('a',1)
    fresh.execute('UPDATE positions SET last_update_ts=?,current=100',(time.time()-3600,))
    before=cap.cash(fresh);r=cap.summary(fresh)
    assert r['available']==before and r['marked_equity_before_exit_cost'] is None
    assert r['valuation']['excluded_count']==1 and r['valuation']['fresh_open_mark']==0
    fresh.execute('UPDATE positions SET last_update_ts=?',(time.time(),))
    r=cap.summary(fresh);assert r['valuation']['complete'] and r['marked_equity_before_exit_cost']>before
    assert cap.cash(fresh)==before

def test_first_eligible_not_best_and_readonly(monkeypatch):
    import sqlite3
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE paper_orders_v0720(order_id,trade_id,side,signal_ts,reason);
    INSERT INTO paper_orders_v0720 VALUES('o','t','SELL',100,'MAX_HOLD_WATCHDOG');
    CREATE TABLE trades(trade_id,side,ts);
    CREATE TABLE quote_inventory_v0723(trade_id,mint,size_usdc,sol_usdc,token_units,entry_sol_price,raw_amount,decimals);
    INSERT INTO quote_inventory_v0723 VALUES('t','m','3','100','10','.003','10000000',6);
    CREATE TABLE capital_ledger_v0721(trade_id,side,notional,total_cost);
    INSERT INTO capital_ledger_v0721 VALUES('t','BUY',3,.05);
    CREATE TABLE position_checks_v0730(id,trade_id,mint,started_ts,finished_ts,status,input_json,result_json);''')
    row,j,r,inv=payload()
    for ident,end,credit in [('a',104.,40000000),('b',105.,80000000)]:
        r['sale']['quote_received_lamports']=credit;r['sale']['net_after_setup_lamports']=credit-3005000
        c.execute('INSERT INTO position_checks_v0730 VALUES(?,?,?,?,?,?,?,?)',(ident,'t','m',101.,end,'AMM_SALE_EXECUTED',json.dumps(j),json.dumps(r)))
    monkeypatch.setattr(amm.cohort_report,'summary',lambda c,selected:{})
    before=c.total_changes;report=amm.summary(c)
    assert report['selected'][0]['check_id']=='a' and len(report['selected'])==1
    assert report['decisions']['ALREADY_SELECTED']==1 and c.total_changes==before
    assert amm.summary(c)==report
