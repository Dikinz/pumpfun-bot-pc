import json,time,types,os
from pathlib import Path
import pytest
import rpc_fault_v101 as fault,rpc_budget as rpc,paper_fault_test_v101 as paper,rpc_status_v101 as status,config
from test_priority_v101 import incident


def test_fault101_default_off_and_explicit_bounded_window(monkeypatch):
 monkeypatch.delenv(fault.START,raising=False);fault.maybe_raise()
 monkeypatch.setenv(fault.START,'100');monkeypatch.setenv(fault.SPAN,'10')
 monkeypatch.setattr(fault,'time',types.SimpleNamespace(monotonic=lambda:105))
 with pytest.raises(PermissionError) as e:fault.maybe_raise()
 assert e.value.controlled_rpc_priority_fault_v101
 monkeypatch.setattr(fault,'time',types.SimpleNamespace(monotonic=lambda:110));fault.maybe_raise()


def test_fault101_resume_status_proves_734_to_735_without_marker_delete(tmp_path,monkeypatch):
 import exit_quotes,network_check_v88
 b,known=incident(tmp_path);original=b.failed.read_bytes();monkeypatch.setattr(rpc,'budget',lambda:b)
 def probe(*a,**kw):
  with rpc.Permit(b,known['control_state']['endpoints'][0]['key']):return {'result':network_check_v88.MAINNET_GENESIS}
 monkeypatch.setattr(exit_quotes,'request_json',probe)
 report=status.run(recover=True,probe=True)
 assert report['passed'] and report['recovery_applied'] and report['started_before']==734 and report['started_after']==735
 assert b.failed.read_bytes()==original;b.close()


def test_fault101_unknown_latch_never_probed(tmp_path,monkeypatch):
 import exit_quotes
 b=rpc.Budget(tmp_path);b.block(ValueError('unknown'));monkeypatch.setattr(rpc,'budget',lambda:b)
 monkeypatch.setattr(exit_quotes,'request_json',lambda *a,**kw:pytest.fail('blocked RPC sent'))
 report=status.run(recover=True,probe=True)
 assert not report['passed'] and report['probe']['status']=='BLOCKED';b.close()


@pytest.mark.parametrize('finish',[False,True])
def test_fault101_paper_controller_duration_and_early_stop(tmp_path,monkeypatch,finish):
 import launcher
 clock=types.SimpleNamespace(now=100.,monotonic=lambda:clock.now,time=lambda:1000.)
 monkeypatch.setattr(paper,'time',clock);monkeypatch.setattr(fault,'time',clock);monkeypatch.setattr(paper,'OUTPUT',tmp_path/'report.json')
 root=tmp_path/'rpc';monkeypatch.setattr(config,'RPC_CONTROL_DIR',root);monkeypatch.setattr(config,'RPC_ADMISSION_WAIT_SECONDS',.01)
 def launch(stop_condition):
  b=rpc.budget()
  with b.request('https://rpc.invalid'):pass
  clock.now=3701.
  with pytest.raises(rpc.Deferred) as e:
   with b.request('https://rpc.invalid'):pytest.fail('fault permitted')
  assert e.value.rpc_budget_reason=='PRIORITY_READ_BUSY'
  clock.now=3720.;assert stop_condition() is False
  with b.request('https://rpc.invalid'):pass
  clock.now=7300. if finish else 4000.
  assert stop_condition() is finish
  return 0
 monkeypatch.setattr(launcher,'start',launch)
 code=paper.main([]);report=json.loads(paper.OUTPUT.read_text())
 assert report['controlled_fault_observed'] and report['rpc_resumed_after_window'] and not report['rpc_blocked_after']
 assert report['passed'] is finish and code==(0 if finish else 2)
 assert fault.START not in os.environ

def test_fault101_blocked_start_replaces_previous_test_pass(tmp_path,monkeypatch):
 import launcher
 b=rpc.Budget(tmp_path/'rpc');b.block(ValueError('unknown'));original=b.failed.read_bytes()
 monkeypatch.setattr(rpc,'budget',lambda:b);monkeypatch.setattr(paper,'OUTPUT',tmp_path/'report.json')
 paper.OUTPUT.write_text('{"passed":true}')
 monkeypatch.setattr(launcher,'start',lambda **kw:pytest.fail('blocked campaign launched'))
 with pytest.raises(RuntimeError,match='RPC_CONTROL_BLOCKED'):paper.main([])
 report=json.loads(paper.OUTPUT.read_text());assert report['passed'] is False and report['status']=='START_BLOCKED'
 assert b.failed.read_bytes()==original;b.close()

@pytest.mark.parametrize('args',[['--duration','nan'],['--duration','inf'],['--fault-at','nan']])
def test_fault101_nonfinite_timer_is_rejected(args):
 with pytest.raises(SystemExit) as exc:paper.main(args)
 assert exc.value.code==2
