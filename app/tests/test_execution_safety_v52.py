import base64,json,sqlite3,struct,time
from contextlib import closing
from pathlib import Path
import pytest
from solders.keypair import Keypair
from solders.hash import Hash
from solders.message import MessageV0,to_bytes_versioned
from solders.transaction import VersionedTransaction
import live_execution as live,wallet_signer as signer,execution_lab as lab

@pytest.fixture
def plan():
    import local_simulation as snapshot
    bundle=json.loads(Path('research/local_snapshot_v26.json').read_text());names=bundle['account_names'];a=bundle['snapshot']['accounts']
    kp=Keypair();wallet=str(kp.pubkey());mint=bundle['mint']
    names=snapshot.addresses(mint,wallet,a[names['bonding_curve']],a[mint],a[names['global']])
    policy=dict(enabled=True,allowed_mints=[mint],max_order_lamports=1000000,max_sell_tokens='10000',
      max_compute_units=200000,max_priority_lamports=10000,max_tip_lamports=10000,tip_accounts=[])
    return kp,names,policy

def signed(plan,side='BUY',amount=100000,minimum=1,**kwargs):
    kp,names,_=plan
    message=lab.build_message(names,side,amount,minimum,str(Hash.new_unique()),**kwargs)
    return base64.b64encode(bytes(VersionedTransaction(message,[kp]))).decode()

def limits():return dict(day=time.strftime('%Y-%m-%d',time.gmtime()),max_order_lamports=1000000,max_fee_lamports=100000,max_failures=3,daily_loss_bps=1000,max_positions=2)

@pytest.fixture
def store(tmp_path,plan,monkeypatch):
    monkeypatch.setattr(signer,'PRIVATE',tmp_path/'private')
    s=live.Store.for_wallet(str(plan[0].pubkey()));s.begin_day(limits()['day'],1000000);return s

def reserve(store,plan,ident='intent',side='BUY',amount=100000,tokens=0):
    kp,names,_=plan
    store.reserve(ident,str(kp.pubkey()),names['base_mint'],side,amount,tokens,10000,10**9,10000,limits())

class Rpc:
    def __init__(self):self.calls=[];self.send_error=False;self.sim_error=None;self.status=None;self.height=10;self.transaction=None
    def call(self,method,params):
        self.calls.append((method,params))
        if method=='getFeeForMessage':return {'value':5000}
        if method=='simulateTransaction':
            tx=VersionedTransaction.from_bytes(base64.b64decode(params[0]));post=10**9
            for ix in tx.message.instructions:
                if str(tx.message.account_keys[ix.program_id_index])==signer.PUMP:
                    post-=struct.unpack('<Q',bytes(ix.data[8:16]))[0]+5000 if len(ix.data)==25 else -5000
            return {'value':{'err':self.sim_error,'accounts':[{'lamports':post}]}}
        if method=='getBlockHeight':return self.height
        if method=='getBalance':return {'context':{'slot':1},'value':10**9}
        if method=='getTokenAccountsByOwner':return {'context':{'slot':1},'value':[]}
        if method=='getLatestBlockhash':return {'value':{'blockhash':str(Hash.new_unique()),'lastValidBlockHeight':100}}
        if method=='sendTransaction':
            if self.send_error:raise TimeoutError('unknown outcome')
            return str(VersionedTransaction.from_bytes(base64.b64decode(params[0])).signatures[0])
        if method=='getSignatureStatuses':return {'value':[self.status]}
        if method=='getTransaction':return self.transaction
        raise AssertionError(method)

def test_real_signature_and_disabled_default(plan,store):
    reserve(store,plan);encoded=signed(plan)
    tx,sig=live.inspect_signed(encoded,str(plan[0].pubkey()))
    assert tx.verify_with_results()==[True]
    rpc=Rpc()
    with pytest.raises(live.ExecutionError,match='LIVE_DISABLED'):live.Engine(store,rpc).submit('intent',encoded,100)
    assert not rpc.calls

def test_signature_verified_preflight_and_send_options(plan,store):
    reserve(store,plan);rpc=Rpc();engine=live.Engine(store,rpc,enabled=True)
    r=engine.submit('intent',signed(plan),100,skip_preflight=True)
    assert r['state']=='SUBMITTED'
    assert next(p for m,p in rpc.calls if m=='simulateTransaction')[1]['sigVerify'] is True
    options=next(p for m,p in rpc.calls if m=='sendTransaction')[1]
    assert options['skipPreflight'] is True and options['maxRetries']==0
    with pytest.raises(live.ExecutionError,match='NOT_RESERVED'):engine.submit('intent',signed(plan),100)

def test_preflight_failure_never_sent(plan,store):
    reserve(store,plan);rpc=Rpc();rpc.sim_error='BlockhashNotFound'
    r=live.Engine(store,rpc,enabled=True).submit('intent',signed(plan),100)
    assert r['state']=='PREFLIGHT_REJECTED' and not any(m=='sendTransaction' for m,p in rpc.calls)
    reserve(store,plan,ident='next')

def test_unknown_send_blocks_new_intents_even_after_restart(plan,store):
    reserve(store,plan);rpc=Rpc();rpc.send_error=True
    r=live.Engine(store,rpc,enabled=True).submit('intent',signed(plan),100)
    assert r['state']=='UNKNOWN' and r['signature']
    reopened=live.Store(store.path)
    with pytest.raises(live.ExecutionError,match='UNRESOLVED'):reserve(reopened,plan,'next')

def test_expiry_is_not_permission_to_resign(plan,store):
    reserve(store,plan);rpc=Rpc();engine=live.Engine(store,rpc,enabled=True)
    engine.submit('intent',signed(plan),100);rpc.height=101
    assert engine.track('intent')['state']=='EXPIRED_UNRESOLVED'
    with pytest.raises(live.ExecutionError,match='UNRESOLVED'):reserve(store,plan,'next')
    assert len([m for m,p in rpc.calls if m=='sendTransaction'])==1

def test_processed_slot_does_not_mean_finalized_fill(plan,store):
    reserve(store,plan);rpc=Rpc();engine=live.Engine(store,rpc,enabled=True);engine.submit('intent',signed(plan),100)
    rpc.status={'slot':123,'confirmationStatus':'processed','err':None}
    assert engine.track('intent')['state']=='LANDED'
    with pytest.raises(live.ExecutionError,match='UNRESOLVED'):reserve(store,plan,'next')

def finalized(row,delta,tokens,error=None):
    return dict(slot=123,transaction={'signatures':[row['signature']],'message':{'accountKeys':[row['wallet']]}},
      meta={'err':error,'fee':5000,'preBalances':[10**9],'postBalances':[10**9+delta],
       'preTokenBalances':[] if tokens>=0 else [{'owner':row['wallet'],'mint':row['mint'],'uiTokenAmount':{'amount':str(-tokens)}}],
       'postTokenBalances':[] if tokens<0 else [{'owner':row['wallet'],'mint':row['mint'],'uiTokenAmount':{'amount':str(tokens)}}]})

def test_finalized_receipt_cost_basis_pnl_and_idempotency(plan,store):
    reserve(store,plan);rpc=Rpc();engine=live.Engine(store,rpc,enabled=True)
    row=engine.submit('intent',signed(plan),100);rpc.status={'slot':123,'confirmationStatus':'finalized'}
    rpc.transaction=finalized(row,-105000,1000)
    assert engine.track('intent')['state']=='FINALIZED_SUCCESS'
    reserve(store,plan,'sell',side='SELL',amount=0,tokens=1000)
    row=engine.submit('sell',signed(plan,side='SELL',amount=1000),100)
    rpc.transaction=finalized(row,120000,-1000)
    assert engine.track('sell')['state']=='FINALIZED_SUCCESS'
    engine.track('sell')
    assert store.summary()['risk'][0]['realized']==15000

def test_failed_transactions_charge_fees_and_trip_breaker(plan,store):
    rpc=Rpc();engine=live.Engine(store,rpc,enabled=True)
    for i in range(3):
        ident=str(i);reserve(store,plan,ident);row=engine.submit(ident,signed(plan),100)
        rpc.status={'slot':123,'confirmationStatus':'finalized'};rpc.transaction=finalized(row,-5000,0,{'InstructionError':[0,'ComputationalBudgetExceeded']})
        assert engine.track(ident)['state']=='FINALIZED_FAILED'
    assert store.summary()['risk'][0]['failure_streak']==3
    with pytest.raises(live.ExecutionError,match='CIRCUIT_BREAKER'):reserve(store,plan,'fourth')

def test_day_loss_kill_and_unknown_equity(plan,store,tmp_path):
    with closing(store.connect()) as c:c.execute('UPDATE risk SET realized=-100000')
    with pytest.raises(live.ExecutionError,match='CIRCUIT_BREAKER'):reserve(store,plan)
    store.kill(limits()['day'])
    with pytest.raises(live.ExecutionError,match='KILL_SWITCH'):reserve(store,plan,'sell',side='SELL',amount=0,tokens=1)
    unknown=live.Store(tmp_path/'unknown.db')
    with pytest.raises(live.ExecutionError,match='EQUITY_NOT_INITIALIZED'):reserve(unknown,plan)

def test_kill_file_prevents_any_rpc(plan,store,tmp_path):
    reserve(store,plan);p=tmp_path/'KILL_SWITCH';p.write_text('stop');rpc=Rpc()
    with pytest.raises(live.ExecutionError,match='KILL_SWITCH_FILE'):live.Engine(store,rpc,enabled=True,kill_file=p).submit('intent',signed(plan),100)
    assert not rpc.calls

def test_network_balance_and_unknown_inventory_are_checked(plan,store):
    rpc=Rpc();engine=live.Engine(store,rpc,enabled=True);kp,names,_=plan
    engine.reserve_online('a',str(kp.pubkey()),names['base_mint'],'BUY',100000,0,10000,limits())
    assert {m for m,p in rpc.calls}>={'getBalance','getTokenAccountsByOwner'}
    with pytest.raises(live.ExecutionError,match='TOKEN_BALANCE'):reserve(live.Store(store.path),plan,'sell',side='SELL',amount=0,tokens=20000)

def test_rebuild_only_before_broadcast(plan,store):
    rpc=Rpc();rpc.sim_error='BlockhashNotFound';engine=live.Engine(store,rpc,enabled=True)
    calls=[]
    def builder(block,reason):
        calls.append((block['blockhash'],reason))
        if len(calls)==2:rpc.sim_error=None
        message=lab.build_message(plan[1],'BUY',100000,1,block['blockhash'])
        return base64.b64encode(bytes(VersionedTransaction(message,[plan[0]]))).decode()
    r=engine.execute_with_rebuild('a',str(plan[0].pubkey()),plan[1]['base_mint'],'BUY',100000,0,10000,limits(),builder)
    assert r['state']=='SUBMITTED' and len(calls)==2 and calls[0][0]!=calls[1][0]
    assert len([m for m,p in rpc.calls if m=='sendTransaction'])==1

def test_jito_tip_same_transaction_and_cap(plan,store):
    class Jito:
        def tip_accounts(self):return [account]
        def send(self,txs):self.sent=txs;return 'bundle-id'
    account=str(Keypair().pubkey());jito=Jito();reserve(store,plan)
    engine=live.Engine(store,Rpc(),enabled=True,jito=jito)
    encoded=signed(plan,tip_account=account,tip=1000)
    r=engine.submit('intent',encoded,100,mode='jito',tip_account=account,max_tip=1000)
    assert r['bundle_id']=='bundle-id' and jito.sent==[encoded]
    assert live.tip_lamports([{'landed_tips_50th_percentile':.000001}],1000)==1000
    with pytest.raises(live.ExecutionError,match='TIP_ABOVE_CAP'):live.tip_lamports([{'landed_tips_50th_percentile':.001}],1000)

def test_untrusted_program_mint_and_overspend_rejected(plan):
    from solders.system_program import transfer,TransferParams
    message=MessageV0.try_compile(plan[0].pubkey(),[transfer(TransferParams(from_pubkey=plan[0].pubkey(),to_pubkey=Keypair().pubkey(),lamports=1))],[],Hash.new_unique())
    with pytest.raises(signer.SigningError):signer.validate_message(message,str(plan[0].pubkey()),plan[2])
    for changes in [dict(enabled=False),dict(allowed_mints=[]),dict(max_order_lamports=1)]:
        msg=lab.build_message(plan[1],'BUY',100000,1,str(Hash.new_unique()))
        with pytest.raises(signer.SigningError):signer.validate_message(msg,str(plan[0].pubkey()),dict(plan[2],**changes))

def test_aes_gcm_vault_password_tamper_and_no_overwrite(plan,tmp_path):
    path=tmp_path/'vault.json';password='long test password 123'
    signer.create_vault(path,plan[0],password)
    assert bytes(signer.unlock(path,password))==bytes(plan[0])
    with pytest.raises(signer.SigningError):signer.unlock(path,'wrong')
    with pytest.raises(FileExistsError):signer.create_vault(path,plan[0],password)
    text=path.read_text();assert str(plan[0]) not in text
    d=json.loads(text);d['public_key']=str(Keypair().pubkey());path.write_text(json.dumps(d))
    with pytest.raises(signer.SigningError):signer.unlock(path,password)

def test_isolated_signer_real_signature(plan,tmp_path):
    vault=tmp_path/'wallet.json';policy=tmp_path/'policy.json';password='long offline test password'
    signer.create_vault(vault,plan[0],password);policy.write_text(json.dumps(plan[2]));__import__('wallet_manager').restrict_access(policy)
    message=lab.build_message(plan[1],'BUY',100000,1,str(Hash.new_unique()))
    encoded=signer.sign_isolated(base64.b64encode(to_bytes_versioned(message)).decode(),vault,policy,password)
    tx,sig=live.inspect_signed(encoded,str(plan[0].pubkey()))
    assert tx.verify_with_results()==[True]
    assert list(tmp_path.iterdir())==[vault,policy] or set(tmp_path.iterdir())=={vault,policy}

def test_main_strategy_stays_paper():
    import config
    assert config.EXECUTION_MODE=='paper'
    assert lab.status()['main_strategy_live_routing'] is False


def test_kill_after_reservation_prevents_send(plan,store):
    reserve(store,plan);store.kill(limits()['day']);rpc=Rpc()
    with pytest.raises(live.ExecutionError,match='KILL_SWITCH'):live.Engine(store,rpc,enabled=True).submit('intent',signed(plan),100)
    assert not rpc.calls


def test_wallet_journal_is_shared_across_installations(plan,store,tmp_path):
    other=live.Store(tmp_path/'separate.db');other.begin_day(limits()['day'],1000000);reserve(other,plan)
    with pytest.raises(live.ExecutionError,match='CANONICAL'):live.Engine(other,Rpc(),enabled=True).submit('intent',signed(plan),100)


def test_intent_budget_cannot_be_exceeded_by_signed_message(plan,store):
    reserve(store,plan)
    with pytest.raises(signer.SigningError,match='BUY_LIMIT'):live.Engine(store,Rpc(),enabled=True).submit('intent',signed(plan,amount=500000),100)


def test_simulated_total_debit_includes_rent_and_fees(plan,store):
    reserve(store,plan)
    class Expensive(Rpc):
        def call(self,m,p):
            r=super().call(m,p)
            if m=='simulateTransaction':r['value']['accounts'][0]['lamports']=10**9-3000000
            return r
    rpc=Expensive();r=live.Engine(store,rpc,enabled=True).submit('intent',signed(plan),100)
    assert r['state']=='PREFLIGHT_REJECTED'
    assert not any(m=='sendTransaction' for m,p in rpc.calls)
