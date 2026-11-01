import json,sqlite3,threading,time,urllib.error
from pathlib import Path
import pytest
import rpc_budget as rpc
from test_rpc_admission_v95 import URL,outcome
from test_rpc_cleanup_v96 import wait_for

def denied(code=5):
    exc=PermissionError(13,'PRIVATE');exc.winerror=code;return exc

@pytest.mark.parametrize('code',[5,32,33])
def test_receipt972_delete_and_stat_denied_after_commit_keeps_cooldown(tmp_path,monkeypatch,code):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01)
    p=outcome(b,'429',10)
    with b.control_lock():b._drain_locked(unlink=False)
    real_unlink=Path.unlink;real_stat=Path.stat
    # Model delete-pending: inspecting the same name is denied too. No
    # scanner/SQLite/root-cause attribution is implied by this injected case.
    def unlink(path,*a,**kw):
        if path==p:raise denied(code)
        return real_unlink(path,*a,**kw)
    def stat(path,*a,**kw):
        if path==p:raise denied(code)
        return real_stat(path,*a,**kw)
    with monkeypatch.context() as m:
        m.setattr(Path,'unlink',unlink);m.setattr(Path,'stat',stat)
        b._unlink_outcomes([p])
        assert not b.failed.exists()
        with pytest.raises(rpc.Deferred) as exc:
            with b.request(URL):pytest.fail('confirmed 429 bypassed')
        assert exc.value.rpc_budget_reason=='COOLDOWN'
        assert b.snapshot()['endpoints'][0]['server_429']==1
        note=json.loads((tmp_path/'cleanup_v96.json').read_text())
        assert note['receipt_confirmed'] is True and note['winerror']==code
    wait_for(lambda:not p.exists());b.close()

def test_receipt972_unknown_unstatable_record_is_never_deleted(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000);p=outcome(b,'429',10)
    with b.control_lock():
        with b.session():pass
    real=Path.stat;unlinks=[]
    def stat(path,*a,**kw):
        if path==p:raise denied()
        return real(path,*a,**kw)
    with monkeypatch.context() as m:
        m.setattr(Path,'stat',stat);m.setattr(Path,'unlink',lambda path,*a,**kw:unlinks.append(path))
        with pytest.raises(PermissionError):b._unlink_outcomes([p])
    assert not unlinks and p.exists();b.close()

def test_receipt972_record_success_survives_delete_pending_stat_failure(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);pending=set()
    real_unlink=Path.unlink;real_stat=Path.stat
    def unlink(path,*a,**kw):
        if path.parent==tmp_path/'outcomes':pending.add(path);raise denied()
        return real_unlink(path,*a,**kw)
    def stat(path,*a,**kw):
        if path in pending:raise denied()
        return real_stat(path,*a,**kw)
    with monkeypatch.context() as m:
        m.setattr(Path,'unlink',unlink);m.setattr(Path,'stat',stat)
        with b.request(URL):pass
        assert not b.failed.exists() and len(pending)==1
    wait_for(lambda:not b._pending());b.close()

@pytest.mark.parametrize('kind',['busy','sharing'])
def test_receipt972_cleanup_reader_contention_retries_without_permanent_gate(tmp_path,monkeypatch,kind):
    b=rpc.Budget(tmp_path,rate=1000);p=outcome(b,'429',10)
    with b.control_lock():b._drain_locked(unlink=False)
    real=b._unlink_outcomes;entered=threading.Event();calls=[]
    def once(paths,queue=True):
        calls.append(1)
        if len(calls)==1:
            entered.set()
            if kind=='busy':raise sqlite3.OperationalError('database is locked')
            raise denied(32)
        return real(paths,queue=queue)
    monkeypatch.setattr(b,'_unlink_outcomes',once)
    b._queue_cleanup([p]);assert entered.wait(2)
    with b.control_lock():pass
    wait_for(lambda:not p.exists())
    assert len(calls)>=2 and not b.failed.exists()
    assert b.snapshot()['endpoints'][0]['server_429']==1;b.close()

def test_receipt972_cleanup_database_corruption_remains_fatal(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000);p=outcome(b,'429',10)
    with b.control_lock():b._drain_locked(unlink=False)
    def fail(*a,**kw):raise sqlite3.OperationalError('database disk image is malformed')
    monkeypatch.setattr(b,'_unlink_outcomes',fail)
    b._queue_cleanup([p]);wait_for(lambda:b.failed.exists())
    marker=json.loads(b.failed.read_text())
    assert b.blocked() and marker['stage']=='CLEANUP_RECEIPT_UNCONFIRMED'
    assert marker['failure_chain'][0]['reason']=='SQLITE_CORRUPT' and p.exists();b.close()
