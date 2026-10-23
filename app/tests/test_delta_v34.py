import json,time
from contextlib import closing
import pytest
import monitor_service as ms,position_monitor as pm,exit_rules_v33 as rules,paper_capital as cap,main
from test_primary_integrity import fresh
from test_rules_v33 import setup,check

def prepare(c):
    tid=setup(c)
    r=json.loads(c.execute("SELECT result_json FROM preentry_v0729 WHERE job_id='v33pre'").fetchone()[0])
    r.update(status='ROUNDTRIP_EXECUTED',net_gate='PASS',budget_lamports=30000000,all_in_roundtrip_delta_lamports=-3800000)
    c.execute("UPDATE preentry_v0729 SET result_json=? WHERE job_id='v33pre'",(json.dumps(r),))
    return tid

def sample(c,now,net=28000000,volume=1346200,slot=100):
    job,r=check(c,now,net)
    r['slot']=slot;r['sale'].update(raw_amount='1000000',wallet='test-wallet',volume_account_rent_delta_lamports=volume)
    return job,r

def decisions(c):return [r[0] for r in c.execute("SELECT decision FROM rules_decisions_v0733 WHERE branch='DELTA10' ORDER BY available_ts")]

def test_initial_cost_does_not_trigger_delta(fresh):
    prepare(fresh);before=cap.cash(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=sample(c,now);pm.finish(c,job,r,now=now)
        assert decisions(c)==['BASELINE_CAPTURED']
        assert c.execute("SELECT count(*) FROM rules_exits_v0733 WHERE branch='DELTA10'").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM rules_exits_v0733 WHERE branch='NET10'").fetchone()[0]==1
        assert cap.cash(fresh)==before

def test_volume_reuse_does_not_fake_improvement_and_delta_has_exact_boundary(fresh):
    prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=sample(c,now);pm.finish(c,job,r,now=now)
        job,r=sample(c,now+11,29346200,0,101);pm.finish(c,job,r,now=now+11)
        assert decisions(c)[-1]=='NO_TRIGGER'
        # Exactly 0.30 PAPER deterioration at fixed FX=100.
        job,r=sample(c,now+22,26346200,0,102);pm.finish(c,job,r,now=now+22)
        assert decisions(c)[-1]=='SELECTED'
        ex=c.execute("SELECT * FROM rules_exits_v0733 WHERE branch='DELTA10'").fetchone()
        detail=json.loads(ex['details_json']);assert float(detail['deterioration_units'])==pytest.approx(.3)
        assert float(ex['receipt_units'])==pytest.approx((26346200-1488440)*100/1e9)
        assert rules.summary(c)['branches']['DELTA10']['local_exits']==1

def test_baseline_window_cannot_be_reset_later(fresh):
    prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time()+11;job,r=sample(c,now);pm.finish(c,job,r,now=now)
        assert decisions(c)==['BASELINE_WINDOW_MISSED']
        assert c.execute('SELECT count(*) FROM delta_baselines_v0734').fetchone()[0]==0

@pytest.mark.parametrize('change,expected', [('slot','NOT_NEWER_SNAPSHOT'),('amount','COMPARISON_IDENTITY_CHANGED')])
def test_same_snapshot_or_changed_inventory_cannot_trigger(fresh,change,expected):
    prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=sample(c,now);pm.finish(c,job,r,now=now)
        job,r=sample(c,now+11,20000000,0,100 if change=='slot' else 101)
        if change=='amount':r['sale']['raw_amount']='2000000'
        pm.finish(c,job,r,now=now+11);assert decisions(c)[-1]==expected

def test_late_or_failed_result_never_becomes_baseline(fresh):
    prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=sample(c,now);r['sale']['status']='LOCAL_SWAP_FAILED'
        pm.finish(c,job,r,now=now);assert decisions(c)[-1].startswith('UNUSABLE')
        job,r=sample(c,now+11);main.sell('monitor-test-mint',.9,'TEST');pm.finish(c,job,r,now=now+11)
        assert decisions(c)[-1]=='AFTER_CLOSE'
        assert c.execute('SELECT count(*) FROM delta_baselines_v0734').fetchone()[0]==0

def test_reference_survives_restart_and_is_exported(fresh,tmp_path):
    import zipfile
    prepare(fresh);now=time.time()
    with closing(ms.connect()) as c:
        job,r=sample(c,now);pm.finish(c,job,r,now=now)
        base=dict(c.execute('SELECT * FROM delta_baselines_v0734').fetchone())
    with closing(ms.connect()) as c:
        assert dict(c.execute('SELECT * FROM delta_baselines_v0734').fetchone())==base
        job,r=sample(c,now+11,20000000,0,101);pm.finish(c,job,r,now=now+11)
        assert decisions(c)[-1]=='SELECTED'
    import config
    target=ms.export(config.DB,tmp_path/'results')
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        assert json.loads(z.read('delta_baselines_v0734.json'))==[base]
        report=json.loads(z.read('summary.json'))
        assert report['rules']['entry_costs']['expected_known']==1
        assert report['rules']['branches']['DELTA10']['local_exits']==1
