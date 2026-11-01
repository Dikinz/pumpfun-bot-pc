"""Early STOP must remain incomplete even when clean shutdown crosses duration."""
import json,sys,types
from unittest.mock import patch
import pytest
import rpc_budget as rpc, paper_fault_test_v101 as paper

@pytest.mark.parametrize('timer_stop',[False,True])
def test_review1011_shutdown_crossing_deadline_requires_timer_stop(tmp_path,timer_stop):
    clock=types.SimpleNamespace(now=100.,monotonic=lambda:clock.now,time=lambda:1000.)
    cfg=types.SimpleNamespace(EXECUTION_MODE='paper',VERSION='v0.7.101.1',RPC_CONTROL_DIR=tmp_path)
    count=[1]
    def launch(stop_condition):
        clock.now=3720.;assert not stop_condition()
        count[0]=2
        clock.now=7299.;assert not stop_condition()
        if timer_stop:
            clock.now=7300.;assert stop_condition()
        clock.now=7301.
        (tmp_path/'priority_fault_observed_v101.json').write_text(json.dumps(dict(fault_start_mono_v101=3700.,failure_chain=[dict(injected_v101=True)])))
        return 0
    with patch.dict(sys.modules,config=cfg,launcher=types.SimpleNamespace(start=launch)),patch.object(paper,'time',clock),patch.object(paper,'OUTPUT',tmp_path/'report.json'),patch.object(rpc,'budget',lambda:types.SimpleNamespace(blocked=lambda:False)),patch.object(rpc,'snapshot',lambda:dict(status='READY',endpoints=[dict(started=count[0])])):
        rc=paper.main([])
    report=json.loads((tmp_path/'report.json').read_text())
    assert report['completed'] is timer_stop
    assert report['passed'] is timer_stop
    assert rc==(0 if timer_stop else 2)
