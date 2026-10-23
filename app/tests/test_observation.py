import observation_lab as lab

def setup_function():
 lab.ACTIVE.clear();lab.PENDING.clear()

def test_timer_no_future_and_stale():
 lab.start('m',1,0);lab.tick('m',.99,-1,.5,.9);lab.poll(1.)
 records=[__import__('json').loads(r[4]) for r in lab.PENDING if r[3]=='CHECKPOINT']
 assert records[-1]['sec']==1 and records[-1]['usable'] and records[-1]['abort']
 assert not records[0]['usable'] # missed .5 timer, never backfilled as timely
 lab.poll(2.)
 assert not __import__('json').loads(lab.PENDING[-1][4])['usable']

def test_unresolved_no_invented_fill():
 lab.start('m',1,0);lab.tick('m',.99,-1,.5,.9);lab.poll(1.)
 lab.poll(12.)
 assert any(r[3]=='ABORT_UNRESOLVED' for r in lab.PENDING)
 assert not any(r[3]=='ABORT_FILL' for r in lab.PENDING)
 lab.finish('m','REENTRY_TIMEOUT',-.4,13)
 assert 'm' not in lab.ACTIVE

def test_database_roundtrip(tmp_path):
 import sqlite3
 c=sqlite3.connect(tmp_path/'lab.db')
 c.execute('CREATE TABLE observation_v0714(case_id,mint,ts,kind,payload)')
 lab.start('m',1,0);lab.tick('m',1.1,1,.8,.2);lab.flush(c)
 assert c.execute('select count(*) from observation_v0714').fetchone()[0]==2 and not lab.PENDING

def test_transport_age_guard(monkeypatch):
 import main
 monkeypatch.setattr(main.time,'time',lambda:100)
 vals={'trade_watchdog_state':'RECEIVING','trade_last_market_ts':99}
 monkeypatch.setattr(main.db,'getm',lambda k,d=None:vals.get(k,d))
 assert main.transport_entry_ready()
 vals['trade_last_market_ts']=0;assert not main.transport_entry_ready()
 vals['trade_last_market_ts']=101;assert not main.transport_entry_ready()
 vals['trade_last_market_ts']=99;vals['trade_watchdog_state']='DEGRADED'
 assert not main.transport_entry_ready()

def test_gap_blocks_observation():
 lab.start('m',1,0);lab.tick('m',.99,-1,.5,.9);lab.poll(1,transport_ok=False)
 cp=[__import__('json').loads(r[4]) for r in lab.PENDING if r[3]=='CHECKPOINT']
 assert not any(r['usable'] or r['abort'] for r in cp)
