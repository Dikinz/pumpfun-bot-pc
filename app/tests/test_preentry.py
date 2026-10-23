import json,time,sqlite3,zipfile,io
from pathlib import Path
import pytest
import preentry as pre,roundtrip_simulation as rt,local_simulation as lab,config,main,exit_quotes as q
from test_execution_model import env
from test_primary_integrity import fresh

@pytest.fixture
def bundle():return json.loads(Path('research/local_snapshot_v26.json').read_text())

def approval(ts):
    return dict(status='ROUNDTRIP_EXECUTED',reserve_gate='PASS',net_gate='PASS',zero_token_control={'status':'FAILED','error_code':6023},snapshot_received_ts=ts)

def test_real_buy_mints_no_tokens_and_transfers_pool_inventory(bundle):
    r=rt.run_roundtrip(bundle,28000000,1)
    assert r['status']=='ROUNDTRIP_EXECUTED'
    assert r['acquired_tokens_raw']==r['tokens_sold_raw'] and r['tokens_remaining_raw']=='0'
    assert r['post_buy_real_sol_lamports']>r['pre_real_sol_lamports']
    assert abs(r['post_sell_real_sol_lamports']-r['pre_real_sol_lamports'])<=2
    assert r['all_in_roundtrip_delta_lamports']==r['wallet_roundtrip_delta_lamports']-r['token_account_setup_rent_lamports']
    assert r['buy_local_fee_lamports']==r['sell_local_fee_lamports']==5000
    assert r['zero_token_control']['status']=='FAILED' and not r['broadcast']


def test_buy_minimum_is_enforced_and_cost_gate_not_silently_relaxed(bundle):
    r=rt.run_roundtrip(bundle,28000000,2**63)
    assert r['status']=='BUY_SIMULATION_FAILED' and 'sell' not in r
    r=rt.run_roundtrip(bundle,28000000,1,max_loss_bps=0)
    assert r['status']=='ROUNDTRIP_EXECUTED' and r['net_gate']=='ROUNDTRIP_LOSS_TOO_HIGH'


def test_no_paper_order_before_check_and_gate_rechecked_at_fill(env,monkeypatch):
    c,clock,tick,entry=env;monkeypatch.setattr(config,'PREFLIGHT_ENABLED',True)
    monkeypatch.setattr(q,'FX',{'rate':'100','ts':clock['t'],'response':{}})
    cash=__import__('paper_capital').cash(c)
    assert entry()==(False,'PREFLIGHT_PENDING')
    assert not main.P and c.execute('select count(*) from paper_orders_v0720').fetchone()[0]==0
    job=dict(c.execute('select * from preentry_v0729').fetchone())
    pre.complete(c,job,approval(clock['t']),now=clock['t'])
    assert main.buy('a',1,s=main.S['a'])==(True,'ENTRY_PENDING')
    assert __import__('paper_capital').cash(c)==cash
    tick(p=1.1) # >5% move: reject despite technically fresh approval
    assert not main.P
    assert c.execute('select status from paper_orders_v0720').fetchone()[0]=='REJECTED'


def test_approved_gate_allows_baseline_fill(env,monkeypatch):
    c,clock,tick,entry=env;monkeypatch.setattr(config,'PREFLIGHT_ENABLED',True)
    monkeypatch.setattr(q,'FX',{'rate':'100','ts':clock['t'],'response':{}})
    entry();job=dict(c.execute('select * from preentry_v0729').fetchone())
    pre.complete(c,job,approval(clock['t']),now=clock['t'])
    main.buy('a',1,s=main.S['a']);tick(p=1.01)
    assert 'a' in main.P
    order=c.execute('select * from paper_orders_v0720').fetchone();gate=c.execute('select * from preentry_v0729').fetchone()
    assert gate['finished_ts']<=order['signal_ts']<order['fill_ts']
    assert gate['order_id']==order['order_id'] and gate['status']=='USED'

@pytest.mark.parametrize('change,delay,expected',[
    ({'reserve_gate':'INSUFFICIENT_PREENTRY_RESERVES'},1,'INSUFFICIENT_PREENTRY_RESERVES'),
    ({'net_gate':'ROUNDTRIP_LOSS_TOO_HIGH'},1,'ROUNDTRIP_LOSS_TOO_HIGH'),
    ({'zero_token_control':{'status':'EXECUTED'}},1,'CONTROL_NOT_CONFIRMED'),
    ({'zero_token_control':{'status':'FAILED','error_code':6003}},1,'CONTROL_NOT_CONFIRMED'),
    ({},11,'LATE_PREFLIGHT_RESULT'),({},6,'STALE_PREFLIGHT_SNAPSHOT')])
def test_fail_closed_and_classified(fresh,monkeypatch,change,delay,expected):
    monkeypatch.setattr(q,'FX',{'rate':'100','ts':100})
    pre.request(fresh,'mint',1,3,100);j=dict(fresh.execute('select * from preentry_v0729').fetchone())
    pre.complete(fresh,j,dict(approval(100),**change),now=100+delay)
    assert fresh.execute('select status from preentry_v0729').fetchone()[0]==expected
    assert not pre.allow(fresh,'mint',1,3,100+delay)[0]


def test_restart_expiry_fx_and_size_fences(fresh,monkeypatch):
    monkeypatch.setattr(q,'FX',{'rate':'100','ts':100})
    pre.request(fresh,'mint',1,3,100);j=dict(fresh.execute('select * from preentry_v0729').fetchone())
    pre.complete(fresh,j,approval(100),now=101)
    assert pre.allow(fresh,'mint',1,3,102)[0]
    assert not pre.allow(fresh,'mint',1,4,102)[0]
    assert not pre.allow(fresh,'mint',1,3,106)[0]
    monkeypatch.setattr(q,'FX',{'rate':'101','ts':101})
    assert pre.allow(fresh,'mint',1,3,102)[1]=='PREFLIGHT_FX_CHANGED'
    pre.recover(fresh)
    assert fresh.execute('select status from preentry_v0729').fetchone()[0]=='INTERRUPTED_RESTART'


def test_preentry_export(fresh,tmp_path,monkeypatch):
    monkeypatch.setattr(q,'FX',{})
    pre.request(fresh,'mint',1,3,100)
    path=fresh.execute('pragma database_list').fetchone()[2]
    z=pre.export(path,tmp_path/'exports')
    with zipfile.ZipFile(z) as f:
        d=json.loads(f.read('summary.json'));assert d['counts']=={'MISSING_ENTRY_FX':1}


def test_empty_pool_buy_funds_exit_but_reserve_gate_still_rejects(fresh):
    b=json.loads(Path('research/preentry_empty_pool_snapshot.json').read_text())
    old=lab.run_snapshot(b)
    assert '6024' in old['err']
    assert rt.failure_classification(old,b)['category']=='PROGRAM_OVERFLOW_WITH_LOW_RESERVES'
    r=rt.run_roundtrip(b,28000000,1)
    assert r['status']=='ROUNDTRIP_EXECUTED' and r['net_gate']=='PASS'
    assert r['reserve_gate']=='INSUFFICIENT_PREENTRY_RESERVES' and r['pre_real_sol_lamports']==3
    assert r['post_buy_real_sol_lamports']>28000000*.95
    assert r['post_sell_real_sol_lamports']<=4


def test_common_cohort_excludes_repeat_only_admission(fresh):
    import exit_comparison as ec
    # No trades required: three fabricated observation branches exercise the
    # aggregation only, not a claim of program execution.
    for oid,firststatus in [('both','ACCEPTED_LOCAL'),('repeat_only','NET_BELOW_FLOOR')]:
        fresh.execute("insert into auto_sim_v0727(order_id,mint,signal_ts,status) values(?,?,1,'DONE')",(oid,oid))
        data=dict(mint=oid,signal_ts=1)
        for branch,status,pnl in [('FIRST',firststatus,'1'),('REPEAT','ACCEPTED_LOCAL','2'),('TICK','TICK_FILLED','3')]:
            ec.put(fresh,oid,branch,data,status,'OK','OK',2,'4',pnl,{})
    result=ec.summary(fresh);common=result['common_cohort']
    assert common['count']==1 and common['excluded']==1 and common['order_ids']==['both']
    assert [x['count'] for x in common['rows']]==[1,1,1]
    assert [x['pnl_units'] for x in common['rows']]==['1','2','3']
    assert result['aggregates'][1]['paired']==2


def test_ready_gate_resumes_on_new_nonmilestone_signal(env,monkeypatch):
    c,clock,tick,entry=env;monkeypatch.setattr(config,'PREFLIGHT_ENABLED',True)
    monkeypatch.setattr(q,'FX',{'rate':'100','ts':clock['t'],'response':{}})
    entry();job=dict(c.execute('select * from preentry_v0729').fetchone())
    pre.complete(c,job,approval(clock['t']),now=clock['t'])
    main.S['a']['trades']=8
    monkeypatch.setattr(main,'calc',lambda s:(1001,'PASS',2,0))
    tick(p=1)
    assert c.execute("select count(*) from paper_orders_v0720 where side='BUY' and status='PENDING'").fetchone()[0]==1
    assert not main.P
    tick(p=1.01)
    assert main.P['a']['entry']==1.01
