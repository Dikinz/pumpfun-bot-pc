import json,sqlite3
import pytest
import time_order_audit as ta,cost_evidence as ce,execution_model as ex,entry_diagnostics as ed,main,config,preentry,exit_quotes
from test_execution_model import env
from test_primary_integrity import fresh

@pytest.mark.parametrize('raw,reason',[({},'TIME_MISSING'),({'timestamp':'not-a-time'},'TIME_INVALID'),({'timestamp':1792178858001},'TIME_FUTURE')])
def test_missing_invalid_future_are_distinct_and_rejected(raw,reason):
 e=ce.metadata(dict(pool='pump',quoteMint=ce.SOL,signature='s',**raw),'pumpapi_free','provider_price',1792178858,10)
 assert ed.rejection(1,e,10)==reason
 assert not ex.fresh(e,10)
 if reason=='TIME_FUTURE':assert e['provider_lag_ms']==pytest.approx(-1,abs=.001)

@pytest.mark.parametrize('lag,bucket',[(-.1,'FUTURE_0_10'),(-10,'FUTURE_0_10'),(-10.1,'FUTURE_10_100'),(-100,'FUTURE_10_100'),(-1000,'FUTURE_100_1000'),(-1001,'FUTURE_OVER_1000'),(0,'LAG_0_1000'),(1000,'LAG_0_1000'),(1001,'LAG_OVER_1000')])
def test_signed_ranges(lag,bucket):
 assert ta.bucket({'timestamp_input_state':'VALID','lag_ms':lag})==bucket

def test_aggregate_retains_signed_values_and_numeric_raw_sample(fresh):
 for lag in [-2,-8]:
  ta.clock(fresh,dict(provider_lag_ms=lag,provider_ts=100,received_ts=100+lag/1000,timestamp_raw_ms=100000,timestamp_input_state='VALID'),now=120)
 s=ta.summary(fresh);r=s['histogram'][0]
 assert (r['n'],r['min_ms'],r['max_ms'],r['mean_ms'])==(2,-8,-2,-5)
 assert s['recent'][0]['sample']['timestamp_raw_ms']==100000
 json.dumps(s,allow_nan=False)

def test_pending_rejections_then_expiry_and_no_post_terminal_count(env):
 c,clock,tick,entry=env;entry();oid=c.execute('select order_id from paper_orders_v0720').fetchone()[0]
 tick(timestamp=(clock['t']+2)*1000)
 rows=ta.summary(c)['orders'];assert rows[0]['observed_events']==1
 assert any(x['code']=='TIME_FUTURE' for x in rows[0]['observations'])
 clock['m']+=3;clock['t']+=3;ex.expire(c)
 tick()
 row=ta.summary(c)['orders'][0]
 assert row['status']=='EXPIRED' and row['observed_events']==1
 assert any(x['code']=='EXPIRED' for x in row['observations'])
 assert not main.P

def test_delay_fill_and_restart_are_recorded(env):
 c,clock,tick,entry=env;entry();tick(dt=.1);tick(dt=.2)
 row=ta.summary(c)['orders'][0]
 assert row['status']=='FILLED'
 codes={x['code'] for x in row['observations']}
 assert {'CREATED','DELAY_NOT_ELAPSED','ELIGIBLE','FILLED'}<=codes
 entry('b');ex.restart(c)
 row=ta.summary(c)['orders'][0]
 assert row['status']=='CANCELLED_RESTART'
 assert any(x['code']=='CANCELLED_RESTART' for x in row['observations'])

def test_fill_preflight_expiry_is_separate_from_buy_timeout(env,monkeypatch):
 c,clock,tick,entry=env
 monkeypatch.setattr(config,'PREFLIGHT_ENABLED',True)
 monkeypatch.setattr(exit_quotes,'FX',{'rate':'100','ts':clock['t']})
 entry()
 c.execute("UPDATE preentry_v0729 SET status='READY',finished_ts=?,valid_until=?",(clock['t'],clock['t']+.2))
 assert main.buy('a',1,s=main.S['a'])[0]
 tick(dt=.3)
 row=ta.summary(c)['orders'][0]
 assert row['status']=='REJECTED' and row['note']=='PREFLIGHT_STALE'
 assert any(x['kind']=='fill_rejection' and x['code']=='PREFLIGHT_STALE' for x in row['observations'])
 assert not main.P
