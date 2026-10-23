import json
import pytest
import cost_price_audit as audit

def case():
    inv=dict(mint='m',size_usdc='3',sol_usdc='100',token_units='1000',entry_sol_price='.00003',raw_amount='1000000000')
    sale=dict(status='LOCAL_SWAP_EXECUTED',mint='m',raw_amount='1000000000',tokens_debited='1000000000',
        seller_net_sol_delta_lamports=28000000,pool_sol_delta_lamports=-30000000,
        local_fee_from_balance_diff_lamports=5000,synthetic_token_rent_lamports=1500000,
        volume_account_rent_delta_lamports=1000000)
    result=dict(status='CHECKED',snapshot_received_ts=12,zero_token_control_ok=True,sale=sale)
    row=dict(id='x',mint='m',status='SELLABLE',started_ts=10,finished_ts=13,
        input_json=json.dumps(dict(inventory=inv)),result_json=json.dumps(result))
    return row,inv,result

def test_decomposition_does_not_charge_fees_twice():
    row,inv,r=case();a=audit.local_components(row,inv,.05)
    assert a['accounting_status']=='RECONCILED'
    assert a['receipt_units']==pytest.approx(2.65)
    assert a['pnl_units']==pytest.approx(-.40)
    assert a['exit_fee_balance_residual']==pytest.approx(.0995)
    assert a['pnl_without_setup']==pytest.approx(-.15)

def test_unknown_cost_not_zero():
    row,inv,r=case();del r['sale']['volume_account_rent_delta_lamports']
    row['result_json']=json.dumps(r)
    a=audit.local_components(row,inv,.05)
    assert a['accounting_status']=='MISSING_COST_COMPONENT' and 'pnl_units' not in a

def test_amount_mismatch_and_invalid_costs_rejected():
    row,inv,r=case();r['sale']['tokens_debited']='1';row['result_json']=json.dumps(r)
    assert audit.local_components(row,inv,.05)['accounting_status']=='INVALID_OR_INCOMPLETE_PAYLOAD'
    row,inv,r=case();r['sale']['pool_sol_delta_lamports']=-1;row['result_json']=json.dumps(r)
    assert audit.local_components(row,inv,.05)['accounting_status']=='INVALID_OR_INCOMPLETE_PAYLOAD'

def test_nearest_after_is_diagnostic_not_before():
    checks=[dict(check_id='before',available_ts=90,accounting_status='RECONCILED'),
            dict(check_id='after',available_ts=101,snapshot_ts=99,accounting_status='RECONCILED'),
            dict(check_id='error',available_ts=100.1,accounting_status='UNAVAILABLE')]
    x=audit.nearest(checks,100)
    assert x['nearest_attempt']['check_id']=='error'
    assert x['nearest_sale']['check_id']=='after'
    assert x['sale_before']['check_id']=='before'
    assert x['sale_after']['timing']=='AFTER_TICK_DIAGNOSTIC_ONLY'
    assert x['sale_after']['snapshot_delta_to_tick_s']==-1
    assert not x['sale_before']['within_5s']
    assert audit.nearest([],100)['nearest_sale'] is None
