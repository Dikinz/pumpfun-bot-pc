import json,time,zipfile
from pathlib import Path
import pytest
import preentry,shadow_preflight as sp,local_simulation as lab,exit_quotes as q
from test_primary_integrity import fresh

def test_review96_shadow_forwards_rooted_and_metadata_keywords_counts_genesis(tmp_path,monkeypatch):
 seen=[]
 def read(addresses,min_slot=None,**kw):seen.append((min_slot,kw));return {}
 monkeypatch.setattr(lab,'read_accounts',read)
 monkeypatch.setattr(q,'request_json',lambda *a,**k:{'result':'genesis'})
 def child(data,out,replay):
  lab.read_accounts(['mint']);lab.read_accounts(['accounts'],123)
  q.request_json('https://rpc.invalid','PROGRAM_CACHE_GENESIS',{'method':'getGenesisHash'})
  lab.read_accounts(['code'],commitment='finalized')
  lab.read_accounts(['fresh bank'],125)
  lab.read_accounts(['version'],126,data_slice=45)
  with pytest.raises(lab.LabError,match='RPC_CALL_LIMIT'):lab.read_accounts(['seventh'])
  with zipfile.ZipFile(out/'evidence.zip','w') as z:z.writestr('result.json','{}')
  return dict(status='CONTROLLED_INTERFACE_CHECK')
 monkeypatch.setattr(preentry,'child',child)
 r=sp.child({},tmp_path)
 assert r['rpc_calls']==6 and len(seen)==5
 assert r['rpc_activity'][2]['method']=='getGenesisHash'
 assert seen[2][1]['commitment']=='finalized' and seen[-1][1]['data_slice']==45
 assert seen[-1][0]==126


def test_review96_preentry_preserves_sanitized_traceback(tmp_path,monkeypatch):
 def fail(*a,**kw):raise TypeError('PRIVATE_API_KEY_https://private.invalid')
 monkeypatch.setattr(lab,'capture',fail)
 data=dict(size_units='3',fx_rate='100',mint='m',signal_price='1',buy_min_fraction='0.95',signal_ts=time.time(),max_loss_bps=1500,min_reserve_multiple=1)
 r=preentry.child(data,tmp_path)
 assert r['status']=='REQUEST_OR_LAB_ERROR_TypeError'
 diagnostic=r['exception_v96'];assert any(f['function']=='fail' for f in diagnostic['frames'])
 assert len(diagnostic['traceback'])<=4096 and 'PRIVATE' not in json.dumps(diagnostic) and 'https://' not in json.dumps(diagnostic)
 with zipfile.ZipFile(tmp_path/'evidence.zip') as z:assert json.loads(z.read('result.json'))['exception_v96']==diagnostic


def test_review96_unstartable_shadow_sample_is_counted_without_queue_or_spawn(fresh,monkeypatch):
 from test_shadow_preflight_v43 import offer,job,result
 monkeypatch.setattr(q,'FX',dict(rate='100',ts=100))
 offer(fresh,'first',100);j,_=sp.next_job(fresh,100);sp.complete(fresh,j,result(j),now=102)
 assert offer(fresh,'too_early',103)=='SKIPPED_SAMPLING_INTERVAL'
 assert job(fresh,'too_early')['started_ts'] is None
 assert offer(fresh,'can_start_in_age',121)=='PENDING'
 s=sp.summary(fresh);assert s['groups']['ALL']['total']==3 and s['groups']['ALL']['statuses']['SKIPPED_SAMPLING_INTERVAL']==1
 assert fresh.execute('select count(*) from paper_orders_v0720').fetchone()[0]==0


def test_review96_remainder_marks_route_without_releasing_position_or_capital(fresh):
 import main,paper_capital as cap,campaign_view,route_branch
 main.buy('remainder',1);now=time.time();tid=next(iter(main.P.values()))['trade_id']
 fresh.executescript(route_branch.SCHEMA)
 fresh.execute("INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,size,trade_id,reason) VALUES('exit','remainder','SELL','PENDING',?,3,?,'FAST_STOP')",(now-121,tid))
 fresh.execute("INSERT INTO meta(k,v) VALUES('heartbeat_ts',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(str(now),))
 before=cap.summary(fresh);count=fresh.execute('select count(*) from positions').fetchone()[0]
 view=campaign_view.branch(fresh,Path('missing.db'),'route',now)
 row=view['positions'][0];assert row['exit_state_v96']=='OPEN_REMAINDER' and row['slot_retained_v96'] and not row['capital_released_v96']
 assert cap.summary(fresh)==before and fresh.execute('select count(*) from positions').fetchone()[0]==count
 assert not fresh.execute("select 1 from trades where side='SELL'").fetchone()


def test_review96_recovery_budget_is_offline_only_and_worker_deadlines_unchanged():
 import recovery_selftest,position_monitor as pm,rpc_budget
 assert recovery_selftest.SUITE_TIMEOUT_S==600
 assert pm.CHECK_DEADLINE==10 and pm.FRESH_SECONDS==5 and pm.INTERVAL==10
 b=rpc_budget.Budget('unused');assert b.rate==6 and b.slots==4 and b.wait==.75
