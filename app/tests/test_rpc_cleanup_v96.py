import ctypes,io,json,os,sqlite3,threading,time,urllib.error,zipfile
from pathlib import Path
import pytest
import rpc_budget as rpc,rpc_evidence_v892
from test_rpc_admission_v95 import outcome,URL

def wait_for(predicate,seconds=4):
 end=time.monotonic()+seconds
 while not predicate():
  assert time.monotonic()<end,'cleanup did not converge'
  time.sleep(.01)

def deny(path,real,root):
 if path.parent==root/'outcomes':
  exc=PermissionError(13,'private path/key must not be retained');exc.winerror=5;raise exc
 return real(path)

def test_gc96_committed_unlink_denial_preserves_success_and_429(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);real=Path.unlink
 with monkeypatch.context() as m:
  m.setattr(Path,'unlink',lambda p,*a,**kw:deny(p,lambda p:real(p,*a,**kw),tmp_path))
  with b.request(URL):pass
  with pytest.raises(urllib.error.HTTPError):
   with b.request(URL):raise urllib.error.HTTPError(URL,429,'private',{'Retry-After':'10'},None)
  assert not b.failed.exists()
  s=b.snapshot();assert s['status']=='READY' and s['endpoints'][0]['server_429']==1
  assert s['cleanup_v96']['committed_files_pending']==2 and s['cleanup_v96']['unresolved_outcomes']==0
  with pytest.raises(rpc.Deferred) as e:
   with b.request(URL):pytest.fail('429 bypass')
  assert e.value.rpc_budget_reason=='COOLDOWN'
  note=json.loads((tmp_path/'cleanup_v96.json').read_text());assert note['receipt_confirmed'] and note['winerror']==5
  assert 'private' not in json.dumps(note)
 wait_for(lambda:not b._pending());assert b.snapshot()['endpoints'][0]['server_429']==1
 b.close()

def test_gc96_missing_receipt_delete_denial_stays_fatal(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000);p=outcome(b);real=Path.unlink
 with b.control_lock():
  with b.session():pass
 with monkeypatch.context() as m:
  m.setattr(Path,'unlink',lambda p,*a,**kw:deny(p,lambda p:real(p,*a,**kw),tmp_path))
  with pytest.raises(rpc.Unavailable,match='RECEIPT_UNCONFIRMED'):b._unlink_outcomes([p])
 assert p.exists();b.close()

def test_gc96_uncommitted_429_failure_never_downgraded_to_cleanup(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000)
 def fail(c):raise sqlite3.OperationalError('disk I/O error')
 with monkeypatch.context() as m:
  with b.request(URL):pass
  permit=b.request(URL);permit.__enter__()
  m.setattr(b,'commit',fail)
  error=urllib.error.HTTPError(URL,429,'private',{'Retry-After':'10'},None)
  permit.__exit__(type(error),error,None)
  assert error.rpc_control_persist_error=='OperationalError'
 assert b.failed.exists() and b._pending();assert b.snapshot()['status']=='BLOCKED';b.close()

def test_gc96_slow_cleanup_does_not_hold_admission_lock_or_capacity(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,slots=1,wait=.05);p=outcome(b);entered=threading.Event();release=threading.Event();real=Path.unlink
 with b.control_lock():b._drain_locked(unlink=False)
 def slow(path,*a,**kw):
  if path==p:entered.set();assert release.wait(4)
  return real(path,*a,**kw)
 with monkeypatch.context() as m:
  m.setattr(Path,'unlink',slow)
  b._queue_cleanup([p]);assert entered.wait(2)
  with b.control_lock():pass
  with b.request(URL):pass
  assert b.snapshot()['endpoints'][0]['started']==1
  release.set();wait_for(lambda:not p.exists())
 b.close()

def test_gc96_backlog_is_bounded_deferral_and_recovers_without_reset(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);b.CLEANUP_LIMIT=4;real=Path.unlink
 with monkeypatch.context() as m:
  m.setattr(Path,'unlink',lambda p,*a,**kw:deny(p,lambda p:real(p,*a,**kw),tmp_path))
  for _ in range(4):
   with b.request(URL):pass
  with pytest.raises(rpc.Deferred) as e:
   with b.request(URL):pytest.fail('unbounded retained files')
  assert e.value.rpc_budget_reason=='CLEANUP_BACKLOG' and not b.failed.exists()
  assert len(b._pending())==4 and len(b._gc_paths)<=4
 wait_for(lambda:not b._pending())
 with b.request(URL):pass
 assert b.snapshot()['endpoints'][0]['started']==5;b.close()

def test_gc96_pending_publication_remains_blocking_and_old_marker_preserved(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);d=tmp_path/'outcomes';d.mkdir();(d/'unknown.tmp').write_text('partial')
 with pytest.raises(rpc.Deferred):
  with b.request(URL):pytest.fail('partial publication ignored')
 b.block(PermissionError(13,'old incident'));before=b.failed.read_bytes()
 (d/'unknown.tmp').unlink()
 with pytest.raises(rpc.Unavailable,match='BLOCKED'):
  with b.request(URL):pytest.fail('old gate removed')
 assert b.failed.read_bytes()==before;b.close()

def test_gc96_native_delete_sharing_violation_or_injected_model(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b);handle=None
 with b.control_lock():b._drain_locked(unlink=False)
 if os.name=='nt':
  from ctypes import wintypes
  kernel=ctypes.WinDLL('kernel32',use_last_error=True)
  create=kernel.CreateFileW;create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];create.restype=wintypes.HANDLE
  close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
  handle=create(str(p),0x80000000,3,None,3,0x80,None)
  assert handle not in (None,ctypes.c_void_p(-1).value),ctypes.get_last_error()
 try:
  with monkeypatch.context() as m:
   if os.name!='nt':
    real=Path.unlink;m.setattr(Path,'unlink',lambda p,*a,**kw:deny(p,lambda p:real(p,*a,**kw),tmp_path))
   b._unlink_outcomes([p]);assert p.exists() and not b.failed.exists()
   assert b.snapshot()['cleanup_v96']['last_delete_failure']['winerror'] in ((5,32) if os.name=='nt' else (5,))
   with b.request(URL):pass
 finally:
  if handle is not None:assert close(handle)
 wait_for(lambda:not b._pending());b.close()

def test_gc96_cleanup_error_and_receipts_export_without_source_mutation(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path/'control',rate=1000);real=Path.unlink
 monkeypatch.setattr(b,'_queue_cleanup',lambda paths:None)
 with monkeypatch.context() as m:
  m.setattr(Path,'unlink',lambda p,*a,**kw:deny(p,lambda p:real(p,*a,**kw),b.root))
  with b.request(URL):pass
  # Stop retry mutations for a deterministic read-only export assertion.
  before={p.relative_to(b.root).as_posix():p.read_bytes() for p in b.root.rglob('*') if p.is_file()}
  report=rpc_evidence_v892.export(b.root,tmp_path/'export.zip')
  assert report['status']=='CAPTURED'
  with zipfile.ZipFile(tmp_path/'export.zip') as z:
   assert json.loads(z.read('raw/cleanup_v96.json'))['receipt_confirmed']
   assert any(n.startswith('raw/outcomes/') for n in z.namelist())
  for n,data in before.items():assert (b.root/n).read_bytes()==data
 b._unlink_outcomes(b._pending());b.close()


def test_gc96_more_than_old_32_committed_files_do_not_block_or_recount(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,rate=1000,wait=.01);real=Path.unlink
 with monkeypatch.context() as m:
  m.setattr(Path,'unlink',lambda p,*a,**kw:deny(p,lambda p:real(p,*a,**kw),tmp_path))
  for _ in range(40):
   with b.request(URL):pass
  snapshot=b.snapshot();assert snapshot['status']=='READY' and snapshot['cleanup_v96']['committed_files_pending']==40
  assert snapshot['endpoints'][0]['started']==40 and snapshot['endpoints'][0]['other_errors']==0 and not b.failed.exists()
 wait_for(lambda:not b._pending());b.close()


def test_gc96_uncommitted_file_never_deleted_even_if_unlink_would_succeed(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000);p=outcome(b,'429',10)
 with b.control_lock():
  with b.session():pass
 with pytest.raises(rpc.Unavailable,match='RECEIPT_UNCONFIRMED'):b._unlink_outcomes([p])
 assert p.exists()
 with b.control_lock():b._drain_locked()
 assert b.snapshot()['endpoints'][0]['server_429']==1 and not p.exists();b.close()


def test_gc96_committed_429_survives_recorder_process_exit(tmp_path):
 import subprocess,sys,queue
 script="""import pathlib,sys,urllib.error,rpc_budget as r
b=r.Budget(sys.argv[1],rate=1000,slots=2,wait=.01)
real=pathlib.Path.unlink
def deny(p,*a,**kw):
 if p.parent.name=='outcomes':raise PermissionError(13,'controlled')
 return real(p,*a,**kw)
pathlib.Path.unlink=deny
try:
 with b.request('https://rpc.invalid'):raise urllib.error.HTTPError('https://rpc.invalid',429,'controlled',{'Retry-After':'10'},None)
except urllib.error.HTTPError:pass
print('COMMITTED',flush=True)
sys.stdin.readline()
"""
 process=subprocess.Popen([sys.executable,'-u','-c',script,str(tmp_path)],cwd=Path(rpc.__file__).parent,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 ready=queue.Queue();threading.Thread(target=lambda:ready.put(process.stdout.readline()),daemon=True).start()
 try:
  assert ready.get(timeout=5).strip()=='COMMITTED'
  process.stdin.write('stop\n');process.stdin.flush();assert process.wait(timeout=5)==0
  b=rpc.Budget(tmp_path,rate=1000,slots=2,wait=.01)
  with pytest.raises(rpc.Deferred) as exc:
   with b.request(URL):pytest.fail('recorder exit lost cooldown')
  assert exc.value.rpc_budget_reason=='COOLDOWN'
  row=b.snapshot()['endpoints'][0];assert row['started']==1 and row['server_429']==1 and row['cooldown_remaining_s']>7
  wait_for(lambda:not b._pending());assert not b.failed.exists();b.close()
 finally:
  if process.poll() is None:process.kill()
  process.wait(timeout=5)


def test_gc96_new_cleanup_thread_cannot_follow_replaced_control_database(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000);p=outcome(b)
 with b.control_lock():b._drain_locked(unlink=False)
 b.close()
 # No open SQLite handle is needed to retain this Budget's authority identity.
 b._control_identity=(-1,-1)
 errors=[]
 def cleanup():
  try:b._unlink_outcomes([p])
  except Exception as exc:errors.append(exc)
  finally:b.close()
 t=threading.Thread(target=cleanup);t.start();t.join(3)
 assert not t.is_alive() and len(errors)==1 and str(errors[0])=='RPC_CONTROL_DB_REPLACED'
 assert p.exists()
