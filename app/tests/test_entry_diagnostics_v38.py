import json,sqlite3,time
import pytest
import entry_diagnostics as ed,preentry,execution_model as ex,cost_evidence as ce
from test_execution_model import env
from test_primary_integrity import fresh
import main,config

@pytest.fixture
def audit():
 c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.executescript(preentry.SCHEMA);c.executescript(ex.SCHEMA)
 c.execute("INSERT INTO preentry_v0729(job_id,mint,created_ts,status,finished_ts,valid_until,input_json) VALUES('j','m',98,'READY',100,105,'{}')")
 return c

def test_window_boundaries_mixed_causes_and_durable_expiry(audit):
 ed.event(audit,'m','ACCEPTED',99);ed.event(audit,'m','DUPLICATE',100)
 ed.event(audit,'m','ACCEPTED',105);ed.decision(audit,'m','NO_SLOT',105)
 ed.event(audit,'m','ACCEPTED',106)
 ed.expire(audit,106);ed.expire(audit,107)
 row=ed.summary(audit,108)['expired'][0]
 assert row['reason']=='ENTRY_CONDITIONS_CHANGED'
 assert row['events']=={'ACCEPTED':1,'DUPLICATE':1}
 assert row['decisions']=={'NO_SLOT':1}
 assert audit.execute('select count(*) from approval_expiry_v0738').fetchone()[0]==1
 assert ed.summary(audit,108)['accepted']==3

@pytest.mark.parametrize('events,reason', [([], 'NO_OBSERVED_EVENT'),(['PROVIDER_STALE','DUPLICATE'],'DATA_REJECTED'),(['ACCEPTED'],'ELIGIBLE_WITHOUT_DECISION')])
def test_expiry_does_not_invent_cause(audit,events,reason):
 for code in events:ed.event(audit,'m',code,102)
 assert ed.summary(audit,106)['expired'][0]['reason']==reason
 ed.expire(audit,106)
 assert ed.summary(audit,106)['expired'][0]['reason']==reason

def good():return dict(received_mono=10,provider_lag_ms=0,provider_ts=10,source='pumpapi_free',pool='pump',quote_mint=ce.SOL,price_basis='provider_price',signature='sig',clock_status='OK')

@pytest.mark.parametrize('field,value,reason', [('provider_lag_ms',1001,'PROVIDER_STALE'),('received_mono',8,'LOCAL_STALE'),('provider_ts',float('nan'),'INVALID_TIMESTAMPS'),('signature','','MISSING_SIGNATURE'),('pool','amm','OTHER_POOL'),('clock_status','BAD','CLOCK_NOT_OK')])
def test_reason_matches_existing_freshness_rejection(field,value,reason):
 e=good();e[field]=value
 assert not ex.fresh(e,10)
 assert ed.rejection(1,e,10)==reason

def test_live_trade_path_counts_rejected_events_without_trading(env,monkeypatch):
 c,clock,tick,entry=env
 main.S['a']=main.state()
 # Reject before any strategy state is accessed.
 main.process_trade('a','buy',1,'trader','pumpapi_free',None)
 assert ed.summary(c)['counts']['MISSING_EVIDENCE']==1
 assert c.execute('select count(*) from trades').fetchone()[0]==0
 assert c.execute('select count(*) from paper_orders_v0720').fetchone()[0]==0

def test_feed_status_distinct_from_zero_monitor_checks(audit,monkeypatch):
 audit.executescript('CREATE TABLE meta(k TEXT PRIMARY KEY,v); CREATE TABLE reconnect_events(id INTEGER);')
 monkeypatch.setattr(ed.time,'time',lambda:110)
 audit.executemany('INSERT INTO meta VALUES(?,?)',[('heartbeat_ts',110),('trade_last_market_ts',109),('trade_watchdog_state','RECEIVING')])
 assert ed.feed(audit,110)['state']=='RECEIVING'
 audit.execute("UPDATE meta SET v=0 WHERE k='heartbeat_ts'")
 assert ed.feed(audit,110)['state']=='UNKNOWN_HEARTBEAT'
 audit.execute("INSERT INTO meta VALUES('trade_stream_state','STOPPED')")
 assert ed.feed(audit,110)['state']=='STOPPED'


def test_accepted_tick_failing_signal_is_recorded_in_window(env):
 c,clock,tick,entry=env
 now=clock['t']
 c.execute("INSERT INTO preentry_v0729(job_id,mint,created_ts,status,finished_ts,valid_until,input_json) VALUES('j','a',?,'READY',?,?,'{}')",(now,now,now+5))
 tick()
 ed.expire(c,now+6)
 row=ed.summary(c,now+6)['expired'][0]
 assert row['reason']=='ENTRY_CONDITIONS_CHANGED' and row['events']=={'ACCEPTED':1}
 assert sum(row['decisions'].values())==1
 assert not main.P
