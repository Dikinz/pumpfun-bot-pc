"""Replay real saved Curve programs; all counterfactuals remain diagnostic only."""
import json,os,hashlib,sqlite3
from pathlib import Path
from contextlib import closing
import pytest
import curve_overflow_replay as replay,main,paper_capital as cap
from test_primary_integrity import fresh
from test_p0_isolation_v79 import tree_snapshot

def save(report):
    if os.getenv('CURVE_REPLAY_EVIDENCE'):Path(os.environ['CURVE_REPLAY_EVIDENCE']).write_text(json.dumps(report),encoding='utf-8')

@pytest.fixture(scope='module')
def replayed():
    report=replay.run_matrix();assert report['passed'];save(report);return report

@pytest.fixture(autouse=True)
def funded(fresh,tmp_path):
    assert main.buy('overflow-replay-open',1)[0]
    cap.record(fresh,main.P['overflow-replay-open']['trade_id'],'BUY',3,{'pool_fee_rate':.0125})
    assert cap.summary(fresh)['status']=='OK';cash=cap.cash(fresh);before=tree_snapshot();yield
    assert cap.cash(fresh)==cash and fresh.execute('SELECT SUM(size) FROM positions').fetchone()[0]==3
    assert not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()
    after=tree_snapshot();assert all(before.get(pid)==birth for pid,birth in after.items()),(before,after)
    for p in tmp_path.rglob('*.db'):
        with closing(sqlite3.connect(p)) as c:assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'

@pytest.mark.parametrize('mode,expected',[
    ('original','LOCAL_SWAP_FAILED'),('quarter_amount','LOCAL_SWAP_EXECUTED'),('lamports_only','LOCAL_SWAP_FAILED'),
    ('reserve_field_only','LOCAL_SWAP_EXECUTED'),('both','LOCAL_SWAP_EXECUTED')])
def test_pinned_curve_bank_control(replayed,mode,expected):
    row=next(r for r in replayed['rows'] if r['mode']==mode)
    assert row['status']==expected and row['program_sha256']==replayed['source']['program_sha256']
    assert not row['broadcast'] and not row['paper_mutated']
    if mode in ('original','lamports_only'):
        assert row['program_error']==6024 and row['seller_net_lamports'] is None
    else:
        assert row['program_error'] is None and row['tokens_debited']==row['pool_tokens_credited']==row['amount']
    if mode=='original':
        assert row['amount']=='987574537154' and row['compute_units']==40661
        assert row['reserves']['real_sol_lamports']==9_876_550 and row['reserves']['sell_gross_upper_lamports']==27_604_359
    if mode=='quarter_amount':assert row['amount']=='246893634288'
    if mode=='reserve_field_only':
        # Deliberately inconsistent bank: a VM success alone must not authorize a fill.
        assert row['counterfactual'] and row['reserves']['status']=='INSUFFICIENT_REAL_RESERVES'

def test_repeated_overflow_and_zero_token_control(replayed):
    assert replayed['original_repeated_errors']==[6024,6024,6024]
    assert replayed['zero_token_control']==6023 and replayed['original_bundle_unchanged']
    assert replayed['network_used'] is False and replayed['live_execution_proven'] is False

def test_diagnostic_partial_sale_does_not_close_paper_lot(fresh,replayed):
    before=cap.summary(fresh)
    result=replay.run_matrix();assert result['passed']
    after=cap.summary(fresh)
    for key in ('net_cash','available','reserved','open_principal','total_cost'):assert after[key]==before[key]
    assert len(main.P)==1 and fresh.execute('SELECT SUM(size) FROM positions').fetchone()[0]==3
    assert not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()
    replayed['paper_ledger_unchanged']=True;save(replayed)

def test_fixture_hash_mismatch_never_replayed(tmp_path):
    (tmp_path/'source.json').write_bytes((replay.FIXTURE/'source.json').read_bytes())
    (tmp_path/'snapshot.json').write_bytes((replay.FIXTURE/'snapshot.json').read_bytes()+b' ')
    with pytest.raises(RuntimeError,match='FIXTURE_HASH_MISMATCH'):replay.load_fixture(tmp_path)
