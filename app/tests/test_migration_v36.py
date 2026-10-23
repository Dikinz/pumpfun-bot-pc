import asyncio,copy,json,struct,time,zipfile,base64
from pathlib import Path
from contextlib import closing
import pytest
import amm_simulation as amm,position_monitor as pm,monitor_service as ms,cohort_report as cohort,paper_capital as cap
from test_primary_integrity import fresh
from test_delta_v34 import prepare

@pytest.fixture
def bundle():
    with zipfile.ZipFile(Path(__file__).parents[1]/'research/amm_fixture_v36.zip') as z:return json.loads(z.read('snapshot.json'))

def rewrite(bundle,addr,offset,data):
    acc=bundle['snapshot']['accounts'][addr];raw=bytearray(base64.b64decode(acc['data'][0]));raw[offset:offset+len(data)]=data;acc['data']=[base64.b64encode(raw).decode(),'base64']

def field_offset(name):
    at=8
    for f in next(x for x in amm.IDL['types'] if x['name']=='Pool')['type']['fields']:
        if f['name']==name:return at
        at+={'u8':1,'u16':2,'pubkey':32,'u64':8,'bool':1,'i128':16}[f['type']]

def test_pool_identity_and_vault_identity_are_verified(bundle):
    assert amm.reserves(bundle)['status']=='AMM_RESERVES_AVAILABLE'
    bad=copy.deepcopy(bundle);rewrite(bad,bad['account_names']['pool'],field_offset('base_mint'),bytes(32))
    with pytest.raises(amm.AmmError,match='AMM_POOL_ADDRESS_MISMATCH'):amm.reserves(bad)
    bad=copy.deepcopy(bundle);rewrite(bad,bad['account_names']['pool_base_token_account'],32,bytes(32))
    with pytest.raises(amm.AmmError,match='AMM_VAULT_IDENTITY_MISMATCH'):amm.reserves(bad)

def test_signed_effective_reserve_not_double_subtracted(bundle):
    q=int(amm.reserves(bundle)['quote_raw']);pool=bundle['account_names']['pool']
    rewrite(bundle,pool,field_offset('virtual_quote_reserves'),(-1000).to_bytes(16,'little',signed=True))
    rewrite(bundle,pool,field_offset('protocol_fees'),(1000).to_bytes(8,'little'))
    assert int(amm.reserves(bundle)['effective_quote_raw'])==q-1000
    rewrite(bundle,pool,field_offset('virtual_quote_reserves'),(-q-1).to_bytes(16,'little',signed=True))
    with pytest.raises(amm.AmmError,match='NEGATIVE_EFFECTIVE'):amm.reserves(bundle)

def test_missing_pool_does_not_mean_sale_failed(bundle):
    with pytest.raises(amm.AmmError,match='AMM_POOL_NOT_FOUND') as ex:amm.names_for(bundle['mint'],bundle['wallet'],{})
    assert ex.value.details['expected_pool']==bundle['account_names']['pool']

def test_real_vm_exact_position_volume_and_zero_control(bundle,monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket,'connect',lambda *x: (_ for _ in ()).throw(AssertionError('No network in replay')))
    r=amm.run_snapshot(bundle);zero=amm.run_snapshot(bundle,zero=True)
    assert r['status']=='AMM_SALE_EXECUTED' and r['tokens_debited']==bundle['amount'] and r['pool_tokens_credited']==bundle['amount']
    assert r['local_fee_lamports']==5000 and zero['status']=='AMM_SIMULATION_FAILED'
    assert r['net_after_setup_lamports']==r['quote_received_lamports']+r['wallet_sol_delta_lamports']-r['base_account_rent_lamports']-r['quote_account_rent_lamports']
    assert r['broadcast'] is False and r['paper_mutated'] is False

def test_separate_child_replay_and_archives(bundle,tmp_path):
    fixture=Path(__file__).parents[1]/'research/amm_fixture_v36.zip'
    job=dict(id='TESTAMM',route='PUMP_AMM',mint=bundle['mint'],scheduled_ts=time.time(),wallet_state={'wallet':bundle['wallet'],'volume':None},inventory={'token_units':'1','raw_amount':bundle['amount'],'decimals':6})
    r=asyncio.run(pm.launch_check(job,tmp_path,replay=fixture))
    assert r['status']=='AMM_SALE_EXECUTED' and r['zero_token_control_ok']
    with zipfile.ZipFile(tmp_path/'evidence.zip') as z:
        assert 'bundle.json' in z.namelist() and json.loads(z.read('result.json'))['sale']['raw_amount']==bundle['amount']

def test_migration_is_not_error_and_cooldown_survives_timeout(fresh):
    tid=prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job=pm.next_job(c,now);pm.finish(c,job,{'status':pm.MIGRATION_STATUS},now=now+1)
        assert cohort.health(c,now+1)['errors_in_row']==0
        assert cohort.health(c,now+1)['state']=='MIGRATION_LAB'
        assert pm.next_job(c,now+1.5) is None
        j=pm.next_job(c,now+2);assert j['route']=='PUMP_AMM'
        pm.finish(c,j,{'status':'REQUEST_TIMEOUT'},now=now+27)
        assert pm.next_job(c,now+61) is None
        assert not ms.overdue(c,now+61)
        j2=pm.next_job(c,now+62);assert j2['route']=='PUMP_AMM'

def test_amm_sale_does_not_select_trading_rule_or_change_capital(fresh,bundle):
    tid=prepare(fresh);before=cap.summary(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job=pm.next_job(c,now);pm.finish(c,job,{'status':pm.MIGRATION_STATUS},now=now)
        counts=c.execute('SELECT count(*) FROM rules_decisions_v0733').fetchone()[0]
        job=pm.next_job(c,now+2)
        pm.finish(c,job,{'status':'AMM_SALE_EXECUTED','snapshot_received_ts':now+2,'scheduled_ts':now+2,'sale':{'net_after_setup_lamports':99999999999}},now=now+3)
        assert c.execute('SELECT count(*) FROM rules_exits_v0733').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM rules_decisions_v0733').fetchone()[0]==counts
        assert cap.summary(fresh)==before
        assert pm.summary(c,now+3)['migration']['counts']=={'AMM_SALE_EXECUTED':1}

def test_late_success_is_not_fresh():
    r={'status':'AMM_SALE_EXECUTED','snapshot_received_ts':10,'scheduled_ts':9}
    assert pm.classify(r,16)=='AMM_STALE_RESULT'
