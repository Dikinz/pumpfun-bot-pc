"""Windows 10 / Python 3.14 RPC regulator regressions (WIN-QA-01, RPC-W1, RPC-W2)."""
import json,sqlite3,threading,time,urllib.error
from contextlib import closing
from pathlib import Path
import pytest
import rpc_budget as rpc
from test_rpc_admission_v95 import URL,outcome
from test_rpc_cleanup_v96 import wait_for
from test_rpc_transient_v97 import winerr,old_marker,durable_failure


def receipts(b):
    with closing(sqlite3.connect(b.path)) as c:return {r[0] for r in c.execute('SELECT id FROM outcome_receipts')}


def test_win314_applied_429_file_is_removed_asynchronously_after_receipt(tmp_path,monkeypatch):
    # WIN-QA-01: deletion runs on the rpc-outcome-cleanup thread. A slow thread
    # (Windows under load) leaves the file present right after admission returns;
    # its receipt and cooldown are already committed and deletion still converges.
    b=rpc.Budget(tmp_path,rate=1000,slots=2,wait=.01);gate=threading.Event();real=b._unlink_outcomes
    def slow(paths,queue=True):
        if paths and threading.current_thread().name=='rpc-outcome-cleanup':gate.wait(10)
        return real(paths,queue)
    monkeypatch.setattr(b,'_unlink_outcomes',slow)
    p=outcome(b,'429',10)
    with pytest.raises(rpc.Deferred) as e:
        with b.request(URL):pytest.fail('429 bypassed')
    assert e.value.rpc_budget_reason=='COOLDOWN'
    assert b._pending()==[p] and p.stem in receipts(b)
    row=b.snapshot()['endpoints'][0];assert row['server_429']==1 and row['cooldown_remaining_s']>8
    gate.set();wait_for(lambda:not b._pending(),seconds=10)
    assert b.snapshot()['endpoints'][0]['server_429']==1;b.close()


def deny_marker_writes(monkeypatch,b):
    state=dict(denied=True,attempts=0);real=Path.open
    def guarded(self,mode='r',*a,**kw):
        if state['denied'] and self==b.failed and 'w' in mode:
            state['attempts']+=1;raise winerr(32)
        return real(self,mode,*a,**kw)
    monkeypatch.setattr(Path,'open',guarded);return state


def test_win314_unwritten_marker_is_persisted_after_handle_release(tmp_path,monkeypatch):
    # RPC-W1: an old marker with a matching recovery proof lets other processes run.
    # A new failure whose marker write is denied must not stay a private block
    # forever: once the handle is gone, all processes see the same new marker.
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);durable_failure(b,monkeypatch);old_marker(b)
    assert b.recover() and not b.blocked()
    other=rpc.Budget(tmp_path,rate=1000,wait=.01)
    assert not other.blocked()
    state=deny_marker_writes(monkeypatch,b)
    with pytest.raises(PermissionError):b.block(RuntimeError('PRIVATE'),stage='RECORD_HTTP_OUTCOME')
    assert b.blocked() and state['attempts']>=2 # retried, still denied
    state['denied']=False
    assert b.blocked() # still blocked, now through the persisted marker
    marker=json.loads(b.failed.read_text(encoding='utf-8'))
    assert marker['stage']=='RECORD_HTTP_OUTCOME' and marker['error_type']=='RuntimeError' and marker['transient'] is False
    assert 'PRIVATE' not in b.failed.read_text(encoding='utf-8')
    assert other.blocked(),'other process kept admitting after the failure became durable'
    with pytest.raises(rpc.Unavailable,match='RPC_CONTROL_BLOCKED'):
        with other.request(URL):pytest.fail('other process admitted HTTP')
    b.close();other.close()


def test_win314_marker_write_success_needs_no_retry(tmp_path):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01)
    with b.request(URL):pass
    b.block(RuntimeError('x'),stage='RECORD_HTTP_OUTCOME')
    assert b.blocked() and b._pending_failure is None and not b._unpersisted_failure
    assert rpc.Budget(tmp_path,rate=1000).blocked();b.close()


def test_win314_short_tmp_hold_does_not_block_exit_reads_for_ten_seconds(tmp_path,monkeypatch):
    # RPC-W2: a scanner holds outcomes/*.tmp beyond the bounded publication retry.
    # After the handle is released and the usual 2 s recovery delay, the next
    # (priority) read must recover the fsynced result instead of waiting for 10 s age.
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);real=rpc.os.replace;hold=dict(on=True)
    def scanner(a,z):
        if hold['on'] and Path(a).suffix=='.tmp' and Path(a).parent.name=='outcomes':raise winerr(32)
        return real(a,z)
    monkeypatch.setattr(rpc.os,'replace',scanner)
    with pytest.raises(rpc.Unavailable,match='RESULT_UNCONFIRMED'):
        with b.request(URL):pass
    tmp=next((tmp_path/'outcomes').glob('*.tmp'))
    assert time.time()-tmp.stat().st_mtime<5 and json.loads(b.failed.read_text())['transient'] is True
    hold['on']=False;old_marker(b) # marker is older than RECOVER_AFTER_SECONDS; .tmp is fresh
    with b.request(URL,priority=True):pass
    proof=json.loads((tmp_path/'failure_recovery_v97.json').read_text(encoding='utf-8'))
    assert proof['outcome_id']==tmp.stem and not tmp.exists() # receipt-proven, then published
    assert b.snapshot()['endpoints'][0]['started']==2 and not b.blocked()
    wait_for(lambda:not b._pending());b.close()


def test_win314_fresh_tmp_of_live_publisher_is_never_promoted(tmp_path):
    # Safety of RPC-W2: without the age gate, the publisher slot lock still decides.
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10);row=json.loads(p.read_text());row['publisher_slot_v97']=0
    tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(row));p.unlink()
    fd=rpc.lock_slot(tmp_path/'publisher_v97_0.lock')
    try:
        with pytest.raises(rpc.Deferred):
            with b.request(URL):pytest.fail('active publisher overridden')
        assert tmp.exists() and not b.failed.exists() and sum(r['started'] for r in b.snapshot()['endpoints'])==0
    finally:rpc.unlock_slot(fd)
    with pytest.raises(rpc.Deferred) as e:
        with b.request(URL):pytest.fail('429 bypassed')
    assert e.value.rpc_budget_reason=='COOLDOWN' and b.snapshot()['endpoints'][0]['server_429']==1;b.close()
