import copy,json,sqlite3
import pytest
import signal_exit as se

def case():
    inv=dict(trade_id='t',mint='m',size_usdc='3',sol_usdc='100',token_units='1000',entry_sol_price='.00003',raw_amount='1000000000')
    row=dict(id='a',trade_id='t',mint='m',started_ts=11,finished_ts=14,status='SELLABLE')
    job=dict(scheduled_ts=11,inventory=inv)
    result=dict(status='CHECKED',snapshot_received_ts=13,zero_token_control_ok=True,
        reserves=dict(status='RESERVES_COVER_GROSS'),sale=dict(status='LOCAL_SWAP_EXECUTED',mint='m',raw_amount='1000000000',seller_net_sol_delta_lamports=28000000,synthetic_token_rent_lamports=1000000))
    return row,job,result,inv

@pytest.mark.parametrize('change,expected',[
    ('before','STARTED_BEFORE_SIGNAL'),('late','AFTER_OR_AT_TICK_CLOSE'),
    ('stale','SNAPSHOT_NOT_TIMELY'),('slow','CHECK_NOT_TIMELY'),
    ('amount','AMOUNT_MISMATCH'),('control','NO_VERIFIED_SALE'),
    ('reserves','RESERVES_NOT_CONFIRMED'),('route','ROUTE_NOT_SUPPORTED_IN_V1')])
def test_exclusions(change,expected):
    row,job,r,inv=case();closed=30
    if change=='before':row['started_ts']=9
    if change=='late':closed=14
    if change=='stale':r['snapshot_received_ts']=8
    if change=='slow':job['scheduled_ts']=-20
    if change=='amount':r['sale']['raw_amount']='10'
    if change=='control':r['zero_token_control_ok']=False
    if change=='reserves':r['reserves']['status']='INSUFFICIENT_REAL_RESERVES'
    if change=='route':job['route']='PUMP_AMM'
    assert se.eligibility(row,job,r,10,closed,inv)==expected

def test_first_available_not_best_and_no_mutation(monkeypatch):
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE paper_orders_v0720(order_id,trade_id,side,signal_ts,reason);
    INSERT INTO paper_orders_v0720 VALUES('o','t','SELL',10,'TP2');
    CREATE TABLE trades(trade_id,side,ts);
    CREATE TABLE quote_inventory_v0723(trade_id,mint,size_usdc,sol_usdc,token_units,entry_sol_price,raw_amount);
    INSERT INTO quote_inventory_v0723 VALUES('t','m','3','100','1000','.00003','1000000000');
    CREATE TABLE capital_ledger_v0721(trade_id,side,notional,total_cost);
    INSERT INTO capital_ledger_v0721 VALUES('t','BUY',3,.05);
    CREATE TABLE position_checks_v0730(id,trade_id,mint,started_ts,finished_ts,status,input_json,result_json);''')
    row,j,r,inv=case()
    for ident,end,net in [('a',14,28000000),('b',15,40000000)]:
        r['sale']['seller_net_sol_delta_lamports']=net
        c.execute('INSERT INTO position_checks_v0730 VALUES(?,?,?,?,?,?,?,?)',(ident,'t','m',11,end,'NET_BELOW_FLOOR',json.dumps(j),json.dumps(r)))
    monkeypatch.setattr(se.cohort_report,'summary',lambda c,selected: {})
    before=c.total_changes;s=se.summary(c)
    assert len(s['selected'])==1 and s['selected'][0]['check_id']=='a'
    assert float(s['selected'][0]['receipt_units'])==pytest.approx(2.7)
    assert float(s['selected'][0]['pnl_units'])==pytest.approx(-.35)
    assert s['decisions']['ALREADY_SELECTED']==1
    assert se.summary(c)==s and c.total_changes==before
