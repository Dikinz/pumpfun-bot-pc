import base64,json,struct
import pytest
from solders.pubkey import Pubkey
import execution_verification as v,exit_quotes as q,config

MINT='DgLZBPedMxSx5FQc9Ts1ML2CcwRxUjtLCn8orP3bpump'
ADDRESS='4sEDC8ikZt7bdfPqJNNszftMs32WNeX9DYmBfdZnqZqF'

def account(real=100000000,lamports=200000000):
    raw=bytearray(166);raw[:8]=v.DISC
    struct.pack_into('<5Q',raw,8,1073000000000000,30000000008,793100000000000,real,1000000000000000)
    return {'owner':v.PUMP,'executable':False,'lamports':lamports,'data':[base64.b64encode(raw).decode(),'base64']}

@pytest.fixture
def case(monkeypatch):
    job={'mint':MINT,'slippage_bps':100}
    inv={'decimals':6};quote={'raw_amount':'892271392151','out_lamports':'24590089','floor_lamports':'22550614','received_ts':q.time.time(),
      'response':{'routePlan':[{'swapInfo':{'ammKey':ADDRESS,'label':'Pump.fun','inputMint':MINT,'outputMint':q.SOL,'inAmount':'892271392151','outAmount':'24614703'}}]}}
    mint=bytearray(82);mint[44]=6;mint[45]=1
    batch={'context':{'slot':100},'value':[account(),{'owner':sorted(v.TOKEN_PROGRAMS)[0],'data':[base64.b64encode(mint).decode(),'base64']}]}
    def rpc(method,params,stage='RESERVES'):
        if method=='getMultipleAccounts':return batch
        if method=='getMinimumBalanceForRentExemption':return 1493520
        raise AssertionError(method)
    monkeypatch.setattr(v,'rpc',rpc);v.RENT_CACHE.clear();monkeypatch.setattr(config,'SIMULATION_PUBLIC_KEY','')
    return job,inv,quote,batch

def test_official_pda_and_current_real_snapshot():
    assert v.curve_address(MINT)==ADDRESS
    d=json.load(open('research/current_curve_DgLZ.json'))
    curve=v.decode_curve(d['result']['value'])
    assert curve['real_sol']==8 and curve['virtual_sol']==30000000008

def test_virtual_liquidity_cannot_pass_real_reserve_gate(case):
    j,i,q,b=case;b['value'][0]=account(real=8,lamports=1493528)
    r=v.verify(j,i,q)
    assert r['reserves']['status']=='INSUFFICIENT_REAL_RESERVES'
    assert r['simulation']['status']=='SKIPPED_RESERVE_GATE'
    assert r['reserves']['spendable_lamports']=='8'

def test_rent_is_not_spendable(case):
    j,i,q,b=case;b['value'][0]=account(real=100000000,lamports=1493528)
    assert v.verify(j,i,q)['reserves']['status']=='INSUFFICIENT_REAL_RESERVES'

def test_cover_does_not_fake_simulation(case):
    j,i,q,b=case;r=v.verify(j,i,q)
    assert r['reserves']['status']=='RESERVES_COVER_UPPER_BOUND'
    assert r['simulation']['status']=='NOT_CONFIGURED'

def test_wrong_pda_cannot_be_used(case):
    j,i,q,b=case;q['response']['routePlan'][0]['swapInfo']['ammKey']='11111111111111111111111111111111'
    assert v.verify(j,i,q)['reserves']['status']=='CURVE_ADDRESS_MISMATCH'

@pytest.mark.parametrize('field,value,status',[
 ('owner','11111111111111111111111111111111','WRONG_CURVE_OWNER'),
 ('data',[base64.b64encode(bytes(166)).decode(),'base64'],'UNSUPPORTED_CURVE_LAYOUT')])
def test_wrong_owner_or_layout(case,field,value,status):
    j,i,q,b=case;b['value'][0][field]=value
    assert v.verify(j,i,q)['reserves']['status']==status

def test_complex_route_never_silently_passes(case):
    j,i,q,b=case;q['response']['routePlan']*=2
    assert v.verify(j,i,q)['reserves']['status']=='UNSUPPORTED_ROUTE'

def test_no_wallet_tokens_distinct_from_pool_failure(case,monkeypatch):
    j,i,q,b=case;res=v.reserve_check(j,i,q)
    monkeypatch.setattr(config,'SIMULATION_PUBLIC_KEY',str(Pubkey.new_unique()))
    monkeypatch.setattr(v,'rpc',lambda *a,**k:{'value':[]})
    assert v.simulate(j,i,res,q)['status']=='INSUFFICIENT_WALLET_TOKENS'

def test_simulation_uses_only_rpc_not_broadcast(case,monkeypatch):
    from solders.instruction import Instruction
    from solders.message import Message
    from solders.hash import Hash
    from solders.transaction import Transaction
    j,i,quote,b=case;res=v.reserve_check(j,i,quote)
    wallet=Pubkey.new_unique();monkeypatch.setattr(config,'SIMULATION_PUBLIC_KEY',str(wallet))
    tx=Transaction.new_unsigned(Message.new_with_blockhash([Instruction(Pubkey.from_string(v.PUMP),b"test",[])],wallet,Hash.default()))
    data=dict(quote['response'],inputMint=MINT,outputMint=q.SOL,inAmount=quote['raw_amount'],outAmount=quote['out_lamports'],otherAmountThreshold='24344188',swapMode='ExactIn',slippageBps=100,router='metis',transaction=base64.b64encode(bytes(tx)).decode())
    monkeypatch.setattr(q,'request_json',lambda *a,**k:data)
    monkeypatch.setattr(v,'reserve_check',lambda *a:res)
    calls=[]
    def rpc(method,params,stage):
        calls.append(method)
        if method=='getTokenAccountsByOwner':return {'value':[{'account':{'data':{'parsed':{'info':{'tokenAmount':{'amount':quote['raw_amount']}}}}}}]}
        if method=='getBalance':return {'value':1000000}
        assert method=='simulateTransaction'
        assert params[1]['sigVerify'] is False and params[1]['replaceRecentBlockhash'] is True
        return {'context':{'slot':101},'value':{'err':None,'fee':5000,'unitsConsumed':80000,'logs':['test']}}
    monkeypatch.setattr(v,'rpc',rpc)
    r=v.simulate(j,i,res,quote)
    assert r['status']=='SIMULATION_OK' and r['network_fee_lamports']==5000
    assert calls==['getTokenAccountsByOwner','getBalance','simulateTransaction']
    assert 'transaction' not in r and 'transaction' not in r['builder_cost_fields']

def test_rpc_allowlist_rejects_send():
    with pytest.raises(v.VerificationError):v.rpc('sendTransaction',[])
