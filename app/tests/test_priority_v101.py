import json,sqlite3,time,urllib.error,types
from pathlib import Path
import pytest
import rpc_budget as rpc,rpc_priority as priority,rpc_priority_recovery_v101 as recovery
URL='https://rpc.invalid'

def deny(code=5):
 e=PermissionError(13,'SECRET');e.winerror=code;return e

@pytest.mark.parametrize('code',[5,32,33])
def test_priority101_read_denied_defers_and_recovers_without_http(tmp_path,monkeypatch,code):
 b=rpc.Budget(tmp_path,rate=1000,wait=.015);read=Path.read_text
 with priority.exit_check({'priority_exit':True}):
  # Lease is in config root; use that real published lease for this budget.
  import config
  b=rpc.Budget(config.RPC_CONTROL_DIR,rate=1000,wait=.015)
  def fail(p,*a,**kw):
   if p.parent.name=='exit_priority_v94' and p.suffix=='.json':raise deny(code)
   return read(p,*a,**kw)
  with monkeypatch.context() as m:
   m.setattr(Path,'read_text',fail)
   with pytest.raises(rpc.Deferred) as caught:
    with b.request(URL,priority=True):pytest.fail('HTTP admitted while priority unknown')
   assert caught.value.rpc_budget_reason=='PRIORITY_READ_BUSY' and b.snapshot()['inflight']==0
   assert not b.failed.exists() and b.snapshot()['status']=='PRIORITY_READ_BUSY'
   note=json.loads(read(b.root/'priority_read_v101.json'))
   assert note['failure_chain'][-1]['winerror']==code and 'SECRET' not in json.dumps(note)
  with b.request(URL,priority=True):pass
  assert b.snapshot()['endpoints'][0]['started']==1
 b.close()


def test_priority101_directory_denied_never_treated_as_absent(tmp_path,monkeypatch):
 b=rpc.Budget(tmp_path,wait=.01);d=tmp_path/'exit_priority_v94';d.mkdir();original=priority.os.scandir
 def fail(p):
  if Path(p)==d:raise deny()
  return original(p)
 with monkeypatch.context() as m:
  m.setattr(priority.os,'scandir',fail)
  with pytest.raises(rpc.Deferred) as caught:
   with b.request(URL):pytest.fail('unreadable directory admitted')
  assert caught.value.rpc_budget_reason=='PRIORITY_READ_BUSY' and not b.failed.exists()
 with b.request(URL):pass
 b.close()


def incident(tmp_path):
 known=json.loads(recovery.AUDIT.read_text());b=rpc.Budget(tmp_path)
 tmp_path.mkdir(exist_ok=True)
 with sqlite3.connect(b.path) as c:
  c.executescript(rpc.SCHEMA)
  for table,rows in known['control_state'].items():
   for row in rows:c.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?'*len(row))+')',tuple(row.values()))
 b.failed.write_bytes(bytes.fromhex(known['marker_hex']))
 for name,raw in known['rate_bytes_hex'].items():(tmp_path/name).write_bytes(bytes.fromhex(raw))
 return b,known


def test_priority101_exact_archived_latch_recovers_on_restart(tmp_path,monkeypatch):
 b,known=incident(tmp_path);marker=b.failed.read_bytes();db=b.path.read_bytes()
 with b.control_lock():assert not b.recover() # Cannot recover past a competing owner.
 assert b.blocked();assert b.recover();assert not b.blocked()
 assert b.failed.read_bytes()==marker and b.path.read_bytes()==db
 b.close();new=rpc.Budget(tmp_path)
 with rpc.Permit(new,known['control_state']['endpoints'][0]['key']):pass
 assert new.snapshot()['endpoints'][0]['started']==735 and new.failed.read_bytes()==marker
 new.close()

@pytest.mark.parametrize('change',['marker','counter','limits','rate','outcome','priority','held_slot','corrupt_db'])
def test_priority101_modified_or_incomplete_incident_never_unlatched(tmp_path,change):
 b,known=incident(tmp_path);fd=None
 if change=='marker':b.failed.write_bytes(b.failed.read_bytes()+b' ')
 elif change=='counter':
  with sqlite3.connect(b.path) as c:c.execute('UPDATE endpoints SET started=started+1')
 elif change=='limits':b.rate=7.
 elif change=='rate':(tmp_path/next(iter(known['rate_bytes_hex']))).write_bytes(b'broken')
 elif change=='outcome':(tmp_path/'outcomes').mkdir();(tmp_path/'outcomes'/'unconfirmed.json').write_text('{}')
 elif change=='priority':(tmp_path/'exit_priority_v94').mkdir();(tmp_path/'exit_priority_v94'/'invalid.json').write_text('{}')
 elif change=='held_slot':fd=rpc.lock_slot(tmp_path/'slot_0.lock')
 elif change=='corrupt_db':b.path.write_bytes(b'broken')
 original=b.failed.read_bytes()
 try:assert not b.recover();assert b.blocked() and b.failed.read_bytes()==original
 finally:rpc.unlock_slot(fd);b.close()


def test_priority101_unknown_priority_corruption_stays_fatal(tmp_path):
 b=rpc.Budget(tmp_path);d=tmp_path/'exit_priority_v94';d.mkdir();(d/'bad.json').write_text('{}')
 with pytest.raises(rpc.Unavailable):
  with b.request(URL):pytest.fail('corrupt state admitted')
 assert b.failed.exists() and not b.recover()
 b.close()


def test_priority101_retry_after_capacity_rate_still_enforced(tmp_path):
 b=rpc.Budget(tmp_path,rate=6,slots=1,wait=.02)
 with b.request(URL,priority=True):
  with pytest.raises(rpc.Deferred) as caught:
   with b.request(URL,priority=True):pytest.fail('slot bypass')
  assert caught.value.rpc_budget_reason=='CAPACITY'
 with pytest.raises(rpc.Deferred):
  with b.request(URL):pytest.fail('rate bypass')
 time.sleep(.18)
 with pytest.raises(urllib.error.HTTPError):
  with b.request(URL):raise urllib.error.HTTPError(URL,429,'SECRET',{'Retry-After':'10'},None)
 time.sleep(.18)
 with pytest.raises(rpc.Deferred) as caught:
  with b.request(URL,priority=True):pytest.fail('429 bypass')
 assert caught.value.rpc_budget_reason=='COOLDOWN' and b.snapshot()['endpoints'][0]['server_429']==1
 b.close()

from test_rpc_transport_v99 import server

def test_priority101_real_http_injection_resumes_without_restart(server,monkeypatch):
 import config,local_simulation as lab
 monkeypatch.setattr(config,'RPC_ADMISSION_WAIT_SECONDS',.03)
 with priority.exit_check({'priority_exit':True}):
  lab.read_accounts(['a']);before=server.requests;read=Path.read_text
  def fail(p,*a,**kw):
   if p.parent.name=='exit_priority_v94' and p.suffix=='.json':raise deny()
   return read(p,*a,**kw)
  with monkeypatch.context() as m:
   m.setattr(Path,'read_text',fail)
   for _ in range(3):
    with pytest.raises(rpc.Deferred):lab.read_accounts(['a'])
   assert server.requests==before and not rpc.budget().failed.exists()
  lab.read_accounts(['a'])
  assert server.requests==before+1 and rpc.snapshot()['inflight']==0

@pytest.mark.parametrize('operation',['stat','read'])
def test_priority101_unreadable_failure_marker_never_looks_absent(tmp_path,monkeypatch,operation):
 b=rpc.Budget(tmp_path);b.block(ValueError('unknown'));method='stat' if operation=='stat' else 'read_bytes';original=getattr(Path,method)
 def fail(path,*a,**kw):
  if path==b.failed:raise deny()
  return original(path,*a,**kw)
 with monkeypatch.context() as m:
  m.setattr(Path,method,fail)
  assert b.blocked()
  with pytest.raises(rpc.Unavailable):
   with b.request(URL):pytest.fail('unreadable marker bypassed')
 b.close()
