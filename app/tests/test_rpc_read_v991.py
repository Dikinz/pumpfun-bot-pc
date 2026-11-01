"""Unreadable unknown outcomes stop HTTP, not the owner; no assumed receipts."""
import errno,json,os,sqlite3,time,urllib.error,zipfile
from pathlib import Path
import pytest
import rpc_budget as rpc
from test_rpc_admission_v95 import URL,outcome
from test_rpc_cleanup_v96 import wait_for


def denied(code):
    e=PermissionError(errno.EACCES,'PRIVATE https://SECRET.invalid/?key=SECRET')
    if code is not None:e.winerror=code
    return e


def deny_path(patch,path,operation,code):
    original=getattr(Path,operation)
    def fail(p,*a,**kw):
        if p==path:raise denied(code)
        return original(p,*a,**kw)
    patch.setattr(Path,operation,fail)


@pytest.mark.parametrize('operation',['stat','read_text'])
@pytest.mark.parametrize('code',[None,5,32,33],ids=['errno13','win5','win32','win33'])
def test_read991_unknown_denied_result_defers_without_http_or_lost_429(tmp_path,monkeypatch,operation,code):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10);raw=p.read_bytes();calls=[]
    with monkeypatch.context() as m:
        deny_path(m,p,operation,code)
        with pytest.raises(rpc.Deferred) as e:
            with b.request(URL):calls.append('HTTP')
        assert e.value.rpc_budget_reason=='OUTCOME_READ_BUSY' and e.value.control_v95['stage']=='OUTCOME_READ'
        assert e.value.control_v95['failure_chain'][1]['errno']==errno.EACCES
        assert e.value.control_v95['failure_chain'][1]['winerror']==code
        assert not b.blocked() and not b.failed.exists() and not calls
        with b.session() as c:assert not c.execute('SELECT 1 FROM outcome_receipts WHERE id=?',(p.stem,)).fetchone()
        note=json.loads((tmp_path/'outcome_read_v991.json').read_text())
        assert note['receipt_confirmed'] is False and note['reason']=='OUTCOME_READ_BUSY'
        assert note['failure_chain'][0]['reason']=='OUTCOME_READ_BUSY'
        assert 'SECRET' not in json.dumps(note)
    assert p.read_bytes()==raw
    with pytest.raises(rpc.Deferred) as e:
        with b.request(URL):calls.append('HTTP')
    assert e.value.rpc_budget_reason=='COOLDOWN' and not calls
    snapshot=b.snapshot();assert snapshot['endpoints'][0]['server_429']==1 and snapshot['endpoints'][0]['started']==0
    assert snapshot['endpoints'][0]['cooldown_remaining_s']>8
    wait_for(lambda:not b._pending());b.close()


def test_read991_temporary_read_denial_recovers_inside_original_admission_deadline(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,wait=.1);p=outcome(b,'OK');original=b._event;calls=[]
    def once(path):
        calls.append(path)
        if len(calls)==1:raise denied(32)
        return original(path)
    monkeypatch.setattr(b,'_event',once)
    with b.request(URL):pass
    assert len(calls)>1 and b.snapshot()['endpoints'][0]['started']==1 and not b.blocked()
    wait_for(lambda:not b._pending());b.close()


@pytest.mark.parametrize('operation',['stat','read_text'])
def test_read991_confirmed_receipt_skips_denied_read_without_double_accounting(tmp_path,monkeypatch,operation):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10)
    with b.control_lock():b._drain_locked(unlink=False)
    with monkeypatch.context() as m:
        deny_path(m,p,operation,5)
        # Keep optional deletion out of this test, so the denied-read contract
        # is exercised against the committed receipt, not a removed file.
        m.setattr(b,'_queue_cleanup',lambda paths:None)
        with pytest.raises(rpc.Deferred) as e:
            with b.request(URL):pytest.fail('cooldown bypassed')
        assert e.value.rpc_budget_reason=='COOLDOWN'
        assert b.snapshot()['endpoints'][0]['server_429']==1 and not b.failed.exists()
    b._unlink_outcomes([p]);b.close()


def test_read991_deleted_receipt_stale_listing_is_not_false_io_failure(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10)
    with b.control_lock():b._drain_locked()
    wait_for(lambda:not p.exists())
    outcome(b,'OK') # Receipt retirement happens in the next non-empty batch.
    with b.control_lock():b._drain_locked()
    with b.session() as c:assert not c.execute('SELECT 1 FROM outcome_receipts WHERE id=?',(p.stem,)).fetchone()
    with monkeypatch.context() as m:
        m.setattr(b,'_pending',lambda:[p]);deny_path(m,p,'stat',5);deny_path(m,p,'read_text',5)
        with pytest.raises(rpc.Deferred) as e:
            with b.request(URL):pytest.fail('stale listing authorized HTTP')
        assert e.value.rpc_budget_reason=='OUTCOME_READ_BUSY' and not b.blocked()
    with pytest.raises(rpc.Deferred) as e:
        with b.request(URL):pytest.fail('recorded cooldown bypassed')
    assert e.value.rpc_budget_reason=='COOLDOWN' and b.snapshot()['endpoints'][0]['server_429']==1;b.close()


def test_read991_nontransient_disk_error_is_not_hidden(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10);original=b._event
    def fail(path):
        if path==p:raise OSError(errno.EIO,'disk error')
        return original(path)
    monkeypatch.setattr(b,'_event',fail)
    with pytest.raises(rpc.Unavailable,match='RPC_CONTROL_IO_ERROR') as e:
        with b.request(URL):pytest.fail('disk error authorized HTTP')
    assert e.value.__cause__.errno==errno.EIO and p.exists();b.close()


def test_read991_corrupt_unknown_result_remains_fatal(tmp_path):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10);p.write_text('{broken')
    with pytest.raises(rpc.Unavailable,match='RPC_OUTCOME_INVALID'):
        with b.request(URL):pytest.fail('corrupt result authorized HTTP')
    assert b.blocked() and p.read_text()=='{broken'
    marker=json.loads(b.failed.read_text());assert marker['stage']=='DRAIN_BEFORE_ADMISSION';b.close()


def test_read991_outcome_record_stays_durable_while_its_file_is_unreadable(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);original=b._event;hits=[]
    with monkeypatch.context() as m:
        def fail(path):raise denied(33)
        m.setattr(b,'_event',fail)
        with b.request(URL):hits.append('HTTP')
        assert hits==['HTTP'] and not b.blocked() and len(b._pending())==1
        note=json.loads((tmp_path/'outcome_read_v991.json').read_text());assert note['receipt_confirmed'] is False
    with b.control_lock():b._drain_locked()
    wait_for(lambda:not b._pending())
    assert b.snapshot()['endpoints'][0]['started']==1 and not b.failed.exists();b.close()


def test_read991_denial_releases_metadata_and_capacity_slots(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,slots=1,wait=.01);p=outcome(b,'429',10)
    with monkeypatch.context() as m:
        deny_path(m,p,'read_text',32)
        with pytest.raises(rpc.Deferred):
            with b.request(URL):pytest.fail('denied read bypassed')
        with b.control_lock():pass
        fd=rpc.lock_slot(tmp_path/'slot_0.lock');assert fd is not None;rpc.unlock_slot(fd)
    with b.control_lock():b._drain_locked()
    wait_for(lambda:not b._pending());b.close()


def test_read991_diagnostics_are_exported_with_original_cause(tmp_path,monkeypatch):
    import rpc_evidence_v892
    b=rpc.Budget(tmp_path/'control',rate=1000,wait=.01);p=outcome(b,'429',10)
    with monkeypatch.context() as m:
        deny_path(m,p,'read_text',None)
        with pytest.raises(rpc.Deferred):
            with b.request(URL):pytest.fail('unknown result bypassed')
    report=rpc_evidence_v892.export(b.root,tmp_path/'evidence.zip')
    with zipfile.ZipFile(tmp_path/'evidence.zip') as z:
        raw=z.read('raw/outcome_read_v991.json');note=json.loads(raw)
        assert note['failure_chain'][1]['errno']==13 and note['failure_chain'][1]['winerror'] is None
        assert b'SECRET' not in raw
    assert any(f['name']=='outcome_read_v991.json' for f in report['files'])
    with b.control_lock():b._drain_locked()
    wait_for(lambda:not b._pending());b.close()


def test_read991_native_windows_denied_reader_or_portable_model(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b,'429',10)
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];k.CreateFileW.restype=wintypes.HANDLE
        k.CloseHandle.argtypes=[wintypes.HANDLE];k.CloseHandle.restype=wintypes.BOOL
        handle=k.CreateFileW(str(p),0x40000000,6,None,3,0,None)
        assert handle not in (None,ctypes.c_void_p(-1).value)
        try:
            with pytest.raises(rpc.Deferred) as e:
                with b.request(URL):pytest.fail('native denied reader bypassed')
            assert e.value.rpc_budget_reason=='OUTCOME_READ_BUSY' and not b.blocked()
        finally:assert k.CloseHandle(handle)
    else:
        with monkeypatch.context() as m:
            deny_path(m,p,'read_text',32)
            with pytest.raises(rpc.Deferred) as e:
                with b.request(URL):pytest.fail('denied reader bypassed')
            assert e.value.rpc_budget_reason=='OUTCOME_READ_BUSY' and not b.blocked()
    with pytest.raises(rpc.Deferred) as e:
        with b.request(URL):pytest.fail('429 after native release bypassed')
    assert e.value.rpc_budget_reason=='COOLDOWN' and b.snapshot()['endpoints'][0]['server_429']==1
    wait_for(lambda:not b._pending());b.close()
