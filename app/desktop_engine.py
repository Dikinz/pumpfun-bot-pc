"""Desktop engine host (DESKTOP_ENGINE_V1): one PAPER campaign under a file-only control protocol.

The window process imports the contract constants below as the single source of truth, so the
top-level imports stay stdlib-only and importing this module has no side effects (no chdir, no
sys.path change, no bot module). `desktop_engine.py run --token <32hex>` repeats bootstrap.py's
start checks, then runs launcher.start(stop_condition, open_browser=False). A stop is only the file
stop_<token>.request: the stop condition turns it into the launcher's own Ctrl+C path (graceful
stop, SQLite shutdown audit); a stop file already present after the start checks means STOPPED (code 0)
without starting the launcher. The window never kills engine processes."""
import argparse,os,re,sys,time
from pathlib import Path
APP=Path(__file__).resolve().parent
ROOT=APP.parent
SCHEMA='DESKTOP_ENGINE_V1'
CONTROL_DIR=APP/'data'/'desktop'
STATE_NAME='engine_state.json'
STOP_TEMPLATE='stop_{token}.request'
LOCK_NAME='engine_host.lock'
LOG_DIR_NAME='logs'
LOG_TEMPLATE='engine_{stamp}_{token8}.log'  # created by the window; stamp=time.strftime('%Y%m%d_%H%M%S')
PHASES=('PREFLIGHT','STARTING','RUNNING','STOP_REQUESTED','STOPPED','FAILED')
ACTIVE_PHASES=PHASES[:4]
TOKEN_RE='[0-9a-f]{32}'
STOP_RE='stop_('+TOKEN_RE+')\\.request'
FIELDS=('schema','version','token','pid','create_time','phase','started_ts','running_ts','stop_requested_ts','finished_ts','exit_code','error_code','log_name')
CLEAN_DB='data/clean_0762.db'
CLEAR_ENV=('PYTHONPATH','PYTHONHOME','DATABASE_PATH','DASHBOARD_PORT')  # bootstrap.main; parity test pins it
EXIT_FAILED=1;EXIT_USAGE=2;EXIT_ALREADY_RUNNING=3  # otherwise the exit code is launcher.start's code
ERROR_TEXT_MAX=200
FINAL_PUBLISH_TIMEOUT_S=3.0  # PREFLIGHT/STOPPED/FAILED: a reader holding engine_state.json open blocks os.replace on Windows
PUBLISH_RETRY_S=.05

def valid_token(token):return isinstance(token,str) and re.fullmatch(TOKEN_RE,token) is not None
def state_path(control=None):return Path(control or CONTROL_DIR)/STATE_NAME
def stop_path(token,control=None):
    if not valid_token(token):raise ValueError('DESKTOP_TOKEN_INVALID')
    return Path(control or CONTROL_DIR)/STOP_TEMPLATE.format(token=token)
def log_dir(control=None):return Path(control or CONTROL_DIR)/LOG_DIR_NAME
def log_name(token,ts=None):
    if not valid_token(token):raise ValueError('DESKTOP_TOKEN_INVALID')
    return LOG_TEMPLATE.format(stamp=time.strftime('%Y%m%d_%H%M%S',time.localtime(ts)),token8=token[:8])

def find_log(token,control=None):
    """Name of the newest engine log the window created for this token, or None."""
    try:names=sorted(p.name for p in log_dir(control).glob('engine_*_'+token[:8]+'.log') if p.is_file())
    except OSError:return None
    return names[-1] if names else None

def read_state(control=None):
    """Last published host state, or {} when absent, unreadable or of another schema."""
    import json
    try:value=json.loads(state_path(control).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
    return value if isinstance(value,dict) and value.get('schema')==SCHEMA else {}

def active(state):return state.get('phase') in ACTIVE_PHASES

def alive(state):
    """True only for the exact recorded host process: pid plus create_time rejects pid reuse.
    Unknown (AccessDenied) counts as alive, so nothing is started over a possibly living host."""
    pid=state.get('pid');created=state.get('create_time')
    if type(pid) is not int or pid<=0 or type(created) not in (int,float):return False
    import psutil
    try:return abs(psutil.Process(pid).create_time()-created)<.01
    except psutil.AccessDenied:return True
    except (psutil.Error,OSError,ValueError,OverflowError):return False

def own_create_time():
    try:
        import psutil
        return psutil.Process(os.getpid()).create_time()
    except Exception:return None

def error_code(exc):
    # RuntimeError texts are the fixed guard/launcher messages (no keys); other types keep only the name.
    name=type(exc).__name__
    return name+': '+str(exc)[:ERROR_TEXT_MAX] if type(exc) is RuntimeError else name

def say(*parts):
    if sys.stdout is not None:
        try:print('DESKTOP ENGINE:',*parts,flush=True)
        except (OSError,ValueError):pass

def warn(*parts):
    if sys.stderr is not None:
        try:print('DESKTOP ENGINE:',*parts,file=sys.stderr,flush=True)
        except (OSError,ValueError):pass

def publish_durable(path,payload,timeout_s=FINAL_PUBLISH_TIMEOUT_S):
    """state_io.atomic_json retried on OSError (PermissionError: WinError 5 while the window has the file open)
    every PUBLISH_RETRY_S until timeout_s has passed, then the last error is re-raised. state_io's own retry is
    ~35 ms, shorter than a window read. Serialization errors (ValueError/TypeError) are never retried."""
    if str(APP) not in sys.path:sys.path.insert(0,str(APP))  # state_io lives next to this file
    import state_io
    deadline=time.monotonic()+timeout_s
    while True:
        try:return state_io.atomic_json(path,payload)
        except OSError:
            if time.monotonic()>=deadline:raise
            time.sleep(PUBLISH_RETRY_S)

def host_lock(control=None):
    """Non-blocking exclusive lock for the host lifetime; None when another host holds it.
    The handle is not inherited by children, so it ends with this process."""
    stream=open(Path(control or CONTROL_DIR)/LOCK_NAME,'a+b')
    try:
        if os.name=='nt':
            import msvcrt
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close();return None
    return stream

def remove_stale_stops(token,control=None):
    """Delete stop files of earlier tokens (this host's own token is never stale)."""
    removed=[]
    for path in Path(control or CONTROL_DIR).glob('stop_*.request'):
        match=re.fullmatch(STOP_RE,path.name)
        if not match or match.group(1)==token:continue
        try:path.unlink();removed.append(path.name)
        except FileNotFoundError:pass
    return removed

def preflight():
    """bootstrap.main() for `start`, step by step (pinned by test_desktop_engine_v1015), plus PAPER_ONLY."""
    import release_guard
    release_guard.verify(ROOT)
    if Path(sys.prefix).resolve()!=(ROOT/'.venv').resolve():
        raise RuntimeError('CLEAN_ENVIRONMENT_MISMATCH: run 01_INSTALL.bat in this folder')
    for key in CLEAR_ENV:os.environ.pop(key,None)
    os.chdir(APP)
    import launcher
    launcher.setup_env()
    try:import config
    except RuntimeError as exc:
        if 'EXECUTION_MODE' in str(exc):raise RuntimeError('PAPER_ONLY: EXECUTION_MODE must remain paper') from None
        raise
    if release_guard.local_database(APP,config.DB)!=(APP/CLEAN_DB).resolve():
        raise RuntimeError('CLEAN_DATABASE_PROFILE_MISMATCH: configure settings in this new folder; do not copy the old .env')
    if config.EXECUTION_MODE!='paper':raise RuntimeError('PAPER_ONLY: EXECUTION_MODE must remain paper')
    return launcher,config

def run(token):
    if not valid_token(token):raise ValueError('DESKTOP_TOKEN_INVALID')
    if str(APP) not in sys.path:sys.path.insert(0,str(APP))  # -I leaves the script folder out
    control=Path(CONTROL_DIR);control.mkdir(parents=True,exist_ok=True)
    lock=host_lock(control)
    previous=read_state(control)
    if lock is None or (active(previous) and alive(previous)):
        if lock is not None:lock.close()
        say('ALREADY_RUNNING: a desktop engine of this folder is running (pid '+str(previous.get('pid'))+'); its files are left untouched')
        return EXIT_ALREADY_RUNNING
    try:
        import state_io,release_guard
        target=state_path(control);stop=stop_path(token,control)
        state=dict.fromkeys(FIELDS)
        state.update(schema=SCHEMA,version=release_guard.VERSION,token=token,pid=os.getpid(),create_time=own_create_time(),
            phase='PREFLIGHT',started_ts=time.time(),log_name=find_log(token,control))
        def publish(phase,**fields):
            state.update(fields,phase=phase)
            try:state_io.atomic_json(target,state);return True
            except (OSError,ValueError,TypeError) as exc:
                say('STATE WRITE FAILED:',phase,type(exc).__name__);return False
        def publish_final(phase,**fields):
            # PREFLIGHT/STOPPED/FAILED must not be lost to a window read: retried up to FINAL_PUBLISH_TIMEOUT_S.
            state.update(fields,phase=phase)
            try:publish_durable(target,state,FINAL_PUBLISH_TIMEOUT_S);return True
            except (OSError,ValueError,TypeError) as exc:
                warn('STATE WRITE FAILED:',phase,type(exc).__name__,'after',FINAL_PUBLISH_TIMEOUT_S,'s; exit code unchanged');return False
        removed=remove_stale_stops(token,control)
        if not publish_final('PREFLIGHT'):return EXIT_FAILED
        say('PREFLIGHT pid',os.getpid(),'token',token[:8],'removed stale stop files:',len(removed))
        pending=[]
        def stop_condition():
            # First call: the launcher has started dashboard, bot and monitor.
            if state['running_ts'] is None:
                state['running_ts']=time.time();pending.append('RUNNING');say('RUNNING')
            if stop.exists():
                if state['stop_requested_ts'] is None:
                    state['stop_requested_ts']=time.time();say('STOP_REQUESTED: graceful stop through the launcher')
                publish('STOP_REQUESTED');return True
            # A failed state write never stops the campaign; it is retried on the next call.
            if pending and publish('RUNNING'):pending.clear()
            return False
        try:
            launcher,config=preflight()
            if stop.exists():
                # Stop requested during PREFLIGHT (e.g. Windows logoff): never start the market. STOPPED with code 0,
                # stop_requested_ts set and running_ts None marks a stop before launch. The stop file stays, as after a
                # normal stop; the next host removes it as stale.
                now=time.time()
                publish_final('STOPPED',version=config.VERSION,stop_requested_ts=now,exit_code=0,finished_ts=now)
                say('STOP_REQUESTED before launch: the launcher was not started');say('STOPPED exit code',0)
                return 0
            publish('STARTING',version=config.VERSION);say('STARTING',config.VERSION)
            code=launcher.start(stop_condition=stop_condition,open_browser=False)
        except (Exception,KeyboardInterrupt) as exc:
            code=EXIT_FAILED;publish_final('FAILED',exit_code=code,error_code=error_code(exc),finished_ts=time.time())
            say('FAILED:',error_code(exc) if type(exc) is RuntimeError else type(exc).__name__+': '+str(exc)[:500])
            return code
        publish_final('STOPPED',exit_code=code,finished_ts=time.time());say('STOPPED exit code',code)
        return code
    finally:lock.close()

def main(argv=None):
    parser=argparse.ArgumentParser(prog='desktop_engine.py',description='PAPER engine host for the desktop window.')
    sub=parser.add_subparsers(dest='command',required=True)
    command=sub.add_parser('run',help='start one PAPER campaign; stop with stop_<token>.request')
    def token(value):
        if not valid_token(value):raise argparse.ArgumentTypeError('token must be 32 lowercase hex characters')
        return value
    command.add_argument('--token',required=True,type=token)
    args=parser.parse_args(argv)
    return run(args.token)

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        say('FAILED:',type(exc).__name__);raise SystemExit(EXIT_FAILED)
