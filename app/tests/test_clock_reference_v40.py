import json,struct,asyncio
import pytest
import clock_lab as cl,execution_model as ex,cost_evidence as ce
from test_primary_integrity import fresh

def packet(t1,t2,t3):
 p=bytearray(48);p[0]=0x24;p[1]=2;p[3]=(-20)&255
 p[24:32]=cl.stamp(t1);p[32:40]=cl.stamp(t2);p[40:48]=cl.stamp(t3)
 return p

def test_sntp_offset_sign_delay_and_uncertainty():
 t=1792190000.;p=packet(t,t+.07,t+.071)
 r=cl.parse(p,cl.stamp(t),t,t+.041,10,10.041)
 assert r['status']=='MEASURED'
 assert r['offset_ms']==pytest.approx(50,abs=.01)
 assert r['network_delay_ms']==pytest.approx(40,abs=.01)
 assert r['uncertainty_ms']>=19.99
 assert r['offset_low_ms']<50<r['offset_high_ms']

@pytest.mark.parametrize('change,match',[(lambda p:p.__setitem__(0,0x23),'MODE'),(lambda p:p.__setitem__(24,0),'ORIGIN'),(lambda p:p.__setitem__(0,0xe4),'UNSYNCHRONIZED')])
def test_invalid_reference_never_measured(change,match):
 t=1792190000.;p=packet(t,t+.07,t+.071);change(p)
 with pytest.raises(ValueError,match=match):cl.parse(p,cl.stamp(t),t,t+.041,10,10.041)

def test_clock_step_rejects_probe_and_refusal_is_not_offset():
 t=1792190000.;p=packet(t,t+.07,t+.071)
 assert cl.parse(p,cl.stamp(t),t,t+.141,10,10.041)['status']=='LOCAL_CLOCK_CHANGED'
 p[1]=0;p[12:16]=b'DENY'
 r=cl.parse(p,cl.stamp(t),t,t+.041,10,10.041)
 assert r['stop_requests'] and 'offset_ms' not in r
 p[12:16]=b'RATE';r=cl.parse(p,cl.stamp(t),t,t+.041,10,10.041)
 assert r['retry_after_s']==900 and not r['stop_requests']

def test_local_drift_distinguishes_elapsed_delay_and_clock_step():
 first=(100.,10.,0.)
 d=cl.drift(first,first,(105.,15.,0.));assert not d['jump'] and d['step_ms']==0
 d=cl.drift(first,(105.,15.,0.),(106.05,16.,0.));assert d['jump'] and d['step_ms']==pytest.approx(50)
 d=cl.drift(first,first,(101.01,11.,.01));assert not d['jump']

def test_shadow_tolerance_does_not_mutate_evidence_or_trade(fresh):
 cl.STATES.clear()
 e=ce.metadata({'timestamp':1792190000050,'pool':'pump','quoteMint':ce.SOL,'signature':'a'},'pumpapi_free','provider_price',1792190000,10)
 saved=dict(e);cl.observe(fresh,'m',1,e,10)
 s=cl.summary(fresh)['shadow'];assert s['STRICT_0']['REJECTED_METADATA']==1 and s['SHADOW_100']['ACCEPTED']==1
 assert e==saved and not ex.fresh(e,10)
 assert fresh.execute('select count(*) from trades').fetchone()[0]==0
 assert fresh.execute('select count(*) from paper_orders_v0720').fetchone()[0]==0
 cl.observe(fresh,'m',1,e,10)
 assert cl.summary(fresh)['shadow']['SHADOW_100']['DUPLICATE']==1
 e=dict(e,signature='b',provider_lag_ms=-101)
 cl.observe(fresh,'m',1,e,10)
 assert cl.summary(fresh)['shadow']['SHADOW_100']['REJECTED_METADATA']==1

@pytest.mark.parametrize('field,value',[('quote_mint','other'),('received_mono',8),('signature',''),('price_basis','other')])
def test_tolerance_does_not_relax_other_requirements(fresh,field,value):
 cl.STATES.clear();e=ce.metadata({'timestamp':1792190000050,'pool':'pump','quoteMint':ce.SOL,'signature':'a'},'pumpapi_free','provider_price',1792190000,10)
 e[field]=value;cl.observe(fresh,'m',1,e,10)
 assert cl.summary(fresh)['shadow']['SHADOW_100']=={'REJECTED_METADATA':1}

def test_frozen_parameters_detect_change(fresh,monkeypatch):
 row=fresh.execute('select * from clock_config_v0740').fetchone();assert json.loads(row['payload'])['shadow_tolerance_ms']==100
 monkeypatch.setattr(cl,'TOLERANCE_MS',200)
 with pytest.raises(RuntimeError):cl.freeze(fresh)

def test_unavailable_reference_explicit(monkeypatch):
 async def run():
  loop=asyncio.get_running_loop()
  async def bad(*a,**kw):raise OSError('sensitive-text-not-exported')
  monkeypatch.setattr(loop,'getaddrinfo',bad)
  return await cl.probe()
 r=asyncio.run(run());assert r['status']=='UNAVAILABLE' and 'offset_ms' not in r
 assert 'sensitive' not in json.dumps(r)
