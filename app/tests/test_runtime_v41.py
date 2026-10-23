import runtime_status as rt
from test_primary_integrity import fresh

def test_runtime_uses_monotonic_and_freezes_on_stop(fresh,monkeypatch):
 clock={'wall':100.,'mono':10.}
 monkeypatch.setattr(rt.time,'time',lambda:clock['wall']);monkeypatch.setattr(rt.time,'monotonic',lambda:clock['mono'])
 assert rt.summary(fresh)['state']=='NOT_STARTED'
 rt.begin(fresh);assert rt.summary(fresh)['state']=='STARTING'
 clock.update(wall=200.,mono=15.);rt.beat(fresh)
 assert rt.summary(fresh)['elapsed_s']==5 and rt.summary(fresh)['state']=='RUNNING'
 clock.update(wall=201.,mono=16.);rt.beat(fresh,stopped=True)
 clock.update(wall=10000.,mono=1000.)
 r=rt.summary(fresh);assert r['elapsed_s']==6 and r['state']=='STOPPED' and r['frozen']

def test_missing_heartbeat_never_claims_running(fresh,monkeypatch):
 monkeypatch.setattr(rt.time,'time',lambda:100.)
 rt.begin(fresh);rt.beat(fresh)
 assert rt.summary(fresh,116)['state']=='UNKNOWN'
 assert rt.summary(fresh,90)['state']=='UNKNOWN'
 assert rt.summary(fresh,116)['frozen']

def test_restart_resets_current_session(fresh,monkeypatch):
 m={'v':1.};monkeypatch.setattr(rt.time,'monotonic',lambda:m['v'])
 rt.begin(fresh);m['v']=901.;rt.beat(fresh);assert rt.summary(fresh)['elapsed_s']==900
 rt.begin(fresh);assert rt.summary(fresh)['elapsed_s']==0
