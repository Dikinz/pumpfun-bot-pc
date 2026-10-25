import asyncio,sqlite3,threading,time
import pytest
import counter_writer as cw,database as db,config

def setup_db(tmp_path):
    p=tmp_path/'counters.db'
    with sqlite3.connect(p) as c:
        c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT)')
    return p

def test_market_counters_never_touch_sql(tmp_path,monkeypatch):
    w=cw.Writer(setup_db(tmp_path),limit=3);monkeypatch.setattr(cw,'ACTIVE',w)
    monkeypatch.setattr(config,'RECORD_RAW_EVENTS',False)
    monkeypatch.setattr(db,'con',lambda:pytest.fail('market counter touched SQLite'))
    for _ in range(10000):db.event('mint','buy',1,'trader','source');db.inc('valid_price_events')
    db.inc('invalid_price_events',2)
    assert w.snapshot()['pending_keys']==3
    w.start();s=asyncio.run(w.close());assert s['committed']==20002 and s['unflushed']==0
    with sqlite3.connect(w.database) as c:assert dict(c.execute('select k,cast(v as real) from meta'))=={'events':10000.,'valid_price_events':10000.,'invalid_price_events':2.}

def test_lock_wait_off_loop_and_recorded(tmp_path,monkeypatch):
    p=setup_db(tmp_path);lock=sqlite3.connect(p,isolation_level=None);lock.execute('BEGIN IMMEDIATE')
    w=cw.Writer(p,interval=.01).start();monkeypatch.setattr(cw,'ACTIVE',w)
    async def sample():
        for _ in range(70):db.inc('events');await asyncio.sleep(.005)
    try:
        asyncio.run(sample());s=w.snapshot();assert s['busy_retries']>=1 and s['committed']==0
        e=s['last_error'];assert e['phase']=='BEGIN_IMMEDIATE' and e['sqlite_errorcode']==5
        assert e['statements']['busy_timeout_ms']==250
        assert 'INSERT INTO meta' in e['statements']['statements'][0]['sql'] and e['phase_ms']>=200
    finally:lock.rollback();lock.close()
    s=asyncio.run(w.close());assert s['committed']==70 and s['unflushed']==0
    with sqlite3.connect(p) as c:assert float(c.execute("select v from meta where k='events'").fetchone()[0])==70

def test_financial_atomicity_unchanged(tmp_path,monkeypatch):
    p=setup_db(tmp_path);c=sqlite3.connect(p,isolation_level=None,factory=db.Connection)
    c.execute("insert into meta values('cash','30')")
    monkeypatch.setattr(db,'_LOCAL',threading.local());db._LOCAL.conn=c
    w=cw.Writer(p);monkeypatch.setattr(cw,'ACTIVE',w)
    with pytest.raises(ValueError):
        with db.atomic():
            db.setm('cash',27);db.inc('realized',-1.5);db.inc('entries');db.inc('exits');raise ValueError()
    assert dict(c.execute('select * from meta'))=={'cash':'30'} and w.snapshot()['accepted']==0
    with db.atomic():db.setm('cash',27);db.inc('entries')
    assert dict(c.execute('select * from meta'))=={'cash':'27','entries':'1.0'};c.close()

def test_failed_writer_no_sync_fallback_bounded(tmp_path,monkeypatch):
    p=setup_db(tmp_path)
    with sqlite3.connect(p) as c:c.execute('drop table meta')
    w=cw.Writer(p,limit=2,interval=.01).start();monkeypatch.setattr(cw,'ACTIVE',w);db.inc('events');w.thread.join(2)
    monkeypatch.setattr(db,'con',lambda:pytest.fail('sync fallback'))
    db.inc('events');db.inc('valid_price_events');db.inc('invalid_price_events');db.inc('events')
    s=asyncio.run(w.close());assert s['status']=='FAILED_UNFLUSHED' and s['committed']==0 and s['dropped']==1
    assert s['pending_keys']<=2 and s['inflight_keys']<=2 and s['unflushed']==4

def test_batch_rollback(tmp_path):
    p=setup_db(tmp_path)
    with sqlite3.connect(p) as c:c.executescript("CREATE TRIGGER deny BEFORE INSERT ON meta WHEN NEW.k='invalid_price_events' BEGIN SELECT RAISE(ABORT,'test'); END;")
    w=cw.Writer(p);w.add('events','',table='meta');w.add('invalid_price_events','',table='meta');w.start();s=asyncio.run(w.close())
    assert s['committed']==0 and s['unflushed']==2
    with sqlite3.connect(p) as c:assert c.execute('select count(*) from meta').fetchone()[0]==0
