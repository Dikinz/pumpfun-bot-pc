import json,subprocess,sys,time,urllib.error
from pathlib import Path
import pytest
import rpc_budget as rpc,rpc_priority as priority,config
URL='https://rpc.invalid'

def test_rpc94_claim_defers_background_without_http(tmp_path):
 b=rpc.budget()
 with priority.exit_check({'priority_exit':{'order_id':'o'}}):
  assert priority.IS_EXIT.get() and priority.pending(b.root)==1
  with pytest.raises(rpc.Deferred) as exc:
   with b.request(URL):pytest.fail('background started')
  assert exc.value.rpc_budget_reason=='EXIT_PRIORITY'
  with rpc.guard(URL):pass
  snap=b.snapshot();assert snap['endpoints'][0]['started']==1 and snap['inflight']==0
 assert not priority.IS_EXIT.get() and priority.pending(b.root)==0
 with b.request(URL):pass
 assert b.snapshot()['endpoints'][0]['started']==2

def test_rpc94_keeps_six_per_second_spacing(tmp_path,monkeypatch):
 monkeypatch.setattr(config,'RPC_MAX_REQUESTS_PER_SECOND',6.);monkeypatch.setattr(config,'RPC_ADMISSION_WAIT_SECONDS',.75);starts=[]
 with priority.exit_check({'priority_exit':{'order_id':'o'}}):
  for _ in range(4):
   with rpc.guard(URL):starts.append(time.monotonic())
 assert all(b-a>=1/6-.003 for a,b in zip(starts,starts[1:]))
 assert rpc.snapshot()['endpoints'][0]['started']==4

def test_rpc94_keeps_server_retry_after(tmp_path):
 with priority.exit_check({'priority_exit':{'order_id':'o'}}):
  with pytest.raises(urllib.error.HTTPError):
   with rpc.guard(URL):raise urllib.error.HTTPError(URL,429,'limited',{'Retry-After':'10'},None)
  with pytest.raises(rpc.Deferred) as exc:
   with rpc.guard(URL):pytest.fail('cooldown bypassed')
  assert exc.value.rpc_budget_reason=='COOLDOWN'
  row=rpc.snapshot()['endpoints'][0];assert row['server_429']==1 and row['started']==1 and row['cooldown_remaining_s']>9

def test_rpc94_keeps_inflight_cap(tmp_path):
 b=rpc.Budget(tmp_path,rate=1000,slots=1,wait=.01)
 with b.request(URL,priority=True):
  with pytest.raises(rpc.Deferred) as exc:
   with b.request(URL,priority=True):pytest.fail('capacity bypassed')
  assert exc.value.rpc_budget_reason=='CAPACITY' and b.snapshot()['inflight']==1

def test_rpc94_abandoned_claim_expires(tmp_path):
 root=Path(config.RPC_CONTROL_DIR)
 with priority.exit_check({'priority_exit':{'order_id':'o'}}):
  path=next((root/'exit_priority_v94').glob('*.json'));row=json.loads(path.read_text());row['created_mono']-=11;row['created_wall']-=11;path.write_text(json.dumps(row))
  assert priority.pending(root)==0 and not path.exists()
 assert priority.pending(root)==0

def test_rpc94_original_error_survives(tmp_path):
 with pytest.raises(RuntimeError,match='ORIGINAL_MONITOR_CAUSE'):
  with priority.exit_check({'priority_exit':{'order_id':'o'}}):raise RuntimeError('ORIGINAL_MONITOR_CAUSE')
 assert not priority.IS_EXIT.get() and priority.pending(config.RPC_CONTROL_DIR)==0

def test_rpc94_claim_visible_to_another_process(tmp_path):
 script="import rpc_budget as r\ntry:\n with r.budget().request('https://rpc.invalid'):raise AssertionError('background admitted')\nexcept r.Deferred as e:\n assert e.rpc_budget_reason=='EXIT_PRIORITY'\n print('deferred')\n"
 with priority.exit_check({'priority_exit':{'order_id':'o'}}):
  r=subprocess.run([sys.executable,'-c',script],cwd=Path(rpc.__file__).parent,capture_output=True,text=True,timeout=8)
  assert r.returncode==0,r.stderr
  assert r.stdout.strip()=='deferred'

def test_rpc94_malformed_claim_fails_closed(tmp_path):
 for i,raw in enumerate(['{}','[]']):
  b=rpc.Budget(tmp_path/str(i));directory=b.root/'exit_priority_v94';directory.mkdir(parents=True);(directory/'bad.json').write_text(raw)
  with pytest.raises(rpc.Unavailable,match='RPC_EXIT_PRIORITY_UNCONFIRMED'):
   with b.request(URL):pytest.fail('invalid state bypassed')
  assert b.failed.exists()


def test_rpc94_release_error_does_not_replace_original_cause(tmp_path,monkeypatch):
 original=Path.unlink
 def denied(path,*args,**kwargs):
  if path.parent.name=='exit_priority_v94' and path.suffix=='.json':raise PermissionError('controlled release')
  return original(path,*args,**kwargs)
 with monkeypatch.context() as m:
  m.setattr(Path,'unlink',denied)
  with pytest.raises(RuntimeError,match='ORIGINAL_MONITOR_CAUSE') as caught:
   with priority.exit_check({'priority_exit':{'order_id':'o'}}):raise RuntimeError('ORIGINAL_MONITOR_CAUSE')
  assert any('PermissionError' in n for n in caught.value.__notes__)
  with priority.exit_check({'priority_exit':{'order_id':'o'}}):pass
 assert not priority.IS_EXIT.get()
