import json
import pytest
import config,main,database as db,execution_model as ex,cost_evidence as ce,primary_integrity
from test_primary_integrity import fresh

@pytest.fixture
def env(fresh,monkeypatch):
    monkeypatch.setattr(config,'EXECUTION_MODEL_ENABLED',True)
    monkeypatch.setattr(config,'MIN_SCORE',1000)
    # Fixture changes strategy settings after fresh() initialized the empty DB.
    # Freeze the actual test configuration before processing any events.
    import clock_lab
    fresh.execute('DELETE FROM clock_config_v0740')
    clock_lab.freeze(fresh)
    clock_lab.STATES.clear()
    main.S.clear();ex.ORIGINS.clear();ce.WATCH.clear();ce.ORIGINS.clear()
    clock={'m':100.,'t':1791700000.}
    monkeypatch.setattr(ex.time,'monotonic',lambda:clock['m'])
    monkeypatch.setattr(ex.time,'time',lambda:clock['t'])
    def tick(m='a',p=1,dt=.3,sig=None,**extra):
        clock['m']+=dt;clock['t']+=dt
        raw={'signature':sig or str(clock['m']),'timestamp':(clock['t']-.05)*1000,'pool':'pump','quoteMint':ce.SOL,'poolFeeRate':.0125};raw.update(extra)
        e=ce.metadata(raw,'pumpapi_free','provider_price',clock['t'],clock['m'])
        main.S.setdefault(m,main.state())
        main.process_trade(m,'buy',p,'person','pumpapi_free',e)
        return e
    def entry(m='a'):
        tick(m,1)
        return main.buy(m,1,s=main.S[m])
    return fresh,clock,tick,entry

def test_pending_reserves_then_fills_new_price(env):
    c,clock,tick,entry=env;cash=float(db.getm('cash'))
    assert entry()==(True,'ENTRY_PENDING')
    assert not main.P and float(db.getm('cash'))==cash
    assert ex.reserve(c)==(1,__import__("paper_capital").budget(c,config.POSITION_SIZE))
    tick(p=1.02,dt=.1);assert not main.P
    tick(p=1.04,dt=.2)
    assert main.P['a']['entry']==1.04
    assert float(db.getm('cash'))==cash-config.POSITION_SIZE
    assert ex.reserve(c)==(0,0)
    assert ex.summary(c)['status']=='OK'
    assert primary_integrity.summarize(c)['status']=='OK'

def test_timeout_releases_reservation_without_trade(env):
    c,clock,tick,entry=env;entry();clock['m']+=3;clock['t']+=3;ex.expire(c)
    assert c.execute('SELECT status FROM paper_orders_v0720').fetchone()[0]=='EXPIRED'
    assert not main.P and ex.reserve(c)==(0,0)
    assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==0

def test_no_same_transaction_or_unsupported_quote_fill(env):
    c,clock,tick,entry=env;entry();sig=main.S['a']['cost_evidence']['signature']
    tick(sig=sig);assert not main.P
    tick(quoteMint='other');assert not main.P
    tick(timestamp=(clock['t']-5)*1000);assert not main.P
    tick(p=1.03);assert main.P['a']['entry']==1.03

def test_stale_exit_retains_money_and_slot_until_new_tick(env):
    c,clock,tick,entry=env;entry();tick();cash=db.getm('cash')
    clock['m']+=70;clock['t']+=70
    main.sell('a',1,'MAX_HOLD_WATCHDOG')
    assert 'a' in main.P and db.getm('cash')==cash
    assert ex.pending(c,'a','SELL')
    main.sell('a',8,'TP2') # original exit intent stays fixed
    assert ex.pending(c,'a','SELL')['reason']=='MAX_HOLD_WATCHDOG'
    tick(p=.8,dt=.1);assert 'a' in main.P
    tick(p=.7,dt=.2);assert 'a' not in main.P
    t=c.execute("SELECT * FROM trades WHERE side='SELL'").fetchone()
    assert t['price']==.7 and t['reason']=='MAX_HOLD_WATCHDOG'
    assert ex.summary(c)['status']=='OK'
    assert primary_integrity.summarize(c)['status']=='OK'

def test_restart_cancels_entry_keeps_exit(env):
    c,clock,tick,entry=env;entry();tick();main.sell('a',1,'TIME');entry('b')
    tid=main.P['a']['trade_id'];cash=db.getm('cash')
    ex.restart(c);main.P.clear();main.ENTERED.clear();main.S.clear();main.restore_positions()
    assert main.P['a']['trade_id']==tid and db.getm('cash')==cash
    assert ex.pending(c,'a','SELL') and not ex.pending(c,'b','BUY')
    tick('a',.95);assert 'a' not in main.P
    assert ex.summary(c)['status']=='OK'

def test_reservations_enforce_slots(env):
    c,clock,tick,entry=env
    assert entry('a')[0] and entry('b')[0]
    assert entry('c')[1]=='NO_SLOT'
    assert ex.reserve(c)[0]==2

def test_failure_during_fill_keeps_pending_and_cash(env,monkeypatch):
    c,clock,tick,entry=env;entry();cash=db.getm('cash');original=ex.filled
    def broken(*args,**kwargs):original(*args,**kwargs);raise RuntimeError('injected')
    monkeypatch.setattr(ex,'filled',broken)
    with pytest.raises(SystemExit):tick()
    assert not main.P and db.getm('cash')==cash
    assert ex.pending(c,'a','BUY')
    assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==0

def test_other_quote_cannot_create_signal(env):
    c,clock,tick,entry=env
    tick('other',quoteMint='custom')
    assert main.S['other']['valid_prices']==0
    assert main.buy('other',1,s=main.S['other'])[1]=='UNSUPPORTED_OR_STALE_PRICE'

def test_stale_fill_corruption_detected(env):
    c,clock,tick,entry=env;entry();tick()
    c.execute('UPDATE paper_orders_v0720 SET price_age_ms=99999')
    assert ex.summary(c)['status']=='ERROR'
