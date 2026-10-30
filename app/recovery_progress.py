"""Offline pytest progress; retains the last observed phase if JUnit is unfinished."""
import json,os,time
import pytest
from pathlib import Path

def record(event,**fields):
    path=os.environ.get('RECOVERY_PROGRESS_PATH')
    if path:
        with Path(path).open('a',encoding='utf-8') as out:
            out.write(json.dumps(dict(event=event,wall_ts=time.time(),monotonic_ts=time.monotonic(),pid=os.getpid(),**fields))+'\n')
            out.flush()

def pytest_sessionstart(session):record('SESSION_START')
def pytest_runtest_logstart(nodeid,location):record('TEST_START',nodeid=nodeid)
def pytest_runtest_logreport(report):record('TEST_PHASE_END',nodeid=report.nodeid,phase=report.when,outcome=report.outcome,duration_s=report.duration)
def pytest_sessionfinish(session,exitstatus):record('SESSION_FINISH',exitstatus=int(exitstatus))

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):record('TEST_PHASE_START',nodeid=item.nodeid,phase='setup')
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):record('TEST_PHASE_START',nodeid=item.nodeid,phase='call')
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item,nextitem):record('TEST_PHASE_START',nodeid=item.nodeid,phase='teardown')
