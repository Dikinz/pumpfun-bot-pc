"""Regression: ROUTE count stays numeric; settlement evidence stays out of /overview."""
import json,sqlite3,time,pytest
import config,clean_campaign,route_branch,campaign_view
from test_primary_integrity import fresh

@pytest.mark.parametrize("settlement_count",[1,100])
def test_review1011_route_overview_summary_does_not_export_settlement_history(fresh,settlement_count):
    clean_campaign.ensure(fresh,config.DB);fresh.commit()
    target=route_branch.seed(config.DB)
    with sqlite3.connect(target) as c:
        c.executemany('INSERT INTO branch_amm_settlements(trade_id,settled_ts,details) VALUES(?,?,?)',[
            (str(i),time.time(),json.dumps({'route':'PUMP_CURVE','accounting':{'fx':100,'token_rent_lamports':2000000,'volume_rent_lamports':1000000},'detail_padding':'x'*4096}))
            for i in range(settlement_count)])
    before=list(fresh.iterdump())
    with sqlite3.connect(target) as c: route_before=list(c.iterdump())
    data=campaign_view.overview(config.DB)
    assert type(data['route']['verified_exits']) is int
    assert data['route']['verified_exits']==settlement_count
    assert data['route']['curve_exits']==settlement_count
    assert data['route']['persistent_wallet_model_v100']['coverage_complete'] is True
    assert data['route']['persistent_wallet_model_v100']['token_setup']==pytest.approx(.2*settlement_count)
    assert 'detail_padding' not in json.dumps(data)
    assert len(json.dumps(data))<30000
    assert list(fresh.iterdump())==before
    with sqlite3.connect(target) as c: assert list(c.iterdump())==route_before

    # Changing diagnostic body size must not change the small API projection.
    with sqlite3.connect(target) as c:
        for tid,details in c.execute('SELECT trade_id,details FROM branch_amm_settlements').fetchall():
            parsed=json.loads(details);parsed['detail_padding']='x'*128
            c.execute('UPDATE branch_amm_settlements SET details=? WHERE trade_id=?',(json.dumps(parsed),tid))
    smaller=campaign_view.overview(config.DB)
    assert smaller['route']['verified_exits']==data['route']['verified_exits']
    assert smaller['route']['persistent_wallet_model_v100']==data['route']['persistent_wallet_model_v100']
    assert 'detail_padding' not in json.dumps(smaller)
    assert abs(len(json.dumps(smaller))-len(json.dumps(data)))<100
