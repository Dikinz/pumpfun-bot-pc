import json,sqlite3
from types import SimpleNamespace
import pytest
import replay_log as journal
import observation_lab as obs
import main
from replay_research import verify

@pytest.fixture
def recorded(monkeypatch):
 journal.ACTIVE.clear();journal.PENDING.clear();obs.ACTIVE.clear();obs.PENDING.clear()
 main.FAST_REENTRY.clear();main.SHADOW_DB_QUEUE.clear()
 monkeypatch.setattr(main.db,'inc',lambda *a,**k:None)
 clock=[100.]
 monkeypatch.setattr(main,'time',SimpleNamespace(time=lambda:clock[0]))
 def begin(m='m'):
  journal.capture(m,'sell',.88,'trader','synthetic',clock[0])
  main.fast_reentry_start(m,dict(entry=1.,ts=90.,mfe=.02,mae=-.12),.88,-.36)
 def tick(t,p,side='buy',m='m'):
  clock[0]=t;journal.capture(m,side,p,'trader','synthetic',t);main.fast_reentry_tick(m,p,side)
 def flush():
  c=sqlite3.connect(':memory:');c.executescript(journal.SCHEMA);journal.flush(c);return c
 return clock,begin,tick,flush

@pytest.mark.parametrize('ending',['stop','trail','watchdog','no_signal','boundary'])
def test_reproduction_includes_exit_tick(recorded,ending):
 clock,begin,tick,flush=recorded;begin()
 if ending in ('stop','trail','watchdog'):
  tick(100.3,.91);tick(100.7,.93);tick(101.1,.95)
  assert main.FAST_REENTRY['m']['reentered']
  if ending=='stop':tick(101.3,.85,'sell')
  elif ending=='trail':tick(101.3,1.1);tick(101.5,1.05,'sell')
  else:clock[0]=121.2;main.fast_reentry_finalize('m','REENTRY_TIMEOUT','WATCHDOG_LAST',clock[0])
 elif ending=='boundary':tick(131.,.9)
 else:clock[0]=131.;main.fast_reentry_finalize('m','NO_SIGNAL','WATCHDOG_LAST',clock[0])
 c=flush();r=verify(c)
 assert r['matched']==1 and not r['mismatched'],r
 if ending=='stop':
  assert any(json.loads(p)['price']==.85 for (p,) in c.execute("select payload from replay_v0716 where kind='EVENT'"))
 assert journal.summary(c)['broken']==0


def test_lost_tick_fails_replay(recorded):
 clock,begin,tick,flush=recorded;begin();tick(100.3,.91);tick(100.7,.93);tick(101.1,.95);tick(101.3,.85,'sell')
 c=flush();c.execute("delete from replay_v0716 where id=(select min(id) from replay_v0716 where kind='EVENT')")
 assert verify(c)['corrupt']==1


def test_incomplete_not_reported_matched(recorded):
 _,begin,tick,flush=recorded;begin();tick(100.3,.91)
 r=verify(flush());assert r['incomplete']==1 and r['matched']==0


def test_buffer_overflow_marked(recorded,monkeypatch):
 clock,begin,tick,flush=recorded;begin();monkeypatch.setattr(journal,'MAX_BUFFER',1)
 tick(100.3,.91);clock[0]=131.;main.fast_reentry_finalize('m','NO_SIGNAL','WATCHDOG_LAST',clock[0])
 c=flush();assert journal.summary(c)['dropped']==1 and verify(c)['corrupt']==1


def test_price_diagnostics_do_not_change_signals():
 obs.ACTIVE.clear();obs.PENDING.clear();obs.start('m',1,100.)
 obs.poll(100.5,market_ts=100.49)
 d=json.loads(obs.PENDING[-1][4]);assert d['price_status']=='no_post_entry_tick' and not d['usable']
 obs.tick('m',.99,-1,.5,100.6);obs.poll(101.,market_ts=100.99)
 d=json.loads(obs.PENDING[-1][4]);assert d['price_status']=='mint_idle' and not d['usable']
 obs.poll(102.,market_ts=90.)
 assert json.loads(obs.PENDING[-1][4])['price_status']=='market_stale'


def test_benefit_harm_accounting():
 from execution_summary import summarize
 c=sqlite3.connect(':memory:');c.execute('create table observation_v0714(id integer primary key,case_id,kind,payload)')
 for case,base,fill in [('loss',-.5,-.2),('winner',1.,-.1)]:
  for kind,d in [('CHECKPOINT',dict(sec=.5,abort=True,usable=True,price_age=.1,timer_lag=.01)),('ABORT_FILL',dict(sec=.5,fill_ts=2.,base_net=fill)),('BASELINE_EXIT',dict(exit_ts=3.,base=base))]:
   c.execute('insert into observation_v0714(case_id,kind,payload) values(?,?,?)',(case,kind,json.dumps(d)))
 r=summarize(c)['rows'][-1]
 assert r['benefit']==pytest.approx(.3) and r['harm']==pytest.approx(1.1)
 assert r['lift']==pytest.approx(r['benefit']-r['harm'])
 assert r['loss_saved']==pytest.approx(.3) and r['profit_lost']==pytest.approx(1.)


def test_changed_input_detected(recorded):
 _,begin,tick,flush=recorded;begin();tick(100.3,.91);tick(100.7,.93);tick(101.1,.95);tick(101.3,.85,'sell')
 c=flush();rid,p=c.execute("select id,payload from replay_v0716 where kind='EVENT' order by id desc limit 1").fetchone()
 d=json.loads(p);d['price']=1.1;c.execute('update replay_v0716 set payload=? where id=?',(json.dumps(d),rid))
 assert verify(c)['mismatched']==1


def test_natural_exit_still_wins_ready_abort(recorded):
 _,begin,tick,flush=recorded;begin();tick(100.3,.91);tick(100.7,.93);tick(101.1,.95)
 tick(101.5,.94,'sell')
 # A diagnostic signal before the next natural exit; execution delay has elapsed.
 q=obs.ACTIVE['m'];q['requests'].append(dict(sec=.5,decision=101.6,ready=102.35,deadline=112.35,status='PENDING'))
 tick(102.5,.85,'sell')
 assert not any(r[3]=='ABORT_FILL' for r in obs.PENDING)
 assert any(r[3]=='ABORT_CANCELLED' for r in obs.PENDING)
 assert verify(flush())['matched']==1
