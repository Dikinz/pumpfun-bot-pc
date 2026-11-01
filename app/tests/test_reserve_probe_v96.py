import copy,json,sqlite3,time,zipfile
from pathlib import Path
import pytest
import curve_reserve_probe_v96 as probe,position_monitor as pm,local_simulation as lab,curve_overflow_replay as replay
from test_primary_integrity import fresh
import main,paper_capital as cap

@pytest.fixture
def bank():
 b=copy.deepcopy(replay.load_fixture()[0]);b['snapshot'].update(started_ts=time.time(),received_ts=time.time());return b

def install(bank,monkeypatch):
 calls=[]
 def read(addresses,*a,**kw):
  calls.append(addresses)
  return dict(bank['snapshot'],accounts={k:bank['snapshot']['accounts'].get(k) for k in addresses})
 monkeypatch.setattr(lab,'read_accounts',read)
 return calls

def test_probe96_low_reserves_only_one_current_rpc_no_vm(bank,monkeypatch,tmp_path):
 calls=install(bank,monkeypatch);monkeypatch.setattr(lab,'run_snapshot',lambda *a,**k:pytest.fail('negative probe ran VM'))
 r=probe.check(bank['mint'],int(bank['amount']),bank['wallet'])
 assert r['status']=='CURVE_RESERVE_PROBE_INSUFFICIENT' and r['simulation_skipped_v96']
 assert len(calls)==1 and len(calls[0])==4 and 'sale' not in r
 assert r['reserves']['real_sol_lamports']==9876550
 r['scheduled_ts']=bank['snapshot']['started_ts']
 assert pm.classify(r,time.time())=='INSUFFICIENT_RESERVES'
 assert pm.classify(r,r['snapshot_received_ts']+6)=='STALE_RESULT'


def test_probe96_reserves_recovered_requires_full_fresh_bank(bank,monkeypatch,tmp_path):
 install(bank,monkeypatch)
 monkeypatch.setattr(pm,'reserve_evidence',lambda b:dict(real_sol_lamports=10**15,spendable_lamports=10**15,status='RESERVES_COVER_GROSS'))
 import exit_quotes as q
 monkeypatch.setattr(q,'resolve_amount',lambda inv:(6,bank['amount']))
 calls=[]
 def capture(*a,**kw):calls.append('fresh capture');raise lab.LabError('FRESH_BANK_REQUIRED')
 monkeypatch.setattr(lab,'capture',capture)
 data=dict(scheduled_ts=time.time(),mint=bank['mint'],inventory={},entry_price='0.000000001',wallet_state=dict(wallet=bank['wallet'],volume=None),reserve_probe_v96=True)
 r=pm.experiment(data,tmp_path)
 assert calls==['fresh capture'] and r['status']=='DATA_REJECTED' and r['error_code']=='FRESH_BANK_REQUIRED'
 assert not r.get('sale') and not r['paper_mutated']


def test_probe96_stale_missing_or_complete_curve_never_becomes_sale(bank,monkeypatch):
 install(bank,monkeypatch);bank['snapshot']['received_ts']=time.time()-6
 with pytest.raises(lab.LabError,match='STALE_RESERVE_PROBE'):probe.check(bank['mint'],int(bank['amount']),bank['wallet'])
 bank['snapshot']['received_ts']=time.time()
 real=lab.addresses
 monkeypatch.setattr(lab,'addresses',lambda *a:(_ for _ in ()).throw(lab.LabError('COMPLETE_CURVE_UNSUPPORTED')))
 with pytest.raises(lab.LabError,match='COMPLETE_CURVE_UNSUPPORTED'):probe.check(bank['mint'],int(bank['amount']),bank['wallet'])
 monkeypatch.setattr(lab,'addresses',real)
 bank['snapshot']['accounts'][lab.RENT]=None
 with pytest.raises(lab.LabError,match='MISSING_INITIAL_ACCOUNT'):probe.check(bank['mint'],int(bank['amount']),bank['wallet'])


def test_probe96_first_check_still_full_and_repeat_keeps_cadence(fresh):
 main.buy('mint',1);before=cap.summary(fresh);now=time.time();first=pm.next_job(fresh,now)
 assert first['reserve_probe_v96'] is False
 result=dict(status='CURVE_RESERVE_PROBE_INSUFFICIENT',scheduled_ts=now,snapshot_received_ts=now,reserves=dict(status='INSUFFICIENT_REAL_RESERVES',real_sol_lamports=1))
 pm.finish(fresh,first,result,now=now)
 assert pm.next_job(fresh,now+9) is None
 second=pm.next_job(fresh,now+10.1);assert second['reserve_probe_v96'] is True and second['route']=='PUMP_CURVE'
 assert cap.summary(fresh)==before and fresh.execute('select count(*) from positions').fetchone()[0]==1
 assert not fresh.execute("select 1 from trades where side='SELL'").fetchone()


def test_probe96_negative_archive_is_replayable_and_never_settles(bank,monkeypatch,tmp_path):
 install(bank,monkeypatch);import exit_quotes as q,route_branch
 monkeypatch.setattr(q,'resolve_amount',lambda inv:(6,bank['amount']))
 monkeypatch.setattr(lab,'capture',lambda *a,**k:pytest.fail('unnecessary full capture'))
 data=dict(id='probe',trade_id='lot',route='PUMP_CURVE',scheduled_ts=bank['snapshot']['started_ts'],mint=bank['mint'],inventory={},entry_price='0.000000001',wallet_state=dict(wallet=bank['wallet'],volume=None),reserve_probe_v96=True)
 r=pm.experiment(data,tmp_path)
 assert pm.classify(r,time.time())=='INSUFFICIENT_RESERVES'
 with zipfile.ZipFile(tmp_path/'evidence.zip') as z:
  saved=json.loads(z.read('snapshot.json'));assert saved['snapshot']==r['reserve_probe_evidence_v96']['snapshot']
 row=dict(trade_id='lot',mint=bank['mint'],started_ts=data['scheduled_ts'],finished_ts=r['finished_ts'])
 monkeypatch.setattr(__import__('recovery_gate'),'allowed',lambda job:True)
 decision,account=route_branch.assess(sqlite3.connect(':memory:'),row,data,r,data['scheduled_ts'],{},time.time())
 assert decision=='INSUFFICIENT_LIQUIDITY_OPEN' and account is None


def test_probe96_rounding_boundary_does_not_skip_simulation(bank,monkeypatch):
 install(bank,monkeypatch);decoded=lab.v.decode_curve(bank['snapshot']['accounts'][bank['account_names']['bonding_curve']]);amount=int(bank['amount'])
 lower=amount*decoded['virtual_sol']//(decoded['virtual_token']+amount)
 monkeypatch.setattr(pm,'reserve_evidence',lambda b:dict(real_sol_lamports=lower,spendable_lamports=lower,status='INSUFFICIENT_REAL_RESERVES'))
 assert probe.check(bank['mint'],amount,bank['wallet']) is None
