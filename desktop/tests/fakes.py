"""Shared fixtures and fakes for desktop tests (imported explicitly; there is no conftest.py here).

forbid_live is autouse in every module that imports it: no connection to 8188/49190 and no start of the
real bot (main.py, bootstrap.py start, desktop_engine.py, launcher.py, any .bat). It also points LOCALAPPDATA
at a per-test folder, so no test can write the real %LOCALAPPDATA%\\PumpfunPaperDesktop (a release has no .git,
so a dev-mode test that forgot to patch app_main.dev_checkout would fall back to the real profile). Test servers
use the first free port in 8210-8219."""
import json,os,socket,subprocess,sys,time
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from desktop import constants as C
from desktop.paths import Layout
from desktop.engine_client import EngineClient
from desktop.controller import Controller
FORBIDDEN_PORTS=(8188,49190)
TEST_PORTS=range(8210,8220)
TOKEN='0123456789abcdef'*2
OTHER='fedcba9876543210'*2
VERSION='v0.0.0-desktop-test'

class LiveAccessError(AssertionError):pass

def _check_address(address):
    try:port=int(address[1])
    except (TypeError,ValueError,IndexError):return
    if port in FORBIDDEN_PORTS:raise LiveAccessError('desktop test touched live port '+str(port))

def forbidden_argv(args):
    """Reason when argv would start the real bot/engine, else None."""
    parts=[args] if isinstance(args,(str,bytes,os.PathLike)) else list(args)
    text=[os.fsdecode(p) for p in parts]
    if len(text)==1 and isinstance(args,(str,bytes)):text=text[0].replace('"',' ').split()
    names=[Path(t).name.lower() for t in text]
    for i,name in enumerate(names):
        if name in ('main.py','desktop_engine.py','launcher.py') or name.endswith('.bat'):return name
        if name=='bootstrap.py' and (i+1>=len(names) or names[i+1]=='start'):return 'bootstrap.py start'
    return None

@pytest.fixture(autouse=True)
def forbid_live(monkeypatch,tmp_path_factory):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path_factory.mktemp('localappdata')))
    real_create=socket.create_connection;real_connect=socket.socket.connect;real_connect_ex=socket.socket.connect_ex;real_popen=subprocess.Popen
    def create_connection(address,*a,**k):_check_address(address);return real_create(address,*a,**k)
    def connect(self,address):_check_address(address);return real_connect(self,address)
    def connect_ex(self,address):_check_address(address);return real_connect_ex(self,address)
    class GuardedPopen(real_popen):
        def __init__(self,args,*a,**k):
            reason=forbidden_argv(args)
            if reason:raise LiveAccessError('desktop test tried to start '+reason)
            super().__init__(args,*a,**k)
    monkeypatch.setattr(socket,'create_connection',create_connection)
    monkeypatch.setattr(socket.socket,'connect',connect);monkeypatch.setattr(socket.socket,'connect_ex',connect_ex)
    monkeypatch.setattr(subprocess,'Popen',GuardedPopen)
    yield

def free_port():
    for port in TEST_PORTS:
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',port))
            except OSError:continue
            return port
    pytest.skip('no free test port in 8210-8219')

def make_layout(tmp_path,port=None,venv=True,**kw):
    root=tmp_path/'inst';(root/'app'/'data').mkdir(parents=True,exist_ok=True)
    if venv:
        python=root/'.venv'/'Scripts'/'python.exe';python.parent.mkdir(parents=True,exist_ok=True);python.write_bytes(b'')
    kw.setdefault('version',VERSION);kw.setdefault('profile_base',tmp_path/'profile')
    return Layout(root=root,port=port or 8215,**kw)

def write_state(layout,**fields):
    state=dict.fromkeys(C.FIELDS)
    state.update(schema=C.SCHEMA,version=layout.version,token=TOKEN,pid=4242,create_time=1000.5,phase='RUNNING',started_ts=1.,log_name=None)
    state.update(fields);layout.control.mkdir(parents=True,exist_ok=True)
    (layout.control/C.STATE_NAME).write_text(json.dumps(state),encoding='utf-8');return state

class Procs:
    """Fake process table pid -> create_time for EngineClient(alive=...)."""
    def __init__(self,**rows):self.rows={4242:1000.5};self.rows.update(rows)
    def __call__(self,state):return self.rows.get(state.get('pid'))==state.get('create_time')

class Health:
    """Fake GET /health: None (no answer) or a dict; counts calls; never opens a socket."""
    def __init__(self,data=None):self.data=data;self.calls=0
    def __call__(self,port):
        assert port not in FORBIDDEN_PORTS
        self.calls+=1;return None if self.data is None else dict(self.data)
    def same(self,layout):self.data=dict(version=layout.version,database_identity=layout.identity,pid=1);return self
    def foreign(self,layout,**kw):self.data=dict(dict(version=layout.version,database_identity=layout.identity,pid=1),**kw);return self

class Lineage:
    """Fake EngineClient lineage (M1 round 2): verdict True = the answering dashboard descends from the dead host
    (ORPHAN_RUNNING), False = it does not (EXTERNAL_SAME, e.g. 03_START), None = undeterminable (EXTERNAL_SAME)."""
    def __init__(self,verdict=True,detail='fake lineage'):self.verdict=verdict;self.detail=detail;self.calls=[]
    def __call__(self,state,health,port):
        assert port not in FORBIDDEN_PORTS
        self.calls.append((state.get('pid'),health.get('pid') if isinstance(health,dict) else None,port))
        if isinstance(self.verdict,BaseException):raise self.verdict
        return self.verdict,self.detail

class FakeProc:
    def __init__(self,code=None,pid=777):self.code=code;self.pid=pid;self.killed=False
    def poll(self):return self.code
    def kill(self):self.killed=True;raise AssertionError('the window must never kill a process')
    terminate=kill

class FakePopen:
    """Records every start instead of creating a process."""
    def __init__(self,code=None):self.calls=[];self.code=code;self.procs=[]
    def __call__(self,args,**kw):
        self.calls.append(dict(args=args,**kw));proc=FakeProc(self.code);self.procs.append(proc);return proc

class Clock:
    def __init__(self,t=1000.):self.t=t
    def __call__(self):return self.t
    def advance(self,s):self.t+=s

def controller(layout,procs=None,health=None,popen=None,clock=None,lineage=None,**kw):
    client=EngineClient(layout,alive=procs or Procs(),health_get=health or Health(),popen=popen or FakePopen(),lineage=lineage or Lineage())
    return Controller(layout,client=client,clock=clock or Clock(),popen=client._popen,startfile=kw.pop('startfile',None),**kw)

def wait_for(predicate,timeout=30.,step=.1,what='condition'):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(step)
    raise AssertionError('timeout waiting for '+what)
