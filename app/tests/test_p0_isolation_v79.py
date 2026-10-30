"""P0: disposable 3 PAPER positions, actual scheduler/pipes/processes, no RPC."""
import asyncio,json,os,sqlite3,subprocess,sys,threading,time,types
from pathlib import Path
import pytest,psutil
import config,main,database as db,branch_transport as bt,branch_isolation as bi
import position_monitor as pm,monitor_service as ms,paper_branch,route_branch
from test_primary_integrity import fresh

def tree_snapshot():
    pid=os.getpid()
    if os.name!='nt':
        # /proc can expose host PIDs while getpid() returns a nested namespace PID.
        line=next((s for s in Path('/proc/self/status').read_text().splitlines() if s.startswith('NSpid:')),None)
        if line:pid=int(line.split()[1])
    root=psutil.Process(pid)
    assert root.name().lower().startswith('python'),root.name()
    rows={}
    for p in root.children(recursive=True):
        try:
            if p.is_running() and p.status()!=psutil.STATUS_ZOMBIE:rows[p.pid]=p.create_time()
        except psutil.NoSuchProcess:pass
    return rows

@pytest.fixture(autouse=True)
def audit(fresh,tmp_path,monkeypatch):
    monkeypatch.setattr(__import__('recovery_gate'),'ACTIVE',None)
    __import__('clean_campaign').ensure(fresh,config.DB);__import__('wallet_costs').ensure(fresh)
    assert main.buy('open-v79',1)[0] and main.P['open-v79']['size']==3
    before=tree_snapshot()
    def integrity():
        checks={}
        for p in tmp_path.rglob('*.db'):
            with sqlite3.connect(p) as c:checks[str(p)]=c.execute('PRAGMA integrity_check').fetchone()[0]
        return checks
    prior=integrity();assert all(x=='ok' for x in prior.values())
    yield
    after=tree_snapshot();remaining={k:v for k,v in after.items() if before.get(k)!=v};checks=integrity()
    evidence=dict(test=tmp_path.name,before=before,after=after,still_running=remaining,integrity_before=prior,integrity_after=checks)
    (tmp_path/'p0_audit.json').write_text(json.dumps(evidence))
    if os.getenv('P0_CHECK_EVIDENCE'):
        with open(os.environ['P0_CHECK_EVIDENCE'],'a') as f:f.write(json.dumps(evidence)+'\n')
    assert not remaining,remaining
    assert all(x=='ok' for x in checks.values())

@pytest.fixture
def process_factory(monkeypatch):
    import candidate_lab
    real=subprocess.Popen
    script='''import sys,asyncio,json,time,config,os
for k,v in json.loads(sys.argv[5]).items():setattr(config,k,v)
import paper_branch,route_branch,exit_quotes,subprocess
async def fx():
 exit_quotes.FX_STATUS='READY';exit_quotes.FX=dict(rate='100',ts=time.time(),response={})
async def idle():
 while True:await asyncio.sleep(1)
exit_quotes.refresh_fx=fx;exit_quotes.worker=idle
class Monitor:
 stdin=type('Pipe',(),{'close':lambda self:None})()
 def __init__(self,*a,**kw):pass
 def poll(self):return None
 def wait(self,*a,**kw):return 0
subprocess.Popen=Monitor
async def inject():
 from pathlib import Path
 f=Path(sys.argv[2]+'.exit-'+sys.argv[1])
 while not f.exists():await asyncio.sleep(.01)
 os._exit(0)
async def run():
 task=asyncio.create_task(inject())
 try:await paper_branch.worker(sys.argv[2],sys.argv[3],sys.argv[4],scenario=route_branch if sys.argv[1]=='route_branch' else None)
 finally:task.cancel()
asyncio.run(run())
'''
    def spawn(cmd,**kw):
        if len(cmd)>2 and cmd[1] in ('paper_branch.py','route_branch.py') and cmd[2]=='--worker':
            values=json.dumps({k:getattr(config,k) for k in candidate_lab.PARAMETERS})
            return real([sys.executable,'-c',script,cmd[1][:-3],*cmd[3:],values],**kw)
        return real(cmd,**kw)
    monkeypatch.setattr(bt.subprocess,'Popen',spawn)

async def until(predicate,timeout=5):
    end=time.monotonic()+timeout
    while not predicate():
        assert time.monotonic()<end,'condition deadline'
        await asyncio.sleep(.02)

def main_harness(monkeypatch,driver):
    async def noop(*a,**kw):return {}
    async def idle(*a,**kw):
        while True:await asyncio.sleep(.02)
    class Sock:
        def bind(self,*a):pass
        def listen(self,*a):pass
    monkeypatch.setattr(config,'JUPITER_API_KEY','offline')
    monkeypatch.setattr(main.exit_quotes,'refresh_fx',noop);monkeypatch.setattr(main.exit_quotes,'FX_STATUS','READY')
    monkeypatch.setattr(main,'socket',types.SimpleNamespace(socket=lambda *a:Sock(),AF_INET=2,SOCK_STREAM=1))
    monkeypatch.setattr(main.time_check,'startup',noop)
    beats=[]
    async def beat():
        while True:beats.append(time.monotonic());db.setm('heartbeat_ts',time.time());await asyncio.sleep(.02)
    monkeypatch.setattr(main,'heartbeat',beat)
    for name in ('observation_timer','pumpportal_discovery'):monkeypatch.setattr(main,name,idle)
    monkeypatch.setattr(main,'pumpapi_trades',driver)
    for obj,name in ((main.clock_lab,'references'),(main.clock_lab,'local_watch'),(main.exit_quotes,'worker'),(main.auto_simulation,'worker'),(main.preentry,'worker'),(main.shadow_preflight,'worker')):monkeypatch.setattr(obj,name,idle)
    return beats

@pytest.mark.parametrize('module',['paper_branch','route_branch'])
@pytest.mark.parametrize('fault',['exit','pipe'])
def test_branch_failure_is_contained_in_actual_primary_loop(fresh,monkeypatch,process_factory,module,fault):
    inspected=threading.Event();original=bt.Fanout.isolate_failures
    def inspection(group):
        r=original(group);inspected.set();return r
    monkeypatch.setattr(bt.Fanout,'isolate_failures',inspection)
    observed={}
    async def driver(*args):
        group=bt.ACTIVE;p=next(p for p in group.publishers if p.module==module)
        await until(inspected.is_set)
        Path(str(Path(config.DB).resolve())+'.exit-'+module).touch()
        await until(lambda:p.child.poll() is not None);assert p.child.returncode==0
        if fault=='pipe':
            # Worker exit closes the read end; write end stays open, raising real BrokenPipeError.
            for _ in range(40):p.emit('m','buy',1,'t','offline',{},time.time())
            await until(lambda:p.error)
        await until(lambda:getattr(p,'failure',None) is not None)
        other=next(x for x in group.publishers if x is not p)
        assert other.child.poll() is None and not other.stopping.is_set() and not other.error
        assert main.P['open-v79']['size']==3
        before=len(beats);await asyncio.sleep(.15);assert len(beats)>before
        main.sell('open-v79',1.1,'P0_SELL_AFTER_BRANCH_FAILURE');assert not main.P
        if fault=='pipe':
            snap=p.snapshot();assert snap['first_gap']['reason']=='PIPE_WRITE_ERROR'
            assert snap['unsent']+snap['delivery_uncertain']+snap['dropped']==40
        observed.update(failure=p.failure,other_running=True)
        raise __import__('runtime_control').RequestedStop()
    beats=main_harness(monkeypatch,driver)
    try:
        with pytest.raises(__import__('runtime_control').RequestedStop):asyncio.run(main.run())
        assert observed['other_running'] and observed['failure']['status']=='FAILED' and len(beats)>2
    finally:bt.ACTIVE=None;bt.LAST=None

def test_branch_outage_marks_comparison_incomplete(fresh,tmp_path,monkeypatch):
    from test_shared_lots_v64 import entry
    row=entry(fresh,monkeypatch);primary=config.DB
    bi.record(primary,dict(session='outage',module='route_branch',ts=time.time()-30,status='FAILED',reason='CHILD_EXITED'))
    main.sell('m',1.1,'TEST');out=tmp_path/'export';out.mkdir()
    rows=__import__('shared_lots').export(primary,out)
    r=next(x for x in rows if x['role']=='route' and x['trade_id']==row['trade_id'])
    assert not r['eligible_pair'] and not r['complete_pair'] and r['exclusion_reason']=='BRANCH_UNAVAILABLE'

def test_primary_fatal_still_stops_branches_and_persists_position(fresh,monkeypatch,process_factory):
    async def driver(*args):await asyncio.sleep(.2);raise RuntimeError('INJECTED_PRIMARY_FATAL')
    main_harness(monkeypatch,driver)
    try:
        with pytest.raises(RuntimeError,match='INJECTED_PRIMARY_FATAL'):asyncio.run(main.run())
        for engine in (paper_branch,route_branch):
            ack=engine.read_lifecycle(config.DB)
            assert ack['status']=='STOPPED' and ack['reason']=='PARENT_STOP',ack
        tid=main.P['open-v79']['trade_id'];main.P.clear();main.restore_positions()
        assert main.P['open-v79']['trade_id']==tid and fresh.execute('SELECT size FROM positions').fetchone()[0]==3
        import run_audit
        balance=run_audit.balance(fresh);cohorts=run_audit.cohorts(fresh,run_audit.max_id(fresh),run_audit.max_id(fresh),balance,balance)
        assert cohorts['carried']['positions_at_start']==1 and cohorts['new']['entries']==0
    finally:bt.ACTIVE=None;bt.LAST=None

def test_hung_check_worker_hits_deadline_and_leaves_no_orphans(fresh,tmp_path,monkeypatch):
    original=pm.create_simulation_process
    async def hung(args,out,trace):return await original([sys.executable,'-c','import time;time.sleep(60)'],out,trace)
    monkeypatch.setattr(pm,'create_simulation_process',hung)
    job=pm.next_job(fresh,time.time());start=time.monotonic();result=asyncio.run(pm.launch_check(job,tmp_path/'hung'))
    assert result['status']=='WORKER_TIMEOUT' and result['deadline_elapsed_s']<=11 and time.monotonic()-start<=11
    assert result['child_diagnostics_v73']['cleanup_phase']=='CLEANUP_CONFIRMED'

def test_hung_worker_does_not_starve_other_position(fresh,monkeypatch):
    assert main.buy('healthy-v79',1)[0]
    stop=threading.Event();healthy=[]
    async def work(job,c):
        if job['mint']=='open-v79':await asyncio.sleep(60)
        else:
            healthy.append(time.monotonic());await asyncio.sleep(.03);pm.finish(c,job,dict(status='REQUEST_ERROR'))
            if len(healthy)>=3:stop.set()
    monkeypatch.setattr(pm,'run_job',work)
    asyncio.run(asyncio.wait_for(ms.run(config.DB,0,stop_event=stop),25))
    assert len(healthy)>=3 and all(7<=b-a<=13 for a,b in zip(healthy,healthy[1:]))
    c=ms.connect(readonly=True)
    try:
        assert not c.execute("SELECT 1 FROM monitor_events_v0731 WHERE kind='MONITOR_LOOP_GAP'").fetchone()
        beats=[r['ts'] for r in map(json.loads,ms.paths(config.DB)['journal'].read_text().splitlines()) if r['kind']=='HEARTBEAT']
        assert len(beats)>=3 and max(b-a for a,b in zip(beats,beats[1:]))<=11
        for row in c.execute("SELECT details_json FROM monitor_events_v0731 WHERE kind='MISSED_CHECK'"):
            assert json.loads(row[0])['mint']!='healthy-v79'
    finally:c.close()

def test_monitor_loop_stall_alert(fresh):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0])
    ms.publish(dict(status='RUNNING',monotonic_ts=100.,heartbeat_ts=time.time()),config.DB)
    assert watch.sample()['status']=='RUNNING'
    now[0]=115.9;assert watch.sample()['status']=='RUNNING'
    now[0]=116.;result=watch.sample();assert result['status']=='MONITOR_NOT_RUNNING'
    assert result['snapshot']['monotonic_ts']==100. and result['age_s']==16
    now[0]=120.;assert watch.sample()['status']=='MONITOR_NOT_RUNNING'
    ms.publish(dict(status='RUNNING',monotonic_ts=120.,heartbeat_ts=time.time()),config.DB)
    assert watch.sample()['status']=='RUNNING'

def test_check_started_before_signal_stays_ineligible_after_timeout(fresh):
    import signal_exit
    from test_route_exit_v59 import candidate
    tid=main.P['open-v79']['trade_id'];row,job,result,inv=candidate(tid);signal=time.time()-1
    row['status']='WORKER_TIMEOUT';assert signal_exit.eligibility(row,job,result,signal,None,inv)=='STARTED_BEFORE_SIGNAL'
    row['status']='SELLABLE';row['started_ts']=signal+.01;row['finished_ts']=signal+.2;job['scheduled_ts']=signal+.01;result['snapshot_received_ts']=signal+.1
    assert signal_exit.eligibility(row,job,result,signal,None,inv)=='ELIGIBLE'
    j=pm.next_job(fresh,time.time());pm.finish(fresh,j,dict(status='WORKER_TIMEOUT'))
    assert fresh.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(j['id'],)).fetchone()[0]=='WORKER_TIMEOUT'

def test_failed_comparison_branch_can_have_confirmed_writer_shutdown(fresh):
    import runtime_control as rt
    db.setm('runtime_state_v41','STOPPED')
    for engine in (paper_branch,route_branch):
        engine.write_lifecycle(config.DB,dict(session='s',status='STOPPED',monitor_forced=False))
        engine.write_transport(config.DB,dict(session='s',status='FAILED',exit_code=0,forced=False,failure={'reason':'PIPE_WRITE_ERROR'},cleanup_confirmed=True))
    assert rt.shutdown_status(config.DB,dict(exit_code=0,forced=False))['confirmed']
    paper_branch.write_transport(config.DB,dict(session='s',status='FAILED',exit_code=0,forced=False,failure={'reason':'CHILD_EXITED'},cleanup_confirmed=False))
    assert not rt.shutdown_status(config.DB,dict(exit_code=0,forced=False))['confirmed']

def test_controlled_injection_is_session_scoped_and_rejects_stale_state(fresh):
    import runtime_control as rt
    primary=config.DB;session='fault-session'
    rt.atomic_json(rt.status_path(primary),dict(state='RUNNING',ts=time.time(),branches=[dict(module='paper_branch',session=session)]))
    result=bi.inject_stop(primary);assert result['session']==session
    assert rt.stop_requested(paper_branch.path(primary),session) and not rt.stop_requested(route_branch.path(primary),session) and not rt.stop_requested(primary,session)
    rt.atomic_json(rt.status_path(primary),dict(state='RUNNING',ts=time.time()-4,branches=[dict(module='paper_branch',session='stale')]))
    with pytest.raises(RuntimeError,match='NOT_FRESH'):bi.inject_stop(primary)

@pytest.mark.skipif(os.name!='nt',reason='Native Windows descendant timeout needs Windows 10+')
def test_windows_timeout_kills_descendants(fresh,tmp_path,monkeypatch):
    original=pm.create_simulation_process
    code="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);time.sleep(60)"
    async def create(args,out,trace):return await original([sys.executable,'-c',code],out,trace)
    monkeypatch.setattr(pm,'create_simulation_process',create)
    result=asyncio.run(pm.launch_check(pm.next_job(fresh,time.time()),tmp_path/'tree',timeout=2))
    assert result['status']=='WORKER_TIMEOUT' and result['child_diagnostics_v73']['cleanup_phase']=='CLEANUP_CONFIRMED'


def test_controlled_branch_stop_keeps_failure_and_confirms_cleanup(fresh,monkeypatch,process_factory):
    observed={}
    async def driver(*args):
        group=bt.ACTIVE;p=group.publishers[0]
        await until(lambda:__import__('runtime_control').read(config.DB).get('branches'))
        request=bi.inject_stop(config.DB);assert request['session']==p.session
        await until(lambda:getattr(p,'failure',None) is not None)
        await until(lambda:p.closed is not None)
        assert p.closed['status']=='FAILED' and p.closed['cleanup_confirmed'] is True
        assert group.publishers[1].child.poll() is None
        observed['confirmed']=True
        raise __import__('runtime_control').RequestedStop()
    main_harness(monkeypatch,driver)
    try:
        with pytest.raises(__import__('runtime_control').RequestedStop):asyncio.run(main.run())
        assert observed['confirmed']
    finally:bt.ACTIVE=None;bt.LAST=None
