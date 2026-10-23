import base64,struct
import pytest
import execution_verification as v
from test_execution_verification import case,account

@pytest.mark.parametrize('offset,feature',[(81,'MAYHEM'),(82,'CASHBACK'),(124,'HOLDER_REWARD'),(141,'CURVE_DEPTH')])
def test_special_curve_keeps_snapshot_and_reason(case,offset,feature):
    j,i,q,b=case;a=b['value'][0];r=bytearray(base64.b64decode(a['data'][0]));r[offset]=1;a['data'][0]=base64.b64encode(r).decode()
    result=v.verify(j,i,q)['reserves']
    assert result['status']=='SPECIAL_CURVE_UNSUPPORTED'
    assert feature in result['unsupported_features']
    assert result['account_snapshot']==a and result['slot']==100
    assert result['route_amm_key'] and result['mint_snapshot']==b['value'][1]
    assert result['timing']['status']=='QUOTE_SLOT_UNKNOWN'

def test_unknown_layout_and_owner_preserve_evidence(case):
    j,i,q,b=case;b['value'][0]['owner']='11111111111111111111111111111111'
    r=v.verify(j,i,q)['reserves']
    assert r['status']=='WRONG_CURVE_OWNER' and r['account_snapshot']['owner'].startswith('111')

@pytest.mark.parametrize('quote_slot,status,time_status',[(None,'PRICE_DIFFERENCE_TIME_UNVERIFIED','QUOTE_SLOT_UNKNOWN'),(99,'PRICE_DIFFERENCE_TIME_UNVERIFIED','DIFFERENT_SLOTS'),(100,'PRICE_DIFFERENCE_SAME_REPORTED_SLOT','SAME_REPORTED_SLOT')])
def test_price_vs_time_separate(case,quote_slot,status,time_status):
    j,i,q,b=case;q['out_lamports']='50000000';q['response']['routePlan'][0]['swapInfo']['outAmount']='50000000'
    if quote_slot is not None:q['response']['contextSlot']=quote_slot
    r=v.verify(j,i,q)['reserves']
    assert r['status']==status and r['timing']['status']==time_status
    assert r['comparison']['route_exceeds_gross']
    assert r['timing']['exact_state_match_proven'] is False

def test_no_price_divergence_still_reports_unknown_time(case):
    j,i,q,b=case;r=v.verify(j,i,q)['reserves']
    assert r['status']=='RESERVES_COVER_UPPER_BOUND'
    assert r['timing']['status']=='QUOTE_SLOT_UNKNOWN'
