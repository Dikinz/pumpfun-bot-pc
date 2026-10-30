import asyncio,json,os,sqlite3,sys,threading,time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import pytest
import config,main,paper_capital as cap,windows_child as wc,position_monitor as pm,monitor_service as ms,late_spawn as late
from test_primary_integrity import fresh


async def until(predicate,seconds=8):
    end=time.monotonic()+seconds
    while not predicate():
        assert time.monotonic()<end,'condition not observed'
        await asyncio.sleep(.01)


class DelayedGroup:
    pid=123456789;created_ts=1234.;proof='INJECTED_HANDLE_MODEL';atomic_assignment=True
    def __init__(self,release):self.release=release;self.count=1;self.closed=False;self.terminations=0
    def poll(self):return 0 if self.release.is_set() else None
    def active(self):return self.count
    def terminate(self):self.terminations+=1;self.count=0
    def close(self):self.closed=True
    def diagnostic(self):return dict(wait_status=0 if self.release.is_set() else 258,raw_exit_code=0,job_active_processes=self.count)


def scenario(fresh,tmp_path,monkeypatch,stop_pending=True,native=False,creation_delay=0.):
    monkeypatch.setattr(__import__('recovery_gate'),'ACTIVE',None);monkeypatch.setattr(late,'ACTIVE',None)
    __import__('wallet_costs').ensure(fresh);assert main.buy('cleanup-owner-v93',1)[0]
    cap.record(fresh,main.P['cleanup-owner-v93']['trade_id'],'BUY',3,{'pool_fee_rate':.0125});money=cap.summary(fresh)
    assert pm.CHECK_DEADLINE==10 and wc.BackgroundChild.STOP_WAIT_SECONDS==5.2
    monkeypatch.setattr(pm,'CHECK_DEADLINE',.08);monkeypatch.setattr(pm,'ROOT',tmp_path/'checks')
    release=threading.Event();stop=threading.Event();owners=[];groups=[]
    async def create(args,out,trace):
        def factory(*unused):
            if native:
                g=wc.NativeGroup([sys.executable,'-u','-c','import time; time.sleep(60)'],out,out/'child.log',trace)
                real=g.k
                class Kernel:
                    def WaitForSingleObject(self,handle,timeout):return 258 if not release.is_set() else real.WaitForSingleObject(handle,timeout)
                    def __getattr__(self,name):return getattr(real,name)
                g.k=Kernel()
            else:
                call=trace.native_enter('CreateProcessW') if creation_delay else None
                if creation_delay:
                    trace.native_started(call);time.sleep(creation_delay)
                g=DelayedGroup(release)
                if call is not None:trace.native_return(call,'CreateProcessW',True,None,time.monotonic(),time.time())
            groups.append(g);return g
        p=wc.BackgroundChild([],out,out/'child.log',trace,factory,retain_cleanup=True);owners.append(p)
        # Test setup must establish a known launcher before the shortened 80ms
        # check deadline can reach cleanup. A real Windows launch can exceed
        # 80ms; that is not the delayed process-handle signal under test here.
        await asyncio.wait_for(p.spawned(),8)
        assert p.pid is not None and p.created_ts is not None and not late.creation_pending(p)
        trace.event('TEST_KNOWN_LAUNCHER_READY_V941',child_pid=p.pid,child_created_ts=p.created_ts)
        return p
    monkeypatch.setattr(pm,'create_simulation_process',create)
    async def run():
        task=asyncio.create_task(ms.run(config.DB,0,stop_event=stop));guard=None
        try:
            await until(lambda:owners and owners[0].spawn_done.is_set())
            await until(lambda:owners[0].cleanup_pending.is_set())
            await until(lambda:ms.read_state(config.DB).get('status')=='CLEANUP_PENDING')
            registry=late.ACTIVE;pending=registry.snapshot();assert pending['pending'] and pending['owners'][0]['child_pid']==owners[0].pid
            assert pending['owners'][0]['source']=='PROCESS_HANDLE_CLEANUP_PENDING_V93'
            assert pending['owners'][0]['child_created_ts']==owners[0].created_ts
            if native or creation_delay:assert pending['owners'][0]['native_boundary']['state']=='RETURNED'
            assert not owners[0].done.is_set() and len(owners)==1
            if not native:assert not groups[0].closed and groups[0].count==0
            else:assert groups[0].process and groups[0].job and groups[0].active()==0
            assert late.blocks_entries(config.DB)
            allowed,reason=main.buy('blocked-while-cleanup',1);assert not allowed
            with closing(ms.connect()) as c:
                await until(lambda:c.execute('select status from position_checks_v0730').fetchone()[0]=='CLEANUP_FAILED')
                row=c.execute('select id,status from position_checks_v0730').fetchone();original=row['id'];assert row['status']=='CLEANUP_FAILED'
            # Existing STOP guard must protect a known-PID owner, not just unreturned creation.
            if stop_pending:
                stop.set();await until(lambda:ms.read_state(config.DB).get('status')=='STOP_WAITING_CHILD_CLEANUP')
                guard=asyncio.create_task(asyncio.to_thread(late.wait_before_force,SimpleNamespace(pid=os.getpid()),(),Path(config.DB).parent))
                await asyncio.sleep(.05);assert not task.done() and not guard.done()
            release.set();await until(lambda:registry.history and registry.history[-1]['state']=='CONFIRMED')
            assert registry.history[-1]['outcome']=='DELAYED_CLEANUP_CONFIRMED' and registry.history[-1]['original_result_ineligible']
            if not stop_pending:
                # Preserve the real cadence: wait for a new check after the unchanged 10s interval.
                await until(lambda:len(owners)>1,seconds=12);stop.set()
            await asyncio.wait_for(task,8)
            if guard:assert await asyncio.wait_for(guard,3)
            artifact=json.loads((pm.ROOT/original/'late_cleanup_v85.json').read_text());assert artifact['original_result_ineligible']
            with closing(ms.connect()) as c:
                assert c.execute('select status from position_checks_v0730 where id=?',(original,)).fetchone()[0]=='CLEANUP_FAILED'
                assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
                if not stop_pending:assert c.execute('select count(distinct id) from position_checks_v0730').fetchone()[0]>=2
            assert ms.read_state(config.DB)['status']=='STOPPED'
            exported=ms.export(config.DB,tmp_path/'export')
            with __import__('zipfile').ZipFile(exported) as z:
                manifest=json.loads(z.read('late_cleanup_manifest_v85.json'));assert any(r['check_id']==original for r in manifest['files'])
        finally:
            release.set();stop.set()
            if not task.done():await asyncio.wait_for(task,8)
            if guard and not guard.done():await asyncio.wait_for(guard,3)
            for owner in owners:
                # Also request cleanup if setup itself failed before launch_check
                # received its proc reference. Never leave a real test child running.
                owner.stop_requested.set()
                await asyncio.to_thread(owner.thread.join,3)
    asyncio.run(run())
    assert all(p.done.is_set() and not p.thread.is_alive() for p in owners)
    for key in ('net_cash','available','reserved','open_principal','total_cost','closed_net'):assert cap.summary(fresh)[key]==money[key]
    assert not fresh.execute("select 1 from trades where side='SELL'").fetchone()
    assert fresh.execute('pragma integrity_check').fetchone()[0]=='ok' and late.ACTIVE is None


def test_known_launcher_stop_retains_handles_until_signal(fresh,tmp_path,monkeypatch):
    scenario(fresh,tmp_path,monkeypatch)


def test_known_launcher_confirmation_resumes_only_new_checks(fresh,tmp_path,monkeypatch):
    scenario(fresh,tmp_path,monkeypatch,stop_pending=False)


@pytest.mark.skipif(os.name!='nt',reason='Native Windows job/process handles require Windows')
def test_native_known_launcher_owner_survives_delayed_signal(fresh,tmp_path,monkeypatch):
    scenario(fresh,tmp_path,monkeypatch,native=True)


def test_known_launcher_setup_slower_than_scaled_deadline_keeps_cleanup_scenario(fresh,tmp_path,monkeypatch):
    scenario(fresh,tmp_path,monkeypatch,creation_delay=.2)


def test_descendants_keep_job_owned_after_launcher_has_exited(tmp_path):
    release=threading.Event()
    class Group(DelayedGroup):
        def poll(self):return 0
        def active(self):return 0 if release.is_set() else 1
        def terminate(self):self.terminations+=1
    g=Group(release);trace=__import__('child_diagnostics').Trace(tmp_path,{'id':'descendants'})
    owner=wc.BackgroundChild([],tmp_path,tmp_path/'child.log',trace,lambda *args:g,retain_cleanup=True)
    async def run():
        try:
            await owner.spawned()
            with pytest.raises(RuntimeError,match='CLEANUP_UNCONFIRMED'):await owner.stop()
            assert owner.cleanup_pending_reason=='JOB_DESCENDANTS_REMAIN' and not g.closed and not owner.done.is_set()
        finally:release.set();await asyncio.to_thread(owner.thread.join,3)
    asyncio.run(run());assert g.closed and owner.done.is_set() and owner.cleanup_error is None


def test_known_launcher_cancellation_retains_owner_and_rejects_saved_result(fresh,tmp_path,monkeypatch):
    monkeypatch.setattr(pm,'ROOT',tmp_path/'checks');registry=late.Registry(config.DB);monkeypatch.setattr(late,'ACTIVE',registry)
    release=threading.Event();owners=[]
    async def create(args,out,trace):
        p=wc.BackgroundChild([],out,out/'child.log',trace,lambda *args:DelayedGroup(release));owners.append(p);return p
    monkeypatch.setattr(pm,'create_simulation_process',create)
    job={'id':'cancel-known','trade_id':'tid','mint':'mint','cancel_status':'SUPERSEDED_BY_EXIT_SIGNAL'}
    async def run():
        out=pm.ROOT/job['id'];task=asyncio.create_task(pm.launch_check(job,out,timeout=10))
        try:
            await until(lambda:owners and owners[0].spawn_done.is_set())
            (out/'result.json').write_text('{"status":"SELLABLE"}')
            task.cancel()
            with pytest.raises(RuntimeError,match='CLEANUP_UNCONFIRMED') as exc:await task
            assert registry.handled(job,exc.value) and registry.pending and not owners[0].done.is_set()
        finally:
            release.set();await asyncio.to_thread(owners[0].thread.join,3)
            if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        resolved=registry.poll();assert resolved[0]['state']=='CONFIRMED' and resolved[0]['original_result_ineligible']
        assert json.loads((out/'diagnostic.json').read_text())['status']=='CLEANUP_FAILED'
    asyncio.run(run())


def test_monitor_cause_has_priority_over_pipe_write_consequence():
    import failure_cause
    stop={'failure_v81':{'exception':{'chain':[{'message':'BRANCH_MONITOR_EXITED: exit_code=1'}]},
          'monitor_state':{'failure_v81':{'chain':[{'message':'SIMULATION_CHILD_CLEANUP_TIMEOUT'}]}}}}
    transport={'failure':{'reason':'route_branch:PIPE_WRITE_ERROR'}}
    r=failure_cause.branch(stop,transport)
    assert r['primary_cause']=='SIMULATION_CHILD_CLEANUP_TIMEOUT' and r['transport_cause']=='route_branch:PIPE_WRITE_ERROR'
    assert failure_cause.branch({},transport)['primary_cause']=='route_branch:PIPE_WRITE_ERROR'


def test_pending_handle_query_error_does_not_release_ownership(tmp_path):
    release=threading.Event();query_failed=threading.Event();owners=[]
    class Group(DelayedGroup):
        def poll(self):
            if owners and owners[0].cleanup_pending.is_set() and not release.is_set():
                query_failed.set();raise OSError(5,'temporary observation failure')
            return super().poll()
    g=Group(release);trace=__import__('child_diagnostics').Trace(tmp_path,{'id':'query'})
    owner=wc.BackgroundChild([],tmp_path,tmp_path/'child.log',trace,lambda *a:g,retain_cleanup=True);owners.append(owner)
    async def run():
        try:
            await owner.spawned()
            with pytest.raises(RuntimeError,match='CLEANUP_UNCONFIRMED'):await owner.stop()
            await until(query_failed.is_set)
            assert not g.closed and owner.thread.is_alive() and not owner.done.is_set()
        finally:release.set();await asyncio.to_thread(owner.thread.join,3)
    asyncio.run(run());assert g.closed and owner.done.is_set() and owner.cleanup_error is None


def test_stop_before_pid_publication_keeps_native_setup_owner(tmp_path,monkeypatch):
    entered=threading.Event();release=threading.Event();trace=__import__('child_diagnostics').Trace(tmp_path,{'id':'setup'})
    def factory(*args):entered.set();release.wait();return DelayedGroup(release)
    owner=wc.BackgroundChild([],tmp_path,tmp_path/'child.log',trace,factory)
    registry=late.Registry(tmp_path/'primary.db')
    monkeypatch.setattr(wc.BackgroundChild,'STOP_WAIT_SECONDS',.05)
    async def run():
        try:
            await until(entered.is_set)
            assert owner.pid is None and registry.register(owner,tmp_path,{'id':'setup'},retain_cleanup=True)
            with pytest.raises(RuntimeError,match='CLEANUP_UNCONFIRMED'):await owner.stop()
            assert registry.pending and owner.pid is None and not owner.done.is_set()
        finally:release.set();await asyncio.to_thread(owner.thread.join,3)
        result=registry.poll();assert result[0]['state']=='CONFIRMED' and result[0]['original_result_ineligible']
        assert owner.done.is_set() and owner.pid is not None
    asyncio.run(run())
