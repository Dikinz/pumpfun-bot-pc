import json,time
from contextlib import closing
import pytest
import main,monitor_service as ms,position_monitor as pm,paper_capital as cap,exit_rules_v33 as rules
from test_primary_integrity import fresh
from test_monitor_v31 import open_case,checked

def setup(c):
    tid=open_case(c);now=time.time()
    c.execute("INSERT INTO paper_orders_v0720(order_id,trade_id,mint,side,status,signal_ts,fill_ts) VALUES('v33order',?,'monitor-test-mint','BUY','FILLED',?,?)",(tid,now-.1,now))
    c.execute("INSERT INTO preentry_v0729(job_id,mint,status,finished_ts,valid_until,order_id,result_json) VALUES('v33pre','monitor-test-mint','USED',?,?,'v33order',?)",(now-1,now+4,json.dumps({'pre_real_sol_lamports':1000000000,'reserve_gate':'PASS','snapshot_received_ts':now-2})))
    return tid

def check(c,now,net=30000000,reserve=1000000000):
    job=pm.next_job(c,now);job['inventory']={'sol_usdc':'100','size_usdc':'3'}
    r=checked(now,net);r['reserves']['real_sol_lamports']=reserve;r['net_status']='NET_MEETS_FLOOR'
    return job,r

def test_net_includes_entry_and_token_costs_without_double_fee(fresh):
    setup(fresh);before=cap.cash(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=check(c,now,28000000)
        # Gross receipt 2.8 exceeds old 2.7 floor. All-in 2.601156 is below it.
        r['sale'].update(local_fee_from_balance_diff_lamports=5000,volume_account_rent_delta_lamports=1346200)
        pm.finish(c,job,r,now=now)
        report=rules.summary(c)
        assert report['branches']['NET10']['local_exits']==1
        assert report['branches']['RESERVE50']['local_exits']==0
        a=json.loads(c.execute("SELECT details_json FROM rules_exits_v0733").fetchone()[0])['accounting']
        assert float(a['receipt_units'])==pytest.approx(2.651156)
        assert float(a['pnl_units'])==pytest.approx(-.398844)
        assert cap.cash(fresh)==before

def test_reserve_reference_is_preentry_not_previous_check(fresh):
    setup(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=check(c,now,reserve=600000000);pm.finish(c,job,r,now=now)
        job,r=check(c,now+11,reserve=500000000);pm.finish(c,job,r,now=now+11)
        reports=rules.summary(c)['branches']
        assert reports['RESERVE50']['local_exits']==1
        assert reports['NET10']['local_exits']==0
        assert reports['RESERVE50']['entries']==1 and reports['RESERVE50']['paper_open']==1

def test_empty_reserves_never_create_fictional_exit(fresh):
    setup(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=check(c,now,reserve=0)
        r['reserves']['status']='INSUFFICIENT_REAL_RESERVES';r['sale']={'status':'LOCAL_SWAP_FAILED'}
        pm.finish(c,job,r,now=now)
        s=rules.summary(c)['branches']
        assert s['RESERVE50']['decisions']=={'TRIGGER_UNEXECUTABLE':1}
        assert s['NET10']['local_exits']==s['RESERVE50']['local_exits']==0
        assert s['RESERVE50']['trial_unresolved']==1

@pytest.mark.parametrize('late,stale',[(True,False),(False,True)])
def test_late_and_stale_cannot_select(fresh,late,stale):
    setup(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=check(c,now,20000000,1000000)
        if late:main.sell('monitor-test-mint',.9,'TEST');now=time.time()+.01
        if stale:r['snapshot_received_ts']=now-6
        pm.finish(c,job,r,now=now)
        assert c.execute('SELECT count(*) FROM rules_exits_v0733').fetchone()[0]==0

def test_freeze_rejects_changed_parameters(fresh,monkeypatch):
    setup(fresh)
    with closing(ms.connect()) as c:
        original=rules.parameters(c)
        monkeypatch.setitem(rules.RULES,'net_loss_bps',900)
        with pytest.raises(RuntimeError,match='RULES_CHANGED'):rules.freeze(c)
        assert rules.parameters(c)==original

def test_first_selection_idempotent_and_independent(fresh):
    setup(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=check(c,now,20000000,900000000);pm.finish(c,job,r,now=now);pm.finish(c,job,r,now=now)
        job2,r2=check(c,now+11,30000000,400000000);pm.finish(c,job2,r2,now=now+11)
        exits={x['branch']:x['check_id'] for x in c.execute('SELECT * FROM rules_exits_v0733')}
        assert exits=={'NET10':job['id'],'RESERVE50':job2['id']}

def test_missing_baseline_and_missing_costs_are_explicit(fresh):
    setup(fresh)
    with closing(ms.connect()) as c:
        fresh.execute('DELETE FROM preentry_v0729')
        now=time.time();job,r=check(c,now,20000000,0);r['sale'].pop('synthetic_token_rent_lamports')
        pm.finish(c,job,r,now=now)
        dec=dict(c.execute('SELECT branch,decision FROM rules_decisions_v0733'))
        assert dec['NET10']=='MISSING_ACCOUNTING' and dec['RESERVE50']=='MISSING_RESERVE_BASELINE'

def test_idle_without_positions_is_not_stale(fresh):
    import cohort_report
    assert cohort_report.health(fresh,now=time.time())['state']=='IDLE'
