import json,hashlib
from pathlib import Path
import pytest
import local_simulation as lab

@pytest.fixture(scope='module')
def bundle():return json.loads(Path('research/local_snapshot_v26.json').read_text())

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*a,**k):raise AssertionError('offline replay attempted network')
    monkeypatch.setattr(lab,'read_accounts',fail)

def test_real_pump_program_enforces_original_floor(bundle):
    r=lab.run_snapshot(bundle)
    assert r['status']=='LOCAL_SWAP_FAILED'
    assert any('TooLittleSolReceived' in x for x in r['logs'])
    assert r['broadcast'] is False and r['paper_mutated'] is False

def test_real_pump_program_sells_synthetic_tokens(bundle):
    r=lab.run_snapshot(dict(bundle,minimum='1'))
    assert r['status']=='LOCAL_SWAP_EXECUTED'
    assert r['tokens_debited']==bundle['amount']==r['pool_tokens_credited']
    assert r['seller_net_sol_delta_lamports']>0
    assert r['local_fee_from_balance_diff_lamports']==5000
    assert r['network_fee_lamports'] is None
    assert r['volume_account_rent_delta_lamports']>0
    assert r['mainnet_execution_proven'] is False

def test_zero_tokens_cannot_sell_even_without_price_floor(bundle):
    r=lab.run_snapshot(dict(bundle,minimum='1'),balance_override=0)
    assert r['status']=='LOCAL_SWAP_FAILED'
    assert r['synthetic_token_balance']=='0'

def test_missing_program_snapshot_never_substituted(bundle):
    b=dict(bundle,program_data={})
    with pytest.raises(lab.LabError,match='PROGRAM_SET_MISMATCH'):lab.run_snapshot(b)

def test_latest_case_does_not_modify_db(tmp_path):
    import sqlite3
    p=tmp_path/'test.db'
    with sqlite3.connect(p) as c:
        c.executescript('CREATE TABLE execution_verification_v0724(quote_id,order_id,reserve_status);CREATE TABLE exit_quotes_v0723(id,raw_amount,floor_lamports);CREATE TABLE quote_jobs_v0723(order_id,mint);')
    before=hashlib.sha256(p.read_bytes()).hexdigest()
    with pytest.raises(lab.LabError,match='NO_SUPPORTED_EXIT_IN_DB'):lab.latest_case(p)
    assert hashlib.sha256(p.read_bytes()).hexdigest()==before
