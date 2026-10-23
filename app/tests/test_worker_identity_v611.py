import json,subprocess,sys,time,os
from pathlib import Path
from types import SimpleNamespace
import pytest
import branch_transport as bt,runtime_control as rt

def test_recorded_windows_false_alarm_is_reproduced_and_corrected():
    data=json.loads((Path(__file__).parent/'fixtures/windows_watchdog_181001.json').read_text())
    old=rt.ChildWatchdog(data['session'],data['launcher_pid'])
    corrected=rt.ChildWatchdog(data['session'],data['trigger']['snapshot']['pid'])
    a,b=data['trigger'],data['repeat']
    assert not old.observe(a['snapshot'],a['observed_ts'],0)
    assert old.observe(b['snapshot'],b['observed_ts'],16)
    assert old.confirmed['repeat']['reason']=='PID_MISMATCH'
    assert not corrected.observe(a['snapshot'],a['observed_ts'],0)
    assert not corrected.observe(b['snapshot'],b['observed_ts'],16)
    assert corrected.snapshot()['state']=='HEALTHY'

def test_actual_launcher_and_worker_can_have_different_pids(tmp_path,monkeypatch):
    ready=tmp_path/'ready.json';stop=tmp_path/'stop'
    worker="import os,json,sys,time;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(json.dumps(dict(session='s',pid=os.getpid(),parent_pid=os.getppid())));stop=Path(sys.argv[2]);exec('while not stop.exists():time.sleep(.01)')"
    wrapper="import subprocess,sys;raise SystemExit(subprocess.call([sys.executable,'-c',sys.argv[1],*sys.argv[2:]]))"
    child=subprocess.Popen([sys.executable,'-c',wrapper,worker,str(ready),str(stop)])
    try:
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
        report=json.loads(ready.read_text());assert report['pid']!=child.pid
        assert report['parent_pid']==child.pid
        # This sandbox virtualizes /proc parents. Validate real parent via IPC,
        # then provide the same ancestry through the psutil adapter.
        import psutil
        monkeypatch.setattr(psutil,'Process',lambda pid:SimpleNamespace(parents=lambda:[SimpleNamespace(pid=report['parent_pid'])] if pid==report['pid'] else []))
        pid=bt.bind_worker(child,report,'s');assert pid==report['pid']
        w=rt.ChildWatchdog('s',pid)
        assert not w.observe(dict(report,ts=100,state='RUNNING',loop_pulse_age_s=.01),100,0)
        assert w.snapshot()['state']=='HEALTHY'
        with pytest.raises(RuntimeError,match='NOT_DESCENDANT'):
            bt.bind_worker(child,dict(session='s',pid=os.getpid(),parent_pid=os.getppid()),'s')
    finally:
        stop.touch();child.wait(timeout=5)

@pytest.mark.parametrize('pid',[None,0,-1,True,'123'])
def test_ready_invalid_pid_is_rejected(pid):
    with pytest.raises(RuntimeError,match='INVALID_PID'):
        bt.bind_worker(SimpleNamespace(pid=7),dict(session='s',pid=pid),'s')

def test_ready_foreign_session_rejected_even_when_pid_matches():
    with pytest.raises(RuntimeError,match='SESSION_MISMATCH'):
        bt.bind_worker(SimpleNamespace(pid=7),dict(session='old',pid=7),'s')

def test_bound_worker_pid_change_is_still_a_confirmed_fault():
    w=rt.ChildWatchdog('s',7);s=dict(session='s',pid=8,ts=100,state='RUNNING',loop_pulse_age_s=.01)
    assert not w.observe(s,100,0)
    assert w.observe(s,116,16) and w.confirmed['repeat']['reason']=='PID_MISMATCH'
