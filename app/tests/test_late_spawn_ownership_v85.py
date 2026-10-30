"""Production monitor retains a pending native owner across deadline/cancel/STOP."""
import asyncio,ctypes as C,json,os,sys,threading,time,sqlite3
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
import pytest,psutil
import config,main,position_monitor as pm,monitor_service as ms,windows_child as wc,late_spawn as late,paper_capital as cap
from test_primary_integrity import fresh
from test_spawn_boundaries_v82 import fake_last_error,Group
from test_p0_isolation_v79 import tree_snapshot


def record(value):
    if os.getenv('LATE_OWNERSHIP_EVIDENCE'):
        with open(os.environ['LATE_OWNERSHIP_EVIDENCE'],'a') as f:f.write(json.dumps(value)+'\n')

@pytest.fixture(autouse=True)
def audit(fresh,tmp_path,monkeypatch):
    monkeypatch.setattr(__import__('recovery_gate'),'ACTIVE',None)
    monkeypatch.setattr(late,'ACTIVE',None)
    __import__('wallet_costs').ensure(fresh)
    for mint in ('late-owner','other-position'):
        assert main.buy(mint,1)[0]
        cap.record(fresh,main.P[mint]['trade_id'],'BUY',3,{'pool_fee_rate':.0125})
    before=tree_snapshot();money=cap.summary(fresh)
    def integrity():
        out={}
        for path in tmp_path.rglob('*.db'):
            with closing(sqlite3.connect(path)) as c:out[path.name]=c.execute('PRAGMA integrity_check').fetchone()[0]
        assert all(x=='ok' for x in out.values());return out
    initial=integrity();yield;final=integrity()
    after=tree_snapshot();assert all(before.get(pid)==birth for pid,birth in after.items()),(before,after)
    current=cap.summary(fresh)
    for key in ('net_cash','reserved','available','open_principal','total_cost','closed_net'):assert current[key]==money[key]
    assert current['open_principal']==6 and not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()
    assert late.ACTIVE is None
    record(dict(kind='FINANCIAL_PROCESS_SQLITE_AUDIT',before=before,after=after,integrity_before=initial,integrity_after=final,open_principal=6))

async def until(predicate,seconds=7):
    end=time.monotonic()+seconds
    while not predicate():
        assert time.monotonic()<end,'scenario readiness timeout'
        await asyncio.sleep(.01)


def scenario(fresh,tmp_path,monkeypatch,cause='deadline',stop_while_pending=True,backend='model',fail_create=False):
    release=threading.Event();entered=threading.Event();stop=threading.Event();owners=[];groups=[];folders=[];dispatches=[];held=[False]
    monkeypatch.setattr(pm,'ROOT',tmp_path/'checks')
    # Test-only time scaling; production 10/5.2/3/2 settings are unchanged.
    monkeypatch.setattr(pm,'CHECK_DEADLINE',2 if cause=='cancel' else .08)
    monkeypatch.setattr(wc.BackgroundChild,'STOP_WAIT_SECONDS',.05)
    original_next=pm.next_job
    def next_job(*args,**kw):dispatches.append(time.monotonic());return original_next(*args,**kw)
    monkeypatch.setattr(pm,'next_job',next_job)
    if backend=='native_windows':
        original_dll=C.WinDLL
        class HeldCall:
            def __init__(self,fn):object.__setattr__(self,'fn',fn)
            def __setattr__(self,key,value):setattr(self.fn,key,value)
            def __call__(self,*args):entered.set();assert release.wait(12);return self.fn(*args)
        class Proxy:
            def __init__(self,k):self.k=k;self.held=HeldCall(k.CreateProcessW)
            def __getattr__(self,name):return self.held if name=='CreateProcessW' else getattr(self.k,name)
        monkeypatch.setattr(C,'WinDLL',lambda *a,**kw:Proxy(original_dll(*a,**kw)))
    async def create(args,folder,trace):
        if held[0]:
            def quick(*unused):
                group=Group();group.count=0;group.code=0
                (folder/'result.json').write_text(json.dumps(dict(status='CHILD_ERROR')))
                return group
            return wc.BackgroundChild([],folder,folder/'child.log',trace,quick)
        held[0]=True;folders.append(folder)
        def factory(*unused):
            if backend=='native_windows':
                ready=folder/'worker.ready'
                worker="import sys,time;from pathlib import Path;Path(sys.argv[1]).write_text('ready');time.sleep(60)"
                wrapper="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(60)"
                group=wc.NativeGroup([sys.executable,'-c',wrapper,worker,str(ready)],folder,folder/'child.log',trace,identity_callback=lambda *x:owners[0].observe_identity(*x))
                try:
                    deadline=time.monotonic()+5
                    while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
                    assert ready.exists();groups.append(group);return group
                except BaseException:group.close();raise
            native=wc.NativeGroup.__new__(wc.NativeGroup);native.trace=trace;native.identity_callback=lambda *x:owners[0].observe_identity(*x)
            def call(*args):
                entered.set();assert release.wait(12)
                if fail_create:C.set_last_error(5);return 0
                info=C.cast(args[-1],C.POINTER(wc.PROCESSINFO)).contents
                info.hProcess=1;info.hThread=2;info.dwProcessId=123456789;return 1
            native.k=SimpleNamespace(CreateProcessW=call)
            info=wc.PROCESSINFO();result=native.native('CreateProcessW',C.byref(info))
            if not result:raise OSError(5,'injected creation denied')
            group=Group();groups.append(group);return group
        owner=wc.BackgroundChild([],folder,folder/'child.log',trace,factory);owners.append(owner);return owner
    monkeypatch.setattr(pm,'create_simulation_process',create)
    async def run():
        monitor=asyncio.create_task(ms.run(config.DB,0,stop_event=stop));guard=None
        try:
            await until(entered.is_set)
            if cause=='cancel':
                with closing(ms.connect()) as c:job=dict(c.execute("SELECT * FROM position_checks_v0730 WHERE status='RUNNING' ORDER BY started_ts LIMIT 1").fetchone())
                fresh.execute("INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,size,reason,trade_id) VALUES('late-sell',?,'SELL','PENDING',?,1,3,'TP2',?)",(job['mint'],time.time(),job['trade_id']))
            await until(lambda:late.path(config.DB).exists() and json.loads(late.path(config.DB).read_text()).get('pending'))
            await until(lambda:ms.read_state(config.DB).get('status')=='CLEANUP_PENDING')
            pending=json.loads(late.path(config.DB).read_text());check_id=pending['owners'][0]['check_id']
            assert pending['owners'][0]['child_pid'] is None and not monitor.done()
            assert main._buy('blocked-v85',1)==(False,'CHILD_CREATION_CLEANUP_PENDING')
            count=len(dispatches);await asyncio.sleep(.25)
            assert len(dispatches)==count # No replacement/native-owner growth while ownership is unresolved.
            with closing(ms.connect()) as c:
                row=dict(c.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(check_id,)).fetchone())
                assert row['status']=='CLEANUP_FAILED'
            if stop_while_pending:
                stop.set()
                await until(lambda:ms.read_state(config.DB).get('status')=='STOP_WAITING_CHILD_CLEANUP')
                guard=asyncio.create_task(asyncio.to_thread(late.wait_before_force,SimpleNamespace(pid=os.getpid()),(),Path(config.DB).resolve().parent))
                await asyncio.sleep(.15);assert not guard.done() and not monitor.done()
            release.set()
            await until(lambda:not json.loads(late.path(config.DB).read_text()).get('pending'))
            if not stop_while_pending:
                await until(lambda:len(dispatches)>count)
                assert not monitor.done();stop.set()
            await asyncio.wait_for(monitor,8)
            if guard:assert await asyncio.wait_for(guard,3) is True
            assert owners[0].done.is_set();await asyncio.to_thread(owners[0].thread.join,1)
            assert not owners[0].thread.is_alive()
            final=json.loads(late.path(config.DB).read_text());resolved=next(x for x in final['history'] if x['check_id']==check_id)
            assert resolved['state']=='CONFIRMED' and resolved['original_result_ineligible'] is True
            assert resolved['outcome']==('SPAWN_FAILED_NO_PROCESS' if fail_create else 'LATE_PROCESS_CLEANED')
            artifact=json.loads((folders[0]/'late_cleanup_v85.json').read_text());trace=artifact['child_diagnostics_v73']
            assert all(x['state']=='RETURNED' for x in trace['native_calls_v82'])
            if not fail_create:
                assert resolved['child_pid'] is not None
                assert next(e for e in trace['events'] if e['phase']=='SPAWN_RETURNED_V78')['stop_already_requested'] is True
                assert trace['cleanup_phase']=='CLEANUP_CONFIRMED'
            with closing(ms.connect()) as c:assert c.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(check_id,)).fetchone()[0]=='CLEANUP_FAILED'
            assert ms.read_state(config.DB)['status']=='STOPPED'
            exported=ms.export(config.DB,tmp_path/'model-export')
            with __import__('zipfile').ZipFile(exported) as z:
                manifest=json.loads(z.read('late_cleanup_manifest_v85.json'))
                assert manifest['status']=='OK' and any(x['check_id']==check_id for x in manifest['files'])
                for item in manifest['files']:assert __import__('hashlib').sha256(z.read(item['file'])).hexdigest()==item['sha256']
            record(dict(kind='SCENARIO',backend=backend,cause=cause,stop_while_pending=stop_while_pending,fail_create=fail_create,pending=pending,resolved=artifact,monitor_stopped=True,result_still_ineligible=True))
        finally:
            release.set();stop.set()
            if not monitor.done():await asyncio.wait_for(monitor,8)
            if guard and not guard.done():await asyncio.wait_for(guard,3)
            for owner in owners:await asyncio.to_thread(owner.thread.join,2)
    asyncio.run(run())


def test_deadline_retains_owner_and_stop_waits_for_late_cleanup(fresh,tmp_path,monkeypatch,fake_last_error):
    scenario(fresh,tmp_path,monkeypatch)

def test_sell_cancel_retains_owner_then_resumes_fresh_scheduling(fresh,tmp_path,monkeypatch,fake_last_error):
    scenario(fresh,tmp_path,monkeypatch,cause='cancel',stop_while_pending=False)

def test_late_creation_error_is_proven_no_process(fresh,tmp_path,monkeypatch,fake_last_error):
    scenario(fresh,tmp_path,monkeypatch,fail_create=True)

@pytest.mark.skipif(os.name!='nt',reason='Native Windows job/process tree verification requires Windows')
def test_native_late_owner_survives_stop_and_reaps_tree(fresh,tmp_path,monkeypatch):
    scenario(fresh,tmp_path,monkeypatch,backend='native_windows')


def test_stale_or_reused_monitor_identity_does_not_hold_stop(tmp_path):
    primary=tmp_path/'test.db';pid,birth=late.own_identity()
    late.write(late.path(primary),dict(pending=True,monitor_pid=pid,monitor_created_ts=birth-100))
    assert not late.blocks_entries(primary)
    assert not late.wait_before_force(SimpleNamespace(pid=os.getpid()),directory=tmp_path)

def test_unknown_pending_state_fails_closed(tmp_path):
    primary=tmp_path/'test.db';late.path(primary).write_text('{bad')
    assert late.blocks_entries(primary)
    with pytest.raises(RuntimeError,match='LATE_CREATION_STATE_UNREADABLE'):late.wait_before_force(SimpleNamespace(pid=os.getpid()),directory=tmp_path)


@pytest.mark.skipif(os.name!='nt' and Path('/proc/self/stat').exists() and int(Path('/proc/self/stat').read_text().split()[0])!=os.getpid(),reason='PID namespace differs from procfs; real monitor tree proof requires Windows check here')
def test_actual_shutdown_does_not_kill_retained_monitor(fresh,tmp_path,monkeypatch):
    import subprocess,branch_transport
    release=tmp_path/'release';checks=tmp_path/'separate-checks'
    monkeypatch.setattr(pm,'ROOT',checks)
    script=r'''import asyncio,ctypes as C,json,sys,threading,time
from pathlib import Path
from types import SimpleNamespace
import config,position_monitor as pm,monitor_service as ms,windows_child as wc
config.DB=sys.argv[1];pm.ROOT=Path(sys.argv[2]);release=Path(sys.argv[3])
pm.CHECK_DEADLINE=.08;wc.BackgroundChild.STOP_WAIT_SECONDS=.05
if not hasattr(C,'set_last_error'):
 local=threading.local()
 C.set_last_error=lambda n:setattr(local,'value',n)
 C.get_last_error=lambda:getattr(local,'value',0)
held=False;owners=[]
class Group:
 pid=123456789;created_ts=1000.;proof='INJECTED_TEST_GROUP'
 def __init__(self):self.code=None;self.count=1
 def poll(self):return self.code
 def active(self):return self.count
 def terminate(self):self.code=1;self.count=0
 def close(self):pass
async def create(args,folder,trace):
 global held
 first=not held;held=True
 def factory(*unused):
  if first:
   native=wc.NativeGroup.__new__(wc.NativeGroup);native.trace=trace
   native.identity_callback=lambda *args:owners[0].observe_identity(*args)
   def call(*args):
    deadline=time.monotonic()+20
    while not release.exists():
     assert time.monotonic()<deadline
     time.sleep(.01)
    info=C.cast(args[-1],C.POINTER(wc.PROCESSINFO)).contents
    info.hProcess=1;info.hThread=2;info.dwProcessId=123456789;return 1
   native.k=SimpleNamespace(CreateProcessW=call)
   info=wc.PROCESSINFO();native.native('CreateProcessW',C.byref(info))
  group=Group()
  if not first:
   group.code=0;group.count=0
   (folder/'result.json').write_text(json.dumps(dict(status='CHILD_ERROR')))
  return group
 owner=wc.BackgroundChild([],folder,folder/'child.log',trace,factory);owners.append(owner);return owner
pm.create_simulation_process=create
asyncio.run(ms.run_with_stdin(config.DB,0))
'''
    child=subprocess.Popen([sys.executable,'-c',script,str(config.DB),str(checks),str(release)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    async def run():
        killer=None
        try:
            await until(lambda:ms.read_state(config.DB).get('status')=='CLEANUP_PENDING')
            child.stdin.close()
            await until(lambda:ms.read_state(config.DB).get('status')=='STOP_WAITING_CHILD_CLEANUP')
            killer=asyncio.create_task(asyncio.to_thread(branch_transport.terminate_tree,child))
            await asyncio.sleep(.2)
            assert not killer.done() and child.poll() is None
            release.write_text('release')
            assert await asyncio.wait_for(killer,8) is False # Real stop path returned without terminate()/kill().
            assert child.returncode==0
            assert ms.read_state(config.DB)['status']=='STOPPED'
            state=json.loads(late.path(config.DB).read_text());assert not state['pending']
            assert any(x['state']=='CONFIRMED' for x in state['history'])
            evidence=ms.export(config.DB,tmp_path/'export')
            with __import__('zipfile').ZipFile(evidence) as z:
                assert late.path(config.DB).name in z.namelist()
                assert any(n.endswith('/late_cleanup_v85.json') for n in z.namelist())
            record(dict(kind='ACTUAL_MONITOR_PROCESS_STOP',forced=False,exit_code=0,state=state))
        finally:
            release.write_text('release')
            if child.stdin and not child.stdin.closed:child.stdin.close()
            if killer and not killer.done():await asyncio.wait_for(killer,8)
            if child.poll() is None:child.kill();child.wait(timeout=5)
            stdout=child.stdout.read().decode(errors='replace');stderr=child.stderr.read().decode(errors='replace')
            if child.returncode:print('monitor subprocess:',child.returncode,stderr)
            child.stdout.close();child.stderr.close()
            assert 'Fatal Python error' not in stderr and '_enter_buffered_busy' not in stderr,stderr
    asyncio.run(run())


def test_failed_late_cleanup_remains_fatal_at_stop(fresh,tmp_path,monkeypatch):
    import child_diagnostics
    out=tmp_path/'failed-check';out.mkdir()
    trace=child_diagnostics.Trace(out,dict(id='failed-check',trade_id=main.P['late-owner']['trade_id']))
    trace.event('JOB_LAUNCHER_TIMEOUT_V80',snapshot=dict(job_active_processes=0,wait_status=258))
    done=threading.Event();done.set()
    owner=SimpleNamespace(done=done,thread=SimpleNamespace(is_alive=lambda:False),trace=trace,pid=123456789,created_ts=1000.,returncode=None,cleanup_error=RuntimeError('SIMULATION_CHILD_CLEANUP_TIMEOUT'))
    registry=late.Registry(config.DB)
    registry.owners['failed-check']=(owner,out,dict(check_id='failed-check',trade_id=main.P['late-owner']['trade_id'],state='PENDING',original_result_ineligible=True))
    monkeypatch.setattr(late,'Registry',lambda *args:registry)
    with pytest.raises(RuntimeError,match='SIMULATION_LATE_CHILD_CLEANUP_FAILED'):asyncio.run(ms.run(config.DB,0,stop_event=threading.Event()))
    assert ms.read_state(config.DB)['status']=='FAILED'
    final=json.loads((out/'late_cleanup_v85.json').read_text())
    assert final['state']=='FAILED' and final['outcome']=='CLEANUP_NOT_CONFIRMED'
    assert not any(x['state']=='CONFIRMED' for x in registry.history)
    record(dict(kind='FAILED_LATE_CLEANUP_NOT_PROMOTED',result=final))
