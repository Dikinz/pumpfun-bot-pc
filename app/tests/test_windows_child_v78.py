import asyncio,ctypes,json,os,subprocess,sys,threading,time
from pathlib import Path
import pytest
import windows_child as wc,child_diagnostics as cd,position_monitor as pm
from test_primary_integrity import fresh

class Group:
    pid=123456789;created_ts=1000.
    def __init__(self,*args):self.code=None;self.count=2;self.killed=False;self.closed=False
    def poll(self):return self.code
    def active(self):return self.count
    def terminate(self):self.killed=True;self.code=1;self.count=0
    def close(self):self.closed=True

def test_windows_64bit_structure_layout():
    if ctypes.sizeof(ctypes.c_void_p)!=8:pytest.skip('64-bit ABI check')
    assert ctypes.sizeof(wc.STARTUPINFO)==104
    assert ctypes.sizeof(wc.STARTUPINFOEX)==112
    assert ctypes.sizeof(wc.PROCESSINFO)==24
    assert ctypes.sizeof(wc.EXTENDEDLIMIT)==144
    assert ctypes.sizeof(wc.ACCOUNTING)==48

def test_delayed_native_creation_does_not_block_loop_and_late_child_is_cleaned(tmp_path,monkeypatch):
    groups=[]
    def slow(*args):time.sleep(.25);g=Group();groups.append(g);return g
    async def create(args,out,trace):return wc.BackgroundChild(args,out,out/'child.log',trace,factory=slow)
    monkeypatch.setattr(pm,'create_simulation_process',create)
    async def run():
        pulses=0;task=asyncio.create_task(pm.launch_check({'id':'slow-spawn'},tmp_path,timeout=.03))
        while not task.done():pulses+=1;await asyncio.sleep(.01)
        return await task,pulses
    r,pulses=asyncio.run(run())
    assert pulses>=10 and r['status']=='WORKER_TIMEOUT'
    assert groups[0].killed and groups[0].closed
    t=r['child_diagnostics_v73'];assert t['child_pid']==Group.pid and t['child_created_ts']==1000.
    assert t['cleanup_phase']=='CLEANUP_CONFIRMED'
    event=next(e for e in t['events'] if e['phase']=='SPAWN_RETURNED_V78')
    assert event['stop_already_requested'] is True and event['spawn_ms']>=200

def test_cancellation_during_creation_waits_for_late_process_cleanup(tmp_path):
    groups=[]
    def slow(*args):time.sleep(.12);g=Group();groups.append(g);return g
    t=cd.Trace(tmp_path,{'id':'cancel'});p=wc.BackgroundChild([],tmp_path,tmp_path/'log',t,slow)
    async def run():
        task=asyncio.create_task(p.wait());await asyncio.sleep(.01);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        await p.stop()
    asyncio.run(run());assert p.done.is_set() and groups[0].closed and groups[0].killed

def test_creation_access_denied_preserves_winerror_and_thread_finishes(tmp_path,monkeypatch):
    error=PermissionError(13,'denied');error.winerror=5
    def fail(*args):raise error
    async def create(args,out,trace):return wc.BackgroundChild(args,out,out/'log',trace,fail)
    monkeypatch.setattr(pm,'create_simulation_process',create)
    r=asyncio.run(pm.launch_check({'id':'denied'},tmp_path))
    assert r['status']=='WORKER_ERROR'
    e=r['child_diagnostics_v73']['worker_exception']['chain'][0]
    assert e['errno']==13 and e['winerror']==5 and e['frames']

def test_reused_unrelated_pid_never_receives_a_kill(tmp_path,monkeypatch):
    # Managed cleanup only uses the job object; psutil lookup must never be called.
    import psutil
    def forbidden(*a,**kw):raise AssertionError('PID lookup during kernel-job cleanup')
    monkeypatch.setattr(psutil,'Process',forbidden)
    g=Group();p=wc.BackgroundChild([],tmp_path,tmp_path/'log',cd.Trace(tmp_path,{}),lambda *args:g)
    async def run():await p.spawned();await p.stop()
    asyncio.run(run());assert g.killed and g.closed

def test_uncertain_job_query_remains_cleanup_failure(tmp_path):
    class Denied(Group):
        def active(self):raise PermissionError(13,'query denied')
    g=Denied();p=wc.BackgroundChild([],tmp_path,tmp_path/'log',cd.Trace(tmp_path,{}),lambda *a:g)
    async def run():
        await p.spawned()
        p.stop_requested.set()
        with pytest.raises(PermissionError):await p.wait()
        with pytest.raises(PermissionError):await p.stop()
    asyncio.run(run());assert g.closed

def test_nonterminating_creation_is_explicit_not_confirmed(tmp_path,monkeypatch):
    release=threading.Event();entered=threading.Event();g=Group()
    def stuck(*args):entered.set();release.wait();return g
    p=wc.BackgroundChild([],tmp_path,tmp_path/'log',cd.Trace(tmp_path,{}),stuck)
    assert entered.wait(1)
    original=asyncio.wait_for
    async def short(awaitable,timeout):return await original(awaitable,.05)
    monkeypatch.setattr(wc.asyncio,'wait_for',short)
    async def run():
        with pytest.raises(RuntimeError,match='CLEANUP_UNCONFIRMED'):await p.stop()
    try:asyncio.run(run())
    finally:release.set();p.thread.join(2)
    assert g.killed and g.closed and p.done.is_set()

def test_close_failure_is_reported_and_waiters_are_released(tmp_path):
    class BadClose(Group):
        def close(self):raise OSError(5,'close failed')
    p=wc.BackgroundChild([],tmp_path,tmp_path/'log',cd.Trace(tmp_path,{}),lambda *a:BadClose())
    async def run():
        await p.spawned();p.stop_requested.set()
        with pytest.raises(OSError):await asyncio.wait_for(p.wait(),1)
    asyncio.run(run());assert p.done.is_set() and p.cleanup_error is not None

@pytest.mark.parametrize('module,table,key,columns',[
    ('preentry','preentry_v0729','job_id','mint,created_ts'),
    ('auto_simulation','auto_sim_v0727','order_id','mint,signal_ts'),
    ('shadow_preflight','shadow_preflight_v0743','job_id','mint,created_ts'),
])
def test_failed_auxiliary_check_exports_lifecycle(fresh,tmp_path,monkeypatch,module,table,key,columns):
    import importlib,zipfile,config
    m=importlib.import_module(module);root=tmp_path/module;root.mkdir();jid='a'*32;out=root/jid;out.mkdir()
    monkeypatch.setattr(m,'ROOT',root)
    fresh.execute(f"INSERT INTO {table}({key},{columns},started_ts,status,input_json) VALUES(?, 'mint',1,1,'WORKER_ERROR','{{}}')",(jid,))
    (out/'child_lifecycle_v73.json').write_text('{"phase":"SPAWN_FAILED_V78"}')
    with zipfile.ZipFile(out/'diagnostic.zip','w') as z:z.writestr('diagnostic.json','{}')
    folder=tmp_path/(module+'-export');folder.mkdir()
    exporter=m.export_evidence if module=='auto_simulation' else m.export
    archive=exporter(config.DB,folder)
    with zipfile.ZipFile(archive) as z:
        assert jid+'/diagnostic.zip' in z.namelist() and jid+'/child_lifecycle_v73.json' in z.namelist()

@pytest.mark.skipif(os.name!='nt',reason='Native Windows Job Object proof requires Windows 10+')
def test_native_job_terminates_wrapper_and_grandchild(tmp_path):
    grand=tmp_path/'grand.json'
    code="import os,json,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid()}));time.sleep(60)"
    wrapper="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(60)"
    t=cd.Trace(tmp_path,{'id':'native-job'});p=wc.BackgroundChild([sys.executable,'-c',wrapper,code,str(grand)],tmp_path,tmp_path/'log',t)
    async def run():
        try:
            await asyncio.wait_for(p.spawned(),10)
            deadline=time.monotonic()+8
            while not grand.exists() and time.monotonic()<deadline:await asyncio.sleep(.02)
            assert grand.exists(),'grandchild readiness timeout'
        finally:await p.stop()
    asyncio.run(run())
    assert t.data['cleanup_phase']=='CLEANUP_CONFIRMED'
    assert t.data['events'][-1]['active_processes']==0

@pytest.mark.skipif(os.name!='nt',reason='Native Windows Job Object proof requires Windows 10+')
def test_native_job_cleans_descendant_after_root_exits(tmp_path):
    # This catches the old "root has exited, skip all descendant cleanup" path.
    code="import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])"
    t=cd.Trace(tmp_path,{'id':'native-orphan'});p=wc.BackgroundChild([sys.executable,'-c',code],tmp_path,tmp_path/'log',t)
    async def run():
        try:await asyncio.wait_for(p.wait(),10)
        finally:await p.stop()
    asyncio.run(run());assert t.data['cleanup_phase']=='CLEANUP_CONFIRMED'
