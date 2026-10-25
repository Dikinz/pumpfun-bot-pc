import asyncio,io,json,queue,sqlite3,threading,time
from types import SimpleNamespace
import pytest
import branch_transport as bt,runtime_control as rt,paper_branch,system_health,config,database as db
from test_primary_integrity import fresh
from test_branch_shutdown_v541 import offline_worker
from test_runtime_delivery_v60 import publisher_without_child

def test_real_stop_stops_sender_before_control_and_confirms_identity(fresh,offline_worker,monkeypatch):
    p=bt.Publisher(config.DB);calls=[];original=rt.request_stop
    def control(primary,session):
        assert p.stopping.is_set() and p.stop_started_ts
        saved=json.loads(paper_branch.path(config.DB).with_name(paper_branch.path(config.DB).stem+'_transport_status_v60.json').read_text())
        assert saved['stop_requested'] and saved['session']==session
        calls.append(session);original(primary,session)
    monkeypatch.setattr(rt,'request_stop',control)
    p.begin_stop();time.sleep(.15)
    assert p.child.poll() is None # quiescence does not close stdin before control file
    try:r=p.close()
    finally:
        if p.child.poll() is None:bt.terminate_tree(p.child)
    ack=paper_branch.read_lifecycle(config.DB)
    assert calls==[p.session] and r['status']=='STOPPED' and r['sender_finished']
    assert ack['pid']==p.worker_pid and ack['ts']>=r['stop_started_ts']
    assert not p.error and p.first_gap is None and r['stop_pipe_error'] is None
    assert p.close() is r

def test_stop_blocks_new_frames_and_never_writes_stop_to_pipe(tmp_path):
    p=publisher_without_child(tmp_path/'p.db');p.thread=None
    class Pipe(io.StringIO):
        def close(self):pass
        def write(self,s):pytest.fail('write after begin_stop')
    p.child=SimpleNamespace(stdin=Pipe(),pid=1)
    p.queue.put({'pending':'frame'});p.begin_stop();seq=p.seq
    p.emit('m','buy',1,'t','feed',{},0);p.send()
    assert p.seq==seq and p.queue.qsize()==1 and not p.error

def test_pipe_error_before_stop_remains_error_and_after_stop_is_separate(tmp_path):
    for stopping in (False,True):
        p=publisher_without_child(tmp_path/str(stopping));p.thread=None
        class Pipe:
            def write(self,s):
                if stopping:p.begin_stop() # in-flight write interrupted by STOP
                raise BrokenPipeError(32,'closed')
            def flush(self):pass
            def close(self):pass
        p.child=SimpleNamespace(stdin=Pipe(),pid=1)
        p.emit('m','buy',1,'t','feed',{},0);p.send()
        assert p.error is (not stopping)
        if stopping:assert p.first_gap is None and p.stop_pipe_error['error_type']=='BrokenPipeError'
        else:assert p.first_gap['reason']=='PIPE_WRITE_ERROR'

@pytest.mark.parametrize('alter',[{'session':'old'},{'pid':999},{'ts':1},{'status':'FAILED'},{'monitor_forced':True}])
def test_bad_ack_never_confirms_stop(tmp_path,alter):
    p=publisher_without_child(tmp_path/'p.db');p.thread=None;p.worker_pid=123;p.begin_stop()
    a=dict(session=p.session,pid=123,status='STOPPED',ts=time.time(),monitor_forced=False)
    assert p.valid_stop_ack(a);a.update(alter);assert not p.valid_stop_ack(a)

def test_unexpected_zero_exit_still_fails_but_requested_ack_is_accepted(tmp_path,monkeypatch):
    p=publisher_without_child(tmp_path/'p.db');p.thread=None;p.worker_pid=123
    p.child=SimpleNamespace(pid=123,poll=lambda:0);f=bt.Fanout.__new__(bt.Fanout);f.publishers=[p]
    assert f.failed and f.failure_reason=='paper_branch:CHILD_EXITED'
    p.begin_stop();ack=dict(session=p.session,pid=123,status='STOPPED',ts=time.time())
    monkeypatch.setattr(paper_branch,'read_lifecycle',lambda _:ack)
    assert not f.failed
    p.error=True;assert f.failed

def test_primary_request_bound_to_active_run_and_supervisor_does_not_log_failure(fresh,monkeypatch):
    import run_audit
    run_audit.begin(fresh);session=run_audit.ACTIVE
    assert rt.request_primary_stop(config.DB)==session
    assert rt.stop_requested(config.DB,session) and not rt.stop_requested(config.DB,'old-session')
    monkeypatch.setattr(system_health,'failure',lambda *a:pytest.fail('normal request logged as failure'))
    async def stop():raise rt.RequestedStop()
    with pytest.raises(rt.RequestedStop):asyncio.run(system_health.supervise('watch',stop(),config.DB))

def test_primary_observes_request_before_failed_branch_check(fresh,monkeypatch):
    import main,types,run_audit
    stopped=threading.Event();order=[]
    async def noop(*a,**kw):return {}
    async def idle(*a,**kw):
        try:
            while True:await asyncio.sleep(1)
        finally:stopped.set()
    class Sock:
        def bind(self,*a):pass
        def listen(self,*a):pass
    class Group:
        pending=0;dropped=0
        def __init__(self,primary):rt.request_stop(primary,run_audit.ACTIVE)
        def begin_stop(self):order.append('stop')
        @property
        def failed(self):pytest.fail('observed intentional stop as branch failure')
        def refresh_positions(self):pass
        def snapshot(self):return {}
        def close(self):assert stopped.is_set() and order;order.append('closed');return []
    monkeypatch.setattr(config,'JUPITER_API_KEY','test')
    monkeypatch.setattr(main.exit_quotes,'refresh_fx',noop);monkeypatch.setattr(main.exit_quotes,'FX_STATUS','READY')
    monkeypatch.setattr(main,'socket',types.SimpleNamespace(socket=lambda *a:Sock(),AF_INET=2,SOCK_STREAM=1))
    monkeypatch.setattr(main.time_check,'startup',noop);monkeypatch.setattr(bt,'Fanout',Group)
    for name in ('heartbeat','observation_timer','pumpportal_discovery','pumpapi_trades'):monkeypatch.setattr(main,name,idle)
    for obj,name in ((main.clock_lab,'references'),(main.clock_lab,'local_watch'),(main.exit_quotes,'worker'),(main.auto_simulation,'worker'),(main.preentry,'worker'),(main.shadow_preflight,'worker')):monkeypatch.setattr(obj,name,idle)
    try:
        with pytest.raises(rt.RequestedStop):asyncio.run(main.run())
        assert order[-1]=='closed' and rt.ACTIVE is None
    finally:bt.ACTIVE=None
