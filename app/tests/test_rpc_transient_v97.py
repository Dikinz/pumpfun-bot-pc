import json,os,threading,time,urllib.error,uuid
from pathlib import Path
import pytest
import rpc_budget as rpc
from test_rpc_admission_v95 import URL,outcome
from test_rpc_cleanup_v96 import wait_for

def winerr(code=5):
 e=PermissionError(13,'PRIVATE');e.winerror=code;return e

def old_marker(b):
 d=json.loads(b.failed.read_text());d['ts']=time.time()-3;b.failed.write_text(json.dumps(d));return b.failed.read_bytes()

def durable_failure(b,monkeypatch,kind='OK'):
 real=b._drain_locked
 def fail(*a,**kw):
  if b._pending():raise winerr()
  return real(*a,**kw)
 with monkeypatch.context() as m:
  m.setattr(b,'_drain_locked',fail)
  if kind=='429':
   with pytest.raises(urllib.error.HTTPError):
    with b.request(URL):raise urllib.error.HTTPError(URL,429,'PRIVATE',{'Retry-After':'10'},None)
  else:
   with pytest.raises(rpc.Unavailable,match='RESULT_UNCONFIRMED'):
    with b.request(URL):pass
 assert json.loads(b.failed.read_text())['transient'] is True

@pytest.mark.parametrize('code',[5,32,33])
def test_transient97_bounded_fs_retry_recovers_supported_codes(code):
 calls=[]
 def operation():
  calls.append(1)
  if len(calls)<3:raise winerr(code)
  return 'done'
 assert rpc.retry_fs(operation)=='done' and len(calls)==3

def test_transient97_retry_is_bounded_and_not_used_for_unrelated_io(monkeypatch):
 class Clock:
  now=0
  def monotonic(self):return self.now
  def sleep(self,delta):self.now+=delta
 clock=Clock();monkeypatch.setattr(rpc,'time',clock);calls=[]
 def denied():calls.append(1);raise winerr()
 with pytest.raises(PermissionError):rpc.retry_fs(denied)
 assert .1<clock.now<=.2 and 2<len(calls)<12
 calls.clear()
 def disk():calls.append(1);raise OSError(28,'disk full')
 with pytest.raises(OSError):rpc.retry_fs(disk)
 assert len(calls)==1

def test_transient97_published_429_recovers_only_after_receipt_and_keeps_marker(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);durable_failure(b,monkeypatch,'429');before=old_marker(b)
 with pytest.raises(rpc.Deferred) as e:
  with b.request(URL):pytest.fail('429 bypassed')
 assert e.value.rpc_budget_reason=='COOLDOWN'
 assert b.failed.read_bytes()==before and not b.blocked()
 row=b.snapshot()['endpoints'][0];assert row['server_429']==1 and row['started']==1 and row['cooldown_remaining_s']>8
 receipt=json.loads((tmp_path/'failure_recovery_v97.json').read_text());assert receipt['original_failure_v97']['failure_chain'][0]['winerror']==5
 wait_for(lambda:not b._pending());assert b.recover() and b.snapshot()['endpoints'][0]['server_429']==1;b.close()

def test_transient97_replace_failure_keeps_fsynced_tmp_and_recovers_429(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);real=rpc.os.replace
 def denied(a,z):
  if Path(a).suffix=='.tmp' and Path(a).parent.name=='outcomes':raise winerr()
  return real(a,z)
 with monkeypatch.context() as m:
  m.setattr(rpc.os,'replace',denied)
  with pytest.raises(urllib.error.HTTPError):
   with b.request(URL):raise urllib.error.HTTPError(URL,429,'PRIVATE',{'Retry-After':'10'},None)
 p=next((tmp_path/'outcomes').glob('*.tmp'));event=json.loads(p.read_text());assert event['kind']=='429'
 before=old_marker(b);old=time.time()-11;os.utime(p,(old,old))
 with pytest.raises(rpc.Deferred) as e:
  with b.request(URL):pytest.fail('429 lost with failed publication')
 assert e.value.rpc_budget_reason=='COOLDOWN' and b.failed.read_bytes()==before
 assert b.snapshot()['endpoints'][0]['server_429']==1 and not b.blocked();b.close()

@pytest.mark.parametrize('body',['{}','{"kind":"429"}'])
def test_transient97_unknown_aged_tmp_never_discarded(tmp_path,body):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);d=tmp_path/'outcomes';d.mkdir();p=d/'dead.tmp';p.write_text(body)
 old=time.time()-60;os.utime(p,(old,old))
 with pytest.raises(rpc.Deferred):
  with b.request(URL):pytest.fail('unknown outcome discarded')
 assert p.read_text()==body and not b.failed.exists();b.close()

def test_transient97_live_owner_prevents_aged_tmp_promotion(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10);row=json.loads(p.read_text());row['publisher_slot_v97']=0
 p=p.with_suffix('.tmp');p.write_text(json.dumps(row));next((tmp_path/'outcomes').glob('*.json')).unlink()
 lock=tmp_path/'publisher_v97_0.lock';fd=rpc.lock_slot(lock);old=time.time()-60;os.utime(p,(old,old))
 try:
  with pytest.raises(rpc.Deferred):
   with b.request(URL):pytest.fail('active publisher overridden')
  assert p.exists() and sum(r['started'] for r in b.snapshot()['endpoints'])==0
 finally:rpc.unlock_slot(fd)
 with pytest.raises(rpc.Deferred) as e:
  with b.request(URL):pytest.fail('429 bypassed')
 assert e.value.rpc_budget_reason=='COOLDOWN' and b.snapshot()['endpoints'][0]['server_429']==1;b.close()

def test_transient97_boolean_without_identity_never_recovers_or_erases_marker(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000)
 with b.request(URL):pass
 b.block(winerr(),durable=True);before=old_marker(b)
 assert not b.recover()
 with pytest.raises(rpc.Unavailable,match='BLOCKED'):
  with b.request(URL):pytest.fail('boolean trusted without outcome proof')
 assert b.failed.read_bytes()==before and not (tmp_path/'failure_recovery_v97.json').exists();b.close()

def test_transient97_new_fatal_marker_during_recovery_is_never_cleared(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000);durable_failure(b,monkeypatch);old_marker(b);real=b._drain_locked
 def new_failure(*a,**kw):
  paths=real(*a,**kw);b.block(rpc.Unavailable('RPC_OUTCOME_INVALID'),stage='NEW_FATAL');return paths
 with monkeypatch.context() as m:
  m.setattr(b,'_drain_locked',new_failure);assert not b.recover()
 marker=json.loads(b.failed.read_text());assert marker['stage']=='NEW_FATAL' and not marker['transient'] and b.blocked()
 assert not (tmp_path/'failure_recovery_v97.json').exists();b.close()

def test_transient97_recovery_does_not_bypass_dispatch_spacing(tmp_path,monkeypatch):
 from test_rpc_outcomes_v892 import RateClock
 clock=RateClock();monkeypatch.setattr(rpc,'time',clock)
 b=rpc.Budget(tmp_path,rate=1,wait=.01);durable_failure(b,monkeypatch);b.RECOVER_AFTER_SECONDS=0
 with pytest.raises(rpc.Deferred) as e:
  with b.request(URL):pytest.fail('rate bypass during recovery')
 assert e.value.rpc_budget_reason=='RATE' and b.snapshot()['endpoints'][0]['started']==1;b.close()

def test_transient97_replaced_database_cannot_confirm_recovery(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000);durable_failure(b,monkeypatch)
 d=json.loads(b.failed.read_text());d['ts']=time.time()-3;d['recovery_evidence_v97']['database_identity']=[-1,-1];b.failed.write_text(json.dumps(d));before=b.failed.read_bytes()
 assert not b.recover() and b.failed.read_bytes()==before and b.blocked();b.close()

def test_transient97_six_threads_remain_bounded_and_no_permanent_failures(tmp_path):
 errors=[];budgets=[]
 def worker():
  b=rpc.Budget(tmp_path,rate=1000,slots=4,wait=.1);budgets.append(b)
  try:
   for _ in range(40):
    try:
     with b.request(URL):pass
    except rpc.Deferred:pass
  except Exception as exc:errors.append(dict(worker=threading.current_thread().name,failure_chain=rpc.failure_details(exc)))
  finally:b.close()
 threads=[threading.Thread(target=worker) for _ in range(6)]
 for t in threads:t.start()
 end=time.monotonic()+15
 for t in threads:t.join(max(0,end-time.monotonic()))
 alive=sum(t.is_alive() for t in threads)
 collectors=[b._gc_thread for b in budgets]
 if not alive:
  for collector in collectors:
   if collector is not None:collector.join(max(0,end-time.monotonic()))
 cleanup_alive=sum(collector is not None and collector.is_alive() for collector in collectors)
 if not alive and not cleanup_alive and not errors:
  for b in budgets:
   try:
    with b.control_lock():b._drain_locked()
   except Exception as exc:errors.append(dict(worker='final_drain',failure_chain=rpc.failure_details(exc)))
   finally:b.close()
 from rpc_concurrency_evidence_v972 import capture
 evidence=capture(tmp_path,errors,alive,cleanup_alive)
 assert evidence['passed'],json.dumps(evidence,sort_keys=True)
 assert len(list(tmp_path.glob('publisher_v97_*.lock')))<=8

def test_transient97_complete_legacy_tmp_without_owner_is_preserved(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10);raw=p.read_bytes();target=p.with_suffix('.tmp');p.rename(target);old=time.time()-60;os.utime(target,(old,old))
 with pytest.raises(rpc.Deferred):
  with b.request(URL):pytest.fail('legacy publisher ownership guessed')
 assert target.read_bytes()==raw and not b.failed.exists();b.close()

def test_transient97_dead_process_releases_owner_but_429_is_recovered(tmp_path):
 import subprocess,sys,queue
 script="""import sys,json,time,os,uuid,pathlib,rpc_budget as r
root=pathlib.Path(sys.argv[1]);root.mkdir(exist_ok=True)
fd=r.lock_slot(root/'publisher_v97_0.lock');assert fd is not None
folder=root/'outcomes';folder.mkdir();identifier=uuid.uuid4().hex
p=folder/(identifier+'.tmp')
row=dict(schema='RPC_OUTCOME_V892',id=identifier,key=r.endpoint_key('https://rpc.invalid'),kind='429',retry_after_s=10.,wait_ms=0.,observed_mono=time.monotonic(),observed_wall=time.time(),publisher_slot_v97=0)
with p.open('x') as f:json.dump(row,f);f.flush();os.fsync(f.fileno())
old=time.time()-11;os.utime(p,(old,old));print('READY',flush=True)
sys.stdin.readline()
"""
 proc=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-u','-c',script,str(tmp_path)],cwd=Path(rpc.__file__).parent,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 q=queue.Queue();threading.Thread(target=lambda:q.put(proc.stdout.readline()),daemon=True).start()
 try:
  assert q.get(timeout=5).strip()=='READY'
  b=rpc.Budget(tmp_path,rate=1000,wait=.01)
  with pytest.raises(rpc.Deferred):
   with b.request(URL):pytest.fail('live publisher bypassed')
  proc.kill();proc.wait(timeout=5)
  with pytest.raises(rpc.Deferred) as e:
   with b.request(URL):pytest.fail('dead publisher lost 429')
  assert e.value.rpc_budget_reason=='COOLDOWN' and b.snapshot()['endpoints'][0]['server_429']==1;b.close()
 finally:
  if proc.poll() is None:proc.kill()
  proc.wait(timeout=5)

def test_transient97_recovery_proof_and_original_error_are_exported(tmp_path,monkeypatch):
 import rpc_evidence_v892,zipfile
 b=rpc.Budget(tmp_path/'control',rate=1000);durable_failure(b,monkeypatch);old_marker(b)
 assert b.recover();wait_for(lambda:not b._pending())
 before={n:(b.root/n).read_bytes() for n in ['control_failed.json','failure_recovery_v97.json']}
 target=tmp_path/'evidence.zip';r=rpc_evidence_v892.export(b.root,target);assert r['status']=='CAPTURED'
 with zipfile.ZipFile(target) as z:
  assert z.read('raw/failure_recovery_v97.json')==before['failure_recovery_v97.json']
  assert z.read('raw/control_failed.json')==before['control_failed.json']
  assert any('publisher_v97_' in n for n in z.namelist())
 for n,raw in before.items():assert (b.root/n).read_bytes()==raw
 b.close()

def test_transient97_empty_existing_database_is_not_reinitialized_as_recovery_proof(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000);durable_failure(b,monkeypatch);before=old_marker(b);b.close()
 identity=(b.path.stat().st_dev,b.path.stat().st_ino)
 b.path.write_bytes(b'')
 assert (b.path.stat().st_dev,b.path.stat().st_ino)==identity
 candidate=rpc.Budget(tmp_path,rate=1000)
 assert not candidate.recover() and candidate.blocked() and candidate.failed.read_bytes()==before
 assert b.path.stat().st_size==0 and not (tmp_path/'failure_recovery_v97.json').exists()
 candidate.close()
