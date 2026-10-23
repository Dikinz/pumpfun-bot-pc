import json
import diagnostics as d,main,database as db,config,execution_model as ex
from test_execution_model import env
from test_primary_integrity import fresh

def test_blocked_candidate_observed_without_trade(env,monkeypatch):
    c,clock,tick,entry=env
    monkeypatch.setattr(config,'MAX_POSITIONS',0)
    assert entry()[1]=='NO_SLOT'
    assert c.execute('select count(*) from trades').fetchone()[0]==0
    r=c.execute('select * from signal_observations_v0722').fetchone()
    assert r['entry_result']=='NO_SLOT'
    original=r['features'];entry()
    assert c.execute('select count(*) from signal_observations_v0722').fetchone()[0]==1
    assert c.execute('select features from signal_observations_v0722').fetchone()[0]==original

def test_late_tick_never_backfills_horizons(env):
    c,clock,tick,entry=env
    d.observe(c,'z',1,5,{},'NO_SLOT',now=100)
    d.tick(c,'z',2,now=120);d.tick(c,'z',9,now=140)
    r=c.execute('select * from observation_marks_v0722 where mint="z" and horizon=30').fetchone()
    assert r['last_price']==2 and r['last_ts']==120 and r['ticks']==1
    d.advance(c,500)
    assert c.execute('select count(*) from observation_marks_v0722 where mint="z"').fetchone()[0]==3
    assert not c.execute('select * from trades').fetchall()

def test_exit_diagnoses_coexist_and_restart_persists(env):
    c,clock,tick,entry=env;entry();tick();main.sell('a',1,'TEST')
    d.evidence(c,'a','OTHER_POOL','pumpswap',now=100)
    d.evidence(c,'a','DUPLICATE','pump',now=101)
    ex.restart(c)
    db.setm('heartbeat_ts',200);db.setm('trade_watchdog_state','DEGRADED')
    r=d.summary(c,now=200)['exits'][0]
    assert {'OTHER_POOL_OBSERVED','REJECTED_DATA_OBSERVED','CONNECTION_UNAVAILABLE'}<=set(r['causes'])
    assert r['raw_count']==2 and r['other_pool']==1 and r['rejected']==1
    assert ex.pending(c,'a','SELL') and 'a' in main.P

def test_stopped_wait_freezes_at_heartbeat(env):
    c,clock,tick,entry=env;entry();tick();main.sell('a',1,'TEST')
    start=ex.pending(c,'a','SELL')['signal_ts']
    db.setm('heartbeat_ts',start+100);db.setm('stream_state','STOPPED')
    r=d.summary(c,now=start+10000)
    assert r['frozen'] and r['exits'][0]['wait_s']==100
    assert 'NO_TOKEN_EVENTS_OBSERVED' in r['exits'][0]['causes']
