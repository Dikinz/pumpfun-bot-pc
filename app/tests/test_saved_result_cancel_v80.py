"""SAVE_RESULT while alive -> actual SELL preemption -> launcher/tree cleanup."""
import asyncio,json,os,sys,time,sqlite3
from pathlib import Path
from contextlib import closing
import pytest,psutil
import main,config,database as db,paper_capital as capital
import position_monitor as pm,monitor_service as ms,priority_exit as priority
import windows_child as wc,recovery_gate as rg
from test_primary_integrity import fresh
from test_p0_isolation_v79 import tree_snapshot
from native_exit_proof_v971 import ExitProbe

class SavedGroup:
    pid=123456789;created_ts=1000.;proof='INJECTED_TEST_GROUP';atomic_assignment=False
    def __init__(self,out,refuse=False):
        self.code=None;self.count=2;self.refuse=refuse;self.closed=False
        (out/'result.json').write_text(json.dumps(dict(status='CHECKED',sentinel='SAVED_BUT_NOT_AUTHORIZED')))
        (out/'progress.json').write_text(json.dumps(dict(stage='SAVE_RESULT',ts=time.time())))
    def poll(self):return self.code
    def active(self):return self.count
    def terminate(self):
        if not self.refuse:self.code=1;self.count=0
    def close(self):self.closed=True

@pytest.fixture(autouse=True)
def audit(fresh,tmp_path,monkeypatch,request):
    monkeypatch.setattr(rg,'ACTIVE',None)
    __import__('wallet_costs').ensure(fresh)
    assert main.buy('saved-result',1)[0] and main.P['saved-result']['size']==3
    capital.record(fresh,main.P['saved-result']['trade_id'],'BUY',3,{'pool_fee_rate':.0125})
    assert capital.summary(fresh)['status']=='OK'
    def integrity():
        checks={}
        for p in tmp_path.rglob('*.db'):
            with closing(sqlite3.connect(p)) as c:checks[str(p)]=c.execute('PRAGMA integrity_check').fetchone()[0]
        return checks
    before=tree_snapshot();before_db=integrity();yield
    after=tree_snapshot();after_db=integrity()
    record(dict(kind='PROCESS_AND_DATABASE_AUDIT',test=request.node.name,before=before,after=after,integrity_before=before_db,integrity_after=after_db))
    assert all(before.get(pid)==created for pid,created in after.items()),(before,after)
    assert all(v=='ok' for v in before_db.values()) and all(v=='ok' for v in after_db.values())

async def until(predicate,timeout=8):
    end=time.monotonic()+timeout
    while not predicate():
        assert time.monotonic()<end,'scenario readiness timeout'
        await asyncio.sleep(.02)

def record(evidence):
    if os.getenv('SAVED_RESULT_CANCEL_EVIDENCE'):
        with open(os.environ['SAVED_RESULT_CANCEL_EVIDENCE'],'a') as f:f.write(json.dumps(evidence)+'\n')

def scenario(fresh,tmp_path,monkeypatch,backend,refuse=False):
    out=tmp_path/'monitor';monkeypatch.setattr(pm,'ROOT',out)
    g=rg.Coordinator(config.DB,'saved-result-scenario');monkeypatch.setattr(rg,'ACTIVE',g)
    original=pm.create_simulation_process;owners=[];models=[]
    worker="""import os,json,sys,time
from pathlib import Path
from contextlib import closing
p=Path(sys.argv[1])
(p/'result.json').write_text(json.dumps(dict(status='CHECKED',sentinel='SAVED_BUT_NOT_AUTHORIZED')))
(p/'progress.json').write_text(json.dumps(dict(stage='SAVE_RESULT',ts=time.time())))
import psutil
ready=p/'worker_ready.tmp';ready.write_text(json.dumps(dict(pid=os.getpid(),created_ts=psutil.Process().create_time())));ready.replace(p/'worker_ready.json')
time.sleep(60)
"""
    wrapper="import subprocess,sys,time;subprocess.Popen([sys.executable,'-u','-c',sys.argv[1],sys.argv[2]]);time.sleep(60)"
    async def create(args,folder,trace):
        if backend=='native_windows':
            proc=await original([sys.executable,'-u','-c',wrapper,worker,str(folder)],folder,trace)
            assert isinstance(proc,wc.BackgroundChild)
        else:
            def factory(*args):
                model=SavedGroup(folder,refuse);models.append(model);return model
            proc=wc.BackgroundChild([],folder,folder/'child.log',trace,factory)
        owners.append(proc);return proc
    monkeypatch.setattr(pm,'create_simulation_process',create)
    before=capital.summary(fresh);tid=main.P['saved-result']['trade_id']
    with ms.connect() as c:
        job=pm.next_job(c,time.time());assert job['trade_id']==tid
        folder=out/job['id'];details=dict(kind='SAVE_RESULT_CANCEL_SCENARIO',backend=backend,refused_termination=refuse);probes=[];worker_id=None
        async def run():
            task=asyncio.create_task(pm.run_job(job,c))
            try:
                await until(lambda:(folder/'result.json').exists() and (folder/'progress.json').exists() and owners and owners[0].pid is not None)
                if backend=='native_windows':
                    await until(lambda:(folder/'worker_ready.json').exists() and owners[0].spawn_done.is_set())
                    worker_id=json.loads((folder/'worker_ready.json').read_text())
                    for pid,birth in [(owners[0].pid,owners[0].created_ts),(worker_id['pid'],worker_id['created_ts'])]:
                        probes.append(ExitProbe(pid,birth))
                    details['pre_cancel_handles_v971']=[dict(pid=p.pid,created_ts=p.created_ts,wait_status=258) for p in probes]
                assert json.loads((folder/'progress.json').read_text())['stage']=='SAVE_RESULT'
                assert not task.done() and not owners[0].done.is_set()
                saved_ts=time.time();signal_ts=time.time()
                fresh.execute("INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,size,reason,trade_id) VALUES('new-sell','saved-result','SELL','PENDING',?,1,3,'TP2',?)",(signal_ts,tid))
                priority.observe(c,signal_ts)
                active={'initial':(task,job)};confirmation_deadline=time.monotonic()+6;priority.preempt(c,active)
                assert job['cancel_status']=='SUPERSEDED_BY_EXIT_SIGNAL'
                if refuse:
                    with pytest.raises(RuntimeError,match='SIMULATION_CHILD_CLEANUP_TIMEOUT'):await asyncio.wait_for(task,6)
                else:
                    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,6)
                row=dict(c.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone())
                assert row['status']==('CLEANUP_FAILED' if refuse else 'SUPERSEDED_BY_EXIT_SIGNAL')
                diagnostic=json.loads((folder/'diagnostic.json').read_text());trace=diagnostic['child_diagnostics_v73']
                events=trace['events'];by={x['phase']:x for x in events}
                assert by['CHECK_CANCELLED_V80']['reason']=='SUPERSEDED_BY_EXIT_SIGNAL'
                assert by['SAVED_RESULT_AT_CANCEL_V80']['artifact']['stage']=='SAVE_RESULT'
                assert by['SAVED_RESULT_AT_CANCEL_V80']['artifact']['exists'] is True
                assert json.loads((folder/'result.json').read_text())['sentinel']=='SAVED_BUT_NOT_AUTHORIZED'
                assert not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()
                after=capital.summary(fresh)
                assert after['status']=='OK'
                for key in ('net_cash','reserved','available','open_principal','total_cost','closed_net'):
                    assert after[key]==before[key],key
                if refuse:
                    assert diagnostic['status']=='CLEANUP_FAILED'
                    assert diagnostic['worker_result']['status']=='SUPERSEDED_BY_EXIT_SIGNAL'
                    assert 'CLEANUP_CONFIRMED' not in by
                    snapshot=by['JOB_LAUNCHER_TIMEOUT_V80']['snapshot']
                    assert snapshot['job_active_processes']==2 and snapshot['launcher_returncode'] is None
                    assert c.execute("SELECT status FROM priority_exit_requests_v65 WHERE order_id='new-sell'").fetchone()[0]=='QUEUED'
                else:
                    assert diagnostic['status']=='SUPERSEDED_BY_EXIT_SIGNAL'
                    assert by['CLEANUP_CONFIRMED']['active_processes']==0
                    if backend=='native_windows':
                        await until(lambda:all(p.signaled() for p in probes),timeout=max(0,confirmation_deadline-time.monotonic()))
                        assert [(p.pid,p.expected_birth) for p in probes]==[(trace['child_pid'],trace['child_created_ts']),(worker_id['pid'],worker_id['created_ts'])]
                        details['retained_exit_proof_v971']=[p.exited() for p in probes]
                    replacement=priority.dispatch(c,time.time())
                    assert replacement and replacement['id']!=job['id'] and replacement['scheduled_ts']>=signal_ts
                    pm.finish(c,replacement,dict(status='REQUEST_ERROR'))
                details.update(kind='SAVE_RESULT_CANCEL_SCENARIO',backend=backend,refused_termination=refuse,saved_ts=saved_ts,signal_ts=signal_ts,job_started_ts=job['scheduled_ts'],outcome=row['status'],cancel_reason=job['cancel_status'],saved_result_unused=True,financial_integrity='OK',child_diagnostics=trace)
            finally:
                try:
                    if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
                    for p in owners:
                        p.stop_requested.set()
                        await asyncio.to_thread(p.thread.join,6)
                        assert not p.thread.is_alive()
                finally:
                    for probe in probes:probe.close()
        try:asyncio.run(run())
        finally:record(details)
    assert all(x.closed for x in models)

@pytest.mark.parametrize('backend',['model','native_windows'])
def test_saved_result_sell_cancels_live_tree(fresh,tmp_path,monkeypatch,backend):
    if backend=='native_windows' and os.name!='nt':pytest.skip('Exact native Windows SAVE_RESULT/SELL/tree scenario requires Windows')
    scenario(fresh,tmp_path,monkeypatch,backend)

def test_saved_result_cancel_cleanup_timeout_is_never_success(fresh,tmp_path,monkeypatch):
    scenario(fresh,tmp_path,monkeypatch,'model',refuse=True)
