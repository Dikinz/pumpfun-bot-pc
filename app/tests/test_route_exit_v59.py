import asyncio,copy,json,sqlite3,time,subprocess,sys
from contextlib import closing
from pathlib import Path
import pytest
import config,main,database as db,paper_capital as cap,route_branch as route,monitor_service as ms,execution_model as ex
from test_primary_integrity import fresh
from test_parallel_paper_v54 import opened

def candidate(tid,now=None):
    now=time.time() if now is None else now
    inv=dict(trade_id=tid,mint='m',size_usdc='3',sol_usdc='100',token_units='10',entry_sol_price='.003',raw_amount='10000000',decimals=6)
    row=dict(id='check',trade_id=tid,mint='m',status='NET_BELOW_FLOOR',started_ts=now-2,finished_ts=now-.1)
    job=dict(trade_id=tid,mint='m',route='PUMP_CURVE',scheduled_ts=now-2.1,inventory=inv)
    sale=dict(status='LOCAL_SWAP_EXECUTED',mint='m',err=None,raw_amount='10000000',tokens_debited='10000000',pool_tokens_credited='10000000',
        pool_sol_delta_lamports=-28000000,seller_net_sol_delta_lamports=26000000,synthetic_token_rent_lamports=1000000,
        local_fee_from_balance_diff_lamports=5000,volume_account_rent_delta_lamports=0,broadcast=False,paper_mutated=False)
    result=dict(status='CHECKED',snapshot_received_ts=now-.5,zero_token_control_ok=True,reserves=dict(status='RESERVES_COVER_GROSS'),sale=sale,broadcast=False,paper_mutated=False)
    return row,job,result,inv

def save_check(fresh,row,job,r,inv):
    fresh.execute('INSERT OR IGNORE INTO quote_inventory_v0723(trade_id,mint) VALUES(?,?)',(row['trade_id'],row['mint']))
    fresh.execute('UPDATE quote_inventory_v0723 SET size_usdc=?,sol_usdc=?,token_units=?,entry_sol_price=?,raw_amount=?,decimals=? WHERE trade_id=?',tuple(inv[k] for k in ('size_usdc','sol_usdc','token_units','entry_sol_price','raw_amount','decimals','trade_id')))
    assert fresh.execute('SELECT COUNT(*) FROM quote_inventory_v0723 WHERE trade_id=?',(row['trade_id'],)).fetchone()[0]==1
    with closing(ms.connect()) as c:
        c.execute('INSERT INTO position_checks_v0730(id,trade_id,mint,started_ts,finished_ts,status,input_json,result_json) VALUES(?,?,?,?,?,?,?,?)',
            (row['id'],row['trade_id'],row['mint'],row['started_ts'],row['finished_ts'],row['status'],json.dumps(job),json.dumps(r)))

@pytest.mark.parametrize('kind',['curve','amm'])
def test_real_poll_settles_without_tick_and_releases_slot(fresh,kind):
    tid=opened(fresh);fresh.executescript(route.SCHEMA);before=cap.cash(fresh)
    if kind=='curve':row,j,r,inv=candidate(tid);receipt=2.5
    else:
        from test_amm_scenario_v53 import payload
        row,j,r,inv=payload();now=time.time();row.update(id='amm',trade_id=tid,started_ts=now-2,finished_ts=now-.1)
        j.update(trade_id=tid,scheduled_ts=now-2);inv['trade_id']=tid;j['inventory']=dict(inv);r['snapshot_received_ts']=now-.5;receipt=3.6995
    save_check(fresh,row,j,r,inv)
    route.poll(main)
    assert fresh.execute('SELECT decision FROM route_exit_decisions_v59').fetchone()[0]=='SETTLED', [dict(x) for x in fresh.execute('SELECT * FROM quote_inventory_v0723')]
    assert 'm' not in main.P and fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0
    assert cap.cash(fresh)==pytest.approx(before+receipt) and cap.summary(fresh)['status']=='OK'
    assert fresh.execute('SELECT decision FROM route_exit_decisions_v59').fetchone()[0]=='SETTLED', [dict(x) for x in fresh.execute('SELECT * FROM quote_inventory_v0723')]
    assert fresh.execute("SELECT note FROM paper_orders_v0720 WHERE side='SELL'").fetchone()[0]==('LOCAL_CURVE_VERIFIED_NO_BROADCAST' if kind=='curve' else 'LOCAL_AMM_VERIFIED_NO_BROADCAST')
    assert fresh.execute("SELECT reason FROM trades WHERE side='SELL'").fetchone()[0]==('CURVE_VERIFIED_EXIT' if kind=='curve' else 'AMM_VERIFIED_EXIT')
    route.poll(main);assert fresh.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==1
    import primary_integrity
    assert primary_integrity.summarize(fresh)['status']=='OK'

@pytest.mark.parametrize('bad',['liquidity','old','pre_signal','amount','identity','broadcast','control','effects','costs','future'])
def test_rejected_evidence_keeps_cash_position_and_pending_order(fresh,bad):
    tid=opened(fresh);fresh.executescript(route.SCHEMA);before=cap.cash(fresh)
    row,j,r,inv=candidate(tid)
    if bad=='liquidity':r['reserves']['status']='INSUFFICIENT_REAL_RESERVES';row['status']='INSUFFICIENT_RESERVES'
    if bad=='old':r['snapshot_received_ts']=time.time()-60
    if bad=='pre_signal':row['started_ts']=time.time()-100
    if bad=='amount':r['sale']['raw_amount']='1'
    if bad=='identity':j['trade_id']='other'
    if bad=='broadcast':r['broadcast']=True
    if bad=='control':r['zero_token_control_ok']=False
    if bad=='effects':r['sale']['tokens_debited']='1'
    if bad=='costs':r['sale']['synthetic_token_rent_lamports']=-1
    if bad=='future':row['finished_ts']=time.time()+2
    save_check(fresh,row,j,r,inv);route.poll(main)
    assert 'm' in main.P and cap.cash(fresh)==before
    assert fresh.execute("SELECT status FROM paper_orders_v0720 WHERE side='SELL'").fetchone()[0]=='PENDING'
    assert fresh.execute('SELECT COUNT(*) FROM branch_amm_settlements').fetchone()[0]==0
    if bad=='liquidity':assert fresh.execute('SELECT decision FROM route_exit_decisions_v59').fetchone()[0]=='INSUFFICIENT_LIQUIDITY_OPEN'

def test_old_success_not_reused_after_restart(fresh):
    tid=opened(fresh);fresh.executescript(route.SCHEMA);row,j,r,inv=candidate(tid,time.time()-60)
    save_check(fresh,row,j,r,inv);main.P.clear();main.restore_positions();route.poll(main)
    assert 'm' in main.P and not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()

def test_new_branch_keeps_old_scenarios_and_does_not_reseed(fresh):
    import paper_branch,clean_campaign,campaign_view
    clean_campaign.ensure(fresh,config.DB);tid=opened(fresh)
    old=paper_branch.seed(config.DB);target=route.seed(config.DB);before=Path(old).read_bytes()
    route.seed(config.DB)
    assert target!=old and Path(old).read_bytes()==before
    with sqlite3.connect(target) as c:assert c.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==1
    report=route.report(config.DB);assert report['carried_ids']==[tid]
    o=campaign_view.overview(config.DB)['route'];assert o['closed']==0 and o['entries']==0 and o['positions']

def test_no_tick_sell_fallback_only_in_new_process_configuration(fresh,monkeypatch):
    original=ex.eligible;monkeypatch.setattr(ex,'eligible',original);route.configure()
    assert ex.eligible(dict(side='SELL'),{}) is False
    monkeypatch.setattr(ex,'eligible',original)

def test_real_two_child_shutdown_has_separate_ack(fresh,monkeypatch):
    import branch_transport as bt,paper_branch,candidate_lab
    real=subprocess.Popen
    script='''import asyncio,sys,json,config
for k,v in json.loads(sys.argv[5]).items():setattr(config,k,v)
import exit_quotes
async def fx():exit_quotes.FX_STATUS='READY'
exit_quotes.refresh_fx=fx
engine=__import__(sys.argv[1])
import main
# Lifecycle test isolates resource sampling in restricted Linux PID namespaces.
async def heartbeat():
    while True:await asyncio.sleep(.1)
main.heartbeat=heartbeat
import paper_branch
asyncio.run(paper_branch.worker(sys.argv[2],sys.argv[3],sys.argv[4],scenario=engine if sys.argv[1]=='route_branch' else None))
'''
    def launch(cmd,**kwargs):
        if len(cmd)>2 and cmd[1] in ('paper_branch.py','route_branch.py') and cmd[2]=='--worker':
            parameters=json.dumps({k:getattr(config,k) for k in candidate_lab.PARAMETERS})
            return real([sys.executable,'-c',script,cmd[1][:-3],*cmd[3:],parameters],**kwargs)
        return real(cmd,**kwargs)
    monkeypatch.setattr(bt.subprocess,'Popen',launch)
    group=bt.Fanout(config.DB)
    try:
        group.refresh_positions();assert not group.failed
        result=group.close();assert len(result)==2 and all(x['status']=='STOPPED' and x['exit_code']==0 for x in result)
        assert paper_branch.read_lifecycle(config.DB)['reason']=='PARENT_STOP' and route.read_lifecycle(config.DB)['reason']=='PARENT_STOP'
        assert paper_branch.lifecycle_path(config.DB)!=route.lifecycle_path(config.DB)
    finally:
        group.close();bt.ACTIVE=None;bt.LAST=None

def test_target_amm_live_label_requires_fresh_canonical_result(fresh):
    from test_amm_scenario_v53 import payload
    import target_amm_check as target
    row,j,r,inv=payload();j['id']='target'
    report=target.evaluate(j,r,101,104,inv,dict(rate='100',ts=100))
    assert report['passed'] and not report['mainnet_execution_proven'] and not report['paper_mutated']
    r['snapshot_received_ts']=1
    assert not target.evaluate(j,r,101,104,inv,dict(rate='100',ts=100))['passed']


def test_curve_resolver_amount_without_jupiter_sizing(fresh):
    tid=opened(fresh);row,j,r,inv=candidate(tid);r['amount_decimals']=6
    inv=dict(inv,raw_amount=None,decimals=None);j['inventory']=dict(inv)
    decision,a=route.assess(fresh,row,j,r,time.time()-10,inv,time.time())
    assert decision=='ELIGIBLE' and float(a['receipt_units'])==2.5
    r['amount_decimals']=5
    assert route.assess(fresh,row,j,r,time.time()-10,inv,time.time())[0]=='AMOUNT_MISMATCH'


def test_v58_depleted_reserve_diagnostic_never_creates_proceeds(fresh):
    fixture=json.loads((Path(__file__).parents[1]/'research/v58_unresolved_diagnostic.json').read_text())
    assert fixture['last_result']['real_sol_lamports']==10
    tid=opened(fresh);fresh.executescript(route.SCHEMA);row,j,r,inv=candidate(tid)
    r['reserves'].update(status='INSUFFICIENT_REAL_RESERVES',real_sol_lamports=fixture['last_result']['real_sol_lamports'])
    r['sale']['status']='LOCAL_SWAP_FAILED';row['status']='INSUFFICIENT_RESERVES'
    save_check(fresh,row,j,r,inv);before=cap.cash(fresh);route.poll(main)
    assert 'm' in main.P and cap.cash(fresh)==before and not fresh.execute('SELECT 1 FROM branch_amm_settlements').fetchone()


def test_partial_fanout_start_failure_closes_both_children(monkeypatch):
    import branch_transport as bt
    closed=[]
    class Child:
        def __init__(self,primary,module):
            self.child=object();self.module=module
            if module=='route_branch':raise RuntimeError('startup failed')
        def begin_stop(self):pass
        def close(self):closed.append(self.module);return dict(status='STOPPED')
    monkeypatch.setattr(bt,'Publisher',Child)
    with pytest.raises(RuntimeError,match='startup failed'):bt.Fanout('unused')
    assert set(closed)=={'paper_branch','route_branch'}
    bt.LAST=None


def test_route_export_retains_sqlite_and_monitor_evidence(fresh,tmp_path):
    import zipfile,clean_campaign
    clean_campaign.ensure(fresh,config.DB);route.seed(config.DB)
    out=tmp_path/'exports';out.mkdir();route.export(config.DB,out)
    with zipfile.ZipFile(out/'route_paper.zip') as z:
        assert 'route_monitor_evidence.zip' in z.namelist()
        assert route.path(config.DB).name in z.namelist()
        assert json.loads(z.read('summary.json'))['status']=='AVAILABLE'
