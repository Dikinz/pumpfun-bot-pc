import io,json,sqlite3,subprocess,sys,time,urllib.error,zipfile
from contextlib import closing
from pathlib import Path
import pytest
import rpc_budget as rpc
import rpc_evidence_v892 as evidence


def test_real_process_metadata_contention_does_not_discard_429(tmp_path):
    b=rpc.Budget(tmp_path,rate=1000,slots=2,wait=.01)
    permit=b.request('https://rpc.invalid');permit.__enter__()
    script="""import sys,rpc_budget as r
b=r.Budget(sys.argv[1],rate=1000,slots=2)
with b.control_lock():
 print('held',flush=True)
 sys.stdin.readline()
"""
    p=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-u','-c',script,str(tmp_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=Path(rpc.__file__).parent)
    try:
        # communicate is bounded even if the helper fails before readiness.
        import threading,queue
        ready=queue.Queue();t=threading.Thread(target=lambda:ready.put(p.stdout.readline()),daemon=True);t.start()
        assert ready.get(timeout=5).strip()=='held'
        err=urllib.error.HTTPError('https://SECRET',429,'SECRET',{'Retry-After':'10'},None)
        permit.__exit__(type(err),err,None)
        assert b.snapshot()['status']=='PENDING' and not b.failed.exists()
        with pytest.raises(rpc.Deferred):
            with b.request('https://rpc.invalid'):pytest.fail('pending 429 bypassed')
        p.stdin.write('release\n');p.stdin.flush();assert p.wait(timeout=5)==0
        with pytest.raises(rpc.Deferred) as exc:
            with b.request('https://rpc.invalid'):pytest.fail('cooldown lost')
        assert exc.value.rpc_budget_reason=='COOLDOWN'
        row=b.snapshot()['endpoints'][0]
        assert row['started']==1 and row['server_429']==1 and row['cooldown_remaining_s']>8
        assert not b._pending() and 'SECRET' not in b.path.read_bytes().decode(errors='ignore')
    finally:
        if p.poll() is None:p.kill()
        p.wait(timeout=5)
        if permit.fd is not None:rpc.unlock_slot(permit.fd)


def test_commit_then_unlink_failure_replay_counts_only_once(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,slots=2)
    permit=b.request('https://rpc.invalid');permit.__enter__()
    original=Path.unlink
    def fail(self,*a,**k):
        if self.parent.name=='outcomes':raise PermissionError('SECRET')
        return original(self,*a,**k)
    monkeypatch.setattr(Path,'unlink',fail)
    permit.__exit__(None,None,None)
    assert not b.failed.exists() and len(b._pending())==1
    # Replaying the committed receipt must not count the same outcome again.
    monkeypatch.setattr(Path,'unlink',original)
    with b.control_lock():b._drain_locked()
    assert b.snapshot()['endpoints'][0]['started']==1
    assert not b.failed.exists() and not b._pending()
    assert b.snapshot()['endpoints'][0]['total_wait_ms']==pytest.approx(permit.wait_ms)
    with closing(sqlite3.connect(b.path)) as c:
        assert c.execute('select count(*) from outcome_receipts').fetchone()[0]<=32 # Bounded receipts remain replay-safe until the next atomic batch GC.


def test_failure_details_distinguish_metadata_sqlite_and_redact(tmp_path):
    b=rpc.Budget(tmp_path)
    b.block(rpc.MetadataBusy('database is locked SECRET'))
    d=json.loads(b.failed.read_text());assert d['failure_chain'][0]['reason']=='METADATA_LOCK_BUSY'
    assert 'SECRET' not in b.failed.read_text()
    c=sqlite3.connect(':memory:')
    try:
        with pytest.raises(sqlite3.OperationalError) as err:c.execute('select * from missing')
        d=rpc.failure_details(err.value)[0]
        assert d['sqlite_errorcode'] is not None and d['sqlite_errorname']=='SQLITE_ERROR'
    finally:c.close()


def test_bad_or_incomplete_journal_never_allows_new_http(tmp_path):
    b=rpc.Budget(tmp_path,wait=.01)
    d=tmp_path/'outcomes';d.mkdir()
    (d/'unknown.tmp').write_text('partial')
    with pytest.raises(rpc.Deferred):
        with b.request('https://rpc.invalid'):pytest.fail('incomplete outcome ignored')
    assert b.snapshot()['status']=='PENDING'
    (d/'unknown.tmp').unlink();(d/'unknown.json').write_text('{}')
    with pytest.raises(rpc.Unavailable,match='OUTCOME_INVALID'):
        with b.request('https://rpc.invalid'):pytest.fail('corrupt outcome ignored')
    assert b.failed.exists()
    marker=json.loads(b.failed.read_text())
    assert marker['stage']=='DRAIN_BEFORE_ADMISSION'
    assert marker['failure_chain'][0]['reason']=='RPC_OUTCOME_INVALID'
    assert marker['failure_chain'][0]['frames']


def test_evidence_contains_failure_db_wal_locks_outcomes_without_mutation(tmp_path):
    b=rpc.Budget(tmp_path/'control',rate=1000,slots=1)
    with b.request('https://rpc.invalid'):pass
    b.block(rpc.MetadataBusy('busy'))
    d=b.root/'outcomes';d.mkdir(exist_ok=True);(d/'incomplete.tmp').write_text('partial')
    before={p.relative_to(b.root).as_posix():p.read_bytes() for p in b.root.rglob('*') if p.is_file()}
    target=tmp_path/'evidence.zip';report=evidence.export(b.root,target)
    assert report['status']=='CAPTURED' and report['integrity']==['ok']
    with zipfile.ZipFile(target) as z:
        assert z.read('raw/control_failed.json')==before['control_failed.json']
        assert z.read('raw/outcomes/incomplete.tmp')==b'partial'
        assert 'consistent.db' in z.namelist() and 'raw/metadata.lock' in z.namelist()
        assert any(n.startswith('raw/rate_') and n.endswith('.bin') for n in z.namelist())
    for n,data in before.items():assert (b.root/n).read_bytes()==data


def test_results_preserves_rpc_evidence_even_without_trading_database(tmp_path,monkeypatch):
    import launcher,config
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    monkeypatch.setattr(config,'DB',str(tmp_path/'data/missing.db'))
    b=rpc.Budget(tmp_path/'rpc',rate=1000,slots=1)
    with b.request('https://rpc.invalid'):pass
    b.block(rpc.MetadataBusy('busy'))
    monkeypatch.setattr(config,'RPC_CONTROL_DIR',b.root)
    assert launcher.results()==1
    target=next((tmp_path/'results').glob('preservation_*.zip'))
    with zipfile.ZipFile(target) as outer:
        assert 'REPORT_ERROR.json' in outer.namelist()
        with zipfile.ZipFile(io.BytesIO(outer.read('rpc_control_evidence_v892.zip'))) as inner:
            assert json.loads(inner.read('raw/control_failed.json'))['failure_chain'][0]['reason']=='METADATA_LOCK_BUSY'
            assert 'consistent.db' in inner.namelist()


class RateClock:
    """Advance only explicitly; real disk/scheduler latency cannot expire a rate window."""
    def __init__(self):self.now=1000.
    def monotonic(self):return self.now
    def time(self):return 1800000000.+self.now
    def sleep(self,seconds):self.now+=seconds


def slow_commit_budget(tmp_path,monkeypatch):
    clock=RateClock()
    # Replace rpc_budget's module binding, not the process-wide time module:
    # other tests, SQLite and subprocesses continue to use the real OS clock.
    monkeypatch.setattr(rpc,'time',clock)
    b=rpc.Budget(tmp_path,rate=10,slots=2,wait=.01)
    original=b._reserve_once
    def slow_commit(key):
        result=original(key)
        if result is None:clock.sleep(.12)
        return result
    monkeypatch.setattr(b,'_reserve_once',slow_commit)
    return b,clock


def test_rate_spacing_starts_after_slow_control_commit(tmp_path,monkeypatch):
    b,clock=slow_commit_budget(tmp_path,monkeypatch)
    with b.request('https://rpc.invalid'):pass
    dispatched=clock.monotonic()
    assert dispatched==pytest.approx(1000.12)
    # SQLite's pre-commit reservation expired during the injected 120 ms.
    # An independent Budget must still respect the post-commit shared clock.
    other=rpc.Budget(tmp_path,rate=10,slots=2,wait=.01)
    with pytest.raises(rpc.Deferred) as err:
        with other.request('https://rpc.invalid'):pytest.fail('control-commit delay allowed burst')
    assert err.value.rpc_budget_reason=='RATE'
    assert clock.monotonic()==dispatched
    other.wait=0
    clock.sleep(.099)
    with pytest.raises(rpc.Deferred):
        with other.request('https://rpc.invalid'):pytest.fail('request before 100 ms')
    assert b.snapshot()['endpoints'][0]['started']==1
    clock.sleep(.001001)
    with other.request('https://rpc.invalid'):pass
    assert b.snapshot()['endpoints'][0]['started']==2


def test_elapsed_rate_interval_during_bookkeeping_allows_next_request(tmp_path,monkeypatch):
    b,clock=slow_commit_budget(tmp_path,monkeypatch)
    record=b.record
    def slow_record(*args,**kwargs):
        result=record(*args,**kwargs)
        clock.sleep(.20)
        return result
    monkeypatch.setattr(b,'record',slow_record)
    with b.request('https://rpc.invalid'):pass
    assert clock.monotonic()==pytest.approx(1000.32)
    # This was the unaccounted path in the old test: the real 100 ms interval
    # has legitimately elapsed while the first result was being recorded.
    with rpc.Budget(tmp_path,rate=10,slots=2,wait=.01).request('https://rpc.invalid'):pass
    assert b.snapshot()['endpoints'][0]['started']==2


@pytest.mark.parametrize('data',[b'bad',__import__('struct').pack('d',float('nan'))])
def test_invalid_shared_rate_timestamp_denies_admission(tmp_path,data):
    b=rpc.Budget(tmp_path,rate=1000,slots=1)
    with b.request('https://rpc.invalid'):pass
    next(tmp_path.glob('rate_*.bin')).write_bytes(data)
    with pytest.raises(rpc.Unavailable,match='RPC_RATE_STATE_INVALID'):
        with b.request('https://rpc.invalid'):pytest.fail('invalid rate state bypassed')
    assert b.failed.exists()
    assert json.loads(b.failed.read_text())['failure_chain'][0]['reason']=='RPC_RATE_STATE_INVALID'
