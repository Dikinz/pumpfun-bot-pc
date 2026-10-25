import asyncio,json,os,sqlite3,zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
import child_diagnostics as cd,position_monitor as pm,monitor_service as ms,config
from test_primary_integrity import fresh
from test_monitor_v31 import open_case

class Proc:
    pid=987654321
    returncode=None
    async def wait(self):await asyncio.sleep(10)
    def kill(self):self.returncode=-9

async def spawn(*a,**kw):return Proc()

async def broken_cleanup(proc,diagnostic=None):
    diagnostic.event('CLEANUP_WAIT_LAUNCHER',timeout_s=3)
    raise RuntimeError('SIMULATION_CHILD_CLEANUP_TIMEOUT')

def test_cleanup_error_always_saves_pid_cause_stack_and_archive(tmp_path,monkeypatch):
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn);monkeypatch.setattr(pm,'stop_simulation',broken_cleanup)
    with pytest.raises(RuntimeError,match='CLEANUP_TIMEOUT') as caught:asyncio.run(pm.launch_check({'id':'check'},tmp_path,timeout=.001))
    d=json.loads((tmp_path/'diagnostic.json').read_text());t=d['child_diagnostics_v73']
    assert d['status']=='CLEANUP_FAILED' and t['child_pid']==Proc.pid and t['monitor_pid']==os.getpid()
    assert t['worker_exception']['chain'][0]['error_type']=='TimeoutError'
    e=t['cleanup_exception'];assert e['at_phase']=='CLEANUP_WAIT_LAUNCHER' and e['phase_elapsed_ms']>=0
    assert e['chain'][0]['message']=='SIMULATION_CHILD_CLEANUP_TIMEOUT' and e['chain'][0]['frames']
    assert caught.value.child_diagnostics_v73 is not None
    with zipfile.ZipFile(tmp_path/'diagnostic.zip') as z:assert 'child_lifecycle_v73.json' in z.namelist()

def test_cancel_does_not_hide_cleanup_error(tmp_path,monkeypatch):
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn);monkeypatch.setattr(pm,'stop_simulation',broken_cleanup)
    async def run():
        task=asyncio.create_task(pm.launch_check({'id':'check'},tmp_path));await asyncio.sleep(.01);task.cancel();await task
    with pytest.raises(RuntimeError,match='CLEANUP_TIMEOUT'):asyncio.run(run())
    d=json.loads((tmp_path/'diagnostic.json').read_text());assert d['status']=='CLEANUP_FAILED' and d['child_diagnostics_v73']['cancellation']['chain'][0]['error_type']=='CancelledError'

def test_secondary_archive_error_does_not_mask_cleanup_error(tmp_path,monkeypatch):
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn);monkeypatch.setattr(pm,'stop_simulation',broken_cleanup)
    def fail(*a):raise OSError(13,'denied')
    monkeypatch.setattr(__import__('rpc_timing'),'attach',fail)
    with pytest.raises(RuntimeError,match='CLEANUP_TIMEOUT'):asyncio.run(pm.launch_check({'id':'check'},tmp_path,timeout=.001))
    t=json.loads((tmp_path/'child_lifecycle_v73.json').read_text());assert t['diagnostic_exception']['chain'][0]['errno']==13 and t['cleanup_exception']

def test_real_child_timeout_records_reaping(tmp_path):
    r=asyncio.run(pm.launch_check({'id':'real'},tmp_path,timeout=.001));t=r['child_diagnostics_v73']
    assert r['status']=='REQUEST_TIMEOUT' and t['child_pid']>0 and t['cleanup_phase']=='CLEANUP_CONFIRMED'
    events={x['phase']:x for x in t['events']};assert events['CLEANUP_LAUNCHER_EXIT']['returncode'] is not None
    assert events['CLEANUP_LAUNCHER_EXIT']['wait_ms']>=0

def test_access_denied_preserved_with_wait_result(tmp_path,monkeypatch):
    import psutil
    def denied(pid):raise psutil.AccessDenied(pid)
    monkeypatch.setattr(psutil,'Process',denied)
    proc=Proc()
    async def wait():proc.returncode=-9;return -9
    proc.wait=wait;t=cd.Trace(tmp_path,{'id':'x'})
    with pytest.raises(RuntimeError,match='UNCONFIRMED'):asyncio.run(pm.stop_simulation(proc,t))
    phases={e['phase']:e for e in t.data['events']}
    assert phases['CLEANUP_INSPECT_FAILED']['exception']['chain'][0]['error_type']=='AccessDenied'
    assert phases['CLEANUP_LAUNCHER_EXIT']['returncode']==-9

def test_run_job_populates_result_json_and_monitor_journal(fresh,tmp_path,monkeypatch):
    open_case(fresh);monkeypatch.setattr(pm,'ROOT',tmp_path)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn);monkeypatch.setattr(pm,'stop_simulation',broken_cleanup)
    with ms.connect() as c:
        job=pm.next_job(c,__import__('time').time());job['deadline_timeout_s']=.001
        with pytest.raises(RuntimeError) as caught:asyncio.run(pm.run_job(job,c))
        row=c.execute('select status,result_json from position_checks_v0730 where id=?',(job['id'],)).fetchone()
        assert row['status']=='CLEANUP_FAILED' and json.loads(row['result_json'])['child_diagnostics_v73']['child_pid']==Proc.pid
        ms.journal_worker_exception(config.DB,c,'initial',job,caught.value)
        event=json.loads(c.execute("select details_json from monitor_events_v0731 where kind='WORKER_EXCEPTION'").fetchone()[0])
        assert event['check_id']==job['id'] and event['exception']['chain'][0]['frames'] and event['child_diagnostics_v73']['cleanup_phase']=='CLEANUP_WAIT_LAUNCHER'

def test_redaction_and_bounded_trace(tmp_path):
    try:raise ValueError('request https://rpc.example/?api-key=abc api_key=xyz\nAuthorization: Bearer mytoken')
    except ValueError as e:d=cd.exception(e)
    text=json.dumps(d);assert 'abc' not in text and 'xyz' not in text and 'mytoken' not in text
    t=cd.Trace(tmp_path,{'id':'x'})
    for i in range(100):t.event('PHASE',i=i)
    assert len(t.data['events'])==48
