import json,os,sqlite3,threading,time,uuid,urllib.error
from pathlib import Path
import pytest
import rpc_budget as rpc,connection_audit
URL='https://rpc.invalid'

def outcome(b,kind='OK',delay=None):
 d=b.root/'outcomes';d.mkdir(parents=True,exist_ok=True);identifier=uuid.uuid4().hex
 row=dict(schema='RPC_OUTCOME_V892',id=identifier,key=rpc.endpoint_key(URL),kind=kind,retry_after_s=delay,wait_ms=1.,observed_mono=time.monotonic(),observed_wall=time.time())
 p=d/(identifier+'.json');p.write_text(json.dumps(row));return p

def test_rpc95_one_connection_reused_and_no_extra_receipt_commit(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,wait=.2);opened=[];sql=[];real=rpc.sqlite3.connect
 def connect(*a,**kw):
  c=real(*a,**kw);c.set_trace_callback(sql.append);opened.append(c);return c
 monkeypatch.setattr(rpc.sqlite3,'connect',connect)
 for _ in range(10):
  with b.request(URL):pass
 assert len(opened)==1
 assert sum(s=='COMMIT' for s in sql)==20
 assert not any(s=='DELETE FROM outcome_receipts' for s in sql)
 assert b.metrics()['transactions']==2 # Last permit, reserve and HTTP outcome, no separate receipt-GC commit.
 b.close()

def test_rpc95_batch_commits_multiple_429_once_and_replay_is_idempotent(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000)
 with b.request(URL):pass
 paths=[outcome(b,'429',10),outcome(b,'OK'),outcome(b,'ERROR')]
 before=b.metrics()['transactions']
 with b.control_lock():b._drain_locked(unlink=False)
 assert b.metrics()['transactions']==before+1
 first=b.snapshot()['endpoints'][0];assert first['server_429']==1 and first['other_errors']==1 and first['cooldown_remaining_s']>9
 with b.control_lock():b._drain_locked()
 again=b.snapshot()['endpoints'][0];assert again['server_429']==1 and again['other_errors']==1 and not b._pending()
 assert first['total_wait_ms']==again['total_wait_ms']
 b.close()

def test_rpc95_receipts_stay_bounded_across_long_sequence(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000,wait=.2)
 for _ in range(100):
  with b.request(URL):pass
 with b.session() as c:assert c.execute('select count(*) from outcome_receipts').fetchone()[0]<=32
 assert b.snapshot()['endpoints'][0]['started']==100 and not b._pending()
 b.close()

def test_rpc95_slow_unlink_does_not_hold_metadata_or_reopen_committed_payload(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,wait=.2);entered=threading.Event();release=threading.Event();errors=[];real=Path.unlink
 recorder=None
 def unlink(p,*a,**kw):
  if p.parent.name=='outcomes' and threading.current_thread() is recorder:
   entered.set();assert release.wait(5)
  return real(p,*a,**kw)
 monkeypatch.setattr(Path,'unlink',unlink)
 def work():
  try:
   with b.request(URL):pass
  except BaseException as exc:errors.append(exc)
  finally:b.close()
 recorder=threading.Thread(target=work);recorder.start()
 try:
  assert entered.wait(2)
  other=rpc.Budget(tmp_path,rate=1000,wait=.2)
  with other.control_lock():pass
  old=Path.read_text
  def forbid_payload(p,*a,**kw):
   if p.parent.name=='outcomes':raise AssertionError('reopened a committed outcome')
   return old(p,*a,**kw)
  # Reserve skips the already-applied payload, but still honors its committed state.
  with monkeypatch.context() as m:
   m.setattr(Path,'read_text',forbid_payload)
   with other.control_lock():other._drain_locked()
  with other.request(URL):pass
  assert other.snapshot()['endpoints'][0]['started']==2
  other.close()
 finally:release.set();recorder.join(3)
 assert not recorder.is_alive() and not errors

def test_rpc95_metadata_contention_has_exact_stage(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01)
 with b.control_lock():
  with pytest.raises(rpc.Deferred) as e:
   with rpc.Budget(tmp_path,rate=1000,wait=.01).request(URL):pytest.fail('lock bypass')
 assert e.value.rpc_budget_reason=='STATE_LOCKED'
 d=connection_audit.diagnostic(e.value);assert d['chain'][0]['control_v95']['stage']=='METADATA_LOCK'
 assert d['chain'][0]['control_v95']['sqlite_errorcode'] is None
 b.close()

def test_rpc95_sqlite_contention_has_code_and_rollback_restores_connection(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01)
 with b.request(URL):pass
 blocker=sqlite3.connect(b.path,isolation_level=None);blocker.execute('BEGIN IMMEDIATE')
 try:
  with pytest.raises(rpc.Deferred) as e:
   with b.request(URL):pytest.fail('writer bypass')
  d=e.value.control_v95;assert d['stage']=='SQLITE_BEGIN' and d['sqlite_errorcode']==5
  with b.session() as c:assert not c.in_transaction
 finally:blocker.rollback();blocker.close()
 with b.request(URL):pass
 assert b.snapshot()['endpoints'][0]['started']==2
 b.close()

def test_rpc95_partial_batch_failure_rolls_back_all_receipts(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000)
 with b.request(URL):pass
 paths=[outcome(b,'429',10),outcome(b,'ERROR')];real=b._apply_record;calls=[]
 def fail(*a,**kw):
  calls.append(1)
  if len(calls)==2:raise sqlite3.OperationalError('controlled batch failure')
  return real(*a,**kw)
 with monkeypatch.context() as m:
  m.setattr(b,'_apply_record',fail)
  with pytest.raises(sqlite3.OperationalError):
   with b.control_lock():b._drain_locked()
 assert b.snapshot()['endpoints'][0]['server_429']==0 and all(p.exists()for p in paths)
 with b.control_lock():b._drain_locked()
 assert b.snapshot()['endpoints'][0]['server_429']==1
 b.close()

def test_rpc95_replaced_control_db_cannot_reuse_cached_authority(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000)
 with b.request(URL):pass
 # Windows normally prevents replacing an open SQLite file; inject the
 # remembered identity mismatch there, and exercise actual replacement on POSIX.
 if os.name=='nt':b._local.identity=(-1,-1)
 else:b.path.rename(tmp_path/'old.db')
 replacement=rpc.Budget(tmp_path,rate=1000)
 with replacement.control_lock():
  with replacement.session():pass
 with pytest.raises(rpc.Unavailable,match='RPC_CONTROL_DB_REPLACED'):
  with b.request(URL):pytest.fail('cached old DB reused')
 assert b.failed.exists();b.close();replacement.close()

def test_rpc95_thread_connections_are_separate_and_settings_still_match(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000);ids=[];errors=[]
 def worker():
  try:
   with b.control_lock():
    with b.session() as c:ids.append(c)
  except Exception as e:errors.append(e)
  finally:b.close()
 # Sequential threads remove unrelated mutex contention from this isolation check.
 for _ in range(2):
  t=threading.Thread(target=worker);t.start();t.join(3)
 assert not errors and len(ids)==2 and ids[0]is not ids[1]
 with pytest.raises(rpc.Unavailable,match='DISAGREE'):
  with rpc.Budget(tmp_path,rate=6).request(URL):pass

def test_rpc95_corrupt_journal_never_partially_applies_429(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000)
 with b.request(URL):pass
 outcome(b,'429',10);p=outcome(b);p.write_text('{}')
 with pytest.raises(rpc.Unavailable,match='RPC_OUTCOME_INVALID'):
  with b.request(URL):pytest.fail('corrupt state admitted')
 assert b.failed.exists() and b.snapshot()['endpoints'][0]['server_429']==0
 b.close()

def test_rpc95_successful_admission_exposes_bounded_control_timings(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000)
 with b.request(URL) as permit:
  d=permit.telemetry()['control_v95'];assert d['transactions']==1 and d['connections_opened']==1
  assert d['metadata_hold_ms']>=d['sqlite_commit_ms']>=0
 import rpc_timing
 report=rpc_timing.rpc_summary([dict(rpc_timing={'requests':[{'rpc_admission_v89':permit.telemetry(),'status':'OK'}]},network_error={'chain':[{'control_v95':{'stage':'METADATA_LOCK'}},{'control_v95':{'stage':'METADATA_LOCK'}}]})])
 assert report['control_v95']['counts']['transactions']==2
 assert report['control_v95']['checks_by_contention_stage']=={'METADATA_LOCK':1}
 b.close()

def test_rpc95_default_budget_cache_tracks_configuration(tmp_path,monkeypatch):
 import config
 first=rpc.budget();assert rpc.budget()is first
 monkeypatch.setattr(config,'RPC_CONTROL_DIR',tmp_path/'other');second=rpc.budget();assert second is not first
 assert second.root==tmp_path/'other'
