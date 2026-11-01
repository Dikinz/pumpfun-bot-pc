"""Unknown RPC observations never become zero or proof of resumed admission."""
import json,sys,types
from unittest.mock import patch
import pytest
import rpc_budget as rpc,paper_fault_test_v101 as paper

@pytest.mark.parametrize('case',['midpoint_unknown','final_unknown','unknown_then_valid'])
def test_review1011_rpc_resume_requires_known_counter_pair(tmp_path,case):
    clock=types.SimpleNamespace(now=100.,monotonic=lambda:clock.now,time=lambda:1000.)
    cfg=types.SimpleNamespace(EXECUTION_MODE='paper',VERSION='v0.7.101.1',RPC_CONTROL_DIR=tmp_path)
    finished=False
    def snapshot():
        unknown=(case=='midpoint_unknown' and not finished and clock.now>=3720.) or (case=='final_unknown' and finished) or (case=='unknown_then_valid' and clock.now==3720.)
        return dict(status='UNAVAILABLE',endpoints=[]) if unknown else dict(status='READY',endpoints=[dict(started=735 if finished and case=='unknown_then_valid' else 734)])
    def launch(stop_condition):
        nonlocal finished
        clock.now=3720.;assert not stop_condition()
        if case=='unknown_then_valid':clock.now=4000.;assert not stop_condition()
        clock.now=7300.;assert stop_condition()
        (tmp_path/'priority_fault_observed_v101.json').write_text(json.dumps(dict(fault_start_mono_v101=3700.,failure_chain=[dict(injected_v101=True)])))
        finished=True
        return 0
    with patch.dict(sys.modules,config=cfg,launcher=types.SimpleNamespace(start=launch)),patch.object(paper,'time',clock),patch.object(paper,'OUTPUT',tmp_path/'report.json'),patch.object(rpc,'budget',lambda:types.SimpleNamespace(blocked=lambda:False)),patch.object(rpc,'snapshot',snapshot):
        code=paper.main([])
    report=json.loads((tmp_path/'report.json').read_text())
    expected=case=='unknown_then_valid'
    assert report['passed'] is expected and report['rpc_resumed_after_window'] is expected
    assert code==(0 if expected else 2)
    if case=='midpoint_unknown':assert report['started_at_fault_end'] is None and report['started_before']==report['started_after']==734
    if case=='final_unknown':assert report['started_after'] is None
    if expected:assert report['started_at_fault_end']==734 and report['started_after']==735
