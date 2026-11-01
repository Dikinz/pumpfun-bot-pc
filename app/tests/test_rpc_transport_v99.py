"""Local HTTP transport model, real solders and common budget; no provider IO."""
import contextlib,http.client,http.server,json,os,socket,threading,time,urllib.error
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import pytest
import config,local_simulation as lab,program_cache as cache,rpc_budget as budget,rpc_timing as timing,rpc_transport_v99 as transport
from solders.hash import Hash

GENESIS=str(Hash.from_bytes(bytes(range(32))))

@pytest.fixture
def server(tmp_path,monkeypatch):
    state=SimpleNamespace(connections=0,requests=0,status=200,body=None,close=False,drop=False,slot=100,paths=[])
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads=True
        def get_request(self):
            result=super().get_request();state.connections+=1;return result
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_POST(self):
            state.requests+=1;state.paths.append(self.path)
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if state.drop:self.close_connection=True;return
            body=state.body if state.body is not None else json.dumps({'result':GENESIS if payload.get('method')=='getGenesisHash' else {'context':{'slot':state.slot},'value':[None]*len(payload['params'][0])}}).encode()
            self.send_response(state.status);self.send_header('Content-Length',str(len(body)));self.send_header('Retry-After','7')
            if state.close:self.send_header('Connection','close')
            self.end_headers();self.wfile.write(body);self.wfile.flush()
    httpd=Server(('127.0.0.1',0),Handler);thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(transport.http.client,'HTTPSConnection',http.client.HTTPConnection)
    monkeypatch.setattr(transport.urllib.request,'getproxies',lambda:{})
    monkeypatch.setattr(config,'RPC_KEEPALIVE',True)
    monkeypatch.setattr(config,'QUOTE_RPC_URL',f'https://127.0.0.1:{httpd.server_port}/rpc?key=PRIVATE')
    timing.start(tmp_path/'timing');transport.close()
    yield state
    transport.close();timing.ACTIVE=None;httpd.shutdown();httpd.server_close();thread.join(2)


def test_transport99_reuses_socket_but_admits_every_request(server):
    for _ in range(5):assert lab.read_accounts(['a'],99)['slot']==100
    assert server.connections==1 and server.requests==5
    rows=timing.snapshot()['requests'];assert len(rows)==5
    assert [r['connection_reused_v99'] for r in rows]==[False,True,True,True,True]
    assert budget.snapshot()['endpoints'][0]['started']==5
    assert all(r['transport_v99']=='HTTPS_KEEPALIVE' for r in rows)
    assert 'PRIVATE' not in json.dumps(timing.snapshot())


def test_transport99_429_retains_retry_after_and_common_cooldown(server):
    server.status=429
    with pytest.raises(urllib.error.HTTPError) as err:lab.read_accounts(['a'])
    assert err.value.headers['Retry-After']=='7' and transport._LOCAL.connection is None
    with pytest.raises(budget.Deferred):lab.read_accounts(['a'])
    assert server.requests==1 and budget.snapshot()['endpoints'][0]['server_429']==1


@pytest.mark.parametrize('code',[301,302,307,308])
def test_transport99_redirects_are_never_followed(server,code):
    server.status=code
    with pytest.raises(urllib.error.HTTPError) as err:lab.read_accounts(['a'])
    assert err.value.code==code and server.requests==1 and transport._LOCAL.connection is None


def test_transport99_server_close_reconnects_next_independent_call(server):
    server.close=True
    for _ in range(3):lab.read_accounts(['a'])
    assert server.connections==server.requests==3


def test_transport99_broken_connection_has_no_automatic_retry(server):
    lab.read_accounts(['a']);server.drop=True
    with pytest.raises(http.client.RemoteDisconnected):lab.read_accounts(['a'])
    assert server.requests==2 and transport._LOCAL.connection is None
    server.drop=False;lab.read_accounts(['a'])
    assert server.requests==3 and server.connections==2


def test_transport99_json_rpc_error_is_consumed_before_socket_reuse(server):
    server.body=b'{"error":{"code":-32005}}'
    with pytest.raises(lab.LabError,match='RPC_ERROR_-32005'):lab.read_accounts(['a'])
    server.body=None;lab.read_accounts(['a'])
    assert server.connections==1 and server.requests==2


def test_transport99_snapshot_slot_validation_is_unchanged(server):
    with pytest.raises(lab.LabError,match='INVALID_RPC_SNAPSHOT'):lab.read_accounts(['a'],101)
    server.slot=102;assert lab.read_accounts(['a'],101)['slot']==102
    assert server.requests==2


def test_transport99_full_endpoint_change_closes_old_connection(server,monkeypatch):
    lab.read_accounts(['a']);monkeypatch.setattr(config,'QUOTE_RPC_URL',config.QUOTE_RPC_URL+'&changed=1');lab.read_accounts(['a'])
    assert server.connections==2 and server.paths[-1].endswith('&changed=1')


def _read_when_admitted(deadline,stats):
    # Test scheduler only. A Deferred means no HTTP was sent. Never retry an
    # HTTP/transport error here; every subsequent attempt uses the real guard.
    while True:
        remaining=deadline-time.monotonic()
        if remaining<=0:raise AssertionError('LOCAL_ADMISSION_DEADLINE '+json.dumps(dict(stats,last_deferrals=list(stats['last_deferrals']))))
        try:return lab.read_accounts(['a'])
        except budget.Deferred as exc:
            stats['deferrals']+=1
            stats['last_deferrals'].append(dict(reason=exc.rpc_budget_reason,wait_ms=exc.wait_ms,
                retry_after_s=exc.retry_after_s,control=getattr(exc,'control_v95',None)))
            remaining=deadline-time.monotonic();delay=max(.001,exc.retry_after_s)
            if delay>=remaining:raise AssertionError('LOCAL_ADMISSION_DEADLINE '+json.dumps(dict(stats,last_deferrals=list(stats['last_deferrals'])))) from exc
            time.sleep(delay)


def _thread_evidence(server,rows,status,error=None):
    value=dict(schema='TRANSPORT_THREAD_V992',status=status,requests=server.requests,connections=server.connections,
        workers=[dict(deferrals=r['stats']['deferrals'],last_deferrals=list(r['stats']['last_deferrals']),
            same_connection=r['connections'][0] is r['connections'][1]) for r in rows],error_type=type(error).__name__ if error else None)
    target=os.getenv('TRANSPORT_THREAD_EVIDENCE_V992')
    if target:
        with Path(target).open('a',encoding='utf-8') as f:f.write(json.dumps(value)+'\n')
    return value


def _exercise_thread_connections(server):
    barrier=threading.Barrier(3);rows=[]
    def work(_):
        stats=dict(deferrals=0,last_deferrals=deque(maxlen=8));connections=[]
        try:
            barrier.wait(timeout=3)
            deadline=time.monotonic()+10 # Test job deadline, not a production admission timeout.
            for _ in range(2):
                _read_when_admitted(deadline,stats);connections.append(transport._LOCAL.connection)
            # Keep references in the result so id reuse after close cannot fake
            # distinct thread ownership. Connections are closed in finally.
            return dict(stats=stats,connections=connections)
        finally:transport.close()
    try:
        with ThreadPoolExecutor(3) as pool:rows=list(pool.map(work,range(3)))
        assert server.requests==6 and server.connections==3
        assert all(len(r['connections'])==2 and r['connections'][0] is not None and r['connections'][0] is r['connections'][1] for r in rows)
        assert len({id(r['connections'][0]) for r in rows})==3
    except BaseException as exc:
        try:_thread_evidence(server,rows,'FAILED',exc)
        except OSError as evidence_error:exc.add_note('Evidence write failed: '+type(evidence_error).__name__)
        raise
    return _thread_evidence(server,rows,'PASSED')


def test_transport99_one_connection_per_thread(server):
    _exercise_thread_connections(server)


@pytest.mark.parametrize('ordinal',[1,2],ids=['before-first-http','warm-connection'])
def test_transport992_local_deferral_preserves_real_guard_and_thread_connections(server,monkeypatch,ordinal):
    original=budget.guard;local=threading.local();injected_states=[];injected_lock=threading.Lock()
    @contextlib.contextmanager
    def guard(url):
        present=getattr(transport._LOCAL,'connection',None) is not None
        if not getattr(local,'deferred',False) and (ordinal==1 or present):
            local.deferred=True
            with injected_lock:injected_states.append((threading.get_ident(),present))
            exc=budget.Deferred('STATE_LOCKED',.001,100.)
            exc.control_v95=dict(stage='INJECTED_METADATA_CONTENTION',error_type='MetadataBusy',connection_present=present)
            raise exc
        with original(url) as permit:yield permit
    monkeypatch.setattr(budget,'guard',guard)
    row=_exercise_thread_connections(server)
    assert len(row['workers'])==3 and all(w['deferrals']>=1 for w in row['workers'])
    assert len(injected_states)==3 and len({tid for tid,present in injected_states})==3
    assert all(present is (ordinal==2) for tid,present in injected_states)
    assert budget.snapshot()['endpoints'][0]['started']==6


def test_transport992_permanent_local_deferral_hits_bounded_deadline_without_http(server,monkeypatch):
    @contextlib.contextmanager
    def guard(url):
        raise budget.Deferred('STATE_LOCKED',.01,0.)
        yield # Context manager interface; never reached.
    monkeypatch.setattr(budget,'guard',guard);stats=dict(deferrals=0,last_deferrals=deque(maxlen=8));start=time.monotonic()
    with pytest.raises(AssertionError,match='LOCAL_ADMISSION_DEADLINE'):_read_when_admitted(start+.08,stats)
    assert time.monotonic()-start<1.5 and stats['deferrals']>0 and server.requests==server.connections==0


def test_transport992_scheduler_does_not_retry_http_429(server):
    server.status=429;stats=dict(deferrals=0,last_deferrals=deque(maxlen=8))
    with pytest.raises(urllib.error.HTTPError) as exc:_read_when_admitted(time.monotonic()+1,stats)
    assert exc.value.code==429 and server.requests==1 and stats['deferrals']==0
    assert budget.snapshot()['endpoints'][0]['server_429']==1


def test_transport992_scheduler_does_not_hide_other_failures(server,monkeypatch):
    def fail(*a,**kw):raise ValueError('CONTROLLED_MODEL_ERROR')
    monkeypatch.setattr(lab,'read_accounts',fail);stats=dict(deferrals=0,last_deferrals=deque(maxlen=8))
    with pytest.raises(ValueError,match='CONTROLLED_MODEL_ERROR'):_read_when_admitted(time.monotonic()+1,stats)
    assert server.requests==0 and stats['deferrals']==0


def test_transport99_proxy_uses_original_url_lib_backend(monkeypatch):
    monkeypatch.setattr(config,'RPC_KEEPALIVE',True);monkeypatch.setattr(config,'QUOTE_RPC_URL','https://rpc.invalid/private')
    monkeypatch.setattr(transport.urllib.request,'getproxies',lambda:{'https':'http://proxy.invalid:8080'})
    monkeypatch.setattr(transport.urllib.request,'proxy_bypass',lambda host:False)
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return b'{"result":{"context":{"slot":1},"value":[null]}}'
    calls=[]
    def open(req,timeout):calls.append((req.full_url,timeout));return Response()
    monkeypatch.setattr(lab.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=open))
    assert lab.read_accounts(['a'])['slot']==1 and calls==[(config.QUOTE_RPC_URL,6)]


@pytest.fixture
def genesis_lab(tmp_path,monkeypatch):
    import exit_quotes
    calls=[];cfg=SimpleNamespace(QUOTE_RPC_URL='https://rpc.invalid/?secret=PRIVATE',GENESIS_CACHE_TTL_S=60,GENESIS_CACHE_PATH=tmp_path/'genesis.json')
    def request(*a,**kw):calls.append(a);return {'result':GENESIS}
    monkeypatch.setattr(exit_quotes,'request_json',request)
    return SimpleNamespace(config=cfg),calls


def test_transport99_genesis_default_off_always_verifies_live(genesis_lab):
    obj,calls=genesis_lab;obj.config.GENESIS_CACHE_TTL_S=0
    for _ in range(3):assert cache.genesis(obj)==GENESIS
    assert len(calls)==3 and not obj.config.GENESIS_CACHE_PATH.exists()


def test_transport99_genesis_hit_is_endpoint_scoped_and_validated(genesis_lab):
    obj,calls=genesis_lab
    for _ in range(3):assert cache.genesis(obj)==GENESIS
    assert len(calls)==1
    obj.config.QUOTE_RPC_URL+='&new=1';assert cache.genesis(obj)==GENESIS and len(calls)==2
    assert 'PRIVATE' not in obj.config.GENESIS_CACHE_PATH.read_text()


@pytest.mark.parametrize('stamp',[0,time.time()+3600,float('nan'),True,'bad'])
def test_transport99_genesis_expired_future_or_invalid_timestamp_misses(genesis_lab,stamp):
    obj,calls=genesis_lab;path=obj.config.GENESIS_CACHE_PATH
    path.write_text(json.dumps({cache._genesis_key(obj.config.QUOTE_RPC_URL):{'ts':stamp,'value':GENESIS}}))
    assert cache.genesis(obj)==GENESIS and len(calls)==1


@pytest.mark.parametrize('raw',['{broken','[]','x'*33000,json.dumps({'bad':None})],ids=['invalid-json','array','oversized-33kb','invalid-record'])
def test_transport99_genesis_corrupt_or_oversized_cache_is_not_authority(genesis_lab,raw):
    obj,calls=genesis_lab;obj.config.GENESIS_CACHE_PATH.write_text(raw)
    assert cache.genesis(obj)==GENESIS and len(calls)==1


def test_transport99_genesis_bad_hash_never_hits_or_stores(genesis_lab,monkeypatch):
    obj,calls=genesis_lab;path=obj.config.GENESIS_CACHE_PATH
    path.write_text(json.dumps({cache._genesis_key(obj.config.QUOTE_RPC_URL):{'ts':time.time(),'value':'a'*32}}))
    assert cache.genesis(obj)==GENESIS and len(calls)==1
    import exit_quotes
    monkeypatch.setattr(exit_quotes,'request_json',lambda *a,**kw:{'result':'NOT_A_HASH'})
    obj.config.GENESIS_CACHE_TTL_S=0
    with pytest.raises(cache.CacheError,match='CACHE_GENESIS_UNVERIFIED'):cache.genesis(obj)


def test_transport99_genesis_optional_write_failure_keeps_live_result(genesis_lab,monkeypatch):
    obj,calls=genesis_lab
    monkeypatch.setattr(__import__('state_io'),'atomic_json',lambda *a:(_ for _ in ()).throw(PermissionError(13,'denied')))
    assert cache.genesis(obj)==GENESIS and len(calls)==1


def test_transport99_genesis_concurrent_writers_are_atomic_and_bounded(tmp_path):
    path=tmp_path/'genesis.json'
    def write(i):return cache._genesis_store(path,f'https://rpc.invalid/{i}',GENESIS)
    with ThreadPoolExecutor(6) as pool:assert all(pool.map(write,range(12)))
    data=json.loads(path.read_text());assert len(data)==8
    assert all(Hash.from_string(row['value']) for row in data.values())
    assert not list(tmp_path.glob('*.tmp'))


def test_transport99_oversize_body_drops_connection(server):
    with timing.request('getMultipleAccounts') as trace:
        data=transport.post(config.QUOTE_RPC_URL,b'{"params":[["a"]]}',trace,8)
    assert len(data)==9 and transport._LOCAL.connection is None and server.requests==1


def test_transport99_budget_record_cannot_freshen_snapshot(server,monkeypatch):
    now=[1000.];monkeypatch.setattr(lab.time,'time',lambda:now[0])
    original=budget.Budget.record
    def record(self,*a,**kw):
        now[0]+=20;return original(self,*a,**kw)
    monkeypatch.setattr(budget.Budget,'record',record)
    result=lab.read_accounts(['a'])
    assert result['received_ts']==1000. and now[0]==1020.


def test_transport99_genesis_invalid_record_does_not_break_live_lookup(genesis_lab):
    obj,calls=genesis_lab;path=obj.config.GENESIS_CACHE_PATH;key=cache._genesis_key(obj.config.QUOTE_RPC_URL)
    for row in (None,[],{'ts':time.time(),'value':None},{'ts':10**400,'value':GENESIS}):
        path.write_text(json.dumps({key:row}))
        assert cache.genesis(obj)==GENESIS
    assert len(calls)==4
