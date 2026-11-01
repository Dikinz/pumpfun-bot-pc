"""Remainder scheduling, shared transport, valuation, exports: no provider IO."""
import json,sqlite3,time,zipfile,hashlib,subprocess,sys,os,socket,ssl
from pathlib import Path
import pytest
import main,config,position_monitor as pm,remainder_probe_v100 as policy,paper_capital
from test_primary_integrity import fresh
from test_reserve_hint_v98 import negative,local_error
from test_rpc_transport_v99 import server,GENESIS

def pending(c):
 c.execute("INSERT OR REPLACE INTO meta VALUES('branch_role','ROUTE_PAPER')")
 assert main.buy('remainder',1)[0]
 tid=main.P['remainder']['trade_id'];now=time.time()
 c.execute("INSERT INTO paper_orders_v0720(order_id,trade_id,mint,side,status,signal_ts) VALUES('pending',?,'remainder','SELL','PENDING',?)",(tid,now-121))
 c.execute("INSERT INTO quote_inventory_v0723(trade_id,mint,token_units,sol_usdc,raw_amount,decimals) VALUES(?,'remainder','3',100,'3000000',6)",(tid,))
 job=pm.next_job(c,now);assert job
 pm.finish(c,job,negative(job),now=now)
 assert policy.enabled(c),dict(c.execute('SELECT k,v FROM meta'))
 assert c.execute('SELECT * FROM remainder_probe_v100').fetchone(),dict(job)
 return job,now

def test_review100_backoff_event_wake_and_capital(fresh):
 job,t=pending(fresh);before=paper_capital.summary(fresh)
 assert policy.due(fresh,job['trade_id'],t)==pytest.approx(t+30,abs=.001,rel=0)
 assert pm.next_job(fresh,t+29) is None
 j=pm.next_job(fresh,t+31);assert j['reserve_probe_v96']
 assert pm.next_job(fresh,t+32) is None
 pm.finish(fresh,j,negative(j),now=t+31)
 assert policy.due(fresh,j['trade_id'],t+31)==pytest.approx(t+91,abs=.001,rel=0)
 j=pm.next_job(fresh,t+92);pm.finish(fresh,j,negative(j),now=t+92)
 assert policy.due(fresh,j['trade_id'],t+92)==pytest.approx(t+392,abs=.001,rel=0)
 fresh.execute('UPDATE positions SET last_update_ts=? WHERE trade_id=?',(t+94,j['trade_id']))
 assert policy.due(fresh,j['trade_id'],t+94)==pytest.approx(t+94,abs=.001,rel=0)
 assert pm.next_job(fresh,t+93) is None
 wake=pm.next_job(fresh,t+94);assert wake['reserve_probe_v96']
 pm.finish(fresh,wake,local_error(),now=t+95)
 assert policy.due(fresh,wake['trade_id'],t+99)>=t+392
 after=paper_capital.summary(fresh)
 assert {k:after[k] for k in ('net_cash','available','reserved','closed_net','open_principal','total_cost')}=={k:before[k] for k in ('net_cash','available','reserved','closed_net','open_principal','total_cost')}
 assert fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==1
 assert not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()

def test_review100_event_during_probe_not_acknowledged(fresh):
 job,t=pending(fresh);j=pm.next_job(fresh,t+31)
 fresh.execute('UPDATE positions SET last_update_ts=? WHERE trade_id=?',(t+32,j['trade_id']))
 pm.finish(fresh,j,negative(j),now=t+33)
 assert policy.due(fresh,j['trade_id'],t+34)==pytest.approx(t+33,abs=.001,rel=0)

@pytest.mark.parametrize('change',['inventory','wallet','recovery'])
def test_review100_context_change_full_check(fresh,monkeypatch,change):
 job,t=pending(fresh)
 if change=='inventory':fresh.execute('UPDATE quote_inventory_v0723 SET raw_amount=? WHERE trade_id=?',('888',job['trade_id']))
 elif change=='wallet':
  import wallet_costs
  old=wallet_costs.state;monkeypatch.setattr(wallet_costs,'state',lambda c:dict(old(c),wallet='different'))
 else:
  import recovery_gate
  old=recovery_gate.stamp
  def stamp(d):old(d);d['recovery_epoch_v75']='changed'
  monkeypatch.setattr(recovery_gate,'stamp',stamp)
 assert policy.due(fresh,job['trade_id'],t+11) is None
 j=pm.next_job(fresh,t+11);assert j and not j['reserve_probe_v96']

def test_review100_global_cap_and_reopen(fresh):
 job,t=pending(fresh)
 fresh.execute('UPDATE remainder_probe_v100 SET next_due_ts=?,last_dispatch_ts=?',(t,t))
 assert policy.due(fresh,job['trade_id'],t)==pytest.approx(t+2,abs=.001,rel=0)
 db=fresh.execute('PRAGMA database_list').fetchone()[2]
 with sqlite3.connect(db) as c:
  c.row_factory=sqlite3.Row
  assert policy.due(c,job['trade_id'],t)==pytest.approx(t+2,abs=.001,rel=0)

def test_review100_stress_unknown_no_cash_mutation(fresh,tmp_path):
 job,t=pending(fresh);before=paper_capital.summary(fresh)
 from remainder_reporting_v100 import stress
 r=stress(fresh,tmp_path/'missing.db',t,before)
 assert r['complete'] is False and r['equity_upper_before_exit_cost'] is None
 assert r['cash_only']==before['net_cash'] and r['all_entry_zero_receipt_pnl']==before['net_cash']-before['settings']['start']
 assert paper_capital.summary(fresh)==before

def test_review100_stress_fresh_stale_bound(fresh):
 job,t=pending(fresh);path=fresh.execute('PRAGMA database_list').fetchone()[2]
 data=json.loads(fresh.execute('SELECT result_json FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()[0])
 data['reserves'].update(real_sol_lamports=8,spendable_lamports=5,sell_gross_upper_lamports=100)
 fresh.execute('UPDATE position_checks_v0730 SET result_json=? WHERE id=?',(json.dumps(data),job['id']))
 from remainder_reporting_v100 import stress
 cap=paper_capital.summary(fresh);r=stress(fresh,path,t,cap)
 assert r['complete'] and r['reserve_capped_gross_upper']>=0
 assert r['equity_upper_before_exit_cost']==pytest.approx(cap['net_cash']+r['reserve_capped_gross_upper'])
 old=stress(fresh,path,t+6,cap)
 assert not old['complete'] and old['equity_upper_before_exit_cost'] is None
 assert old['rows'][0]['last_evidence_gross_cap']==r['reserve_capped_gross_upper']

def test_review100_slot_counter_unique(fresh):
 job,t=pending(fresh)
 from remainder_reporting_v100 import record_slot_exclusion,slot_exclusions
 before=paper_capital.summary(fresh)
 record_slot_exclusion(fresh,'missed',t);record_slot_exclusion(fresh,'missed',t+1)
 assert slot_exclusions(fresh)['count']==1 and paper_capital.summary(fresh)==before

def test_review100_shared_genesis_and_admission(server,monkeypatch):
 import local_simulation as lab,program_cache as cache,rpc_timing as timing,rpc_budget as budget
 monkeypatch.setattr(config,'GENESIS_CACHE_TTL_S',0)
 lab.read_accounts(['a']);assert cache.genesis(lab)==GENESIS;lab.read_accounts(['b'])
 assert server.connections==1 and server.requests==3
 assert [r['connection_reused_v99'] for r in timing.snapshot()['requests']]==[False,True,True]
 assert budget.snapshot()['endpoints'][0]['started']==3
 assert cache.genesis(lab)==GENESIS and server.requests==4

def test_review100_genesis_429_common_cooldown(server):
 import program_cache as cache,local_simulation as lab,exit_quotes as quotes,rpc_budget as budget
 server.status=429
 with pytest.raises(quotes.ProbeError) as err:cache.genesis(lab)
 assert err.value.status=='RATE_LIMITED'
 with pytest.raises(budget.Deferred):lab.read_accounts(['a'])
 assert server.requests==1 and budget.snapshot()['endpoints'][0]['server_429']==1

def test_review100_json_export_timestamp_hash_limits(tmp_path):
 from review_bundle_v100 import export
 raw=json.dumps({'created_ts':1000,'x':'a'*10000}).encode();(tmp_path/'acceptance.json').write_bytes(raw)
 (tmp_path/'unknown.json').write_text('{"x":2}');(tmp_path/'private.db').write_bytes(b'no export');(tmp_path/'bad.json').write_text('invalid')
 r=export(tmp_path,max_volume_bytes=1000)
 assert len(r['files'])==2 and r['errors']==[{'name':'bad.json','error_type':'JSONDecodeError'}]
 for v in r['volumes']:
  assert v['bytes']<=1000 or v['oversize_single_report']
  with zipfile.ZipFile(tmp_path/v['name']) as z:
   assert not any(n.endswith('.db') for n in z.namelist())
   for item in json.loads(z.read('REVIEW_MANIFEST.json'))['files']:
    assert hashlib.sha256(z.read(item['name'])).hexdigest()==item['sha256']
    if item['source_name']=='acceptance.json':assert '19700101T001640' in item['name'] and z.read(item['name'])==raw
    else:assert item['report_ts'] is None and item['name']=='unknown.json'
 assert (tmp_path/'acceptance.json').read_bytes()==raw

def test_review100_raw_volumes_source_unchanged(tmp_path):
 from review_bundle_v100 import database_volumes,sha
 source=tmp_path/'preserved.zip'
 with zipfile.ZipFile(source,'w') as z:
  z.writestr('MANIFEST.json',json.dumps({'databases':[{'database':'a.db'},{'database':'b.db'}]}))
  for name in ('a.db','b.db'):
   z.writestr('raw/'+name,b'db');z.writestr('raw/'+name+'-wal',b'wal');z.writestr('readable/'+name+'/consistent.db',b'copy')
 before=sha(source);r=database_volumes(source,tmp_path/'volumes')
 assert sha(source)==before and len(r['volumes'])==2
 for volume in r['volumes']:
  with zipfile.ZipFile(tmp_path/'volumes'/volume['name']) as z:
   assert 'raw/'+volume['database'] in z.namelist()
   assert len([n for n in z.namelist() if n.startswith('raw/')])==2
   assert json.loads(z.read('VOLUME_MANIFEST.json'))['source_archive_sha256']==before

def test_review100_amm_default_off_retained_positions(tmp_path,monkeypatch):
 import branch_mode_v100 as mode,paper_branch
 monkeypatch.setattr(config,'AMM_BRANCH_ENABLED',False);primary=tmp_path/'primary.db'
 assert mode.modules(primary)==('route_branch',)
 with sqlite3.connect(paper_branch.path(primary)) as c:
  c.execute('CREATE TABLE positions(trade_id TEXT)');c.execute("INSERT INTO positions VALUES('retained')")
 with pytest.raises(RuntimeError,match='AMM_DISABLED_WITH_OPEN_POSITIONS'):mode.modules(primary)
 monkeypatch.setattr(config,'AMM_BRANCH_ENABLED',True)
 assert mode.modules(primary)==('paper_branch','route_branch')
 isolated=tmp_path/'clean_config';isolated.mkdir();(isolated/'config.py').write_bytes(Path(config.__file__).read_bytes())
 env=dict(os.environ);env.pop('AMM_BRANCH_ENABLED',None);env.pop('PYTHONPATH',None);env['EXECUTION_MODE']='paper'
 done=subprocess.run([sys.executable,'-c','import config;print(config.AMM_BRANCH_ENABLED)'],env=env,cwd=isolated,capture_output=True,text=True,timeout=15)
 assert done.returncode==0 and done.stdout.strip()=='False'

def test_review100_wallet_sensitivity_no_refund():
 from cost_classes_v100 import wallet_model,classify
 rows=[{'details':{'route':r,'accounting':{'fx':100,'token_rent_lamports':2000000,'volume_rent_lamports':1000000}}} for r in ('PUMP_CURVE','PUMP_CURVE','PUMP_AMM')]
 model=wallet_model(rows,-2.)
 assert model['retained_first_volume_by_program']==pytest.approx(.2)
 assert model['retain_token_accounts_model_net']==pytest.approx(-1.9)
 assert model['assumed_ata_close_model_net']==pytest.approx(-1.3)
 assert model['actual_refunds_proven'] is False and model['common_wallet_costs_proven'] is False
 assert classify(1,2,3)[1]['classes']==['ONE_TIME_PER_WALLET']
 assert wallet_model([{'details':{}}],-2.)['retain_token_accounts_model_net'] is None

def test_review100_gap_boundaries_not_candidate_loss(fresh):
 import discovery_gaps_v100 as gaps
 gaps.failed(fresh,'HTTP502',100);gaps.failed(fresh,'HTTP502',102);gaps.recovered(fresh,110)
 row=dict(fresh.execute('SELECT * FROM discovery_gaps_v100').fetchone())
 assert row['duration_s']==10 and row['error_count']==2
 gaps.failed(fresh,'DNS',200);gaps.recovered(fresh,199)
 assert fresh.execute('SELECT duration_s FROM discovery_gaps_v100 ORDER BY id DESC LIMIT 1').fetchone()[0] is None

def test_review100_expiry_denominator_no_unsupported_approvals(fresh):
 import run_review_v100 as review
 # Use a small dedicated schema to check bins independent of trading eligibility.
 with sqlite3.connect(':memory:') as c:
  c.row_factory=sqlite3.Row;c.execute('CREATE TABLE preentry_v0729(status,started_ts,finished_ts,result_json)')
  c.executemany('INSERT INTO preentry_v0729 VALUES(?,?,?,?)',[
   ('SPECIAL_CURVE_UNSUPPORTED',1,2,'{}'),('USED',1,3,'{}'),('APPROVAL_EXPIRED',1,3.2,'{}'),
   ('BUY_SIMULATION_FAILED',1,4,json.dumps({'buy':{'error_code':6042,'error_name':'BuySlippageBelowMinTokensOut'}}))])
  r=review.preentry(c)
 assert r['expiry_by_wall_runtime'][0]['expiry_rate'] is None
 assert r['expiry_by_wall_runtime'][1]['expiry_rate']==.5
 assert r['buy_failure_reasons'][0]['error_code']=='6042'

def test_review100_price_path_aggregated_bounded_no_interpolation():
 import price_path_v100 as path
 path.begin('lot',100,10,'process')
 for i in range(1000):path.event('lot',100+i/1000,10+i/1000,1+i/1000,'provider','basis','process')
 r=path.snapshot('lot','process')
 assert len(r['bars'])==1 and r['bars'][0]['n']==1000 and r['bars'][0]['low']==1
 for i in range(1,210):path.event('lot',100+i,10+i,2,'provider','basis','process')
 r=path.snapshot('lot','process');assert len(r['bars'])==200 and r['evicted_bars']==10
 assert not path.snapshot('lot','restarted')['available']
 path.PATHS.pop('lot')

def test_review100_positive_probe_clears_policy_full_fresh_next(fresh):
 source,t=pending(fresh);job=pm.next_job(fresh,t+31)
 before=list(fresh.execute('SELECT cash_delta,total_cost FROM capital_ledger_v0721'))
 result=dict(status='CHECKED',scheduled_ts=job['scheduled_ts'],snapshot_received_ts=job['scheduled_ts'],reserves=dict(status='RESERVES_COVER_GROSS',real_sol_lamports=100),sale=dict(status='LOCAL_SWAP_EXECUTED'),zero_token_control_ok=True,net_status='NET_MEETS_FLOOR')
 pm.finish(fresh,job,result,now=job['scheduled_ts'])
 assert not fresh.execute('SELECT 1 FROM remainder_probe_v100').fetchone()
 following=pm.next_job(fresh,t+42)
 assert following and not following['reserve_probe_v96']
 assert list(fresh.execute('SELECT cash_delta,total_cost FROM capital_ledger_v0721'))==before

def test_review100_unrelated_event_does_not_wake_remainder(fresh):
 source,t=pending(fresh)
 assert main.buy('other',1)[0]
 fresh.execute("UPDATE positions SET last_update_ts=? WHERE mint='other'",(t+3,))
 assert policy.due(fresh,source['trade_id'],t+3)==pytest.approx(t+30,abs=.001,rel=0)

def test_review100_failed_schedule_rolls_back_probe_ownership(fresh):
 source,t=pending(fresh);before=dict(fresh.execute('SELECT * FROM remainder_probe_v100').fetchone())
 fresh.execute("CREATE TEMP TRIGGER fail_check BEFORE INSERT ON position_checks_v0730 BEGIN SELECT RAISE(ABORT,'injected schedule failure'); END")
 with pytest.raises(sqlite3.IntegrityError,match='injected schedule failure'):pm.next_job(fresh,t+31)
 assert dict(fresh.execute('SELECT * FROM remainder_probe_v100').fetchone())==before
 assert not fresh.execute("SELECT 1 FROM position_checks_v0730 WHERE status='RUNNING'").fetchone()
 fresh.execute('DROP TRIGGER fail_check')
 assert pm.next_job(fresh,t+31)['reserve_probe_v96']

def test_review100_roadmap_collection_survives_amm_off():
 import roadmap_progress as roadmap
 model=roadmap.build([{'id':i} for i in range(4)],{'new_closed':11},None,{}, {'status':'READY','endpoints':[]}, {})
 assert model['collection_basis_v100']=='PRIMARY_NEW_CLOSED' and model['stages'][1]['percent']==6
 assert not any(c['passed'] for c in model['stages'][0]['checkpoints'])

@pytest.mark.parametrize('error',[socket.gaierror(-2,'injected DNS failure'),ssl.SSLError(1,'injected TLS failure')])
def test_review100_genesis_os_failure_keeps_original_cause(server,monkeypatch,error):
 import program_cache as cache,local_simulation as lab,exit_quotes as quotes,rpc_transport_v99 as transport,rpc_budget as budget
 def fail(*a,**k):raise error
 monkeypatch.setattr(transport,'post',fail)
 with pytest.raises(quotes.ProbeError) as caught:cache.genesis(lab)
 assert caught.value.status=='REQUEST_ERROR' and caught.value.__cause__ is error
 assert server.requests==0 and budget.snapshot()['endpoints'][0]['started']==1
