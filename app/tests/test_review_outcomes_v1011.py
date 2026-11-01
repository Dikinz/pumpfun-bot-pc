"""Offline directory-fault models; no external provider or native Windows IO."""
import importlib.util,json,os,sqlite3,sys,time
from pathlib import Path
import pytest
import rpc_budget as rpc, rpc_priority_recovery_v101 as recovery
URL='https://rpc.invalid'

def deny_enumeration(monkeypatch,directory,stage):
    original=os.scandir
    class DeniedIterator:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def __iter__(self):return self
        def __next__(self):raise PermissionError(13,'SECRET')
    def denied(path):
        if Path(path)==directory:
            if stage=='open':raise PermissionError(13,'SECRET')
            return DeniedIterator()
        return original(path)
    monkeypatch.setattr(os,'scandir',denied)

def outcome(b):
    d=b.root/'outcomes';d.mkdir(parents=True);p=d/('a'*32+'.json')
    p.write_text(json.dumps(dict(schema='RPC_OUTCOME_V892',id=p.stem,key=rpc.endpoint_key(URL),kind='429',wait_ms=0.,observed_mono=time.monotonic(),observed_wall=time.time(),retry_after_s=10.)))
    return p

@pytest.mark.parametrize('stage',['open','iteration'])
def test_review1011_denied_directory_preserves_429_and_defers(tmp_path,monkeypatch,stage):
    b=rpc.Budget(tmp_path,rate=1000,wait=.01);p=outcome(b);raw=p.read_bytes();calls=[]
    with monkeypatch.context() as m:
        deny_enumeration(m,p.parent,stage)
        with pytest.raises(rpc.Deferred) as caught:
            with b.request(URL):calls.append('HTTP')
        assert caught.value.rpc_budget_reason=='OUTCOME_READ_BUSY'
        snap=b.snapshot();assert snap['status']=='OUTCOME_READ_BUSY' and snap['pending_outcomes'] is None
        assert snap['failure_chain'][1]['errno']==13 and 'SECRET' not in json.dumps(snap)
        assert not b.blocked() and not calls
    assert p.read_bytes()==raw
    with pytest.raises(rpc.Deferred) as caught:
        with b.request(URL):calls.append('HTTP')
    assert caught.value.rpc_budget_reason=='COOLDOWN' and not calls
    snap=b.snapshot();assert snap['endpoints'][0]['server_429']==1 and snap['endpoints'][0]['started']==0
    assert snap['endpoints'][0]['cooldown_remaining_s']>8
    b.close()

@pytest.mark.parametrize('stage',['open','iteration'])
def test_review1011_audited_recovery_refuses_unknown_directory(tmp_path,monkeypatch,stage):
    b=rpc.Budget(tmp_path);known=json.loads(recovery.AUDIT.read_text())
    with sqlite3.connect(b.path) as c:
        c.executescript(rpc.SCHEMA)
        for table,rows in known['control_state'].items():
            for row in rows:c.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?'*len(row))+')',tuple(row.values()))
    b.failed.write_bytes(bytes.fromhex(known['marker_hex']));original=b.failed.read_bytes()
    for name,raw in known['rate_bytes_hex'].items():(tmp_path/name).write_bytes(bytes.fromhex(raw))
    outcome(b)
    deny_enumeration(monkeypatch,tmp_path/'outcomes',stage)
    assert not b.recover() and b.blocked() and b.failed.read_bytes()==original
    assert not (tmp_path/'failure_recovery_v97.json').exists()
    snap=b.snapshot();assert snap['status']=='BLOCKED' and snap['reason']=='OUTCOME_READ_BUSY' and snap['pending_outcomes'] is None
    b.close()

def test_review1011_missing_directory_is_normal_and_limits_unchanged(tmp_path):
    b=rpc.Budget(tmp_path,rate=6,slots=1,wait=.01)
    assert b._pending()==[]
    with b.request(URL):
        with pytest.raises(rpc.Deferred) as caught:
            with b.request(URL):pytest.fail('capacity bypass')
        assert caught.value.rpc_budget_reason=='CAPACITY'
    with pytest.raises(rpc.Deferred) as caught:
        with b.request(URL):pytest.fail('rate bypass')
    assert caught.value.rpc_budget_reason=='RATE'
    b.close()
