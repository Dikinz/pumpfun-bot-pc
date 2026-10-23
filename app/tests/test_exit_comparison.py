import json,time,sqlite3
from pathlib import Path
import pytest
import exit_comparison as ec,auto_simulation as a,local_simulation as lab
from test_exit_quotes import prepared
from test_execution_model import env
from test_primary_integrity import fresh


def good(net=100,floor=100):
    return dict(status='LOCAL_SWAP_EXECUTED',seller_net_sol_delta_lamports=net,minimum_lamports=str(floor),
                zero_token_control_status='LOCAL_SWAP_FAILED',timing_status='WITHIN_5S')

@pytest.mark.parametrize('net,expected',[(99,'NET_BELOW_FLOOR'),(100,'NET_MEETS_FLOOR'),(101,'NET_MEETS_FLOOR')])
def test_net_floor_integer_boundary(net,expected):
    r=ec.evaluate(good(net),10,12)
    assert r['technical_status']=='LOCAL_SWAP_EXECUTED' and r['net_status']==expected
    assert r['comparison_status']==('NET_BELOW_FLOOR' if net<100 else 'ACCEPTED_LOCAL')

@pytest.mark.parametrize('change,available,expected',[
    ({},16,'LATE_RESULT'),({},9,'CLOCK_ANOMALY'),
    ({'timing_status':'LATE_SNAPSHOT'},12,'SNAPSHOT_NOT_TIMELY'),
    ({'zero_token_control_status':'LOCAL_SWAP_EXECUTED'},12,'CONTROL_NOT_CONFIRMED'),
    ({'status':'LOCAL_SWAP_FAILED'},12,'LOCAL_SWAP_FAILED'),
    ({'seller_net_sol_delta_lamports':None},12,'NOT_EVALUATED')])
def test_technical_success_never_overrides_acceptance(change,available,expected):
    assert ec.evaluate(dict(good(),**change),10,available)['comparison_status']==expected


def test_first_repeat_real_program_same_wallet_pool_and_threshold():
    b=json.loads(Path('research/local_snapshot_v26.json').read_text());b['minimum']='13000000'
    first=lab.run_snapshot(b);repeat=lab.run_snapshot(b,volume_scenario='REPEAT')
    assert first['status']==repeat['status']=='LOCAL_SWAP_EXECUTED'
    assert first['wallet']==repeat['wallet']==b['wallet']
    assert first['pool_sol_delta_lamports']==repeat['pool_sol_delta_lamports']
    assert first['volume_account_rent_delta_lamports']==1346200
    assert repeat['volume_account_rent_delta_lamports']==0
    assert repeat['seller_net_sol_delta_lamports']-first['seller_net_sol_delta_lamports']==1346200
    assert repeat['volume_setup']['rent_lamports']==1346200
    assert first['local_fee_from_balance_diff_lamports']==repeat['local_fee_from_balance_diff_lamports']==5000
    assert ec.evaluate(first,0,1)['net_status']=='NET_BELOW_FLOOR'
    assert ec.evaluate(repeat,0,1)['net_status']=='NET_MEETS_FLOOR'
    assert lab.run_snapshot(b,balance_override=0,volume_scenario='REPEAT')['status']=='LOCAL_SWAP_FAILED'
    b['minimum']='21634647'
    assert lab.run_snapshot(b,volume_scenario='REPEAT')['status']=='LOCAL_SWAP_FAILED'


def test_parallel_ledger_does_not_change_trades_and_counts_paired_only(prepared):
    c,j,inv=prepared;oid=j['order_id'];signal=j['signal_ts']
    tables=['capital_ledger_v0721','paper_orders_v0720','trades','positions','meta']
    before={t:[tuple(x) for x in c.execute('select * from '+t)] for t in tables}
    first=good(25000000,26000000);repeat=good(27000000,26000000)
    ec.record_simulation(c,oid,{'scenarios':{'FIRST':first,'REPEAT':repeat}},signal+2)
    ec.record_simulation(c,oid,{'scenarios':{'FIRST':good(999999999),'REPEAT':repeat}},signal+3)
    assert c.execute('select count(*) from exit_comparison_v0728').fetchone()[0]==2
    assert before=={t:[tuple(x) for x in c.execute('select * from '+t)] for t in tables}
    result=ec.summary(c)
    assert [r['accepted'] for r in result['aggregates']]==[0,1]
    assert result['aggregates'][1]['paired']==0
    assert result['aggregates'][1]['paired_sim_pnl_units'] is None
    data=ec.source(c,oid)
    # Known baseline proceeds after fees = 2.8; same entry fee = .05.
    ec.put(c,oid,'TICK',data,'TICK_FILLED','TICK_PROXY','NOT_APPLICABLE',signal+4,'2.8','-0.25',{})
    result=ec.summary(c);r=result['aggregates'][1]
    assert r['paired']==1 and float(r['paired_sim_pnl_units'])==pytest.approx(-.35)
    assert float(r['paired_difference_units'])==pytest.approx(-.1)
    assert r['sim_earlier_than_tick']==1
    assert result['aggregates'][0]['paired']==0


def test_actual_tick_hook_records_once(prepared):
    import main
    c,j,inv=prepared
    c.execute("update paper_orders_v0720 set status='FILLED',fill_ts=? where order_id=?",(j['signal_ts']+2,j['order_id']))
    import paper_capital
    paper_capital.record(c,j['trade_id'],'SELL',3.1,{'pool_fee_rate':.0125})
    ec.record_tick(c,j['order_id']);ec.record_tick(c,j['order_id'])
    row=c.execute("select * from exit_comparison_v0728 where branch='TICK'").fetchone()
    assert row and row['status']=='TICK_FILLED'
    assert float(row['receipt_units'])==pytest.approx(3.1-3.1*.015-.005)
    assert c.execute("select count(*) from exit_comparison_v0728 where branch='TICK'").fetchone()[0]==1


def test_unavailable_cases_recorded_without_zero_profit(prepared):
    c,j,_=prepared;a.recover(c)
    r=ec.summary(c)
    for b in r['rows'][0]['branches'].values():
        assert b['pnl_units'] is None and b['receipt_units'] is None
        assert b['status']=='INTERRUPTED_RESTART'


def test_finish_is_atomic_idempotent_and_preserves_evidence(prepared):
    c,j,_=prepared;oid=j['order_id']
    a.finish(c,oid,{'status':'LOCAL_SWAP_FAILED'})
    before=[tuple(x) for x in c.execute('select * from exit_comparison_v0728')]
    a.finish(c,oid,{'status':'LOCAL_SWAP_EXECUTED'})
    assert c.execute('select status from auto_sim_v0727').fetchone()[0]=='LOCAL_SWAP_FAILED'
    assert before==[tuple(x) for x in c.execute('select * from exit_comparison_v0728')]


def test_journal_failure_rolls_back_simulation_completion(prepared,monkeypatch):
    c,j,_=prepared
    def fail(*args):raise RuntimeError('test journal failure')
    monkeypatch.setattr(ec,'record_simulation',fail)
    with pytest.raises(RuntimeError):a.finish(c,j['order_id'],{'status':'LOCAL_SWAP_FAILED'})
    assert c.execute('select status from auto_sim_v0727').fetchone()[0]=='PENDING'
    assert c.execute('select count(*) from exit_comparison_v0728').fetchone()[0]==0
