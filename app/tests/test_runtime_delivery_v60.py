import asyncio,json,queue,sqlite3,threading,time,sys,subprocess
from pathlib import Path
import pytest
import runtime_control as rt,config,main,database as db,branch_transport as bt,paper_branch,route_branch,system_health
from test_primary_integrity import fresh

def test_input_first_gap_immutable_and_freshness_unchanged():
    now=[10.];s=rt.InputState(lambda:now[0])
    assert s.observe(dict(seq=1,ts=10.),2)
    assert s.observe(dict(seq=3,ts=10.),4)
    first=dict(s.first);assert first['reason']=='SEQUENCE_GAP' and first['expected_seq']==2 and first['received_seq']==3
    now[0]=12.;assert not s.observe(dict(seq=4,ts=10.),9)
    assert s.first==first and s.stale==1 and s.seq==4 and s.applied==3

def test_stale_first_input_does_not_become_fresh():
    s=rt.InputState(lambda:20.)
    assert not s.observe(dict(seq=1,ts=18.99),5)
    assert s.first['reason']=='STALE_INPUT'
    assert s.observe(dict(seq=2,ts=20.)) and s.first['reason']=='STALE_INPUT'

def test_byte_inbox_blocks_by_bytes_and_recovers():
    q=rt.ByteInbox(maxsize=2000,max_bytes=8);q.put_nowait(b'123456')
    with pytest.raises(queue.Full):q.put_nowait(b'abc')
    assert q.bytes==6 and q.get_nowait()==b'123456' and q.bytes==0
    q.put_nowait(b'abc');assert q.get()==b'abc'

def publisher_without_child(primary):
    p=bt.Publisher.__new__(bt.Publisher);p.engine=paper_branch;p.primary=primary;p.module='paper_branch';p.session='test'
    p.queue=queue.Queue(maxsize=2000);p.seq=0;p.dropped=0;p.error=False;p.stopping=threading.Event()
    p.byte_lock=threading.Lock();p.pending_bytes=0;p.peak_bytes=0;p.first_gap=None
    return p

def test_parent_queue_has_byte_ceiling_and_records_first_loss(tmp_path):
    p=publisher_without_child(tmp_path/'base.db')
    for _ in range(1000):p.emit('m','buy',1,'t','feed',dict(padding='x'*8192),0)
    assert p.pending_bytes<=4*1024*1024 and p.dropped>0
    first=dict(p.first_gap);assert first['reason']=='QUEUE_BYTE_LIMIT'
    p.emit('m','buy',1,'t','feed',dict(padding='x'*70000),0)
    assert p.error and p.first_gap==first

def test_wrong_stop_file_does_not_stop_current_session(tmp_path):
    p=tmp_path/'base.db';rt.request_stop(p,'old')
    assert not rt.stop_requested(p,'current')
    rt.atomic_json(rt.control_path(p,'current'),dict(control='STOP',session='wrong'))
    assert not rt.stop_requested(p,'current')
    rt.request_stop(p,'current');assert rt.stop_requested(p,'current')

def test_telemetry_batch_is_exact_and_does_not_buffer_capital(fresh,monkeypatch):
    clock=[1.];monkeypatch.setattr(rt,'time',type('Clock',(),{'monotonic':staticmethod(lambda:clock[0])})())
    t=rt.MarketTelemetry();before=db.getm('cash');statements=[];fresh.set_trace_callback(statements.append)
    for i in range(1000):t.received(100+i/1000);t.market(100+i/1000);t.flush(db)
    t.flush(db,True);fresh.set_trace_callback(None)
    assert float(db.getm('pumpapi_messages'))==1000 and float(db.getm('trade_last_market_ts'))==pytest.approx(100.999)
    assert db.getm('cash')==before and len([x for x in statements if x.startswith('BEGIN')])==2

def test_probe_captures_blocked_stack_without_locals_or_params(tmp_path):
    p=rt.Probe(tmp_path/'base.db');p.pulse=time.monotonic()-5;p.stage='SQL_SELECT'
    out=p.inspect();assert out['stalls']==1 and out['first_stall']['stage']=='SQL_SELECT'
    assert out['first_stall']['frames'] and set(out['first_stall']['frames'][0])=={'file','function','line'}
    assert p.inspect()['stalls']==1

def test_empty_scenarios_do_not_open_monitor_connections(fresh,monkeypatch):
    import monitor_service
    def fail(*a,**kw):raise AssertionError('unneeded monitor connection')
    monkeypatch.setattr(monitor_service,'connect',fail)
    paper_branch.poll(main);route_branch.poll(main)

def test_unconfirmed_shutdown_cannot_hide_unknown_primary(fresh):
    import diagnostics
    from test_stop_consistency_v57 import running,recorder_stop
    running(fresh);recorder_stop(0,'USER_STOP')
    state=system_health.read_state(config.DB);state['shutdown_confirmed']=False;system_health.write_state(config.DB,state)
    assert diagnostics.summary(fresh)['runtime_state']=='UNKNOWN'

def test_shutdown_requires_both_branch_ack_and_primary_checkpoint(fresh):
    db.setm('runtime_state_v41','STOPPED')
    for engine in (paper_branch,route_branch):
        engine.write_lifecycle(config.DB,dict(session='s',status='STOPPED',monitor_forced=False))
        engine.write_transport(config.DB,dict(session='s',status='STOPPED',exit_code=0,forced=False))
    assert rt.shutdown_status(config.DB,dict(forced=False,exit_code=0))['confirmed']
    assert not rt.shutdown_status(config.DB,dict(forced=True,exit_code=0))['confirmed']
    route_branch.write_transport(config.DB,dict(session='old',status='STOPPED',exit_code=0,forced=False))
    assert not rt.shutdown_status(config.DB,dict(forced=False,exit_code=0))['confirmed']

def test_external_stop_bypasses_real_backlog_and_idle_monitor_not_spawned(fresh,monkeypatch):
    import candidate_lab
    real=subprocess.Popen
    script='''import asyncio,sys,json,config,time
for k,v in json.loads(sys.argv[4]).items():setattr(config,k,v)
import exit_quotes
async def fx():exit_quotes.FX_STATUS='READY'
exit_quotes.refresh_fx=fx
import main,paper_branch
# Actual pipe/reader/scheduler/heartbeat/STOP; deterministic slow callback, no market/RPC.
def busy(*args):time.sleep(.015)
main.process_trade=busy
asyncio.run(paper_branch.worker(sys.argv[1],sys.argv[2],sys.argv[3]))
'''
    def launch(cmd,**kw):
        if len(cmd)>2 and cmd[1]=='paper_branch.py' and cmd[2]=='--worker':
            values=json.dumps({k:getattr(config,k) for k in candidate_lab.PARAMETERS})
            return real([sys.executable,'-c',script,*cmd[3:],values],**kw)
        return real(cmd,**kw)
    monkeypatch.setattr(bt.subprocess,'Popen',launch)
    p=bt.Publisher(config.DB)
    try:
        for _ in range(800):p.emit('m','buy',1,'t','test',{},time.time())
        time.sleep(.08);started=time.monotonic();out=p.close()
        assert out['status']=='STOPPED' and not out['forced'] and time.monotonic()-started<4
        assert not __import__('monitor_service').paths(paper_branch.path(config.DB))['db'].exists()
        report=rt.read(paper_branch.path(config.DB));assert report.get('input',{}).get('applied_seq',0)<800
    finally:
        if p.child.poll() is None:bt.terminate_tree(p.child)
        bt.LAST=None

def test_sql_stall_scopes_do_not_change_rollback(fresh):
    p=rt.Probe(config.DB);old=rt.ACTIVE;rt.ACTIVE=p
    try:
        with pytest.raises(RuntimeError):
            with db.atomic():db.setm('rollback_v60','x');raise RuntimeError('rollback')
        assert db.getm('rollback_v60','missing')=='missing' and 'SQL_INSERT' in p.samples
    finally:rt.ACTIVE=old

def test_child_stall_is_detected_without_waiting_for_process_exit(tmp_path,monkeypatch):
    p=publisher_without_child(tmp_path/'base.db');p.created_mono=time.monotonic()-40
    p.child=type('Child',(),{'pid':123,'poll':lambda self:None})()
    rt.atomic_json(rt.status_path(paper_branch.path(p.primary)),dict(ts=time.time(),loop_pulse_age_s=16,session=p.session,pid=123,state='RUNNING'))
    group=bt.Fanout.__new__(bt.Fanout);group.publishers=[p]
    assert not group.failed and p.first_gap is None
    p.watchdog.pending['mono']-=3
    assert group.failed and p.first_gap['reason']=='CHILD_WATCHDOG_CONFIRMED'
    assert p.first_gap['watchdog']['trigger']['snapshot']['loop_pulse_age_s']==16

def test_primary_background_tasks_cancel_before_branch_close(fresh,monkeypatch):
    import types
    stopped=threading.Event()
    async def noop(*a,**kw):return {}
    async def idle(*a,**kw):
        try:
            while True:await asyncio.sleep(1)
        finally:stopped.set()
    class Sock:
        def bind(self,*a):pass
        def listen(self,*a):pass
    class Group:
        pending=0;dropped=0;failed=True
        def __init__(self,*a):pass
        def refresh_positions(self):pass
        def snapshot(self):return {}
        def close(self):assert stopped.is_set();return []
    monkeypatch.setattr(config,'JUPITER_API_KEY','test')
    monkeypatch.setattr(main.exit_quotes,'refresh_fx',noop);monkeypatch.setattr(main.exit_quotes,'FX_STATUS','READY')
    monkeypatch.setattr(main,'socket',types.SimpleNamespace(socket=lambda *a:Sock(),AF_INET=2,SOCK_STREAM=1))
    monkeypatch.setattr(main.time_check,'startup',noop);monkeypatch.setattr(bt,'Fanout',Group)
    for name in ('heartbeat','observation_timer','pumpportal_discovery','pumpapi_trades'):monkeypatch.setattr(main,name,idle)
    for obj,name in ((main.clock_lab,'references'),(main.clock_lab,'local_watch'),(main.exit_quotes,'worker'),(main.auto_simulation,'worker'),(main.preentry,'worker'),(main.shadow_preflight,'worker')):monkeypatch.setattr(obj,name,idle)
    try:
        with pytest.raises(RuntimeError,match='PAPER_SCENARIO_FAILED'):asyncio.run(main.run())
        assert stopped.is_set() and rt.ACTIVE is None
    finally:bt.ACTIVE=None


def test_runtime_sqlite_lock_budget_is_bounded_and_financial_write_rolls_back(fresh):
    db.runtime_mode();assert fresh.execute('PRAGMA busy_timeout').fetchone()[0]==250
    other=sqlite3.connect(config.DB,isolation_level=None);other.execute('BEGIN IMMEDIATE')
    before=dict(main.P);started=time.monotonic()
    try:
        with pytest.raises(SystemExit,match='rolled back'):main.buy('locked-entry',1)
        assert time.monotonic()-started<2 and main.P==before
    finally:other.rollback();other.close()
    assert not fresh.execute("SELECT 1 FROM trades WHERE mint='locked-entry'").fetchone()
