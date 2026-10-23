import base64,json,time
from datetime import datetime,timezone
from contextlib import closing
import pytest
from solders.hash import Hash
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solders.keypair import Keypair
import jito_execution as jito,live_execution as live,wallet_signer
from test_execution_safety_v52 import plan,store,Rpc,limits,finalized

TIP=str(Keypair().pubkey())
def floor(**kwargs):
    result=dict(time=datetime.now(timezone.utc).isoformat(),landed_tips_75th_percentile=.00001,landed_tips_95th_percentile=.00002);result.update(kwargs);return [result]
def config(plan):
    kp,names,_=plan
    return dict(wallet=str(kp.pubkey()),mint=names['base_mint'],side='BUY',amount=1000,limit=2000,fee_budget=100000,
        expected_genesis='chain',risk_limits=limits(),jito=dict(max_tip_lamports=50000,max_attempts=2,priority_micro_lamports=5000))
def message(plan):
    import direct_pump_v2 as direct
    b=json.load(open('research/local_snapshot_v26.json'));n=b['account_names'];a=b['snapshot']['accounts']
    names=direct.accounts(b['mint'],str(plan[0].pubkey()),a[n['bonding_curve']],a[n['base_mint']],a[n['global']])
    return direct.build(names,'BUY',1000,2000,str(Hash.new_unique()))
class BlockEngine:
    def __init__(self):self.sent=[];self.bundle=None;self.inflight_state='Pending';self.transport_fail=False
    def tip_accounts(self):return [TIP]
    def tip_floor(self):return floor()
    def send(self,txs):
        self.sent.append(txs)
        if self.transport_fail:raise TimeoutError()
        return 'a'*64
    def status(self,bundle):return {'value':[self.bundle]}
    def inflight(self,bundle):return {'value':[dict(bundle_id=bundle,status=self.inflight_state)]}
class Chain(Rpc):
    def __init__(self):super().__init__();self.slot=10
    def call(self,m,p):
        if m=='getSlot':self.slot+=2;return self.slot
        return super().call(m,p)

def submit(plan,store):
    p=config(plan);chain=Chain();be=BlockEngine();ident='jito-test'
    msg=jito.compose(message(plan),TIP,10000,5000)
    encoded=base64.b64encode(bytes(VersionedTransaction(msg,[plan[0]]))).decode()
    live.Engine(store,chain,enabled=True,jito=be).reserve_online(ident,p['wallet'],p['mint'],'BUY',2000,1000,100000,limits())
    jito.initialize(store);jito.record(store,ident,'group',0,10,10000,5000,msg,p)
    live.Engine(store,chain,enabled=True,jito=be).submit(ident,encoded,100,mode='jito',tip_account=TIP,max_tip=50000)
    return chain,be,ident

def test_disabled_by_default():
    with pytest.raises(live.ExecutionError,match='BROADCAST_DISABLED'):jito.send(None,{},None,None)
@pytest.mark.parametrize('percentile,expected',[(75,10000),(95,20000)])
def test_tip_selection(percentile,expected):assert jito.choose_tip(floor(),50000,percentile)==expected
@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,'bad'])
def test_invalid_tip(value):
    with pytest.raises(live.ExecutionError):jito.choose_tip(floor(landed_tips_75th_percentile=value),50000)
def test_tip_ceiling_stale_and_cap():
    assert jito.choose_tip(floor(landed_tips_75th_percentile='0.00000100001'),50000)==1001
    with pytest.raises(live.ExecutionError,match='STALE'):jito.choose_tip(floor(time='2020-01-01T00:00:00Z'),50000)
    with pytest.raises(live.ExecutionError,match='CAP'):jito.choose_tip(floor(),9999)
def test_tip_and_swap_one_signed_transaction(plan):
    msg=jito.compose(message(plan),TIP,10000,5000)
    p=config(plan);wallet_signer.validate_message(msg,p['wallet'],dict(enabled=True,allowed_mints=[p['mint']],max_order_lamports=2000,max_compute_units=200000,max_priority_lamports=1000,max_tip_lamports=10000,tip_accounts=[TIP]))
    tx=VersionedTransaction(msg,[plan[0]]);tx.verify_and_hash_message();live.verify_tip(tx,p['wallet'],TIP,10000)
    assert len(bytes(tx))<=1232
    with pytest.raises(wallet_signer.SigningError,match='PRIORITY'):wallet_signer.validate_message(msg,p['wallet'],dict(enabled=True,allowed_mints=[p['mint']],max_order_lamports=2000,max_compute_units=200000,max_priority_lamports=999,max_tip_lamports=10000,tip_accounts=[TIP]))
def test_pending_after_two_slots_does_not_release_wallet(plan,store):
    rpc,be,ident=submit(plan,store)
    result=jito.track(store,rpc,be,ident)
    assert result['landing']['two_slot_deadline_reached']
    assert not result['landing']['rebuild_allowed'] and result['state']=='SUBMITTED'
    with pytest.raises(live.ExecutionError,match='UNRESOLVED'):live.Store(store.path).reserve('next',config(plan)['wallet'],config(plan)['mint'],'BUY',1,0,10000,10**9,0,limits())
@pytest.mark.parametrize('state',['Pending','Invalid','Landed'])
def test_ambiguous_status_never_retry(state,plan,store):
    rpc,be,ident=submit(plan,store);be.inflight_state=state;rpc.height=101
    result=jito.track(store,rpc,be,ident)
    assert result['state']=='EXPIRED_UNRESOLVED' and not result['landing']['rebuild_allowed']
def test_explicit_not_forwarded_releases_once(plan,store):
    rpc,be,ident=submit(plan,store);be.inflight_state='Failed'
    assert jito.track(store,rpc,be,ident)['state']=='JITO_NOT_FORWARDED'
    assert jito.track(store,rpc,be,ident)['landing']['rebuild_allowed']
    assert store.summary()['risk'][0]['failure_streak']==1
    p=config(plan);store.reserve('next',p['wallet'],p['mint'],'BUY',2000,0,10000,10**9,0,limits())
def test_failed_bundle_but_observed_signature_not_retryable(plan,store):
    rpc,be,ident=submit(plan,store);be.inflight_state='Failed';rpc.status=dict(slot=20,confirmationStatus='processed')
    result=jito.track(store,rpc,be,ident)
    assert result['state']=='LANDED' and not result['landing']['rebuild_allowed']
def test_bundle_query_failure_never_releases(plan,store):
    rpc,be,ident=submit(plan,store)
    def broken(*a):raise TimeoutError()
    be.status=broken;be.inflight_state='Failed'
    result=jito.track(store,rpc,be,ident)
    assert result['state']=='SUBMITTED' and not result['landing']['rebuild_allowed']
def test_bundle_confirmation_and_finalized_costs(plan,store):
    rpc,be,ident=submit(plan,store);row=store.get(ident)
    be.bundle=dict(bundle_id='a'*64,transactions=[row['signature']],slot=123,confirmation_status='finalized',err=None);be.inflight_state='Landed'
    rpc.status=dict(slot=123,confirmationStatus='finalized');rpc.transaction=finalized(row,-17000,1000)
    result=jito.track(store,rpc,be,ident)
    assert result['state']=='FINALIZED_SUCCESS' and result['landing']['bundle_state']=='LANDED'
    assert json.loads(store.get(ident)['result_json'])['wallet_delta_lamports']==-17000
    assert jito.summary(store)['inclusion_rate']==1
@pytest.mark.parametrize('mutate',[lambda x:x.update(transactions=['wrong']),lambda x:x.update(bundle_id='b'*64)])
def test_foreign_bundle_evidence_rejected(mutate,plan,store):
    rpc,be,ident=submit(plan,store);be.inflight_state='Failed'
    be.bundle=dict(bundle_id='a'*64,transactions=[store.get(ident)['signature']],slot=123,confirmation_status='finalized');mutate(be.bundle)
    result=jito.track(store,rpc,be,ident)
    assert result['state']=='SUBMITTED' and not result['landing']['rebuild_allowed']

def test_send_rebuilds_only_proven_rejection_and_bumps_tip(plan,store,monkeypatch):
    rpc=Chain();be=BlockEngine();be.inflight_state='Failed';blocks=[]
    def prepare(*a):
        msg=message(plan);blocks.append(str(msg.recent_blockhash));return msg,dict(blockhash=str(msg.recent_blockhash),lastValidBlockHeight=100),{}
    monkeypatch.setattr(jito.direct,'prepare',prepare)
    sign=lambda msg:base64.b64encode(bytes(VersionedTransaction(msg,[plan[0]]))).decode()
    result=jito.send(rpc,config(plan),sign,store,enabled=True,jito=be,timeout=5)
    assert len(be.sent)==2 and blocks[0]!=blocks[1] and be.sent[0]!=be.sent[1]
    assert result['tip_lamports']==12500 and result['attempt']==1
    assert not any(m=='sendTransaction' for m,p in rpc.calls)
    assert all(p[1]['sigVerify'] for m,p in rpc.calls if m=='simulateTransaction')
def test_send_timeout_does_not_retry(plan,store,monkeypatch):
    rpc=Chain();be=BlockEngine();be.transport_fail=True
    msg=message(plan)
    monkeypatch.setattr(jito.direct,'prepare',lambda *a:(msg,dict(blockhash=str(msg.recent_blockhash),lastValidBlockHeight=100),{}))
    sign=lambda msg:base64.b64encode(bytes(VersionedTransaction(msg,[plan[0]]))).decode()
    result=jito.send(rpc,config(plan),sign,store,enabled=True,jito=be,timeout=.1)
    assert len(be.sent)==1 and result['state']=='UNKNOWN' and not result['bundle_id']

def test_transport_encoding_validation_and_bundle_size(monkeypatch):
    be=jito.Jito();calls=[]
    monkeypatch.setattr(be,'call',lambda m,p:(calls.append((m,p)) or 'a'*64))
    assert be.send(['signed'])=='a'*64
    assert calls==[('sendBundle',[['signed'],{'encoding':'base64'}])]
    for n in (0,6):
        with pytest.raises(live.ExecutionError,match='BUNDLE_SIZE'):be.send(['x']*n)
    monkeypatch.setattr(be,'call',lambda *a:'invalid')
    with pytest.raises(live.ExecutionError,match='BUNDLE_ID'):be.send(['signed'])
def test_fresh_hash_required_on_rebuild(plan,store,monkeypatch):
    rpc=Chain();be=BlockEngine();be.inflight_state='Failed';msg=message(plan)
    monkeypatch.setattr(jito.direct,'prepare',lambda *a:(msg,dict(blockhash=str(msg.recent_blockhash),lastValidBlockHeight=100),{}))
    sign=lambda msg:base64.b64encode(bytes(VersionedTransaction(msg,[plan[0]]))).decode()
    with pytest.raises(live.ExecutionError,match='FRESH_BLOCKHASH'):jito.send(rpc,config(plan),sign,store,enabled=True,jito=be)
    assert len(be.sent)==1

def test_bump_does_not_raise_approved_cap(plan,store,monkeypatch):
    rpc=Chain();be=BlockEngine();be.inflight_state='Failed';p=config(plan);p['jito']['max_tip_lamports']=10000
    monkeypatch.setattr(jito.direct,'prepare',lambda *a:(message(plan),dict(blockhash=str(Hash.new_unique()),lastValidBlockHeight=100),{}))
    sign=lambda msg:base64.b64encode(bytes(VersionedTransaction(msg,[plan[0]]))).decode()
    with pytest.raises(live.ExecutionError,match='TIP_ABOVE_CAP'):jito.send(rpc,p,sign,store,enabled=True,jito=be)
    assert len(be.sent)==1

def test_signer_cannot_substitute_message(plan,store,monkeypatch):
    rpc=Chain();be=BlockEngine();msg=message(plan)
    monkeypatch.setattr(jito.direct,'prepare',lambda *a:(msg,dict(blockhash=str(msg.recent_blockhash),lastValidBlockHeight=100),{}))
    sign=lambda ignored:base64.b64encode(bytes(VersionedTransaction(message(plan),[plan[0]]))).decode()
    with pytest.raises(live.ExecutionError,match='SIGNED_MESSAGE_MISMATCH'):jito.send(rpc,config(plan),sign,store,enabled=True,jito=be)
    assert not be.sent and store.summary()['states']=={'BUILD_REJECTED':1}

def test_jito_failed_finalized_charges_fee_not_tip(plan,store):
    rpc,be,ident=submit(plan,store);row=store.get(ident)
    rpc.status=dict(slot=123,confirmationStatus='finalized',err={'InstructionError':[2,'Custom']})
    rpc.transaction=finalized(row,-5000,0,{'InstructionError':[2,'Custom']})
    result=jito.track(store,rpc,be,ident)
    assert result['state']=='FINALIZED_FAILED' and result['paid_tip_lamports']==0
    assert result['finalized_accounting']['fee_lamports']==5000
    assert result['finalized_accounting']['wallet_delta_lamports']==-5000

def test_unknown_survives_reopen_and_track(plan,store):
    rpc,be,ident=submit(plan,store);store.update(ident,'UNKNOWN',bundle_id=None)
    reopened=live.Store(store.path);result=jito.track(reopened,rpc,be,ident)
    assert result['state']=='UNKNOWN' and not result['landing']['rebuild_allowed']
    p=config(plan)
    with pytest.raises(live.ExecutionError,match='UNRESOLVED'):reopened.reserve('new',p['wallet'],p['mint'],'BUY',1,0,10000,10**9,0,limits())

def test_readiness_and_export_include_landing_without_vault_secrets(plan,store,tmp_path):
    import execution_lab
    rpc,be,ident=submit(plan,store)
    vault=wallet_signer.PRIVATE/'wallet.enc.json';vault.write_text(json.dumps(dict(public_key=config(plan)['wallet'],ciphertext='DO_NOT_EXPORT_CIPHERTEXT')))
    jito.track(store,rpc,be,ident)
    before=store.path.read_bytes()
    readiness=execution_lab.status()
    assert readiness['jito_phase3']['available'] and not readiness['main_strategy_live_routing']
    assert readiness['jito_landing']['accepted_ids']==1 and readiness['jito_landing']['inclusion_rate']==0
    execution_lab.export(tmp_path)
    import zipfile
    with zipfile.ZipFile(tmp_path/'execution_journal.zip') as z:
        assert 'DO_NOT_EXPORT_CIPHERTEXT' not in str(z.read('readiness.json'))
        assert set(z.namelist())=={'readiness.json','execution_journal.db'}
    assert store.path.read_bytes()==before
