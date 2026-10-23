import json,base64,copy
from pathlib import Path
import pytest
from solders.hash import Hash
import direct_pump_v2 as direct,wallet_signer
from test_execution_safety_v52 import plan,store

@pytest.fixture
def names():
    bundle=json.loads(Path('research/local_snapshot_v26.json').read_text());n=bundle['account_names'];a=bundle['snapshot']['accounts']
    return direct.accounts(bundle['mint'],bundle['wallet'],a[n['bonding_curve']],a[n['base_mint']],a[n['global']])

@pytest.mark.parametrize('side,count',[('BUY',27),('SELL',26)])
def test_direct_v2_layout_and_required_volume_account(names,side,count):
    message=direct.build(names,side,1000,2000,str(Hash.default()))
    ix=message.instructions[-1]
    assert len(ix.accounts)==count
    spec=next(i for i in json.loads(direct.lab.IDL.read_text())['instructions'] if i['name']==('buy_v2' if side=='BUY' else 'sell_v2'))
    mapped=dict(zip([a['name'] for a in spec['accounts']],[str(message.account_keys[i]) for i in ix.accounts]))
    assert mapped['user_volume_accumulator']==names['user_volume_accumulator']
    policy=dict(enabled=True,allowed_mints=[names['base_mint']],max_order_lamports=2000,max_sell_tokens=1000,max_compute_units=200000,max_priority_lamports=0)
    wallet_signer.validate_message(message,names['user'],policy)
    policy['max_order_lamports']=1999
    if side=='BUY':
        with pytest.raises(wallet_signer.SigningError):wallet_signer.validate_message(message,names['user'],policy)

def test_phase2_defaults_never_broadcast():
    with pytest.raises(Exception,match='BROADCAST_DISABLED'):direct.send(None,{},None,None)

def test_prepare_queries_rent_instead_of_constant(names,monkeypatch):
    monkeypatch.setattr(direct,'accounts',lambda *a:names)
    calls=[]
    class Rpc:
        def call(self,method,params):
            calls.append((method,params))
            return {'getGenesisHash':'chain','getMultipleAccounts':{'context':{'slot':5},'value':[{}, {}, {}]},
                'getAccountInfo':{'context':{'slot':5},'value':None},'getMinimumBalanceForRentExemption':2222222,
                'getLatestBlockhash':{'value':{'blockhash':str(Hash.default()),'lastValidBlockHeight':10}}}[method]
    # Presence is checked before parsing, so use non-empty placeholders.
    class GoodRpc(Rpc):
        def call(self,m,p):
            r=super().call(m,p)
            if m=='getMultipleAccounts':r['value']=[{'x':1}]*3
            return r
    plan=dict(expected_genesis='chain',mint=names['base_mint'],wallet=names['user'],side='BUY',amount=1000,limit=2000,fee_budget=3000000)
    _,_,r=direct.prepare(GoodRpc(),plan)
    assert r['volume_topup_lamports']==2222222
    assert ('getMinimumBalanceForRentExemption',[137,{'commitment':'confirmed'}]) in calls
    assert not any(m=='sendTransaction' for m,p in calls)

def test_signed_v2_buy_and_sell_send_and_finalize_offline(plan,store):
    import live_execution as live
    from solders.transaction import VersionedTransaction
    from test_execution_safety_v52 import Rpc,limits
    kp=plan[0];wallet=str(kp.pubkey());b=json.loads(Path('research/local_snapshot_v26.json').read_text());n=b['account_names'];a=b['snapshot']['accounts']
    names=direct.accounts(b['mint'],wallet,a[n['bonding_curve']],a[n['base_mint']],a[n['global']])
    rpc=Rpc();engine=live.Engine(store,rpc,enabled=True);mint=names['base_mint']
    for side,budget,tokens,delta,before,after in [('BUY',2000,0,-7000,0,1000),('SELL',0,1000,8000,1000,0)]:
        ident=side;store.reserve(ident,wallet,mint,side,budget,tokens,10000,10**9,1000,limits())
        tx=VersionedTransaction(direct.build(names,side,1000,2000 if side=='BUY' else 1,str(Hash.new_unique())),[kp])
        signed=base64.b64encode(bytes(tx)).decode();row=engine.submit(ident,signed,100,skip_preflight=True)
        assert row['state']=='SUBMITTED'
        token=lambda amount:[dict(owner=wallet,mint=mint,uiTokenAmount={'amount':str(amount)})]
        rpc.status=dict(slot=17,confirmationStatus='finalized',err=None)
        rpc.transaction=dict(slot=17,transaction=dict(signatures=[str(tx.signatures[0])],message={'accountKeys':[wallet]}),
            meta=dict(err=None,fee=5000,preBalances=[10**9],postBalances=[10**9+delta],preTokenBalances=token(before),postTokenBalances=token(after)))
        assert engine.track(ident)['state']=='FINALIZED_SUCCESS'
    sends=[p for m,p in rpc.calls if m=='sendTransaction']
    assert len(sends)==2 and all(p[1]['maxRetries']==0 and p[1]['skipPreflight'] is True for p in sends)
    assert all(p[1]['sigVerify'] is True for m,p in rpc.calls if m=='simulateTransaction')
