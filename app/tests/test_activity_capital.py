import json
import pytest
import config,main,database as db,paper_capital as cap,entry_activity as act,execution_model as ex
from test_execution_model import env
from test_primary_integrity import fresh

def test_fee_ledger_and_open_entry_cost(env):
    c,clock,tick,entry=env;entry();tick(p=1)
    r=cap.summary(c)
    assert r['status']=='OK' and r['net_cash']==pytest.approx(26.95)
    assert r['open_entry_cost']==pytest.approx(.05)
    main.sell('a',1,'TEST');tick(p=1.1)
    r=cap.summary(c)
    assert r['status']=='OK' and r['closed_net']==pytest.approx(.1955)
    assert r['net_cash']==pytest.approx(30.1955)
    assert r['total_cost']==pytest.approx(.1045)

def test_net_cash_not_gross_cash_controls_entry(env,monkeypatch):
    c,clock,tick,entry=env
    c.execute("UPDATE capital_settings_v0721 SET v=3.05 WHERE k='start'");db.setm('cash',3.05)
    assert entry()[1]=='CASH_AFTER_COSTS' # 3.05 cannot cover 3.1025 reservation
    assert not ex.pending(c,'a','BUY')

def test_timeout_cancels_reserve_without_charging_fee(env):
    c,clock,tick,entry=env;entry();r=cap.summary(c)
    assert r['reserved']==pytest.approx(3.1025) and r['available']==pytest.approx(26.8975)
    clock['m']+=3;ex.expire(c);r=cap.summary(c)
    assert r['available']==30 and r['total_cost']==0

def test_fee_spike_rejects_entry_without_cash_loss(env):
    c,clock,tick,entry=env;entry();tick(poolFeeRate=.04)
    assert not main.P
    assert c.execute('SELECT status FROM paper_orders_v0720').fetchone()[0]=='REJECTED'
    assert cap.cash(c)==30

def test_pending_exit_does_not_refund_costs(env):
    c,clock,tick,entry=env;entry();tick();main.sell('a',1,'TIME');r=cap.summary(c)
    assert r['net_cash']==pytest.approx(26.95) and r['open_principal']==3
    assert r['open_entry_cost']==pytest.approx(.05)
    ex.restart(c);assert cap.summary(c)['net_cash']==r['net_cash']

def test_missing_sell_rate_fallback_does_not_block_exit(env):
    c,clock,tick,entry=env;entry();tick();main.sell('a',1,'TEST');tick(poolFeeRate=None)
    r=cap.summary(c)
    assert 'a' not in main.P and r['status']=='OK' and r['missing_exit_rate_count']==1
    assert r['total_cost']==pytest.approx(.6625)

def test_ledger_rollback_includes_fee_write(env,monkeypatch):
    c,clock,tick,entry=env;entry();original=cap.record
    def broken(*a,**kw):original(*a,**kw);raise RuntimeError('injected fee failure')
    monkeypatch.setattr(cap,'record',broken)
    with pytest.raises(SystemExit):tick()
    assert cap.cash(c)==30 and c.execute('SELECT COUNT(*) FROM capital_ledger_v0721').fetchone()[0]==0
    assert ex.pending(c,'a','BUY')

def test_settings_frozen_and_corruption_detected(env,monkeypatch):
    c,clock,tick,entry=env;entry();tick()
    monkeypatch.setattr(config,'START_BALANCE',999);db.init()
    assert cap.settings(c)['start']==30 and cap.summary(c)['status']=='OK'
    c.execute('UPDATE capital_ledger_v0721 SET cash_delta=cash_delta+1')
    assert cap.summary(c)['status']=='ERROR'

def test_features_only_use_pre_signal_data():
    s={'model_events':[['buy',1,t] for t in (8,8.5,9,9.5,10)]}
    f=act.features(s,10)
    assert f['decisions']['ACT4'] and f['decisions']['CONT5'] and not f['decisions']['SELL10']
    s['model_events'].append(['sell',1,11])
    assert not act.features(s,10)['valid']

def test_stored_features_immutable_and_audited(env):
    c,clock,tick,entry=env;entry()
    before=c.execute('SELECT features FROM entry_activity_v0721').fetchone()[0]
    tick(p=1.1)
    assert c.execute('SELECT features FROM entry_activity_v0721').fetchone()[0]==before
    assert not act.summarize(c)['errors']
    c.execute('DELETE FROM entry_activity_v0721')
    assert act.summarize(c)['errors']

def test_rebalance_releases_reserve_when_capital_falls(env):
    c,clock,tick,entry=env;entry()
    # Synthetic ledger state used only to exercise reservation cancellation.
    c.execute("UPDATE capital_settings_v0721 SET v=1 WHERE k='start'")
    cap.rebalance(c)
    assert not ex.pending(c,'a','BUY') and ex.reserve(c)==(0,0)

def test_spent_costs_block_entry_even_when_gross_cash_is_high(env):
    c,clock,tick,entry=env
    c.execute("UPDATE capital_settings_v0721 SET v=14 WHERE k='fixed_per_fill'")
    entry();tick();main.sell('a',1,'TEST');tick()
    assert float(db.getm('cash'))==30
    assert cap.cash(c)==pytest.approx(1.91)
    assert entry('b')[1]=='CASH_AFTER_COSTS'
    assert cap.summary(c)['status']=='OK'
