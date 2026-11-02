"""WP2 engine client: /health over a real local test server (8210-8219), stop file, detached spawn
arguments, state reading and the pid+create_time check. Also pins the forbid_live guard itself."""
import json,os,socket,subprocess,sys,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import pytest
from fakes import forbid_live,forbidden_argv,LiveAccessError,TOKEN,OTHER,make_layout,write_state,free_port,FakePopen,Health,Procs,controller,C
from desktop import engine_client
from desktop.engine_client import EngineClient,get_health,new_token

class Server:
    def __init__(self,routes):
        self.port=free_port();self.hits=[];routes=routes;hits=self.hits
        class H(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                hits.append(self.path);status,body=routes.get(self.path,(404,b'{}'))
                self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_POST(self):hits.append('POST '+self.path);self.send_response(405);self.end_headers()
        self.server=ThreadingHTTPServer(('127.0.0.1',self.port),H);self.server.daemon_threads=True
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def close(self):self.server.shutdown();self.server.server_close()

def test_forbid_live_guard_blocks_live_ports_and_real_bot(tmp_path):
    for port in (8188,49190):
        with pytest.raises(LiveAccessError):socket.create_connection(('127.0.0.1',port),timeout=.1)
        with socket.socket() as s:
            with pytest.raises(LiveAccessError):s.connect(('127.0.0.1',port))
    for argv in (['py','-I','app/main.py'],['py','-I','x/app/bootstrap.py','start'],['py','bootstrap.py'],['py','-I','app/desktop_engine.py','run','--token',TOKEN],
            '"C:\\Windows\\System32\\cmd.exe" /c "C:\\x\\03_START.bat"',['py','launcher.py']):
        assert forbidden_argv(argv)
        with pytest.raises(LiveAccessError):subprocess.Popen(argv)
    for argv in (['py','-I','desktop/tests/fake_engine.py','run','--token',TOKEN],['py','app/bootstrap.py','results'],['py','app/dashboard/server.py']):
        assert forbidden_argv(argv) is None

def test_health_reads_json_and_only_get_health():
    payload=json.dumps(dict(version='v1',database_identity='abc',pid=1)).encode()
    server=Server({'/health':(200,payload)})
    try:
        assert get_health(server.port)==dict(version='v1',database_identity='abc',pid=1)
        assert server.hits==['/health']
    finally:server.close()
    for routes in ({'/health':(500,payload)},{'/health':(200,b'not json')},{'/health':(200,b'[1,2]')},{}):
        server=Server(routes)
        try:assert get_health(server.port) is None
        finally:server.close()

def test_health_closed_port_and_forbidden_port():
    port=free_port();t=time.monotonic()
    assert get_health(port) is None and time.monotonic()-t<3
    with pytest.raises(ValueError):get_health(49190)

def test_health_kind_needs_version_and_identity(tmp_path):
    lay=make_layout(tmp_path);c=EngineClient(lay)
    same=dict(version=lay.version,database_identity=lay.identity)
    assert c.health_kind(same)=='EXTERNAL_SAME' and c.health_kind(None) is None and c.health_kind([1]) is None
    assert c.health_kind(dict(same,version='v0'))=='EXTERNAL_FOREIGN' and c.health_kind(dict(same,database_identity='x'))=='EXTERNAL_FOREIGN'
    lay.version=None;assert c.health_kind(dict(same,version=None))=='EXTERNAL_FOREIGN'

def test_request_stop_is_named_by_token_and_idempotent(tmp_path):
    lay=make_layout(tmp_path);c=EngineClient(lay)
    p=c.request_stop(TOKEN);assert p==lay.control/('stop_'+TOKEN+'.request') and p.is_file()
    assert c.request_stop(TOKEN)==p and sorted(x.name for x in lay.control.iterdir())==[p.name]
    for bad in (TOKEN.upper(),'../'+TOKEN[3:],'',None):
        with pytest.raises(ValueError):c.request_stop(bad)
    assert len(list(lay.control.iterdir()))==1

def test_state_reading_and_alive(tmp_path):
    import psutil
    lay=make_layout(tmp_path);c=EngineClient(lay)
    assert c.state()=={} and c.alive({}) is False
    write_state(lay,pid=os.getpid(),create_time=psutil.Process().create_time())
    assert c.state()['token']==TOKEN and c.alive(c.state()) is True
    write_state(lay,pid=os.getpid(),create_time=1.0);assert c.alive(c.state()) is False
    write_state(lay,schema='OTHER');assert c.state()=={}
    (lay.control/C.STATE_NAME).write_text('{bad',encoding='utf-8');assert c.state()=={}

def test_spawn_is_detached_and_keeps_no_handle(tmp_path):
    lay=make_layout(tmp_path);pop=FakePopen();c=EngineClient(lay,popen=pop)
    s=c.spawn(TOKEN,['x','y'],dict(A='1'))
    call,=pop.calls
    assert call['args']==['x','y'] and call['env']==dict(A='1') and call['stdin']==subprocess.DEVNULL and call['stderr']==subprocess.STDOUT
    assert call['creationflags']==(C.CREATE_NO_WINDOW|C.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0) and call['cwd']==str(lay.root)
    assert s.log.parent==lay.logs and s.log.name.endswith('_'+TOKEN[:8]+'.log') and s.exited() is None
    assert C.engine.find_log(TOKEN,lay.control)==s.log.name  # the host records it as log_name
    s.log.unlink()  # no handle left open in the window
    with pytest.raises(ValueError):c.spawn('bad',['x'],{})

def test_new_token_is_valid_and_unique():
    tokens={new_token() for _ in range(50)}
    assert len(tokens)==50 and all(C.engine.valid_token(t) for t in tokens)

# ---- M1 round 2: is the answering dashboard a descendant of the dead host? ----
class FakePsutil:
    """Process table pid -> (ppid, create_time); 'denied' pids raise AccessDenied; listeners for net_connections."""
    CONN_LISTEN='LISTEN'
    class Error(Exception):pass
    class NoSuchProcess(Error):pass
    class AccessDenied(Error):pass
    def __init__(self,table,denied=(),listeners=()):
        self.table=dict(table);self.denied=set(denied);self.listeners=list(listeners);outer=self
        class Process:
            def __init__(self,pid):
                if pid in outer.denied:raise outer.AccessDenied(pid)
                if pid not in outer.table:raise outer.NoSuchProcess(pid)
                self.pid=pid
            def ppid(self):return outer.table[self.pid][0]
            def create_time(self):return outer.table[self.pid][1]
        self.Process=Process
    def net_connections(self,kind='inet'):
        from types import SimpleNamespace as NS
        return [NS(status=status,laddr=NS(ip=ip,port=port),pid=pid) for ip,port,status,pid in self.listeners]

HOST=dict(pid=4242,create_time=1000.5)
@pytest.mark.parametrize('table,health,listeners,denied,expected',[
    # dashboard 30 -> venv redirector 20 -> dead host 4242 (Windows keeps the dead parent's pid as ppid)
    ({30:(20,1003.),20:(4242,1002.)},dict(pid=30),(),(),True),
    ({30:(4242,1003.)},dict(pid=30),(),(),True),
    # no pid in /health: the listener of the layout port (only a 127.0.0.1/0.0.0.0 LISTEN on that port counts)
    ({30:(4242,1003.),31:(1,5.)},{},[('127.0.0.1',8216,'LISTEN',31),('127.0.0.1',8215,'ESTABLISHED',31),('127.0.0.1',8215,'LISTEN',30)],(),True),
    ({30:(4242,1003.)},{},[('192.168.1.5',8215,'LISTEN',30)],(),None),  # nothing on loopback: undeterminable
    # 03_START after a stale engine_state.json: the chain ends without the host
    ({30:(20,1003.),20:(10,900.),10:(0,1.)},dict(pid=30),(),(),False),
    ({30:(20,1003.),20:(20,900.)},dict(pid=30),(),(),False),
    # the host pid was later reused by a living process: a link created before the new owner is still the old host's child
    ({30:(4242,1003.),4242:(1,1500.)},dict(pid=30),(),(),True),
    # ...but a link created after the new owner of the pid is that owner's child: not ours
    ({30:(4242,1600.),4242:(1,1500.)},dict(pid=30),(),(),False),
    # the host is still exactly the recorded one (race with alive()): consistent, a descendant
    ({30:(4242,1003.),4242:(1,1000.5)},dict(pid=30),(),(),True),
    # a link older than the host cannot be its child (the host pid was reused before)
    ({30:(4242,900.)},dict(pid=30),(),(),False),
    # an intermediate ppid reused by a younger process, a dead intermediate, AccessDenied, a gone dashboard
    ({30:(20,1003.),20:(4242,1500.)},dict(pid=30),(),(),None),
    ({30:(20,1003.)},dict(pid=30),(),(),None),
    ({30:(20,1003.),20:(4242,1002.)},dict(pid=30),(),(20,),None),
    ({},dict(pid=30),(),(),None),
])
def test_dashboard_lineage_with_fake_process_table(monkeypatch,table,health,listeners,denied,expected):
    monkeypatch.setitem(sys.modules,'psutil',FakePsutil(table,denied=denied,listeners=listeners))
    verdict,detail=engine_client.dashboard_lineage(dict(HOST),health,8215)
    assert verdict is expected,detail
    assert isinstance(detail,str) and detail

def test_dashboard_lineage_needs_host_identity(monkeypatch):
    monkeypatch.setitem(sys.modules,'psutil',FakePsutil({30:(4242,1003.)}))
    for state in (dict(pid=4242),dict(create_time=1.),dict(pid='4242',create_time=1.),dict(pid=0,create_time=1.)):
        assert engine_client.dashboard_lineage(state,dict(pid=30),8215)[0] is None
    class Boom(FakePsutil):
        def net_connections(self,kind='inet'):raise self.AccessDenied('table')
    monkeypatch.setitem(sys.modules,'psutil',Boom({}))
    assert engine_client.dashboard_lineage(dict(HOST),{},8215)[0] is None

def test_client_lineage_never_raises(tmp_path):
    lay=make_layout(tmp_path)
    def boom(state,health,port):raise RuntimeError('x')
    assert EngineClient(lay,lineage=boom).lineage(dict(HOST),{})==(None,'lineage error: RuntimeError')
    seen=[];client=EngineClient(lay,lineage=lambda s,h,p:(seen.append(p),(True,'ok'))[1])
    assert client.lineage(dict(HOST),{})==(True,'ok') and seen==[8215]
    assert EngineClient(lay)._lineage is engine_client.dashboard_lineage

LISTENER=r'''
import os,socket,sys,time
port,ready,stop=int(sys.argv[1]),sys.argv[2],sys.argv[3]
s=socket.socket();s.bind(('127.0.0.1',port));s.listen(1);s.settimeout(.1)
open(ready+'.tmp','w').write(str(os.getpid()));os.replace(ready+'.tmp',ready)
deadline=time.monotonic()+60
while not os.path.exists(stop) and time.monotonic()<deadline:
    try:s.accept()[0].close()
    except OSError:pass
'''
PARENT=r'''
import json,os,subprocess,sys,time,psutil
code,port,ready,stop,out=sys.argv[1:6]
subprocess.Popen([sys.executable,'-I','-c',code,port,ready,stop],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
deadline=time.monotonic()+30
while not os.path.exists(ready) and time.monotonic()<deadline:time.sleep(.05)
open(out+'.tmp','w').write(json.dumps(dict(pid=os.getpid(),create_time=psutil.Process().create_time())));os.replace(out+'.tmp',out)
'''

def _wait_file(path,timeout=30.):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            text=path.read_text()
            if text:return text
        except OSError:pass
        time.sleep(.05)
    raise AssertionError('timeout waiting for '+str(path))

def _end_listener(stop,ready):
    """Ends the listener this test started: stop file first, then by its pid if it still runs."""
    import psutil
    stop.write_text('x')
    try:pid=int(ready.read_text())
    except (OSError,ValueError):return
    try:
        p=psutil.Process(pid)
        try:p.wait(10)
        except psutil.TimeoutExpired:p.kill()
    except psutil.NoSuchProcess:pass

def test_real_dashboard_descends_from_dead_parent_and_unrelated_listener_does_not(tmp_path):
    '''A parent python starts a child listening on a test port and exits (the dead "host"): the child is found as
    its descendant both by the /health pid and by the listener of the port -> ORPHAN_RUNNING. A listener the test
    starts itself does not descend from that dead parent -> EXTERNAL_SAME (an external 03_START run).'''
    import psutil
    port=free_port();ready=tmp_path/'ready';stop=tmp_path/'stop';out=tmp_path/'host.json'
    parent=subprocess.Popen([sys.executable,'-I','-c',PARENT,LISTENER,str(port),str(ready),str(stop),str(out)])
    try:
        assert parent.wait(60)==0
        host=json.loads(_wait_file(out));child=int(_wait_file(ready))
        assert not psutil.pid_exists(host['pid']) or abs(psutil.Process(host['pid']).create_time()-host['create_time'])>=.01
        state=dict(host,token=TOKEN,phase='RUNNING')
        verdict,detail=engine_client.dashboard_lineage(state,dict(pid=child),port);assert verdict is True,detail
        assert engine_client.listener_pid(port)==child
        verdict,detail=engine_client.dashboard_lineage(state,{},port);assert verdict is True,detail
        lay=make_layout(tmp_path/'a',port=port)
        h=Health().same(lay);h.data['pid']=child
        ctl=controller(lay,procs=Procs(),health=h,lineage=engine_client.dashboard_lineage)
        write_state(lay,phase='RUNNING',pid=host['pid'],create_time=host['create_time'])
        assert ctl.refresh().kind=='ORPHAN_RUNNING'
    finally:_end_listener(stop,ready)
    port2=free_port();ready2=tmp_path/'ready2';stop2=tmp_path/'stop2'
    other=subprocess.Popen([sys.executable,'-I','-c',LISTENER,str(port2),str(ready2),str(stop2)])
    try:
        unrelated=int(_wait_file(ready2))
        verdict,detail=engine_client.dashboard_lineage(state,dict(pid=unrelated),port2);assert verdict is not True,detail
        assert engine_client.dashboard_lineage(state,{},port2)[0] is not True
        lay2=make_layout(tmp_path/'b',port=port2)
        h2=Health().same(lay2);h2.data['pid']=unrelated
        ctl2=controller(lay2,procs=Procs(),health=h2,lineage=engine_client.dashboard_lineage)
        write_state(lay2,phase='RUNNING',pid=host['pid'],create_time=host['create_time'])
        s=ctl2.refresh();assert s.kind=='EXTERNAL_SAME' and len(ctl2.lineage_log)==1 and 'DESKTOP LINEAGE' in ctl2.lineage_log[0]
    finally:
        _end_listener(stop2,ready2)
        try:other.wait(10)
        except subprocess.TimeoutExpired:other.kill()
