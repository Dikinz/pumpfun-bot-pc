import asyncio,json,sqlite3,subprocess,sys,time
from pathlib import Path
import pytest
import shutdown_audit as audit

def db(tmp_path):
    p=tmp_path/'clean_0762.db'
    with sqlite3.connect(p) as c:c.execute('create table t(x)');c.execute('insert into t values(1)')
    return p

def test_read_only_probe_and_corrupt_database_are_separate(tmp_path):
    p=db(tmp_path);before=p.read_bytes();bad=p.with_name(p.stem+'_route_paper.db');bad.write_bytes(b'broken')
    r=audit.probe(p,'before');by={x['database']:x for x in r['databases']}
    assert by[p.name]['status']=='OK' and by[bad.name]['status']=='CORRUPT'
    assert by[bad.name]['error']['sqlite_errorname']=='SQLITE_NOTADB' and p.read_bytes()==before

def test_actual_probe_subprocess_has_explicit_phase(tmp_path):
    p=db(tmp_path);r=audit.collect(p,'before')
    assert r['phase']=='before' and r['databases'][0]['status']=='OK'
    assert r['probe_pid']!=__import__('os').getpid()

def test_unknown_or_alive_pid_never_confirms_post_writer_phase(tmp_path,monkeypatch):
    p=db(tmp_path);a=audit.Audit(p);a.processes[(123,42)]=dict(pid=123,created_ts=42,role='bot')
    class Proc:
        def create_time(self):return 42
        def status(self):return 'running'
    import psutil
    monkeypatch.setattr(psutil,'Process',lambda pid:Proc())
    a.before=audit.probe(p,'before');r=a.finish({},dict(bot={'forced':True}))
    assert not r['all_observed_writers_finished'] and not r['after_is_after_confirmed_writers']
    assert r['processes'][0]['status']=='STILL_RUNNING'

def test_complete_exit_needs_current_runtime_and_connection_acks(tmp_path,monkeypatch):
    import psutil,os
    p=db(tmp_path);a=audit.Audit(p);now=time.time()
    a.processes[(123,now)]=dict(pid=123,created_ts=now,role='bot')
    def absent(pid):raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil,'Process',absent)
    audit.save(p.with_name(p.stem+'_runtime_v60.json'),dict(pid=123,started_ts=now))
    audit.save(p.with_name(p.stem+'_connection_close_v68.json'),dict(pid=123,ts=now,status='CLOSED'))
    audit.save(p.with_name(p.stem+'_counter_writer_stop_v68.json'),dict(pid=123,ts=now,confirmed=True))
    audit.save(p.with_name(p.stem+'_launcher_connection_close_v68.json'),dict(pid=os.getpid(),ts=now,status='CLOSED'))
    a.before=audit.probe(p,'before');r=a.finish({},dict(bot={'forced':False}))
    assert r['all_observed_writers_finished'] and r['integrity_phases_ok']
    # A stale ack cannot attest this session's closure.
    audit.save(p.with_name(p.stem+'_counter_writer_stop_v68.json'),dict(pid=123,ts=now-100,confirmed=True))
    assert not a.finish({}, {})['writer_acks_complete']

def test_connection_close_does_not_reopen_or_modify_financial_rows(tmp_path,monkeypatch):
    import database as database,config
    p=db(tmp_path);c=sqlite3.connect(p);monkeypatch.setattr(database._LOCAL,'conn',c,raising=False)
    result=audit.closed(p,'test','session')
    assert result['status']=='CLOSED' and database._LOCAL.conn is None
    with pytest.raises(sqlite3.ProgrammingError):c.execute('select 1')
    with sqlite3.connect(p) as other:assert other.execute('select x from t').fetchone()[0]==1


def test_counter_stop_ack_matches_actual_flush(tmp_path):
    import counter_writer as cw,candidate_lab
    p=db(tmp_path)
    with sqlite3.connect(p) as c:c.executescript(candidate_lab.SCHEMA)
    w=cw.Writer(p,interval=.01,session='s').start();w.add('v','r');asyncio.run(cw.stop(w))
    r=json.loads(p.with_name(p.stem+'_counter_writer_stop_v68.json').read_text())
    assert r['confirmed'] and r['session']=='s' and not r['thread_alive'] and r['unflushed']==0

def test_ack_write_error_does_not_prevent_real_connection_close(tmp_path,monkeypatch):
    import database as database
    p=db(tmp_path);c=sqlite3.connect(p);monkeypatch.setattr(database._LOCAL,'conn',c,raising=False)
    def fail(*a,**k):raise PermissionError()
    monkeypatch.setattr(audit,'save',fail)
    r=audit.closed(p,'test');assert r['status']=='CLOSED' and r['ack_write_error_type']=='PermissionError'
    assert database._LOCAL.conn is None

def test_pid_reuse_does_not_mean_old_worker_still_running(tmp_path,monkeypatch):
    import psutil
    p=db(tmp_path);a=audit.Audit(p);a.processes[(123,1)]=dict(pid=123,created_ts=1,role='bot')
    class Reused:
        def create_time(self):return 2
    monkeypatch.setattr(psutil,'Process',lambda pid:Reused())
    r=a.finish({},{});assert r['processes'][0]['status']=='PID_REUSED_ORIGINAL_EXITED'
    assert not r['writer_acks_complete'] and not r['all_observed_writers_finished']

def test_after_corruption_is_compared_without_repair(tmp_path):
    p=db(tmp_path);a=audit.Audit(p);a.before=audit.probe(p,'before');p.write_bytes(b'bad database')
    r=a.finish({},{});assert r['comparison']==[dict(database=p.name,before='OK',after='CORRUPT')]
    assert not r['integrity_phases_ok'] and p.read_bytes()==b'bad database'

def test_database_created_during_run_is_absent_before_and_must_be_ok_after(tmp_path):
    # v0.7.102.0: the AMM observer creates <primary>_amm_observer.db on its first start.
    p=db(tmp_path);a=audit.Audit(p);a.before=audit.probe(p,'before')
    new=p.with_name(p.stem+'_amm_observer.db')
    with sqlite3.connect(new) as c:c.execute('create table t(x)')
    r=a.finish({},{})
    assert dict(database=new.name,before='ABSENT_BEFORE',after='OK') in r['comparison'] and r['integrity_phases_ok']
    broken=p.with_name(p.stem+'_other_new.db');broken.write_bytes(b'broken')
    r=a.finish({},{})
    assert dict(database=broken.name,before='ABSENT_BEFORE',after='CORRUPT') in r['comparison'] and not r['integrity_phases_ok']
    # Without a completed before-probe of the primary nothing is ABSENT_BEFORE (test_missing_primary_... covers the rest).
    a.before=dict(databases=[]);assert {x['before'] for x in a.finish({},{})['comparison']}=={'NOT_MEASURED'}

def test_live_wal_probe_does_not_commit_writer_transaction(tmp_path):
    p=db(tmp_path)
    with sqlite3.connect(p,isolation_level=None) as c:
        c.execute('PRAGMA journal_mode=WAL');c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO t VALUES(2)')
        r=audit.probe(p,'before');assert r['databases'][0]['status']=='OK'
        assert c.in_transaction;c.rollback()
        assert c.execute('SELECT COUNT(*) FROM t').fetchone()[0]==1

def test_missing_primary_is_not_hidden_by_healthy_cache(tmp_path):
    p=tmp_path/'clean_0762.db'
    with sqlite3.connect(tmp_path/'program_cache_v57.db') as c:c.execute('CREATE TABLE cache(x)')
    a=audit.Audit(p);a.before=audit.probe(p,'before');r=a.finish({},{})
    assert r['comparison'][0]['database']=='program_cache_v57.db'
    assert not r['integrity_phases_ok'] and not r['all_observed_writers_finished']
