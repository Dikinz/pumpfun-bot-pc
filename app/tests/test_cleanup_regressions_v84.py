"""Captured v83 failures. Injected API model, never a claim of a kernel fix."""
import asyncio,ctypes as C,json,os,threading,time,sqlite3
from contextlib import closing
from types import SimpleNamespace
import pytest
import main,windows_child as wc,position_monitor as pm,monitor_service as ms,paper_capital as cap,priority_exit as priority
from test_primary_integrity import fresh
from test_spawn_boundaries_v82 import fake_last_error,Group
from test_p0_isolation_v79 import tree_snapshot


def record(value):
    if os.getenv('CLEANUP_REGRESSION_EVIDENCE'):
        with open(os.environ['CLEANUP_REGRESSION_EVIDENCE'],'a') as f:f.write(json.dumps(value)+'\n')

@pytest.fixture(autouse=True)
def audit(fresh,tmp_path,monkeypatch):
    monkeypatch.setattr(__import__('recovery_gate'),'ACTIVE',None)
    __import__('wallet_costs').ensure(fresh)
    assert main.buy('cleanup-v84',1)[0]
    cap.record(fresh,main.P['cleanup-v84']['trade_id'],'BUY',3,{'pool_fee_rate':.0125})
    before=tree_snapshot();money=cap.summary(fresh)
    def integrity():
        for path in tmp_path.rglob('*.db'):
            with closing(sqlite3.connect(path)) as c:assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    integrity();yield;integrity()
    after=tree_snapshot();assert all(before.get(pid)==birth for pid,birth in after.items())
    current=cap.summary(fresh)
    for key in ('net_cash','reserved','available','open_principal','total_cost','closed_net'):assert current[key]==money[key]
    assert current['open_principal']==3 and not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()

async def until(predicate):
    deadline=time.monotonic()+3
    while not predicate():
        assert time.monotonic()<deadline,'readiness timeout'
        await asyncio.sleep(.01)


def test_sell_cancels_inside_unreturned_createprocess(fresh,tmp_path,monkeypatch,fake_last_error):
    entered=threading.Event();release=threading.Event();owners=[];groups=[]
    monkeypatch.setattr(pm,'ROOT',tmp_path/'checks')
    # Keep production 10s check and 5.2s stop deadlines; cancellation comes first.
    assert wc.BackgroundChild.STOP_WAIT_SECONDS==5.2 and pm.CHECK_DEADLINE==10
    async def create(args,folder,trace):
        def factory(*unused):
            native=wc.NativeGroup.__new__(wc.NativeGroup);native.trace=trace
            native.identity_callback=lambda *args:owners[0].observe_identity(*args)
            def create_call(*args):
                entered.set();assert release.wait(9)
                info=C.cast(args[-1],C.POINTER(wc.PROCESSINFO)).contents
                info.hProcess=1;info.hThread=2;info.dwProcessId=123456789
                return 1
            native.k=SimpleNamespace(CreateProcessW=create_call)
            info=wc.PROCESSINFO();native.native('CreateProcessW',C.byref(info))
            group=Group();groups.append(group);return group
        owner=wc.BackgroundChild([],folder,folder/'child.log',trace,factory,retain_cleanup=True);owners.append(owner);return owner
    monkeypatch.setattr(pm,'create_simulation_process',create)
    with closing(ms.connect()) as c:
        job=pm.next_job(c,time.time());folder=pm.ROOT/job['id']
        async def run():
            task=asyncio.create_task(pm.run_job(job,c))
            try:
                await until(entered.is_set)
                signal=time.time();tid=main.P['cleanup-v84']['trade_id']
                fresh.execute("INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,size,reason,trade_id) VALUES('sell-v84','cleanup-v84','SELL','PENDING',?,1,3,'TP2',?)",(signal,tid))
                priority.observe(c,signal);priority.preempt(c,{'initial':(task,job)})
                assert job['cancel_status']=='SUPERSEDED_BY_EXIT_SIGNAL'
                with pytest.raises(RuntimeError,match='SIMULATION_CHILD_CLEANUP_UNCONFIRMED'):await asyncio.wait_for(task,7)
                snapshot=json.loads((folder/'child_lifecycle_v73.json').read_text())
                pending=[r for r in snapshot['native_calls_v82'] if r['state']=='ENTERED']
                assert len(pending)==1 and pending[0]['api']=='CreateProcessW' and 'api_started_mono' in pending[0]
                assert snapshot['child_pid'] is None and owners[0].pid is None
                phases={e['phase']:e for e in snapshot['events']}
                assert phases['CHECK_CANCELLED_V80']['reason']=='SUPERSEDED_BY_EXIT_SIGNAL'
                assert phases['SAVED_RESULT_AT_CANCEL_V80']['artifact']['exists'] is False
                assert phases['CLEANUP_SPAWN_OR_WAIT_PENDING_V78']['thread_alive'] is True
                assert 'CLEANUP_CONFIRMED' not in phases
            finally:
                release.set()
                if owners:await asyncio.to_thread(owners[0].thread.join,7)
                if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            assert owners[0].done.is_set() and not owners[0].thread.is_alive()
            final=json.loads((folder/'child_lifecycle_v73.json').read_text())
            assert all(r['state']=='RETURNED' for r in final['native_calls_v82'])
            assert final['cleanup_phase']=='CLEANUP_CONFIRMED' and groups[0].closed and groups[0].count==0
            assert next(e for e in final['events'] if e['phase']=='SPAWN_RETURNED_V78')['stop_already_requested'] is True
            assert c.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()[0]=='CLEANUP_FAILED'
            assert c.execute("SELECT status FROM priority_exit_requests_v65 WHERE order_id='sell-v84'").fetchone()[0]=='QUEUED'
            record(dict(scenario='CANCEL_INSIDE_CREATEPROCESS',backend='INJECTED_API_MODEL',pending_snapshot=snapshot,late_cleanup=final,failed_result_not_promoted=True))
        asyncio.run(run())


@pytest.mark.parametrize('raw_code',[0,259])
def test_empty_job_unsignaled_launcher_is_not_cleanup_success(fresh,tmp_path,monkeypatch,raw_code):
    groups=[];owners=[];release=threading.Event()
    monkeypatch.setattr(pm,'ROOT',tmp_path/'checks')
    # Only worker execution time is accelerated in this injected scenario.
    monkeypatch.setattr(pm,'CHECK_DEADLINE',.05)
    class EmptyJob(wc.NativeGroup):
        def __init__(self):
            self.pid=123456789;self.created_ts=1000.;self.process=1;self.job=2;self.count=1;self.closed=False;self.trace=None
            def get_code(handle,ptr):C.cast(ptr,C.POINTER(wc.DWORD)).contents.value=raw_code;return 1
            def query(job,kind,ptr,size,unused):C.cast(ptr,C.POINTER(wc.ACCOUNTING)).contents.ActiveProcesses=self.count;return 1
            self.k=SimpleNamespace(WaitForSingleObject=lambda *args:0 if release.is_set() else 258,GetExitCodeProcess=get_code,QueryInformationJobObject=query)
        def terminate(self):self.count=0
        def close(self):self.closed=True
    async def create(args,folder,trace):
        def factory(*unused):
            group=EmptyJob();groups.append(group);return group
        owner=wc.BackgroundChild([],folder,folder/'child.log',trace,factory,retain_cleanup=True);owners.append(owner);return owner
    monkeypatch.setattr(pm,'create_simulation_process',create)
    with closing(ms.connect()) as c:
        job=pm.next_job(c,time.time());folder=pm.ROOT/job['id']
        async def run():
            try:
                with pytest.raises(RuntimeError,match='SIMULATION_CHILD_CLEANUP_UNCONFIRMED'):await asyncio.wait_for(pm.run_job(job,c),6)
                assert not owners[0].done.is_set() and owners[0].thread.is_alive() and not groups[0].closed
            finally:
                # Capture the unresolved proof before allowing the process handle to signal.
                trace=json.loads((folder/'child_lifecycle_v73.json').read_text())
                release.set();await asyncio.to_thread(owners[0].thread.join,2)
            assert owners[0].done.is_set() and not owners[0].thread.is_alive() and groups[0].closed
            phases={e['phase']:e for e in trace['events']}
            before=phases['JOB_BEFORE_TERMINATE_V80']['snapshot'];after=phases['JOB_AFTER_TERMINATE_V80']['snapshot'];last=phases['JOB_LAUNCHER_TIMEOUT_V80']['snapshot']
            assert before['job_active_processes']==1
            for snapshot in (after,last):
                assert snapshot['job_active_processes']==0 and snapshot['wait_status']==258 and snapshot['raw_exit_code']==raw_code
            assert 'CLEANUP_CONFIRMED' not in phases
            diagnostic=json.loads((folder/'diagnostic.json').read_text())
            assert diagnostic['status']=='CLEANUP_FAILED' and diagnostic['worker_result']['status']=='WORKER_TIMEOUT'
            assert c.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()[0]=='CLEANUP_FAILED'
            record(dict(scenario='EMPTY_JOB_UNSIGNALED_LAUNCHER',backend='INJECTED_API_MODEL',raw_exit_code=raw_code,trace=trace,cleanup_confirmed=False))
        asyncio.run(run())
