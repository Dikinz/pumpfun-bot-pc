import copy,json,random
import pytest
import candidate_lab as ca,candidate_features as cf,clock_lab as cl,main,config,cost_evidence as ce,database as db
from test_primary_integrity import fresh
from test_clock_reference_v40 import packet

def event(i,lag=-50):
 return ce.metadata({'timestamp':1792190000000+i*10-lag,'pool':'pump','quoteMint':ce.SOL,'signature':str(i)},'pumpapi_free','provider_price',1792190000+i*.01,10)

def run(c,m='future',lag=-50,available=None):
 for i in range(1,5):
  e=event(i,lag);before=dict(e)
  cl.observe(c,m,1+(i-1)*.01,e,10,context=dict(side='buy',trader=str(i),born=1792189990.,now=1792190000.+i*.01,availability=available or {'reasons':[]}))
  assert e==before

def test_imprecise_measurement_keeps_raw_offset():
 t=1792190000.;p=packet(t,t+.01,t+.011)
 r=cl.parse(p,cl.stamp(t),t,t+1.5,10,11.5)
 assert r['status']=='TOO_IMPRECISE' and r['uncertainty_ms']>700
 assert r['offset_low_ms']<r['offset_ms']<r['offset_high_ms']
 r=cl.parse(p,cl.stamp(t),t,t+.04,10,10.04)
 assert r['status']=='MEASURED' and r['uncertainty_ms']<50

def test_extra_candidates_and_no_trading_side_effects(fresh):
 cl.STATES.clear();cash=db.getm('cash');run(fresh)
 s=ca.summary(fresh)
 assert s['matches']['FEATURE_PASS']==dict(both=0,strict_only=0,shadow_only=1)
 assert s['matches']['ENTRY_MILESTONE']['shadow_only']==1
 run(fresh,lag=0,m='both')
 s=ca.summary(fresh)
 assert s['matches']['FEATURE_PASS']==dict(both=1,strict_only=0,shadow_only=1)
 assert not main.P and not main.ENTERED and db.getm('cash')==cash
 for table in ('trades','paper_orders_v0720'):
  assert fresh.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
 row=next(r for r in s['recent'] if r['mint']=='future' and r['stage']=='FEATURE_PASS')
 assert row['details']['original_lag_ms']==pytest.approx(-50,abs=.001)
 assert row['details']['preflight']=='NOT_CHECKED'

def test_no_slot_does_not_hide_candidates(fresh):
 cl.STATES.clear();run(fresh,available={'reasons':['NO_SLOT','CASH_AFTER_COSTS']})
 s=ca.summary(fresh)
 assert s['totals']['SHADOW_100']['FEATURE_PASS']==1
 assert not s['totals']['SHADOW_100'].get('ACTUAL_PORTFOLIO_AVAILABLE')
 assert any(r['reason']=='PORTFOLIO_NO_SLOT' for r in s['reasons'])

def test_dedup_restart_and_first_pass_uniqueness(fresh):
 cl.STATES.clear();run(fresh);run(fresh)
 assert ca.summary(fresh)['totals']['SHADOW_100']['FEATURE_PASS']==1
 cl.STATES.clear();run(fresh)
 reasons=ca.summary(fresh)['reasons']
 assert any(x['reason']=='EXCLUDED_INCOMPLETE_HISTORY' and x['n']==4 for x in reasons)
 assert ca.summary(fresh)['totals']['SHADOW_100']['FEATURE_PASS']==1

def test_rejects_future_beyond_100ms(fresh):
 cl.STATES.clear();run(fresh,lag=-101)
 assert ca.summary(fresh)['totals']=={'STRICT_0':{},'SHADOW_100':{}}

def test_thresholds_frozen(fresh,monkeypatch):
 monkeypatch.setattr(config,'MIN_TRADES',100)
 with pytest.raises(RuntimeError,match='parameters changed'):cl.freeze(fresh)

def test_feature_helpers_match_primary_for_varied_histories():
 rng=random.Random(42)
 for _ in range(100):
  s=cf.state();s['born']=10
  for i in range(rng.randint(1,30)):
   p=rng.uniform(.8,1.2);side=rng.choice(['buy','sell'])
   s['trades']+=1;s['buys']+=side=='buy';s['sells']+=side=='sell'
   if side=='buy':s['buyers'].add(str(i%7))
   s['valid_prices']+=1;s['first']=s['first'] or p;s['last']=p;s['prices'].append(p);s['sides'].append(side);s['model_events'].append((side,p,10+i))
  original=copy.deepcopy(s)
  assert cf.calc(s)==main.calc(s)
  assert cf.trajectory_features(s)==main.trajectory_features(s)
  assert cf.model_snapshot_features(s)==main.model_snapshot_features(s)
  ca.evaluate(s,50)
  assert s==original

def test_feature_refusal_matches_primary_entry_filter(fresh,monkeypatch):
 monkeypatch.setattr(config,'MIN_ENTRY_AGE',2)
 s=cf.state();s.update(born=100,trades=4,buys=4,sells=0,buyers={'a','b','c'},valid_prices=4,first=1,last=1.03)
 s['prices'].extend([1,1.01,1.02,1.03]);s['sides'].extend(['buy']*4)
 monkeypatch.setattr(main.time,'time',lambda:101)
 assert ca.evaluate(s,101)[0]=='ANTI_FOMO_TOO_EARLY'
 assert main.buy('young',1.03,90,s)[1]=='ANTI_FOMO_TOO_EARLY'
 s['born']=90;s['last']=1.5;s['prices'].append(1.5)
 assert ca.evaluate(s,101)[0]=='ANTI_FOMO_MOMENTUM_HOT'
 assert main.buy('hot',1.5,90,s)[1]=='ANTI_FOMO_MOMENTUM_HOT'
