"""Everything the window does with the engine host: read engine_state.json, check that the recorded host
is alive (pid + create_time), GET /health, write stop_<token>.request and start the host detached.
Nothing here waits on a pipe, kills a process or touches any port other than the dashboard's /health."""
import http.client,json,os,secrets,subprocess,time
from dataclasses import dataclass
from pathlib import Path
from . import constants as C,logs

def new_token():return secrets.token_hex(16)

@dataclass
class Spawned:
    token:str
    log:Path
    proc:object  # Popen of the venv redirector: its pid is NOT the host pid (spike workaround 5)
    def exited(self):
        try:return self.proc.poll()
        except OSError:return None

class EngineClient:
    def __init__(self,layout,alive=None,health_get=None,popen=None,lineage=None):
        self.layout=layout
        self._alive=alive or C.engine.alive
        self._health_get=health_get
        self._popen=popen
        self._lineage=lineage or dashboard_lineage
    def lineage(self,state,health):
        """(verdict, detail) for a dead host whose dashboard answers SAME: verdict True only when the dashboard
        process descends from the recorded host (ORPHAN_RUNNING); False (not ours: e.g. 03_START after a stale
        state file) and None (undeterminable) both mean EXTERNAL_SAME. Never raises."""
        try:return self._lineage(state,health,self.layout.port)
        except Exception as exc:return None,'lineage error: '+type(exc).__name__
    def state(self,tries=5,pause=.02):
        """engine_state.json, or {} when absent. A file that exists but cannot be read is retried briefly:
        the host replaces it atomically and Windows refuses an open during the rename for a moment."""
        for attempt in range(tries):
            value=C.engine.read_state(self.layout.control)
            if value or not C.engine.state_path(self.layout.control).exists():return value
            if attempt<tries-1:time.sleep(pause)
        return value
    def alive(self,state):return bool(state) and self._alive(state)
    def stop_file(self,token):return C.engine.stop_path(token,self.layout.control)

    def health(self):
        """GET http://127.0.0.1:<port>/health (0.5 s); the parsed dict or None. No other request is made."""
        if self._health_get is not None:return self._health_get(self.layout.port)
        return get_health(self.layout.port)
    def health_kind(self,data):
        """SAME when version (BUILD_MANIFEST) and database identity match this installation, else FOREIGN."""
        if not isinstance(data,dict):return None
        same=data.get('version')==self.layout.version and self.layout.version is not None and data.get('database_identity')==self.layout.identity
        return C.EXTERNAL_SAME if same else C.EXTERNAL_FOREIGN

    def request_stop(self,token):
        """Create stop_<token>.request (idempotent). The only way the window stops a campaign."""
        path=self.stop_file(token);path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('x',encoding='utf-8') as stream:stream.write('stop requested by desktop window\n')
        except FileExistsError:pass
        return path

    def spawn(self,token,argv,env,cwd=None):
        """Start the host detached: hidden own console + new process group, stdin closed, stdout/stderr into
        a new log. The parent keeps no pipe; its copy of the log handle is closed right after the start."""
        if not C.engine.valid_token(token):raise ValueError('DESKTOP_TOKEN_INVALID')
        logs.rotate(self.layout.logs)
        stream=logs.new_log(self.layout.logs,logs.engine_name(token))
        path=Path(stream.name)
        try:
            popen=self._popen or subprocess.Popen
            proc=popen(list(argv),cwd=str(cwd or self.layout.root),env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,
                creationflags=C.DETACHED_FLAGS if os.name=='nt' else 0,close_fds=True)
        finally:stream.close()
        return Spawned(token,path,proc)

LINEAGE_DEPTH=16
CREATE_TOL_S=.01  # psutil create_time precision; the same tolerance desktop_engine.alive uses

def listener_pid(port):
    """pid of the process listening on 127.0.0.1:<port> (or 0.0.0.0) from the local TCP table, else None.
    Reads the table only: no connection is made to the port."""
    import psutil
    for conn in psutil.net_connections(kind='tcp'):
        if conn.status==psutil.CONN_LISTEN and conn.laddr and conn.laddr.port==port and conn.laddr.ip in (C.HOST,'0.0.0.0') and conn.pid:
            return conn.pid
    return None

def dashboard_lineage(state,health,port):
    """M1 (round 2): is the answering dashboard a descendant of the recorded (dead) host?
    Dashboard pid: /health 'pid', else the listener of the port. Then the ppid chain is walked with psutil
    (Windows keeps a dead parent's pid as ppid). Each living link must be older than its child, otherwise the
    ppid was reused and the walk stops. A link whose ppid is state['pid'] counts when it was created after the
    host's create_time and before any new owner of that pid (Windows may hand the dead host's pid out again).
    Returns (True|False|None, detail); None = undeterminable (AccessDenied, no pid, broken chain)."""
    import psutil
    host=state.get('pid');host_created=state.get('create_time')
    if type(host) is not int or host<=0 or type(host_created) not in (int,float):return None,'no host pid/create_time in engine_state.json'
    pid=health.get('pid') if isinstance(health,dict) else None
    source='/health pid'
    if type(pid) is not int or pid<=0:
        try:pid=listener_pid(port);source='listener of port '+str(port)
        except (psutil.Error,OSError) as exc:return None,'listener lookup failed: '+type(exc).__name__
        if pid is None:return None,'no dashboard pid'
    try:
        proc=psutil.Process(pid);created=proc.create_time()
        for _ in range(LINEAGE_DEPTH):
            parent=proc.ppid()
            if not parent or parent==proc.pid:return False,'chain from '+source+' '+str(pid)+' ends without the host'
            if parent==host:
                try:host_now=psutil.Process(host).create_time()
                except psutil.NoSuchProcess:host_now=None
                if created+CREATE_TOL_S<host_created:return False,'link '+str(proc.pid)+' is older than the host'
                # A reused host pid: a link created before the new owner existed can only be the old host's child.
                if host_now is not None and abs(host_now-host_created)>=CREATE_TOL_S and created>=host_now:
                    return False,'host pid '+str(host)+' was reused'
                return True,'dashboard '+str(pid)+' descends from host '+str(host)
            try:nxt=psutil.Process(parent);nxt_created=nxt.create_time()
            except psutil.NoSuchProcess:return None,'chain from '+source+' '+str(pid)+' broken at dead pid '+str(parent)
            if nxt_created>created+CREATE_TOL_S:return None,'ppid '+str(parent)+' was reused'
            proc,created=nxt,nxt_created
        return None,'chain deeper than '+str(LINEAGE_DEPTH)
    except psutil.NoSuchProcess:return None,'dashboard pid '+str(pid)+' is gone'
    except (psutil.AccessDenied,psutil.Error,OSError) as exc:return None,'access: '+type(exc).__name__

def get_health(port,timeout=C.HEALTH_TIMEOUT_S):
    if port in C.FORBIDDEN_PORTS:raise ValueError('DESKTOP_PORT_FORBIDDEN')
    conn=http.client.HTTPConnection(C.HOST,port,timeout=timeout)
    try:
        conn.request('GET','/health',headers={'Connection':'close'})
        response=conn.getresponse()
        if response.status!=200:return None
        data=json.loads(response.read(65536))
        return data if isinstance(data,dict) else None
    except (OSError,ValueError,http.client.HTTPException):return None
    finally:conn.close()
