"""Native-call boundaries and late creation cleanup; temporary PAPER only."""
import asyncio,ctypes as C,json,os,threading,time,sys,sqlite3
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
import pytest
import config,main,windows_child as wc,child_diagnostics as cd,position_monitor as pm,monitor_service as ms,paper_capital as cap
from test_primary_integrity import fresh
from test_p0_isolation_v79 import tree_snapshot

class Group:
    pid=123456789;created_ts=1000.;proof='INJECTED_TEST_GROUP'
    def __init__(self):self.code=None;self.count=2;self.closed=False
    def poll(self):return self.code
    def active(self):return self.count
    def terminate(self):self.code=1;self.count=0
    def close(self):self.closed=True

@pytest.fixture(autouse=True)
def audit(fresh,tmp_path,monkeypatch):
    monkeypatch.setattr(__import__('recovery_gate'),'ACTIVE',None)
    __import__('wallet_costs').ensure(fresh);assert main.buy('late-spawn',1)[0]
    cap.record(fresh,main.P['late-spawn']['trade_id'],'BUY',3,{'pool_fee_rate':.0125})
    before=tree_snapshot();cash=cap.cash(fresh);yield
    after=tree_snapshot();assert all(before.get(pid)==birth for pid,birth in after.items()),(before,after)
    assert cap.cash(fresh)==cash and cap.summary(fresh)['open_principal']==3
    for p in tmp_path.rglob('*.db'):
        with closing(sqlite3.connect(p)) as c:assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'

def record(value):
    if os.getenv('SPAWN_BOUNDARY_EVIDENCE'):
        with open(os.environ['SPAWN_BOUNDARY_EVIDENCE'],'a') as f:f.write(json.dumps(value)+'\n')

@pytest.fixture
def fake_last_error(monkeypatch):
    local=threading.local()
    monkeypatch.setattr(C,'set_last_error',lambda value:setattr(local,'value',value),raising=False)
    monkeypatch.setattr(C,'get_last_error',lambda:getattr(local,'value',0),raising=False)

@pytest.mark.parametrize('blocked_api',['CreateProcessW','CloseHandle','GetProcessTimes'])
def test_late_model_call_has_exact_boundary_and_cleanup(fresh,tmp_path,monkeypatch,fake_last_error,blocked_api):
    release=threading.Event();entered=threading.Event();bound=threading.Event();owners=[];groups=[]
    monkeypatch.setattr(wc.BackgroundChild,'STOP_WAIT_SECONDS',.05)
    monkeypatch.setattr(pm,'CHECK_DEADLINE',.03);monkeypatch.setattr(pm,'ROOT',tmp_path/'checks')
    async def create(args,folder,trace):
        def factory(*args):
            assert bound.wait(1)
            native=wc.NativeGroup.__new__(wc.NativeGroup);native.trace=trace;native.identity_callback=owners[0].observe_identity
            def call(api,*args):
                if api==blocked_api:entered.set();assert release.wait(3)
                if api=='CreateProcessW':
                    info=C.cast(args[-1],C.POINTER(wc.PROCESSINFO)).contents
                    info.hProcess=1;info.hThread=2;info.dwProcessId=123456789
                return 1
            native.k=SimpleNamespace(**{api:(lambda *a,api=api:call(api,*a)) for api in ('CreateProcessW','CloseHandle','GetProcessTimes')})
            info=wc.PROCESSINFO();native.native('CreateProcessW',C.byref(info));native.native('CloseHandle',2);native.native('GetProcessTimes',1)
            group=Group();groups.append(group);return group
        owner=wc.BackgroundChild([],folder,folder/'child.log',trace,factory);owners.append(owner);bound.set();return owner
    monkeypatch.setattr(pm,'create_simulation_process',create)
    with closing(ms.connect()) as c:
        job=pm.next_job(c,time.time());folder=pm.ROOT/job['id']
        async def run():
            try:
                with pytest.raises(RuntimeError,match='SIMULATION_CHILD_CLEANUP_UNCONFIRMED'):await pm.run_job(job,c)
                assert entered.is_set() and owners[0].thread.is_alive()
                trace=json.loads((folder/'child_lifecycle_v73.json').read_text())
                pending=[r for r in trace['native_calls_v82'] if r['state']=='ENTERED']
                assert len(pending)==1 and pending[0]['api']==blocked_api
                assert (owners[0].pid is None)==(blocked_api=='CreateProcessW')
                assert (trace['child_pid'] is None)==(blocked_api=='CreateProcessW')
                assert trace['child_created_ts'] is None
            finally:
                release.set()
                if owners:await asyncio.to_thread(owners[0].thread.join,6)
            assert owners[0].done.is_set() and not owners[0].thread.is_alive()
            final=json.loads((folder/'child_lifecycle_v73.json').read_text())
            assert all(r['state']=='RETURNED' for r in final['native_calls_v82'])
            assert final['cleanup_phase']=='CLEANUP_CONFIRMED' and groups[0].count==0 and groups[0].closed
            assert next(e for e in final['events'] if e['phase']=='SPAWN_RETURNED_V78')['stop_already_requested'] is True
            assert c.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()[0]=='CLEANUP_FAILED'
            record(dict(backend='MODEL',blocked_api=blocked_api,pending=pending[0],late_cleanup_confirmed=True,failed_result_not_promoted=True,trace=final))
        asyncio.run(run())

def test_native_error_is_preserved_across_diagnostic_io(tmp_path,monkeypatch,fake_last_error):
    trace=cd.Trace(tmp_path,{});actual=trace.native_return
    def noisy(*a,**kw):actual(*a,**kw);C.set_last_error(999)
    monkeypatch.setattr(trace,'native_return',noisy)
    def fail():C.set_last_error(5);return 0
    n=wc.NativeGroup.__new__(wc.NativeGroup);n.k=SimpleNamespace(DuplicateHandle=fail);n.trace=trace;n.identity_callback=None
    assert n.native('DuplicateHandle')==0 and C.get_last_error()==5
    row=trace.snapshot()['native_calls_v82'][0]
    assert row['winerror']==5 and row['state']=='RETURNED' and row['ok'] is False

def test_native_trace_is_bounded_and_contains_no_arguments(tmp_path):
    trace=cd.Trace(tmp_path,{})
    for _ in range(50):
        seq=trace.native_enter('GetCurrentProcess');trace.native_return(seq,'GetCurrentProcess',True,None,time.monotonic(),time.time())
    d=trace.snapshot();assert len(d['native_calls_v82'])==32 and len(d['events'])==48
    assert all(set(r)<={'call_id','api','state','entered_mono','entered_ts','thread_id','expected_probe','returned_mono','returned_ts','elapsed_ms','entry_record_ms','ok','winerror'} for r in d['native_calls_v82'])

@pytest.mark.skipif(os.name!='nt',reason='Late real Windows Job Object creation requires Windows')
def test_native_late_spawn_after_timeout_cleans_tree(fresh,tmp_path,monkeypatch):
    release=threading.Event();bound=threading.Event();entered=threading.Event();owners=[]
    monkeypatch.setattr(wc.BackgroundChild,'STOP_WAIT_SECONDS',.05);monkeypatch.setattr(pm,'CHECK_DEADLINE',.03);monkeypatch.setattr(pm,'ROOT',tmp_path/'native')
    code="import sys,time;from pathlib import Path;Path(sys.argv[1]).write_text('ready');time.sleep(60)"
    wrapper="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(60)"
    async def create(args,folder,trace):
        ready=folder/'worker.ready'
        def factory(*a):
            assert bound.wait(1);entered.set();assert release.wait(3)
            group=wc.NativeGroup([sys.executable,'-c',wrapper,code,str(ready)],folder,folder/'child.log',trace,identity_callback=owners[0].observe_identity)
            try:
                deadline=time.monotonic()+4
                while not ready.exists() and time.monotonic()<deadline:time.sleep(.02)
                assert ready.exists(),'late worker readiness'
                return group
            except BaseException:group.close();raise
        owner=wc.BackgroundChild([],folder,folder/'child.log',trace,factory);owners.append(owner);bound.set();return owner
    monkeypatch.setattr(pm,'create_simulation_process',create)
    with closing(ms.connect()) as c:
        job=pm.next_job(c,time.time());folder=pm.ROOT/job['id']
        async def run():
            try:
                with pytest.raises(RuntimeError,match='SIMULATION_CHILD_CLEANUP_UNCONFIRMED'):await pm.run_job(job,c)
                assert entered.is_set() and owners[0].pid is None
            finally:
                release.set()
                if owners:await asyncio.to_thread(owners[0].thread.join,8)
            assert owners[0].done.is_set() and not owners[0].thread.is_alive()
            trace=json.loads((folder/'child_lifecycle_v73.json').read_text())
            event=next(r for r in trace['events'] if r['phase']=='CLEANUP_CONFIRMED')
            assert event['proof']=='WINDOWS_JOB_ACCOUNTING' and event['active_processes']==0
            assert next(e for e in trace['events'] if e['phase']=='SPAWN_RETURNED_V78')['stop_already_requested'] is True
            assert trace['child_pid'] is not None and all(r['state']=='RETURNED' for r in trace['native_calls_v82'])
            assert c.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()[0]=='CLEANUP_FAILED'
            record(dict(backend='WINDOWS_JOB_OBJECT',delay_injected_before_api=True,late_cleanup_confirmed=True,failed_result_not_promoted=True,trace=trace))
        asyncio.run(run())
