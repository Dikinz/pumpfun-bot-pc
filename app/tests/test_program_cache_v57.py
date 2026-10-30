import base64,copy,json,sqlite3,struct
from types import SimpleNamespace
import pytest
from solders.pubkey import Pubkey
import local_simulation as lab,program_cache as cache

def account(data,owner=cache.LOADER,executable=False):return dict(data=[base64.b64encode(data).decode(),'base64'],owner=owner,executable=executable,space=len(data),lamports=1000,rentEpoch=0)
class RPC:
    def __init__(self):
        self.pid=str(Pubkey.new_unique());self.pd=str(Pubkey.new_unique());self.owner=cache.LOADER;self.upgrade=10;self.root=100;self.slot=101;self.calls=[];self.market=b'fresh';self.change=False;self.missing=False;self.bad_owner=False
        self.config=SimpleNamespace(QUOTE_RPC_URL='https://test.invalid/SECRET',PROGRAM_CACHE_ENABLED=True)
        self.program_data_address=lab.program_data_address
    @property
    def code(self):return struct.pack('<IQB',3,self.upgrade,1)+bytes(Pubkey.default())+b'\x7fELF'+b'code'+bytes([self.upgrade])
    def read_accounts(self,addresses,min_slot=None,*,data_slice=None,commitment='confirmed'):
        self.calls.append((list(addresses),min_slot,data_slice,commitment))
        if data_slice and self.change:self.upgrade+=1;self.change=False
        slot=self.root if commitment=='finalized' else max(self.slot,min_slot or 0)
        values={self.pid:account(struct.pack('<I',2)+bytes(Pubkey.from_string(self.pd)),executable=True),self.pd:account(self.code,owner='wrong' if self.bad_owner else self.owner),'market':account(self.market,owner='market-owner')}
        if self.missing:values[self.pd]=None
        out={}
        for address in addresses:
            a=copy.deepcopy(values[address])
            if data_slice and a:a['data'][0]=base64.b64encode(base64.b64decode(a['data'][0])[:data_slice]).decode()
            out[address]=a
        return dict(slot=slot,started_ts=1,received_ts=2,accounts=out)
    def capture(self,store):return cache.capture_bank(self,['market',self.pid],{self.pid:self.pd},90,store=store,chain='test-chain')

@pytest.fixture
def setup(tmp_path):return RPC(),cache.Store(tmp_path/'cache.db')

def test_cold_finalized_then_warm_never_fetches_full_bytecode(setup):
    rpc,store=setup;first=rpc.capture(store);rpc.calls=[];rpc.market=b'new-market'
    second=rpc.capture(store)
    assert first['program_cache']['downloads']==1 and second['program_cache']['hits']==1
    assert all(commitment!='finalized' for a,m,d,commitment in rpc.calls)
    assert all(rpc.pd not in a or d==45 for a,m,d,commitment in rpc.calls)
    assert cache.raw(second['accounts']['market'])==b'new-market'
    assert cache.raw(first['accounts'][rpc.pd])==cache.raw(second['accounts'][rpc.pd])
    assert second['program_cache']['trading_slot']<=second['program_cache']['metadata_slot']

def test_upgrade_during_check_evicts_and_uses_fresh_full_bank(setup):
    rpc,store=setup;rpc.capture(store);rpc.change=True;rpc.calls=[]
    result=rpc.capture(store)
    assert result['program_cache']['mode']=='FULL_FRESH'
    assert result['program_cache']['fallback_reason']=='VERSION_OR_METADATA_CHANGED'
    assert cache.raw(result['accounts'][rpc.pd])==rpc.code
    assert rpc.calls[-1][2] is None and rpc.calls[-1][1]>=101
    rpc.calls=[];rpc.capture(store);assert any(c=='finalized' for a,m,d,c in rpc.calls)

def test_changed_authority_or_space_misses_fresh_metadata(setup,monkeypatch):
    rpc,store=setup;rpc.capture(store);original=rpc.read_accounts
    def changed(*a,**k):
        result=original(*a,**k)
        if k.get('data_slice'):
            result['accounts'][rpc.pd]['space']+=1
        return result
    monkeypatch.setattr(rpc,'read_accounts',changed)
    assert rpc.capture(store)['program_cache']['mode']=='FULL_FRESH'

def test_corrupt_cached_bytes_trigger_download(setup):
    rpc,store=setup;rpc.capture(store)
    with sqlite3.connect(store.path) as c:
        row=c.execute('SELECT account FROM programs').fetchone();a=json.loads(row[0]);a['data'][0]=base64.b64encode(rpc.code[:-1]+b'X').decode();c.execute('UPDATE programs SET account=?',(json.dumps(a),))
    rpc.calls=[];result=rpc.capture(store)
    assert result['program_cache']['downloads']==1 and result['program_cache']['hits']==0
    assert cache.raw(result['accounts'][rpc.pd])==rpc.code

@pytest.mark.parametrize('field',['missing','bad_owner'])
def test_unverifiable_metadata_never_serves_cached_success(field,setup):
    rpc,store=setup;rpc.capture(store);setattr(rpc,field,True)
    with pytest.raises((cache.CacheError,lab.LabError)):rpc.capture(store)

def test_endpoint_namespace_change_prevents_cross_rpc_reuse(setup):
    rpc,store=setup;rpc.capture(store);rpc.config.QUOTE_RPC_URL='https://different.invalid'
    assert rpc.capture(store)['program_cache']['downloads']==1

def test_readonly_cache_still_uses_verified_network_code(setup,tmp_path):
    rpc,_=setup;p=tmp_path/'file';p.write_text('x');result=rpc.capture(cache.Store(p/'impossible.db'))
    assert result['program_cache']['downloads']==1 and cache.raw(result['accounts'][rpc.pd])==rpc.code

def test_same_slot_code_not_seeded_as_rooted(setup):
    rpc,store=setup;rpc.root=rpc.upgrade
    result=rpc.capture(store)
    assert result['program_cache']['mode']=='FULL_FRESH'
    if store.path.exists():
        with sqlite3.connect(store.path) as c:assert c.execute('SELECT COUNT(*) FROM programs').fetchone()[0]==0

def test_disabled_cache_uses_one_full_bank_without_genesis(setup):
    rpc,store=setup;rpc.config.PROGRAM_CACHE_ENABLED=False
    result=rpc.capture(store)
    assert len(rpc.calls)==1 and result['program_cache']['fallback_reason']=='DISABLED'

def test_stale_live_bank_rejected(setup,monkeypatch):
    rpc,store=setup;rpc.capture(store);original=rpc.read_accounts
    def stale(*a,**k):
        result=original(*a,**k)
        if a[0]==['market',rpc.pid]:result['slot']=1
        return result
    monkeypatch.setattr(rpc,'read_accounts',stale)
    with pytest.raises(cache.CacheError,match='STALE'):rpc.capture(store)

@pytest.mark.parametrize('kind',['pump','amm'])
def test_verified_cached_real_programs_preserve_vm_effects(kind,tmp_path):
    import zipfile
    from pathlib import Path
    import amm_simulation as amm
    if kind=='pump':b=json.loads(Path('research/local_snapshot_v26.json').read_text());b['minimum']='1'
    else:
        with zipfile.ZipFile('research/amm_fixture_v36.zip') as z:b=json.loads(z.read('snapshot.json'))
    original=b['snapshot'];programs=b['program_data'];calls=[]
    def read(addresses,min_slot=None,*,data_slice=None,commitment='confirmed'):
        calls.append((list(addresses),data_slice,commitment));out={}
        for addr in addresses:
            a=copy.deepcopy(original['accounts'][addr])
            if data_slice and a:a['data'][0]=base64.b64encode(cache.raw(a)[:data_slice]).decode()
            out[addr]=a
        return dict(original,accounts=out)
    fake=SimpleNamespace(read_accounts=read,program_data_address=lab.program_data_address,config=SimpleNamespace(QUOTE_RPC_URL='https://offline.invalid',PROGRAM_CACHE_ENABLED=True))
    store=cache.Store(tmp_path/'fixture.db');addresses=[a for a in original['accounts'] if a not in programs.values()]
    first=cache.capture_bank(fake,addresses,programs,original['slot'],store,'fixture-chain')
    calls.clear();second=cache.capture_bank(fake,addresses,programs,original['slot'],store,'fixture-chain')
    assert second['program_cache']['hits']==len(programs)
    assert not any(c=='finalized' for a,d,c in calls)
    assert second['accounts']==original['accounts']
    run=lab.run_snapshot if kind=='pump' else amm.run_snapshot
    old=run(b);new=run(dict(b,snapshot=second))
    for key in ('status','tokens_debited','pool_tokens_credited','quote_received_lamports','seller_net_sol_delta_lamports','wallet_sol_delta_lamports','net_after_setup_lamports'):
        assert new.get(key)==old.get(key),key

def test_metadata_proof_does_not_refresh_trading_snapshot_time(setup,monkeypatch):
    rpc,store=setup;rpc.capture(store);original=rpc.read_accounts
    def read(*a,**k):
        result=original(*a,**k)
        if k.get('data_slice'):result['received_ts']=999999
        return result
    monkeypatch.setattr(rpc,'read_accounts',read)
    assert rpc.capture(store)['received_ts']==2

def test_upgrade_authority_change_forces_full_bank(setup,monkeypatch):
    rpc,store=setup;rpc.capture(store);original=rpc.read_accounts
    def read(*a,**k):
        result=original(*a,**k)
        if k.get('data_slice'):
            data=bytearray(cache.raw(result['accounts'][rpc.pd]));data[13]=123
            result['accounts'][rpc.pd]['data'][0]=base64.b64encode(data).decode()
        return result
    monkeypatch.setattr(rpc,'read_accounts',read)
    assert rpc.capture(store)['program_cache']['mode']=='FULL_FRESH'
