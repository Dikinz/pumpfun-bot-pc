import copy,hashlib,json,sqlite3,zipfile
from pathlib import Path
import pytest
import exit_cost_review as review,main,config,paper_capital as cap,route_branch,monitor_service
from test_primary_integrity import fresh
from test_parallel_paper_v54 import opened
from test_route_exit_v59 import candidate,save_check

def test_balance_components_reconcile_without_double_counting_rent(fresh):
    tid=opened(fresh);fresh.executescript(route_branch.SCHEMA)
    row,j,r,inv=candidate(tid);r['sale']['volume_account_rent_delta_lamports']=1000000
    save_check(fresh,row,j,r,inv);route_branch.poll(main)
    before=cap.cash(fresh)
    report=review.build({'route':(config.DB,monitor_service.paths(config.DB)['db'])})
    x=report['rows'][0];a=x['settlement']['components']
    assert x['setup_status']=='RECONCILED_ISOLATED'
    assert x['net']==pytest.approx(a['trading_component']-a['entry_cost']-a['exit_fee_balance_residual']-a['local_transaction_fee']-a['token_account_setup']-a['volume_account_setup'])
    assert x['setup_excluded_sensitivity_net']==pytest.approx(x['net']+.2)
    assert x['net']!=x['setup_excluded_sensitivity_net'] and cap.cash(fresh)==before
    assert report['totals']['route']['setup_reconciled']==1

def test_missing_rent_model_is_unknown_not_zero_and_database_unchanged(fresh,tmp_path):
    main.buy('m',1);tid=main.P['m']['trade_id'];cap.record(fresh,tid,'BUY',3,{'pool_fee_rate':.0125})
    main.sell('m',1.1,'TEST');cap.record(fresh,tid,'SELL',3.3,{'pool_fee_rate':.0125})
    before=Path(config.DB).read_bytes()
    report=review.build({'primary':(config.DB,None)})
    review.write(report,tmp_path)
    assert Path(config.DB).read_bytes()==before
    assert report['totals']['primary']['token_setup'] is None
    assert report['rows'][0]['setup_status']=='NOT_MODELED_UNKNOWN'
    assert report['rows'][0]['net']==pytest.approx(.3-.05-.0545)

def component(time,stamp):return dict(check_id=str(time),available_ts=time,snapshot_ts=stamp,accounting_status='RECONCILED',timing_status='VALID')

def test_post_target_result_never_becomes_known_before_target():
    result=review.nearest([component(11,9.9)],10)
    assert result['before'] is None
    assert result['after']['timing']=='AFTER_TARGET_DIAGNOSTIC_ONLY'
    assert not result['after']['fresh_reference_at_target']

def test_old_or_invalid_result_does_not_claim_fresh_reference():
    result=review.nearest([component(9,3)],10)
    assert not result['before']['fresh_reference_at_target']
    x=component(9,8);x['timing_status']='INVALID_OR_MISSING'
    assert review.nearest([x],10)['status']=='NO_RECONCILED_RESULT'

def pair_row():
    return dict(trade_id='p',mint='m',entry_signature='sig',entry_price=1,size=3,
      inventory=dict(token_units='123.000',sol_usdc='100'),net=.1,gross=.2,entry_cost=.05,exit_cost=.05)

def test_same_mint_with_different_inventory_cannot_claim_price_effect():
    p=pair_row();r=copy.deepcopy(p);r['trade_id']='r';r['inventory']['sol_usdc']='101';r['inventory']['token_units']='122'
    x=review.pairs([p],[r])[0]
    assert x['match_status']=='DESCRIPTIVE_MATCH_ONLY'
    assert 'TOKEN_AMOUNT_DIFFERS_OR_UNKNOWN' in x['mismatch_flags']
    assert x['causal_price_effect'] is None and x['alternative_wallet_net'] is None

def test_equal_decimals_match_and_duplicate_entries_remain_ambiguous():
    p=pair_row();r=copy.deepcopy(p);r['inventory']['token_units']='123'
    assert review.pairs([p],[r])[0]['match_status']=='IDENTICAL_ENTRY_INVENTORY'
    assert review.pairs([p,p],[r])[0]['match_status']=='NO_UNIQUE_PRIMARY_ENTRY'

def test_archive_replay_keeps_input_bytes_and_needs_no_network(fresh,tmp_path,monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**k:pytest.fail('network forbidden'))
    main.buy('m',1);tid=main.P['m']['trade_id'];cap.record(fresh,tid,'BUY',3,{'pool_fee_rate':.0125})
    main.sell('m',1.1,'TEST');cap.record(fresh,tid,'SELL',3.3,{'pool_fee_rate':.0125})
    nested=tmp_path/'test_data_fixture.zip'
    snapshot=tmp_path/'fixture.db'
    with sqlite3.connect(snapshot) as target:fresh.backup(target)
    with zipfile.ZipFile(nested,'w') as z:z.write(snapshot,'fixture.db')
    source=tmp_path/'report.zip'
    with zipfile.ZipFile(source,'w') as z:z.write(nested,nested.name)
    before=source.read_bytes();report=review.archive(source,tmp_path/'review')
    assert source.read_bytes()==before
    assert report['totals']['primary']['closed']==1
    assert report['source']['sha256']==hashlib.sha256(before).hexdigest()

def test_empty_or_wrong_archive_does_not_create_fake_results(tmp_path):
    source=tmp_path/'empty.zip'
    with zipfile.ZipFile(source,'w') as z:z.writestr('not-a-db.txt','no')
    with pytest.raises(ValueError,match='PRIMARY_DATABASE_NOT_FOUND'):review.archive(source,tmp_path/'out')
    assert not (tmp_path/'out/exit_cost_review.json').exists()
