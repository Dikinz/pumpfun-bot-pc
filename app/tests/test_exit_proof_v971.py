"""Negative controls for the independent pre-cancel process handle observer."""
import pytest
from native_exit_proof_v971 import ExitProbe

class API:
 def __init__(self,birth=1000.):
  self.process=dict(birth=birth,status=258,code=0);self.opened=[];self.closed=[]
 def open(self,pid):self.opened.append(pid);return self.process
 def birth(self,h):return h['birth']
 def wait(self,h):return h['status']
 def exit_code(self,h):return h['code']
 def close(self,h):self.closed.append(h)

def test_exitproof971_terminated_process_object_does_not_need_pid_disappearance(monkeypatch):
 import psutil
 api=API();probe=ExitProbe(7,1000.,api)
 # Model the observed Windows discrepancy: PID/object stays visible after exit.
 monkeypatch.setattr(psutil,'Process',lambda pid:pytest.fail('PID liveness lookup used as exit authority'))
 api.process.update(status=0,code=1)
 proof=probe.exited();assert proof['wait_status']==0 and proof['pid_reopened_after_cancel'] is False
 assert proof['handle_retained_before_cancel'] and api.opened==[7]
 probe.close();assert len(api.closed)==1

@pytest.mark.parametrize('exit_code',[0,259])
def test_exitproof971_unsignaled_process_fails_even_with_plausible_exit_code(exit_code):
 api=API();probe=ExitProbe(7,1000.,api);api.process.update(code=exit_code)
 with pytest.raises(AssertionError,match='NOT_SIGNALED'):probe.exited()
 probe.close()

def test_exitproof971_signaled_handle_with_exit_code_259_is_still_exit():
 api=API();probe=ExitProbe(7,1000.,api);api.process.update(status=0,code=259)
 assert probe.exited()['exit_code']==259
 probe.close()

def test_exitproof971_identity_mismatch_closes_observer_and_never_passes():
 api=API(birth=2000.)
 with pytest.raises(AssertionError,match='IDENTITY_MISMATCH'):ExitProbe(7,1000.,api)
 assert api.opened==[7] and len(api.closed)==1

@pytest.mark.parametrize('wait_status',[0xffffffff,12345])
def test_exitproof971_unknown_wait_is_never_completion(wait_status):
 api=API();probe=ExitProbe(7,1000.,api);api.process.update(status=wait_status)
 with pytest.raises(AssertionError,match='NOT_SIGNALED'):probe.exited()
 probe.close()

def test_exitproof971_pid_reuse_does_not_change_the_retained_process():
 api=API();old=api.process;probe=ExitProbe(7,1000.,api)
 api.process=dict(birth=2000.,status=258,code=259) # Same PID now refers to another live object.
 old.update(status=0,code=1)
 assert probe.exited()['created_ts']==1000. and api.opened==[7]
 probe.close();assert api.closed==[old] and api.process['status']==258


def test_exitproof971_fixture_must_be_alive_before_cancellation():
 api=API();api.process['status']=0
 with pytest.raises(AssertionError,match='NOT_LIVE_BEFORE_CANCEL'):ExitProbe(7,1000.,api)
 assert len(api.closed)==1

def test_exitproof971_observer_close_error_is_not_ignored():
 api=API();probe=ExitProbe(7,1000.,api)
 def denied(h):raise PermissionError('controlled close failure')
 api.close=denied
 with pytest.raises(PermissionError):probe.close()
 assert probe.handle is not None


def test_exitproof971_pending_handle_can_be_observed_until_signal_without_reopen():
 api=API();probe=ExitProbe(7,1000.,api)
 assert probe.signaled() is False and not api.closed
 api.process.update(status=0)
 assert probe.signaled() is True and probe.exited()['wait_status']==0
 assert api.opened==[7];probe.close()
