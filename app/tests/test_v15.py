import sqlite3,json
import observation_lab as lab
from execution_summary import summarize
from export_data import export

def test_cancel_on_exit():
 lab.ACTIVE.clear();lab.PENDING.clear();lab.start('m',1,0);lab.tick('m',.99,-1,.5,.9);lab.poll(1)
 lab.finish('m','REENTRY_STOP',-.4,1.1);lab.tick('m',.98,0,0,2)
 assert any(r[3]=='ABORT_CANCELLED' for r in lab.PENDING)
 assert not any(r[3]=='ABORT_FILL' for r in lab.PENDING)

def test_summary_no_double_count():
 c=sqlite3.connect(':memory:');c.execute('create table observation_v0714(id integer primary key,case_id,kind,payload)')
 def add(kind,d):c.execute('insert into observation_v0714(case_id,kind,payload) values(?,?,?)',('a',kind,json.dumps(d)))
 for sec in (.5,1):
  add('CHECKPOINT',dict(sec=sec,abort=True,usable=True,price_age=.1,timer_lag=.01))
  add('ABORT_FILL',dict(sec=sec,fill_ts=2,base_net=-.2))
 add('BASELINE_EXIT',dict(exit_ts=3,base=-.5))
 r=summarize(c);first=r['rows'][-1];assert first['filled']==1 and abs(first['lift']-.3)<1e-9

def test_backup_live_wal(tmp_path):
 import zipfile
 p=tmp_path/'source.db';c=sqlite3.connect(p);c.execute('pragma journal_mode=WAL');c.execute('create table t(x)');c.execute('insert into t values(42)');c.commit()
 z=export(p,tmp_path/'exports')
 with zipfile.ZipFile(z) as f:f.extract('source.db',tmp_path/'check')
 check=sqlite3.connect(tmp_path/'check/source.db');assert check.execute('select x from t').fetchone()[0]==42
 assert check.execute('pragma integrity_check').fetchone()[0]=='ok';c.close();check.close()
