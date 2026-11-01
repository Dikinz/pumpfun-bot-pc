import asyncio,json,time,zipfile
from pathlib import Path
import pytest
import shadow_preflight as sp,exit_quotes as q,preentry,clock_lab,config,database as db
from test_primary_integrity import fresh
@pytest.fixture
def queue(fresh,monkeypatch):
    monkeypatch.setattr(q,'FX',{'rate':'100','ts':100});return fresh

def offer(c,m='m',now=100):return sp.request(c,m,1e-7,now,{'score':100})
def job(c,m='m'):return dict(c.execute('select * from shadow_preflight_v0743 where mint=?',(m,)).fetchone())
def result(j,ts=102,**kw):
    r=dict(status='ROUNDTRIP_EXECUTED',mint=j['mint'],signal_ts=j['created_ts'],paper_mutated=False,broadcast=False,buy={'status':'EXECUTED'},sell={'status':'EXECUTED'},net_gate='PASS',reserve_gate='PASS',snapshot_received_ts=ts,zero_token_control={'status':'FAILED','error_code':6023});r.update(kw);return r

def test_queue_bound_dedup_and_expiry(queue):
    for i in range(3):assert offer(queue,str(i))=='PENDING'
    assert offer(queue,'full')=='SKIPPED_QUEUE_FULL' and offer(queue,'0')=='PENDING'
    assert queue.execute('select count(*) from shadow_preflight_v0743').fetchone()[0]==4
    assert sp.next_job(queue,101)[0]
    assert sp.next_job(queue,102)==(None,'CHILD_RUNNING')
    sp.next_job(queue,111)
    assert sp.summary(queue)['groups']['ALL']['statuses']=={'RUNNING':1,'SKIPPED_QUEUE_EXPIRED':2,'SKIPPED_QUEUE_FULL':1}

def test_priority_interval_and_hourly_budget(queue,monkeypatch):
    preentry.request(queue,'primary',1,3,100);offer(queue)
    assert sp.next_job(queue,100)==(None,'PRIMARY_PREFLIGHT_BUSY')
    queue.execute("update preentry_v0729 set status='USED'")
    j,_=sp.next_job(queue,100);sp.complete(queue,j,result(j),now=102);offer(queue,'interval',103)
    assert job(queue,'interval')['status']=='SKIPPED_SAMPLING_INTERVAL' and sp.next_job(queue,103)==(None,'IDLE')
    monkeypatch.setattr(sp,'STARTS_PER_HOUR',1)
    assert offer(queue,'hour',104)=='SKIPPED_HOURLY_LIMIT' and sp.next_job(queue,104)==(None,'IDLE')

@pytest.mark.parametrize('changes,now,status',[
 ({},103,'CHECK_PASSED'),({'net_gate':'ROUNDTRIP_LOSS_TOO_HIGH'},103,'CHECK_REJECTED'),
 ({'reserve_gate':'INSUFFICIENT_PREENTRY_RESERVES'},103,'CHECK_REJECTED'),({'zero_token_control':{'status':'EXECUTED'}},103,'CHECK_REJECTED'),
 ({},111,'LATE_RESULT'),({'snapshot_received_ts':100},106,'STALE_SNAPSHOT'),({'snapshot_received_ts':110},103,'STALE_SNAPSHOT'),
 ({'mint':'wrong'},103,'INVALID_RESULT'),({'broadcast':True},103,'INVALID_RESULT'),({},99,'CLOCK_CHANGED')])
def test_execution_cost_timing_and_no_primary_mutation(queue,changes,now,status):
    offer(queue);j,_=sp.next_job(queue,101);sp.complete(queue,j,result(j,**changes),now=now);r=job(queue)
    assert r['status']==status and r['buy_ok']==r['sell_ok']==1
    assert r['cost_pass']==(changes.get('net_gate','PASS')=='PASS')
    for table in ('paper_orders_v0720','preentry_v0729','trades'):assert queue.execute('select count(*) from '+table).fetchone()[0]==0
    assert float(db.getm('cash'))==30

def test_missing_fx_and_backoff(queue,monkeypatch):
    monkeypatch.setattr(q,'FX',{});assert offer(queue,'nofx')=='SKIPPED_MISSING_FX'
    monkeypatch.setattr(q,'FX',{'rate':'100','ts':100});offer(queue);j,_=sp.next_job(queue,100)
    sp.complete(queue,j,{'status':'REQUEST_OR_LAB_ERROR_HTTPError'},now=102);offer(queue,'wait',103)
    assert sp.next_job(queue,103)==(None,'IDLE');sp.next_job(queue,114)
    assert job(queue,'wait')['status']=='SKIPPED_SAMPLING_BACKOFF'
    assert sp.summary(queue)['service']['cooldown_until']==162

def test_membership_and_missing_denominator(queue):
    offer(queue);j,_=sp.next_job(queue,100);sp.complete(queue,j,result(j),now=102)
    assert sp.summary(queue)['groups']['SHADOW_ONLY']['passed']==1
    queue.execute("insert into candidate_first_v0742 values('STRICT_0','m','FEATURE_PASS',105,'s','{}')")
    queue.execute("insert into candidate_first_v0742 values('SHADOW_100','lost','FEATURE_PASS',105,'s','{}')")
    s=sp.summary(queue)
    assert s['groups']['SHARED']['passed']==1 and s['groups']['SHADOW_ONLY']['passed']==0
    assert s['groups']['ALL']['total']==2 and s['groups']['ALL']['pass_fraction_all']==.5
    assert s['missing_jobs']==1 and job(queue)['strict_pass_at_signal']==0

def test_recover_stop_freeze(queue,monkeypatch):
    offer(queue);sp.next_job(queue,100);offer(queue,'pending');sp.recover(queue)
    assert sp.summary(queue)['groups']['ALL']['statuses']=={'INTERRUPTED_RESTART':1,'SKIPPED_SAMPLING_INTERVAL':1}
    offer(queue,'later',121);sp.stop(queue);assert job(queue,'later')['status']=='INTERRUPTED_STOP'
    monkeypatch.setattr(sp,'MAX_PENDING',8)
    with pytest.raises(RuntimeError,match='parameters changed'):sp.freeze(queue)

def test_pipeline_hook_once(fresh,monkeypatch):
    from test_candidates_v42 import run
    clock_lab.STATES.clear();monkeypatch.setattr(q,'FX',{'rate':'100','ts':1792190000});run(fresh);run(fresh)
    assert fresh.execute('select count(*) from shadow_preflight_v0743').fetchone()[0]==1
    assert job(fresh,'future')['status']=='PENDING'
    assert fresh.execute('select count(*) from preentry_v0729').fetchone()[0]==0 and float(db.getm('cash'))==30

def test_real_vm_replay_child_export(queue,tmp_path,monkeypatch):
    import local_simulation as lab
    b=json.loads(Path('research/local_snapshot_v26.json').read_text());now=time.time();b['snapshot']['received_ts']=now
    replay=tmp_path/'snapshot.json';replay.write_text(json.dumps(b));monkeypatch.setattr(q,'FX',{'rate':'100','ts':now})
    sp.request(queue,b['mint'],1.,now,{});j,_=sp.next_job(queue,now);out=tmp_path/j['job_id'];out.mkdir()
    monkeypatch.setattr(lab,'read_accounts',lambda *a,**k:pytest.fail('network in replay'))
    r=sp.child(json.loads(j['input_json']),out,str(replay))
    assert r['status']=='ROUNDTRIP_EXECUTED' and r['rpc_calls']==0
    assert r['acquired_tokens_raw']==r['tokens_sold_raw'] and r['tokens_remaining_raw']=='0'
    assert r['all_in_roundtrip_delta_lamports']==r['wallet_roundtrip_delta_lamports']-r['token_account_setup_rent_lamports']
    sp.complete(queue,j,r,str(out/'evidence.zip'),now=time.time());assert job(queue,b['mint'])['buy_ok']==job(queue,b['mint'])['sell_ok']==1
    monkeypatch.setattr(sp,'ROOT',tmp_path);export=sp.export(config.DB,tmp_path)
    with zipfile.ZipFile(export) as z:
        assert json.loads(z.read('summary.json'))['groups']['ALL']['total']==1
        assert j['job_id']+'/evidence.zip' in z.namelist()
    (out/'evidence.zip').unlink()
    export=sp.export(config.DB,tmp_path)
    with zipfile.ZipFile(export) as z:assert json.loads(z.read('missing_evidence.json'))==[j['job_id']]

def test_rpc_cap_partial_journal(queue,tmp_path,monkeypatch):
    import local_simulation as lab
    offer(queue);j,_=sp.next_job(queue,100);called=[]
    monkeypatch.setattr(lab,'read_accounts',lambda *a,**k:called.append(1) or {})
    def excessive(*a,**k):
        for i in range(sp.MAX_RPC_CALLS+1):lab.read_accounts(['a'])
    monkeypatch.setattr(lab,'capture',excessive);r=sp.child(json.loads(j['input_json']),tmp_path)
    assert r['status']=='RPC_CALL_LIMIT' and r['rpc_calls']==len(called)==sp.MAX_RPC_CALLS
    assert len(json.loads((tmp_path/'rpc_activity.json').read_text()))==sp.MAX_RPC_CALLS

def test_cancel_kills_child(queue,tmp_path,monkeypatch):
    offer(queue);j,_=sp.next_job(queue,100);monkeypatch.setattr(sp,'ROOT',tmp_path)
    class Proc:
        pid=999999999
        returncode=None;killed=False
        async def wait(self):
            if self.killed:return self.returncode
            await asyncio.Event().wait()
        def kill(self):self.killed=True;self.returncode=-9
    proc=Proc()
    async def spawn(*a,**k):return proc
    monkeypatch.setattr(__import__('position_monitor'),'create_simulation_process',spawn)
    async def run():
        task=asyncio.create_task(sp.run_job(j));await asyncio.sleep(.01);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run());assert proc.killed and job(queue)['raw_status']=='INTERRUPTED_STOP'

def test_actual_subprocess_replay(queue,tmp_path,monkeypatch):
    b=json.loads(Path('research/local_snapshot_v26.json').read_text());now=time.time();b['snapshot']['received_ts']=now
    replay=tmp_path/'offline.json';replay.write_text(json.dumps(b));monkeypatch.setattr(q,'FX',{'rate':'100','ts':now});monkeypatch.setattr(sp,'ROOT',tmp_path/'jobs')
    sp.request(queue,b['mint'],1.,now,{});j,_=sp.next_job(queue,now);original=__import__('position_monitor').create_simulation_process
    async def spawn(args,out,trace):return await original([*args,'--replay',str(replay)],out,trace)
    monkeypatch.setattr(__import__('position_monitor'),'create_simulation_process',spawn);asyncio.run(sp.run_job(j));r=job(queue,b['mint'])
    assert r['raw_status']=='ROUNDTRIP_EXECUTED' and r['started_ts']<=r['finished_ts'] and r['wait_s']==0
    assert r['buy_ok']==r['sell_ok']==1 and r['archive'] and json.loads(r['result_json'])['rpc_calls']==0
    assert queue.execute('select count(*) from paper_orders_v0720').fetchone()[0]==0
