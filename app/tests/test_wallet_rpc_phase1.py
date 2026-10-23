import asyncio,json,os
from pathlib import Path
import pytest
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.hash import Hash
import rpc_client as rpc,wallet_signer as signer,wallet_manager as wallet,execution_lab
from test_execution_safety_v52 import plan

def run(coro):return asyncio.run(coro)

def result(payload,value):return dict(jsonrpc='2.0',id=payload['id'],result=value)

def test_read_retry_fallback_chain_and_redacted_stats():
    calls=[]
    async def transport(index,p):
        calls.append((index,p['method']))
        if index==0:raise TimeoutError('https://provider.invalid/?api-key=SECRET')
        return result(p,'chain' if p['method']=='getGenesisHash' else {'value':100})
    async def scenario():
        async with rpc.RobustRPC(['https://a.invalid/SECRET','https://b.invalid'],expected_genesis='chain',transport=transport) as c:
            assert await c.call('getBalance',['pubkey'])=={'value':100}
            assert 'SECRET' not in json.dumps(c.summary())
            assert c.idx==1
    run(scenario())
    assert calls==[(0,'getGenesisHash'),(1,'getGenesisHash'),(1,'getBalance')]

def test_wrong_chain_never_sends():
    calls=[]
    async def transport(index,p):calls.append(p['method']);return result(p,'wrong')
    async def scenario():
        async with rpc.RobustRPC(['https://a.invalid'],expected_genesis='right',allow_send=True,transport=transport) as c:
            with pytest.raises(rpc.RPCError,match='CHAIN_MISMATCH'):await c.call('sendTransaction',['bytes'])
    run(scenario());assert calls==['getGenesisHash']

def test_send_timeout_has_no_retry_or_fallback():
    calls=[]
    async def transport(index,p):
        calls.append((index,p['method']))
        if p['method']=='getGenesisHash':return result(p,'chain')
        raise TimeoutError('lost answer')
    async def scenario():
        async with rpc.RobustRPC(['https://a.invalid','https://b.invalid'],expected_genesis='chain',allow_send=True,transport=transport) as c:
            with pytest.raises(rpc.UnknownSubmission):await c.call('sendTransaction',['bytes'],retries=5)
    run(scenario());assert calls==[(0,'getGenesisHash'),(0,'sendTransaction')]

def test_send_disabled_and_invalid_params_no_retry():
    calls=[]
    async def transport(index,p):calls.append(p);return dict(jsonrpc='2.0',id=p['id'],error={'code':-32602,'message':'SECRET'})
    async def scenario():
        async with rpc.RobustRPC(['https://a.invalid'],transport=transport) as c:
            with pytest.raises(rpc.RPCError,match='SEND_DISABLED'):await c.call('sendTransaction',[])
            with pytest.raises(rpc.RPCError):await c.call('getBalance',[])
    run(scenario());assert len(calls)==1

def test_malformed_envelope_and_retry_budget():
    calls=[]
    async def transport(index,p):calls.append(index);return {'id':'wrong','result':1}
    async def scenario():
        async with rpc.RobustRPC(['https://a.invalid','https://b.invalid'],transport=transport,retries=2) as c:
            with pytest.raises(rpc.RPCError,match='INVALID_RPC_ENVELOPE'):await c.call('getSlot',[])
    run(scenario());assert calls==[0,1]

def test_pooled_session_closed_once():
    async def scenario():
        c=rpc.RobustRPC(['https://a.invalid'])
        async with c:
            session=c.session;assert session is not None and not session.closed
            assert session is c.session
        assert session.closed and c.session is None
    run(scenario())

def test_cancelled_rpc_not_retried():
    calls=[]
    async def transport(index,p):calls.append(index);raise asyncio.CancelledError
    async def scenario():
        async with rpc.RobustRPC(['https://a.invalid'],transport=transport) as c:
            with pytest.raises(asyncio.CancelledError):await c.call('getSlot',[])
    run(scenario());assert calls==[0]

def test_rpc_endpoint_and_chain_validation():
    for urls in [[],['http://insecure.invalid'],['https://user:password@a.invalid']]:
        with pytest.raises(rpc.RPCError):rpc.RobustRPC(urls)
    with pytest.raises(rpc.RPCError,match='CHAIN'):rpc.RobustRPC(['https://a.invalid'],allow_send=True)

@pytest.mark.skipif(os.name=='nt',reason='POSIX permission semantics')
def test_loose_permissions_and_symlink_rejected(plan,tmp_path):
    vault=tmp_path/'wallet.json';signer.create_vault(vault,plan[0],'long offline test password')
    vault.chmod(0o644)
    with pytest.raises(wallet.PermissionError):wallet.assert_private(vault)
    with pytest.raises(signer.SigningError):signer.unlock(vault,'long offline test password')
    vault.chmod(0o600);link=tmp_path/'linked.json';link.symlink_to(vault)
    with pytest.raises(wallet.PermissionError):wallet.assert_private(link)

def test_secure_wallet_keeps_key_only_in_worker(plan,tmp_path):
    vault=tmp_path/'wallet.json';policy=tmp_path/'policy.json';password='long offline test password'
    signer.create_vault(vault,plan[0],password);policy.write_text(json.dumps(plan[2]));wallet.restrict_access(policy)
    w=wallet.SecureWallet(vault,policy)
    assert w.pubkey()==str(plan[0].pubkey()) and not hasattr(w,'_keypair')
    message=execution_lab.build_message(plan[1],'BUY',100000,1,str(Hash.new_unique()))
    unsigned=VersionedTransaction.populate(message,[Signature.default()])
    signed=VersionedTransaction.from_bytes(w.sign(bytes(unsigned),password))
    assert signed.verify_with_results()==[True]

def test_windows_acl_failure_is_closed(monkeypatch,tmp_path):
    class Bad:
        returncode=1
        stdout=''
    monkeypatch.setattr(wallet.subprocess,'run',lambda *a,**k:Bad())
    with pytest.raises(wallet.PermissionError,match='NOT_VERIFIED'):wallet.windows_acl(tmp_path)

def test_signer_has_no_private_key_env_loading():
    import ast
    for filename in ['wallet_manager.py','wallet_signer.py']:
        tree=ast.parse(Path(filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                assert not (isinstance(node.func.value,ast.Name) and node.func.value.id=='os' and node.func.attr in ('getenv',))


def test_offline_wallet_signature_does_not_need_enabled_policy(plan,tmp_path):
    vault=tmp_path/'wallet.json';password='long offline test password'
    signer.create_vault(vault,plan[0],password)
    result=signer.self_test_isolated(vault,password)
    assert result['signature_verified'] is True and result['broadcast'] is False
    assert result['public_key']==str(plan[0].pubkey())
    assert result['blockhash']=='INVALID_FOR_NETWORK_ZERO_HASH'
