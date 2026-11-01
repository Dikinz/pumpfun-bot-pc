import json,sqlite3,subprocess,sys,time,urllib.error,os
from pathlib import Path
import pytest
import rpc_budget as rpc

def test_common_cooldown_across_instances_and_redacted_state(tmp_path):
    a=rpc.Budget(tmp_path,rate=1000,slots=2,wait=.1);b=rpc.Budget(tmp_path,rate=1000,slots=2,wait=.1)
    with pytest.raises(urllib.error.HTTPError):
        with a.request('https://RPC.invalid/a?api-key=SECRET'):
            raise urllib.error.HTTPError('https://SECRET',429,'SECRET',{'Retry-After':'10'},None)
    with pytest.raises(rpc.Deferred) as exc:
        with b.request('https://rpc.invalid/b'):pytest.fail('cooldown bypassed')
    assert exc.value.rpc_budget_reason=='COOLDOWN' and exc.value.retry_after_s>9
    row=a.snapshot()['endpoints'][0]
    assert row['started']==1 and row['server_429']==1 and row['local_deferred']==1
    assert 'SECRET' not in a.path.read_bytes().decode(errors='ignore')
    assert 'rpc.invalid' not in json.dumps(a.snapshot())


def test_capacity_failure_and_success_release_os_slot(tmp_path):
    b=rpc.Budget(tmp_path,rate=1000,slots=1,wait=.05)
    with b.request('https://a.invalid'):
        with pytest.raises(rpc.Deferred) as exc:
            with b.request('https://b.invalid'):pytest.fail('global slot bypassed')
        assert exc.value.rpc_budget_reason=='CAPACITY'
    with b.request('https://b.invalid'):pass
    assert b.snapshot()['inflight']==0


def test_rate_limits_and_mismatched_process_config_fail_closed(tmp_path):
    b=rpc.Budget(tmp_path,rate=1,slots=2,wait=.01)
    with b.request('https://a.invalid'):pass
    with pytest.raises(rpc.Deferred) as exc:
        with b.request('https://a.invalid/path'):pytest.fail('rate bypassed')
    assert exc.value.rpc_budget_reason=='RATE'
    with pytest.raises(rpc.Unavailable,match='DISAGREE'):
        with rpc.Budget(tmp_path,rate=2,slots=2).request('https://a.invalid'):pass


def test_http_date_and_missing_retry_after_backoff():
    from email.utils import formatdate
    assert rpc.retry_after({'Retry-After':formatdate(time.time()+60,usegmt=True)})>58
    assert rpc.retry_after({'Retry-After':'invalid'}) is None


def test_lost_response_bookkeeping_blocks_future_rpc(tmp_path,monkeypatch):
    b=rpc.Budget(tmp_path,rate=1000,slots=1)
    def fail(*a,**k):raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(b,'record',fail)
    with pytest.raises(rpc.Unavailable,match='RESULT_UNCONFIRMED'):
        with b.request('https://a.invalid'):pass
    assert b.failed.exists()
    with pytest.raises(rpc.Unavailable,match='BLOCKED'):
        with rpc.Budget(tmp_path,rate=1000,slots=1).request('https://a.invalid'):pytest.fail('failed control ignored')


def test_synchronous_curve_and_metadata_clients_share_429(tmp_path,monkeypatch):
    import config,local_simulation as lab,exit_quotes as quotes
    monkeypatch.setattr(config,'RPC_CONTROL_DIR',tmp_path)
    monkeypatch.setattr(config,'QUOTE_RPC_URL','https://rpc.invalid/account?api-key=SECRET')
    calls=[]
    class Opener:
        def open(self,*a,**k):
            calls.append(1);raise urllib.error.HTTPError('https://SECRET',429,'SECRET',{'Retry-After':'10'},None)
    monkeypatch.setattr(lab.urllib.request,'build_opener',lambda *a:Opener())
    with pytest.raises(urllib.error.HTTPError):lab.read_accounts(['public-address'])
    with pytest.raises(quotes.ProbeError) as exc:
        quotes.request_json(config.QUOTE_RPC_URL,'METADATA',dict(jsonrpc='2.0',id=1,method='getTokenSupply',params=[]))
    assert exc.value.status=='RPC_DEFERRED' and len(calls)==1
    assert rpc.snapshot()['endpoints'][0]['server_429']==1


def wait_for(predicate,limit=6):
    deadline=time.monotonic()+limit
    while not predicate():
        assert time.monotonic()<deadline,'child readiness timeout'
        time.sleep(.005)


def children(tmp_path,n,rate,slots,mode='normal'):
    helper=Path(__file__).parent/'helpers/rpc_budget_worker.py'
    # Base interpreter avoids the Windows venv redirector so this test owns the
    # exact disposable PID. The helper imports only standard-library modules.
    result=[]
    try:
        for i in range(n):
            with (tmp_path/('rpc_worker_'+str(i)+'.stderr.log')).open('wb') as log:
                result.append(subprocess.Popen([getattr(sys,'_base_executable',sys.executable),str(helper),str(tmp_path),str(i),mode,str(rate),str(slots)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log))
        return result
    except BaseException:
        for p in result:
            if p.poll() is None:p.kill()
            p.wait(timeout=5)
        raise


def child_reports(tmp_path,n):
    rows=[]
    for i in range(n):
        row=dict(worker=i)
        p=tmp_path/(str(i)+'.result.json')
        try:row['result']=json.loads(p.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:row['result_unavailable']=type(exc).__name__
        p=tmp_path/('rpc_worker_'+str(i)+'.stderr.log')
        try:row['stderr_tail']=p.read_bytes()[-16384:].decode('utf-8',errors='replace')
        except OSError as exc:row['stderr_unavailable']=type(exc).__name__
        rows.append(row)
    return rows


def preserve_children(tmp_path,procs,scenario):
    destination=os.getenv('RPC_CONTROL_EVIDENCE_V891')
    rows=child_reports(tmp_path,len(procs))
    for row,p in zip(rows,procs):row['exit_code']=p.poll()
    for p in (tmp_path/'shared').glob('control_failed.json'):
        try:marker=json.loads(p.read_text())
        except (OSError,ValueError):marker={'status':'UNREADABLE'}
        break
    else:marker=None
    if destination:
        with open(destination,'a',encoding='utf-8') as f:f.write(json.dumps(dict(scenario=scenario,workers=rows,control_failure_marker=marker))+'\n')
    return rows


def test_real_processes_obey_global_slots_and_shared_rate(tmp_path):
    procs=children(tmp_path,4,rate=10,slots=2)
    try:
        wait_for(lambda:len(list(tmp_path.glob('*.ready')))==4);(tmp_path/'go').write_text('go')
        codes=[p.wait(timeout=6) for p in procs]
        assert codes==[0]*4,child_reports(tmp_path,4)
        intervals=[json.loads((tmp_path/(str(i)+'.json')).read_text()) for i in range(4)]
        assert len(intervals)==4
        events=sorted([(r['start'],1) for r in intervals]+[(r['end'],-1) for r in intervals])
        active=peak=0
        for _,delta in events:active+=delta;peak=max(peak,active)
        assert peak<=2
        starts=sorted(r['start'] for r in intervals)
        assert all(b-a>=.08 for a,b in zip(starts,starts[1:])),starts
        summary=rpc.Budget(tmp_path/'shared',rate=10,slots=2).snapshot()
        assert summary['inflight']==0 and summary['endpoints'][0]['started']==4
    finally:
        for p in procs:
            if p.poll() is None:p.kill()
            p.wait(timeout=5)
        preserve_children(tmp_path,procs,'FOUR_PROCESS_SHARED_LIMITS')


def test_process_death_releases_slot_without_pid_reclaim(tmp_path):
    procs=children(tmp_path,1,rate=1000,slots=1,mode='hold');p=procs[0]
    try:
        wait_for(lambda:(tmp_path/'0.ready').exists());(tmp_path/'go').write_text('go')
        wait_for(lambda:(tmp_path/'0.held').exists());p.kill();p.wait(timeout=5)
        with rpc.Budget(tmp_path/'shared',rate=1000,slots=1).request('https://another.invalid'):pass
    finally:
        if p.poll() is None:p.kill()
        p.wait(timeout=5)
        preserve_children(tmp_path,procs,'EXPECTED_PROCESS_DEATH_RELEASE')


def test_corrupt_control_db_denies_admission(tmp_path):
    b=rpc.Budget(tmp_path);tmp_path.mkdir(exist_ok=True);b.path.write_bytes(b'not a SQLite database')
    with pytest.raises(rpc.Unavailable,match='CONTROL_DB_ERROR'):
        with b.request('https://rpc.invalid'):pytest.fail('corrupt control admitted request')


def test_local_wait_is_not_reported_as_remote_http_429():
    import connection_audit,fresh_retry
    details=connection_audit.diagnostic(rpc.Deferred('COOLDOWN',7))
    assert details['category']=='LOCAL_RPC_ADMISSION' and not any('http_status' in r for r in details['chain'])
    parsed=fresh_retry.details(dict(network_error=details))
    assert parsed['retry_after_s']==7 and fresh_retry.policy('REQUEST_TIMEOUT',parsed)[0]


def test_coordination_bookkeeping_cannot_freshen_pool_snapshot(monkeypatch):
    import local_simulation as lab
    wall=[100.]
    monkeypatch.setattr(lab.time,'time',lambda:wall[0])
    class Response:
        def read(self,*a):return json.dumps(dict(result=dict(context=dict(slot=1),value=[None]))).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Opener:
        def open(self,*a,**k):return Response()
    monkeypatch.setattr(lab.urllib.request,'build_opener',lambda *a:Opener())
    original=rpc.Budget.record
    def delayed(self,*args,**kwargs):
        wall[0]=200.
        return original(self,*args,**kwargs)
    monkeypatch.setattr(rpc.Budget,'record',delayed)
    result=lab.read_accounts(['public-address'])
    assert result['received_ts']==100. and wall[0]==200.


def hold_control_write(budget):
    import threading
    locked=threading.Event();release=threading.Event()
    def hold():
        with sqlite3.connect(budget.path,isolation_level=None) as c:
            c.execute('BEGIN IMMEDIATE');locked.set();release.wait(3);c.execute('ROLLBACK')
    thread=threading.Thread(target=hold);thread.start()
    assert locked.wait(1)
    return release,thread


def test_short_sqlite_write_contention_is_not_permanent_failure(tmp_path):
    import threading
    b=rpc.Budget(tmp_path,rate=1000,slots=1)
    try:
        with b.request('https://rpc.invalid'):
            release,thread=hold_control_write(b)
            timer=threading.Timer(.08,release.set);timer.start()
    finally:release.set();thread.join(2);timer.join(2)
    assert not b.failed.exists() and b.snapshot()['status']=='READY'
    row=b.snapshot()['endpoints'][0]
    assert row['started']==1 and row['other_errors']==0


def test_429_is_recorded_once_after_short_write_contention(tmp_path):
    import threading
    b=rpc.Budget(tmp_path,rate=1000,slots=1)
    try:
        with pytest.raises(urllib.error.HTTPError):
            with b.request('https://rpc.invalid'):
                release,thread=hold_control_write(b)
                timer=threading.Timer(.08,release.set);timer.start()
                raise urllib.error.HTTPError('https://rpc.invalid',429,'fixture',{'Retry-After':'10'},None)
    finally:release.set();thread.join(2);timer.join(2)
    row=b.snapshot()['endpoints'][0]
    assert row['server_429']==1 and row['started']==1 and row['cooldown_remaining_s']>9
    assert not b.failed.exists()
    with pytest.raises(rpc.Deferred):
        with b.request('https://rpc.invalid'):pytest.fail('cooldown was lost')


def test_pending_http_outcome_denies_admission_until_sqlite_recovers(tmp_path):
    b=rpc.Budget(tmp_path,rate=1000,slots=1,wait=.01)
    try:
        with b.request('https://rpc.invalid'):release,thread=hold_control_write(b)
        assert not b.failed.exists() and b.snapshot()['status']=='PENDING'
        with pytest.raises(rpc.Deferred):
            with b.request('https://rpc.invalid'):pytest.fail('pending outcome bypassed')
    finally:release.set();thread.join(2)
    with b.request('https://rpc.invalid'):pass
    assert b.snapshot()['status']=='READY' and not b._pending()


def test_child_failure_stderr_and_phase_are_preserved(tmp_path,monkeypatch):
    evidence=tmp_path/'rpc_control_evidence.jsonl';monkeypatch.setenv('RPC_CONTROL_EVIDENCE_V891',str(evidence))
    procs=children(tmp_path,1,rate=10,slots=2,mode='controlled_failure');p=procs[0]
    try:
        wait_for(lambda:(tmp_path/'0.ready').exists());(tmp_path/'go').write_text('go')
        assert p.wait(timeout=6)==1
    finally:
        if p.poll() is None:p.kill()
        p.wait(timeout=5)
        rows=preserve_children(tmp_path,procs,'CONTROLLED_FAILURE_EXPECTED')
    row=rows[0]
    assert row['result']['status']=='FAILED' and row['result']['phase']=='ADMISSION'
    assert 'CONTROLLED_WORKER_FAILURE_EXPECTED' in row['stderr_tail']
    assert row['result']['error_chain'][0]['error_type']=='RuntimeError'
    assert json.loads(evidence.read_text())['scenario']=='CONTROLLED_FAILURE_EXPECTED'


def test_repeated_cold_start_coordination_four_processes(tmp_path):
    # Same strict acceptance assertions, across six fresh coordination DBs.
    for index in range(6):
        folder=tmp_path/str(index);folder.mkdir()
        test_real_processes_obey_global_slots_and_shared_rate(folder)

@pytest.fixture(autouse=True)
def legacy_url_lib_backend(monkeypatch):
    # These tests inject urllib openers; keep-alive has separate real-budget tests.
    import config
    monkeypatch.setattr(config,"RPC_KEEPALIVE",False)
