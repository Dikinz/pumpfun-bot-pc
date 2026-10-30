import asyncio,json,sqlite3,threading,time
from pathlib import Path
import pytest
import counter_writer as cw,time_order_audit as audit,roadmap_progress as roadmap
from test_primary_integrity import fresh


def clock_db(tmp_path):
    p=tmp_path/'clock.db'
    with sqlite3.connect(p) as c:
        c.execute('PRAGMA journal_mode=WAL');c.executescript(audit.SCHEMA)
    return p


def event(value):
    return dict(provider_lag_ms=value,timestamp_input_state='VALID' if value is not None else 'MISSING')


def test_clock_aggregation_matches_synchronous_reference_under_real_writer_lock(tmp_path,monkeypatch):
    p=clock_db(tmp_path);reference=sqlite3.connect(':memory:');reference.executescript(audit.SCHEMA)
    events=[event(x) for x in [1,7,None,-3,1500,4]*20]
    for e in events:audit.clock(reference,e,now=120)
    expected=reference.execute('select minute,bucket,n,min_ms,max_ms,sum_ms from clock_minutes_v0739 order by bucket').fetchall()
    blocker=sqlite3.connect(p,isolation_level=None);blocker.execute('BEGIN IMMEDIATE')
    w=cw.Writer(p,interval=.01).start();monkeypatch.setattr(cw,'ACTIVE',w)
    class Forbidden:
        def execute(self,*args):pytest.fail('clock diagnostics performed SQL on event loop')
    ticks=[]
    async def feed():
        for e in events:
            audit.clock(Forbidden(),e,now=120);ticks.append(time.monotonic());await asyncio.sleep(.004)
    try:
        asyncio.run(feed())
        assert len(ticks)==120 and max(b-a for a,b in zip(ticks,ticks[1:]))<.2
        assert w.snapshot()['busy_retries']>=1 and w.snapshot()['committed']==0
    finally:blocker.rollback();blocker.close()
    state=asyncio.run(w.close());assert state['committed']==120 and state['unflushed']==0 and state['dropped']==0
    with sqlite3.connect(p) as c:
        assert c.execute('select minute,bucket,n,min_ms,max_ms,sum_ms from clock_minutes_v0739 order by bucket').fetchall()==expected
        assert all(json.loads(r[0])['processed_ts']==120 for r in c.execute('select last_json from clock_minutes_v0739'))


def test_clock_writer_buffer_bound_and_visible_loss_without_sync_fallback(tmp_path,monkeypatch):
    w=cw.Writer(clock_db(tmp_path),limit=2);monkeypatch.setattr(cw,'ACTIVE',w)
    for i in range(10000):audit.clock(None,event(1),now=120)
    audit.clock(None,event(None),now=120);audit.clock(None,event(2),now=180)
    assert len(w.pending)==2 and w.snapshot()['unflushed']==10001 and w.snapshot()['dropped']==1


def test_hung_diagnostic_driver_retains_writer_and_does_not_block_market_loop(tmp_path,monkeypatch):
    entered=threading.Event();release=threading.Event()
    class Hung(sqlite3.Connection):
        def executemany(self,*args):
            entered.set();release.wait(5);return super().executemany(*args)
    p=clock_db(tmp_path)
    w=cw.Writer(p,interval=.01,connect=lambda *a,**k:sqlite3.connect(*a,factory=Hung,**k))
    monkeypatch.setattr(cw,'ACTIVE',w);audit.clock(None,event(1),now=120);w.start()
    try:
        assert entered.wait(2)
        async def run():
            for _ in range(50):audit.clock(None,event(2),now=120);await asyncio.sleep(.002)
            return await w.close(timeout=.01)
        state=asyncio.run(run())
        assert state['thread_alive'] and state['unflushed']==51 and cw.ACTIVE is w
        assert state['active_sql']['phase']=='WRITE_BATCH'
    finally:release.set();state=asyncio.run(w.close())
    assert not state['thread_alive'] and state['committed']==51


def test_failure_injection_marker_follows_disposable_report_not_production(tmp_path,monkeypatch):
    live=tmp_path/'live/acceptance_v90.json';live.parent.mkdir();live.write_text('{"keep":true}')
    monkeypatch.setattr(roadmap,'acceptance_path',lambda:live)
    target=tmp_path/'disposable/recovery.json'
    roadmap.write_acceptance(dict(version='test',passed=False),target)
    assert live.read_text()=='{"keep":true}'
    assert json.loads((target.parent/live.name).read_text())['passed'] is False


def test_transport_failure_overrides_stale_running_lifecycle_and_false_no_gap(fresh,monkeypatch):
    import campaign_view,config,route_branch
    c=fresh;c.row_factory=sqlite3.Row;c.executescript(route_branch.SCHEMA)
    c.execute("insert into branch_info values('route_fork_trade_id','0')")
    c.execute("insert into branch_info values('route_carried_ids','[]')")
    c.execute("insert or replace into meta values('branch_session','session')")
    failure=dict(reason='route_branch:EVENT_LOOP_STALLED',ts=123)
    monkeypatch.setattr(route_branch,'read_lifecycle',lambda primary,transport=False:dict(session='session',status='FAILED',failure=failure,forced=True) if transport else dict(session='session',status='RUNNING'))
    row=campaign_view.branch(c,config.DB,'route',time.time())
    assert row['runtime']['state']=='FAILED' and row['input_gap'] is True
    assert row['first_gap']['source']=='PARENT_TRANSPORT'
    assert 'EVENT_LOOP_STALLED' in row['blocks'][0]


def test_active_sql_snapshot_survives_until_driver_returns(tmp_path,monkeypatch):
    import config,database,runtime_control as rt
    p=rt.Probe(tmp_path/'sql.db');monkeypatch.setattr(rt,'ACTIVE',p)
    monkeypatch.setattr(config,'DB',str(tmp_path/'sql.db'))
    c=sqlite3.connect(':memory:',factory=database.Connection);c.runtime_busy_timeout_ms=250
    def blocking_model():
        value=p.inspect()['sql_v91'];assert value['operation']=='SQL_INSERT' and value['busy_timeout_ms']==250
        assert value['database']=='sql.db' and value['age_s']>=0
    c.logged('SQL_INSERT',blocking_model)
    assert p.inspect()['sql_v91'] is None
    c.close()
