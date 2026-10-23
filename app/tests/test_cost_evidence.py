import sqlite3
import pytest
import cost_evidence as ce
from test_primary_integrity import fresh
import main, database as db, config
T=1791666000.

@pytest.fixture
def c():
    x=sqlite3.connect(':memory:');x.row_factory=sqlite3.Row;x.executescript(ce.SCHEMA);ce.WATCH.clear();ce.ORIGINS.clear()
    yield x
    x.close();ce.WATCH.clear();ce.ORIGINS.clear()

def ev(sig='a',wall=T,mono=10,**raw):
    data={'signature':sig,'pool':'pump','quoteMint':ce.SOL,'timestamp':(wall-.1)*1000,'poolFeeRate':.0125,'priorityFee':.000015};data.update(raw)
    return ce.metadata(data,'pumpapi_free','provider_price',wall,mono)

def decision(c,side='BUY',e=None):
    ce.record(c,'id',side,'mint',1,3,'TEST',ev() if e is None else e,wall=T,mono=10)

def test_timestamp_contract_and_missing_fields():
    e=ev();assert e['provider_lag_ms']==pytest.approx(100,abs=.001)
    assert e['pool_fee_rate']==.0125
    assert ev(timestamp=T)['provider_ts'] is None # seconds cannot silently masquerade as ms
    assert ev(timestamp=(T+5)*1000)['clock_status']=='FUTURE'
    assert ev(poolFeeRate=float('nan'))['pool_fee_rate'] is None
    assert ev(poolFeeRate=-1)['pool_fee_rate'] is None
    assert ce.metadata({},'other','price',T,10)['pool_fee_rate'] is None

def test_first_tick_after_deadline_and_signs(c):
    decision(c);decision(c,'SELL')
    ce.on_tick(c,'mint',1.01,ev('b',T+.2,10.2),wall=T+.2,mono=10.2)
    assert not c.execute("SELECT 1 FROM cost_probes_v0719 WHERE status<>'PENDING'").fetchone()
    ce.on_tick(c,'mint',1.02,ev('c',T+.3,10.3),wall=T+.3,mono=10.3)
    rows=c.execute('SELECT * FROM cost_probes_v0719 WHERE horizon_ms=250 ORDER BY side').fetchall()
    assert [r['status'] for r in rows]==['OBSERVED','OBSERVED']
    assert rows[0]['adverse_pct']==pytest.approx(2)
    assert rows[1]['adverse_pct']==pytest.approx(-2)
    ce.on_tick(c,'mint',9,ev('d',T+.4,10.4),wall=T+.4,mono=10.4)
    assert c.execute("SELECT price FROM cost_probes_v0719 WHERE side='BUY' AND horizon_ms=250").fetchone()[0]==1.02

def test_duplicate_and_timeout_are_not_zero_cost(c):
    decision(c)
    ce.on_tick(c,'mint',1,ev('a',T+.3,10.3),wall=T+.3,mono=10.3)
    assert c.execute('SELECT status FROM cost_probes_v0719 WHERE horizon_ms=250').fetchone()[0]=='PENDING'
    ce.poll(c,12)
    assert set(r[0] for r in c.execute('SELECT status FROM cost_probes_v0719'))=={'NO_FRESH_TICK'}
    assert not ce.WATCH and not ce.ORIGINS
    assert all(r[0] is None for r in c.execute('SELECT adverse_pct FROM cost_probes_v0719'))

@pytest.mark.parametrize('raw,status',[
 ({'timestamp':(T-2)*1000},'SOURCE_TIME_REVERSED'),
 ({'timestamp':(T+4)*1000},'CLOCK_INVALID'),
 ({'timestamp':None},'TIME_UNKNOWN'),
 ({'quoteMint':'another'},'SOURCE_CHANGED')])
def test_unusable_sample(c,raw,status):
    decision(c)
    ce.on_tick(c,'mint',1.02,ev('b',T+.3,10.3,**raw),wall=T+.3,mono=10.3)
    r=c.execute('SELECT status,adverse_pct FROM cost_probes_v0719 WHERE horizon_ms=250').fetchone()
    assert r[0]==status and r[1] is None

def test_restart_preserves_incomplete(c):
    decision(c);ce.restart(c)
    assert set(r[0] for r in c.execute('SELECT status FROM cost_probes_v0719'))=={'INTERRUPTED'}
    assert not ce.WATCH

def test_stale_decision(c):
    ce.record(c,'id','SELL','mint',1,3,'TIME',ev(),wall=T+3,mono=13)
    r=c.execute('SELECT metadata_status,price_age_ms FROM cost_decisions_v0719').fetchone()
    assert r[0]=='STALE' and r[1]==3000

def test_primary_cost_rows_and_fee_subset(fresh,monkeypatch):
    monkeypatch.setattr(config,'ALLOW_REENTRY',False)
    s=main.state();s['cost_evidence']=ce.metadata({'pool':'pump','quoteMint':ce.SOL,'timestamp':__import__('time').time()*1000-100,'poolFeeRate':.0125},'pumpapi_free','provider_price')
    main.S['mint']=s
    assert main.buy('mint',1,s=s)[0]
    main.sell('mint',1.1,'TEST')
    r=ce.summarize(fresh)
    assert r['status']=='OK' and r['decisions']==2
    assert r['observed_fee_subset']['closed']==1
    assert r['observed_fee_subset']['pool_fee_only']==pytest.approx(3*.0125+3.3*.0125)
    assert r['routes'][0]['after_fee_only']==pytest.approx(.3-6.3*.0125)
    fresh.execute('DELETE FROM cost_probes_v0719 WHERE horizon_ms=250')
    assert ce.summarize(fresh)['status']=='ERROR'

def test_cost_record_failure_rolls_back_trade(fresh,monkeypatch):
    cash=db.getm('cash');original=ce.record
    def broken(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('injected cost write failure')
    monkeypatch.setattr(ce,'record',broken)
    with pytest.raises(SystemExit):main.buy('bad',1)
    assert db.getm('cash')==cash and 'bad' not in main.P
    assert fresh.execute('SELECT COUNT(*) FROM cost_decisions_v0719').fetchone()[0]==0
    assert fresh.execute('SELECT COUNT(*) FROM cost_probes_v0719').fetchone()[0]==0
    ce.poll(fresh);assert not ce.WATCH

def test_end_to_end_trade_adapter_captures_evidence(fresh,monkeypatch):
    import time
    monkeypatch.setattr(config,'MIN_SCORE',1000) # collect ticks without an automatic entry
    main.S['wire']=main.state()
    first=ce.metadata({'pool':'pump','quoteMint':ce.SOL,'timestamp':time.time()*1000-100,'poolFeeRate':.0125,'signature':'first'},'pumpapi_free','provider_price')
    main.process_trade('wire','buy',1,'trader','pumpapi_free',first)
    assert main.buy('wire',1,s=main.S['wire'])[0]
    tid=main.P['wire']['trade_id'];start=ce.ORIGINS[(tid,'BUY')]
    later=ce.metadata({'pool':'pump','quoteMint':ce.SOL,'timestamp':time.time()*1000-100,'poolFeeRate':.0125,'signature':'second'},'pumpapi_free','provider_price',time.time(),start+.3)
    main.process_trade('wire','buy',1.01,'trader','pumpapi_free',later)
    r=fresh.execute("SELECT * FROM cost_probes_v0719 WHERE trade_id=? AND side='BUY' AND horizon_ms=250",(tid,)).fetchone()
    assert r['status']=='OBSERVED' and r['adverse_pct']==pytest.approx(1)
    assert fresh.execute("SELECT signature FROM cost_decisions_v0719 WHERE trade_id=?",(tid,)).fetchone()[0]=='first'
