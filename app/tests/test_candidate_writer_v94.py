import asyncio,json,sqlite3,threading,time
import pytest
import candidate_lab as ca,counter_writer as cw

def database(tmp_path):
 p=tmp_path/'candidate.db'
 with sqlite3.connect(p) as c:c.execute('PRAGMA journal_mode=WAL');c.executescript(ca.SCHEMA)
 return p

def test_candidate94_real_lock_never_writes_on_event_loop(tmp_path,monkeypatch):
 p=database(tmp_path);read=sqlite3.connect(p);block=sqlite3.connect(p,isolation_level=None);block.execute('BEGIN IMMEDIATE')
 w=cw.Writer(p,interval=.01).start();monkeypatch.setattr(cw,'ACTIVE',w)
 class ReadOnly:
  def execute(self,sql,params=()):
   assert sql.startswith('SELECT'),sql
   return read.execute(sql,params)
 monkeypatch.setattr(ca,'evaluate',lambda *a:('FEATURE_PASS',dict(signal_gate='PASS',score=999)))
 requested=[];monkeypatch.setattr(__import__('shadow_preflight'),'request',lambda *a:requested.append(a[1]))
 history={};context=dict(now=100.,born=90.,side='buy',trader='u',availability={'reasons':[]})
 async def feed():
  stamps=[]
  for i in range(100):
   ca.observe(ReadOnly(),'SHADOW_100','mint',1.,dict(signature=str(i),provider_ts=100.),history,context)
   stamps.append(time.monotonic());await asyncio.sleep(.003)
  return stamps
 try:
  stamps=asyncio.run(feed());assert max(b-a for a,b in zip(stamps,stamps[1:]))<.15
  assert requested==['mint'] and w.snapshot()['busy_retries']>=1
 finally:block.rollback();block.close();read.close();state=asyncio.run(w.close())
 assert state['dropped']==0 and state['unflushed']==0 and not state['thread_alive']
 with sqlite3.connect(p) as c:
  assert c.execute('select count(*) from candidate_seen_v0742').fetchone()[0]==1
  assert c.execute('select count(*) from candidate_first_v0742').fetchone()[0]==4
  assert c.execute('pragma integrity_check').fetchone()[0]=='ok'

def test_candidate94_deduplicates_and_freezes_first_payload(tmp_path,monkeypatch):
 w=cw.Writer(database(tmp_path));monkeypatch.setattr(cw,'ACTIVE',w);history={};detail={'price':1};e={'signature':'s'}
 assert ca.first(None,'STRICT_0','m','FEATURE_PASS',10,e,detail,history)==1
 detail['price']=999
 for _ in range(10000):assert ca.first(None,'STRICT_0','m','FEATURE_PASS',11,e,detail,history)==0
 assert w.snapshot()['pending_keys']==1 and w.snapshot()['dropped']==0
 w.start();s=asyncio.run(w.close());assert s['committed']==1
 with sqlite3.connect(w.database) as c:
  ts,raw=c.execute('select ts,details_json from candidate_first_v0742').fetchone();assert ts==10 and json.loads(raw)['price']==1

def test_candidate94_overflow_visible_bounded_no_fallback(tmp_path,monkeypatch):
 w=cw.Writer(database(tmp_path),limit=2);monkeypatch.setattr(cw,'ACTIVE',w)
 for i in range(100):ca.first(None,'STRICT_0',str(i),'FEATURE_PASS',10,{'signature':'s'},{},{})
 state=w.snapshot();assert state['pending_keys']==2 and state['dropped']==98 and state['diagnostic_gap_v94']
 assert state['observation_bytes_v94']<=state['observation_byte_bound_v94']
 history={};ca.first(None,'STRICT_0','lost','FEATURE_PASS',10,{'signature':'s'},{},history);assert history['diagnostic_gap_v94']
 assert not w.add_row('candidate_first_v0742',('v','oversize','FEATURE_PASS',10,'s','x'*9000))

def test_candidate94_pending_seen_excludes_evicted_history(tmp_path,monkeypatch):
 p=database(tmp_path);w=cw.Writer(p);monkeypatch.setattr(cw,'ACTIVE',w);c=sqlite3.connect(p)
 assert w.add_row('candidate_seen_v0742',('STRICT_0','m',1))
 history={};ca.observe(c,'STRICT_0','m',1,dict(signature='s',provider_ts=10),history,dict(now=10,born=1))
 assert history['incomplete'];c.close()

def test_candidate94_hung_insert_retains_owner_and_exposes_phase(tmp_path,monkeypatch):
 p=database(tmp_path);entered=threading.Event();release=threading.Event()
 class Hung(sqlite3.Connection):
  def executemany(self,sql,params):
   if 'candidate_first' in sql:entered.set();assert release.wait(8)
   return super().executemany(sql,params)
 w=cw.Writer(p,interval=.01,connect=lambda *a,**k:sqlite3.connect(*a,factory=Hung,**k));monkeypatch.setattr(cw,'ACTIVE',w)
 ca.first(None,'STRICT_0','m','FEATURE_PASS',10,{'signature':'s'},{},{});w.start()
 try:
  assert entered.wait(2);w.state['active_sql']['started_mono']-=3.1
  async def run():
   for i in range(30):cw.buffered(None,'v','pulse');await asyncio.sleep(.002)
   return await cw.stop(w)
  state=asyncio.run(run());assert state['thread_alive'] and cw.ACTIVE is w
  assert state['writer_stalled_v94'] and state['writer_frames_v94'] and state['active_sql']['phase']=='WRITE_BATCH'
  assert any(x['table']=='candidate_first_v0742' for x in state['active_sql']['statements'])
  assert not json.loads(p.with_name(p.stem+'_counter_writer_stop_v68.json').read_text())['confirmed']
 finally:release.set();state=asyncio.run(cw.stop(w))
 assert not state['thread_alive'] and state['unflushed']==0 and cw.ACTIVE is None

def test_candidate94_sql_error_cause_and_atomicity(tmp_path,monkeypatch):
 p=database(tmp_path)
 with sqlite3.connect(p) as c:c.execute("CREATE TRIGGER deny BEFORE INSERT ON candidate_first_v0742 BEGIN SELECT RAISE(ABORT,'candidate_test_denied'); END")
 w=cw.Writer(p);monkeypatch.setattr(cw,'ACTIVE',w);w.add('STRICT_0','COUNT');ca.first(None,'STRICT_0','m','FEATURE_PASS',10,{'signature':'s'},{},{});w.start();s=asyncio.run(w.close())
 assert s['status']=='FAILED_UNFLUSHED' and s['unflushed']==2
 assert s['last_error']['message']=='candidate_test_denied' and s['last_error']['sqlite_errorcode']==1811 and s['last_error']['phase']=='WRITE_BATCH'
 with sqlite3.connect(p) as c:assert c.execute('select count(*) from candidate_counts_v0742').fetchone()[0]==0

def test_candidate94_buffered_rows_match_reference(tmp_path,monkeypatch):
 p=database(tmp_path);ref=sqlite3.connect(':memory:');ref.executescript(ca.SCHEMA);w=cw.Writer(p);h1={};h2={}
 for stage in ('SIGNAL_PASS','FEATURE_PASS','ENTRY_MILESTONE','ACTUAL_PORTFOLIO_AVAILABLE'):
  assert ca.first(ref,'STRICT_0','m',stage,123,{'signature':'original'},{'price':1},h1)==1
  monkeypatch.setattr(cw,'ACTIVE',w);assert ca.first(None,'STRICT_0','m',stage,123,{'signature':'original'},{'price':1},h2)==1
  monkeypatch.setattr(cw,'ACTIVE',None)
 w.start();s=asyncio.run(w.close());assert s['dropped']==0
 with sqlite3.connect(p) as c:assert c.execute('select * from candidate_first_v0742 order by stage').fetchall()==ref.execute('select * from candidate_first_v0742 order by stage').fetchall()
 ref.close()


def test_candidate94_results_preserve_actual_check_pair_and_missing_evidence(tmp_path):
 import launcher,config,zipfile,rpc_evidence_v892,rpc_priority
 source=tmp_path/'source';(source/'results').mkdir(parents=True);target=tmp_path/'out';target.mkdir()
 report=launcher.export_release_checks(target,source);assert not report['passed'] and not report['pair_consistent']
 value=dict(version=config.VERSION,created_ts=123,passed=True,native_windows_verified=True)
 for name in ('recovery_check_v89.json','acceptance_v90.json'):(source/'results'/name).write_text(json.dumps(value))
 report=launcher.export_release_checks(target,source);assert report['passed'] and report['native_windows_verified']
 assert (target/'recovery_check_v89.json').read_bytes()==(source/'results/recovery_check_v89.json').read_bytes()
 value['created_ts']=124;(source/'results/acceptance_v90.json').write_text(json.dumps(value))
 assert not launcher.export_release_checks(target,source)['pair_consistent']
 (source/'results/acceptance_v90.json').write_text('{broken')
 report=launcher.export_release_checks(target,source);assert not report['passed'] and (target/'acceptance_v90.json').read_text()=='{broken'
 with rpc_priority.exit_check({'priority_exit':{'order_id':'o'}}):
  out=tmp_path/'rpc.zip';rpc_evidence_v892.export(config.RPC_CONTROL_DIR,out)
  with zipfile.ZipFile(out) as z:assert any(n.startswith('raw/exit_priority_v94/') for n in z.namelist())
