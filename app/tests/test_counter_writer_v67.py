import asyncio,json,sqlite3,time
import pytest
import counter_writer as cw,candidate_lab

def database(tmp_path):
    p=tmp_path/'clean_test.db'
    with sqlite3.connect(p) as c:c.execute('PRAGMA journal_mode=WAL');c.executescript(candidate_lab.SCHEMA)
    return p

def test_tick_no_sql_and_bounded_buffer(tmp_path,monkeypatch):
    w=cw.Writer(database(tmp_path),limit=2);monkeypatch.setattr(cw,'ACTIVE',w)
    class Forbidden:
        def execute(self,*a):raise AssertionError('SQLite on market thread')
    for _ in range(10000):candidate_lab.count(Forbidden(),'STRICT_0','SIGNAL_momentum')
    w.add('a','two');w.add('a','three')
    assert w.pending[('candidate_counts_v0742','STRICT_0','SIGNAL_momentum')]==10000
    s=w.snapshot();assert s['pending_keys']==2 and s['dropped']==1 and s['committed']==0

def test_exact_flush_and_restart(tmp_path):
    p=database(tmp_path);w=cw.Writer(p,interval=.01).start()
    for _ in range(1234):w.add('STRICT_0','FEATURE_PASS')
    s=asyncio.run(w.close());assert s['status']=='STOPPED' and s['committed']==1234 and s['unflushed']==0
    w=cw.Writer(p,interval=.01).start();w.add('STRICT_0','FEATURE_PASS');asyncio.run(w.close())
    with sqlite3.connect(p) as c:assert c.execute('select n from candidate_counts_v0742').fetchone()[0]==1235
    m=json.loads(p.with_name(p.stem+'_counter_writer_v67.json').read_text());assert 'max_commit_ms' in m and 'max_begin_ms' in m

def test_lock_keeps_event_loop_responsive_and_stop_bounded(tmp_path):
    p=database(tmp_path);lock=sqlite3.connect(p,isolation_level=None);lock.execute('BEGIN IMMEDIATE')
    w=cw.Writer(p,interval=.01).start();w.add('STRICT_0','BUSY')
    async def wait():
        pulses=0;end=time.monotonic()+.4
        while time.monotonic()<end:await asyncio.sleep(.005);pulses+=1
        return pulses,await w.close()
    try:
        pulses,s=asyncio.run(wait());assert pulses>=20 and s['busy_retries']>=1
        assert s['committed']==0 and s['unflushed']==1 and s['status']=='STOPPED_UNFLUSHED' and not s['thread_alive']
    finally:lock.rollback();lock.close()

def test_retry_does_not_double_count(tmp_path):
    p=database(tmp_path);lock=sqlite3.connect(p,isolation_level=None);lock.execute('BEGIN IMMEDIATE')
    w=cw.Writer(p,interval=.01).start();w.add('v','r');end=time.monotonic()+2
    while not w.snapshot()['busy_retries'] and time.monotonic()<end:time.sleep(.01)
    lock.rollback();lock.close();s=asyncio.run(w.close());assert s['committed']==1 and s['unflushed']==0
    with sqlite3.connect(p) as c:assert c.execute('select n from candidate_counts_v0742').fetchone()[0]==1

def test_failed_writer_no_sync_fallback(tmp_path,monkeypatch):
    p=database(tmp_path)
    with sqlite3.connect(p) as c:c.execute('drop table candidate_counts_v0742')
    w=cw.Writer(p,interval=.01).start();monkeypatch.setattr(cw,'ACTIVE',w);w.add('v','r');w.thread.join(2)
    assert not w.thread.is_alive();candidate_lab.count(None,'v','r');s=asyncio.run(w.close())
    assert s['status']=='FAILED_UNFLUSHED' and s['unflushed']==2 and s['last_error']['sqlite_errorname']=='SQLITE_ERROR'
    m=json.loads(p.with_name(p.stem+'_counter_writer_v67.json').read_text());assert m['unflushed']==2

def test_batch_atomicity(tmp_path):
    p=database(tmp_path)
    with sqlite3.connect(p) as c:c.executescript("CREATE TRIGGER deny BEFORE INSERT ON candidate_counts_v0742 WHEN NEW.reason='bad' BEGIN SELECT RAISE(ABORT,'denied'); END;")
    w=cw.Writer(p);w.add('v','good');w.add('v','bad');w.start();s=asyncio.run(w.close())
    with sqlite3.connect(p) as c:assert c.execute('select count(*) from candidate_counts_v0742').fetchone()[0]==0
    assert s['unflushed']==2 and s['committed']==0


def test_clock_diagnostic_counts_share_buffer_not_clock_decisions(tmp_path,monkeypatch):
    import clock_lab
    p=database(tmp_path)
    with sqlite3.connect(p) as c:c.execute('CREATE TABLE clock_shadow_v0740(variant TEXT,reason TEXT,n INTEGER,PRIMARY KEY(variant,reason))')
    w=cw.Writer(p,interval=.01);monkeypatch.setattr(cw,'ACTIVE',w)
    clock_lab.count(None,'STRICT_0','ACCEPTED');candidate_lab.count(None,'STRICT_0','FEATURE_PASS');w.start();s=asyncio.run(w.close())
    assert s['committed']==2
    with sqlite3.connect(p) as c:
        assert c.execute('SELECT n FROM clock_shadow_v0740').fetchone()[0]==1
        assert c.execute('SELECT n FROM candidate_counts_v0742').fetchone()[0]==1
