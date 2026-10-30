import json,sqlite3,time,urllib.error
from pathlib import Path
import pytest
import rpc_timing as timing
from test_primary_integrity import fresh

@pytest.fixture(autouse=True)
def reset(monkeypatch):monkeypatch.setattr(timing,'ACTIVE',None)

class Response:
    def __init__(self,raw):self.raw=raw
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,*a):return self.raw
class Opener:
    def __init__(self,value):self.value=value;self.calls=0
    def open(self,*a,**k):
        self.calls+=1
        if isinstance(self.value,Exception):raise self.value
        return Response(self.value)

def test_rpc_success_phases_and_no_sensitive_data(tmp_path,monkeypatch):
    import local_simulation as lab
    payload=json.dumps(dict(result=dict(context=dict(slot=5),value=[None]))).encode()
    opener=Opener(payload);monkeypatch.setattr(lab.urllib.request,'build_opener',lambda *a:opener)
    monkeypatch.setattr(lab.config,'QUOTE_RPC_URL','https://private.example/SECRET?api-key=SECRET')
    timing.start(tmp_path);timing.stage('AMM_DISCOVERY')
    assert lab.read_accounts(['PUBLIC_ACCOUNT'],3)['slot']==5
    row=timing.snapshot()['requests'][0]
    assert row['status']=='OK' and row['stage']=='AMM_DISCOVERY'
    assert set(row['phases_ms'])=={'OPEN_HEADERS','READ_BODY','DECODE_VALIDATE'}
    assert opener.calls==1 and 'SECRET' not in (tmp_path/'rpc_timing.json').read_text()

@pytest.mark.parametrize('exc',[TimeoutError('SECRET_URL'),urllib.error.HTTPError('https://secret',429,'SECRET',{},None)])
def test_failures_keep_original_exception_and_last_phase(exc,tmp_path,monkeypatch):
    import local_simulation as lab
    opener=Opener(exc);monkeypatch.setattr(lab.urllib.request,'build_opener',lambda *a:opener)
    timing.start(tmp_path)
    with pytest.raises(type(exc)):lab.read_accounts(['a'])
    row=timing.snapshot()['requests'][0]
    assert row['status']=='ERROR' and row['active_phase']=='OPEN_HEADERS'
    assert row['http_status']==(429 if isinstance(exc,urllib.error.HTTPError) else None)
    assert 'SECRET' not in (tmp_path/'rpc_timing.json').read_text()
    assert opener.calls==1

def test_rpc_error_code_and_stale_snapshot_semantics(tmp_path,monkeypatch):
    import local_simulation as lab
    timing.start(tmp_path)
    for result,expected in [(dict(error=dict(code=-32005,message='SECRET')),'RPC_ERROR_-32005'),(dict(result=dict(context=dict(slot=1),value=[None])),'INVALID_RPC_SNAPSHOT')]:
        monkeypatch.setattr(lab.urllib.request,'build_opener',lambda *a:Opener(json.dumps(result).encode()))
        with pytest.raises(lab.LabError,match=expected):lab.read_accounts(['a'],2)
    assert timing.snapshot()['requests'][0]['rpc_error_code']==-32005
    assert 'SECRET' not in (tmp_path/'rpc_timing.json').read_text()

def test_checkpoint_failure_cannot_break_request(tmp_path):
    timing.start(tmp_path/'missing_directory')
    with timing.request('getMultipleAccounts') as span:
        with span.phase('OPEN_HEADERS'):pass
    assert timing.snapshot()['write_errors']>0 and timing.snapshot()['requests'][0]['status']=='OK'

def test_killed_child_checkpoint_retains_active_rpc(tmp_path):
    timing.start(tmp_path);timing.stage('AMM_PROGRAM_BANK')
    span=timing.request('getMultipleAccounts');span.__enter__()
    with span.phase('OPEN_HEADERS'):
        r=timing.attach(dict(status='REQUEST_TIMEOUT'),tmp_path)
        assert r['rpc_timing']['requests'][0]['status']=='STARTED'
        assert r['rpc_timing']['requests'][0]['active_phase']=='OPEN_HEADERS'
    span.__exit__(None,None,None)

def test_collector_bounded_and_scope_restored(tmp_path):
    timing.start(tmp_path);timing.stage('OUTER')
    for i in range(80):
        with timing.stage_scope('INNER'):
            with timing.request('getMultipleAccounts'):pass
    assert len(timing.snapshot()['requests'])==64 and len(timing.snapshot()['stages'])==64
    assert timing.snapshot()['active_stage']=='OUTER'

def test_quote_rpc_uses_same_phases_and_original_mapping(tmp_path,monkeypatch):
    import exit_quotes as q
    timing.start(tmp_path)
    monkeypatch.setattr(q.urllib.request,'build_opener',lambda *a:Opener(urllib.error.HTTPError('https://SECRET',429,'SECRET',{},None)))
    with pytest.raises(q.ProbeError) as err:q.request_json('https://SECRET','RESERVES',dict(method='getMultipleAccounts'))
    assert err.value.status=='RATE_LIMITED' and timing.snapshot()['requests'][0]['http_status']==429
    assert 'SECRET' not in (tmp_path/'rpc_timing.json').read_text()

def test_queue_monotonic_and_financial_rows_unchanged(fresh,monkeypatch):
    import preentry,exit_quotes,run_audit
    run_audit.begin(fresh,None);monkeypatch.setattr(exit_quotes,'FX',dict(rate='100',ts=time.time()))
    before=list(fresh.execute('SELECT * FROM trades'))
    preentry.request(fresh,'m',1,3,time.time());j=dict(fresh.execute("SELECT * FROM preentry_v0729 WHERE mint='m'").fetchone())
    timing.queue_start(fresh,j,time.time());preentry.complete(fresh,j,dict(status='BUY_SIMULATION_FAILED'))
    s=timing.preentry_summary(fresh)
    assert s['jobs']==1 and s['queue']['n']==1 and s['runtime']['n']==1
    assert s['statuses']=={'BUY_SIMULATION_FAILED':1}
    assert list(fresh.execute('SELECT * FROM trades'))==before

def test_queue_restart_never_compares_other_session_monotonic(fresh,monkeypatch):
    import preentry,exit_quotes
    monkeypatch.setattr(exit_quotes,'FX',dict(rate='100',ts=time.time()));preentry.request(fresh,'m',1,3,time.time())
    j=dict(fresh.execute('SELECT * FROM preentry_v0729').fetchone())
    monkeypatch.setattr(timing,'SESSION','new_process');timing.queue_start(fresh,j,time.time());timing.queue_finish(fresh,j,{})
    row=fresh.execute('SELECT * FROM preentry_timing_v56').fetchone()
    assert row['queue_ms'] is None and row['run_ms'] is None

def test_legacy_tables_are_read_only_and_unknown():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    assert timing.preentry_summary(c)['available'] is False
    assert c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0]==0

def test_aggregate_excludes_other_run_and_does_not_invent_timings(fresh):
    import preentry,run_audit
    fresh.execute("INSERT INTO preentry_v0729(job_id,mint,created_ts,status,input_json) VALUES('old','old',1,'QUEUE_EXPIRED','{}')")
    run_audit.begin(fresh,None)
    fresh.execute("INSERT INTO preentry_v0729(job_id,mint,created_ts,status,input_json) VALUES('new','new',?,'QUEUE_EXPIRED','{}')",(time.time(),))
    result=timing.preentry_summary(fresh)
    assert result['jobs']==1 and result['queue']['n']==0 and result['queue']['p95_ms'] is None
    assert timing.distribution([None,1,2,100])==dict(n=3,p50_ms=2,p95_ms=100,max_ms=100)

def test_read_body_timeout_distinguished(tmp_path,monkeypatch):
    import local_simulation as lab
    class Slow(Response):
        def read(self,*a):raise TimeoutError('do not log')
    opener=Opener(b'');opener.open=lambda *a,**k:Slow(b'')
    monkeypatch.setattr(lab.urllib.request,'build_opener',lambda *a:opener)
    timing.start(tmp_path)
    with pytest.raises(TimeoutError):lab.read_accounts(['a'])
    assert timing.snapshot()['requests'][0]['active_phase']=='READ_BODY'

def test_real_child_timeout_preserves_checkpoint(tmp_path,monkeypatch):
    import asyncio,sys,position_monitor as pm
    original=pm.create_simulation_process
    code="import sys,time,rpc_timing as t;t.start(sys.argv[1]);t.stage('AMM_PROGRAM_BANK');s=t.request('getMultipleAccounts');s.__enter__();p=s.phase('OPEN_HEADERS');p.__enter__();time.sleep(3)"
    async def spawn(args,out,trace):return await original([sys.executable,'-c',code,str(tmp_path)],out,trace)
    monkeypatch.setattr(__import__('position_monitor'),'create_simulation_process',spawn)
    result=asyncio.run(pm.launch_check({},tmp_path,timeout=.7))
    assert result['status']=='WORKER_TIMEOUT'
    assert result['rpc_timing']['active_stage']=='AMM_PROGRAM_BANK'
    assert result['rpc_timing']['requests'][0]['active_phase']=='OPEN_HEADERS'
    import zipfile
    with zipfile.ZipFile(tmp_path/'diagnostic.zip') as z:assert 'rpc_timing.json' in z.namelist()

def test_rpc_summary_incomplete_and_error_codes():
    reports=[dict(rpc_timing=dict(requests=[dict(method='getMultipleAccounts',stage='AMM_DISCOVERY',status='STARTED',phases_ms={}),dict(method='getMultipleAccounts',stage='AMM_DISCOVERY',status='ERROR',phases_ms={'OPEN_HEADERS':5},elapsed_ms=5,http_status=429)]))]
    g=timing.rpc_summary(reports)['groups'][0]
    assert g['requests']==2 and g['incomplete']==1 and g['errors']==1
    assert g['http_statuses']=={'429':1} and g['elapsed']['n']==1

def test_rpc_rejection_return_value_unchanged_but_diagnostic_is_error(tmp_path,monkeypatch):
    import exit_quotes as q
    result=dict(error=dict(code=-32005,message='SECRET_SERVER_TEXT'))
    monkeypatch.setattr(q.urllib.request,'build_opener',lambda *a:Opener(json.dumps(result).encode()))
    timing.start(tmp_path)
    assert q.request_json('https://SECRET','RESERVES',dict(method='getMultipleAccounts'))==result
    trace=timing.snapshot()['requests'][0]
    assert trace['status']=='RPC_ERROR'
    assert timing.rpc_summary([dict(rpc_timing=timing.snapshot())])['groups'][0]['errors']==1
    assert 'SECRET' not in (tmp_path/'rpc_timing.json').read_text()
