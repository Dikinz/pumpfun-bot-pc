"""Stand-in for app/desktop_engine.py in process tests: same CLI (`run --token <32hex>`) and the same file
protocol (engine_state.json phases/fields, stop_<token>.request, ALREADY_RUNNING), but no bot, no market,
no launcher. /health is served on a test port (8210-8219 only) with the version and database identity the
real dashboard reports. Settings come from PFDESK_FAKE_* environment variables:
  CONTROL (required) control folder; PORT; DB; VERSION; START_DELAY; STOP_DELAY; FAIL (error_code -> FAILED);
  EXIT (exit code after a stop).
State writes follow the real host's policy (M2): PREFLIGHT/STOPPED/FAILED go through the real
desktop_engine.publish_durable (state_io.atomic_json retried every PUBLISH_RETRY_S up to FINAL_PUBLISH_TIMEOUT_S),
STARTING/RUNNING/STOP_REQUESTED are one state_io.atomic_json call whose failure is only reported (RUNNING is
retried on the next loop pass, like the host's stop_condition)."""
import argparse,hashlib,importlib.util,json,os,sys,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('desktop_engine',ROOT/'app'/'desktop_engine.py')
de=importlib.util.module_from_spec(spec);spec.loader.exec_module(de)
TEST_PORTS=range(8210,8220)

def env(name,default=None):return os.environ.get('PFDESK_FAKE_'+name,default)
def identity(db):return hashlib.sha256(os.path.normcase(str(Path(db).resolve())).encode()).hexdigest()[:20]
def say(*parts):print('FAKE ENGINE:',*parts,flush=True)

def write_once(target,state):
    """One attempt, like the host's publish(): the real state_io.atomic_json; failure is reported, not raised."""
    if str(de.APP) not in sys.path:sys.path.insert(0,str(de.APP))
    import state_io
    try:state_io.atomic_json(target,state);return True
    except (OSError,ValueError,TypeError) as exc:say('STATE WRITE FAILED:',state.get('phase'),type(exc).__name__);return False

def write_final(target,state):
    """Like the host's publish_final(): the real publish_durable with the real timeout."""
    try:de.publish_durable(target,state,timeout_s=de.FINAL_PUBLISH_TIMEOUT_S);return True
    except (OSError,ValueError,TypeError) as exc:
        print('FAKE ENGINE: STATE WRITE FAILED:',state.get('phase'),type(exc).__name__,'after',de.FINAL_PUBLISH_TIMEOUT_S,'s',file=sys.stderr,flush=True)
        return False

def serve(port,payload):
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            body=json.dumps(payload).encode() if self.path=='/health' else b'{}'
            self.send_response(200 if self.path=='/health' else 404);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',port),H);server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start();return server

def run(token):
    control=Path(env('CONTROL'));control.mkdir(parents=True,exist_ok=True)
    port=int(env('PORT','8219'))
    if port not in TEST_PORTS:say('refusing non-test port',port);return de.EXIT_USAGE
    previous=de.read_state(control)
    if de.active(previous) and de.alive(previous):say('ALREADY_RUNNING');return de.EXIT_ALREADY_RUNNING
    de.remove_stale_stops(token,control)
    import psutil
    target=de.state_path(control);stop=de.stop_path(token,control)
    state=dict.fromkeys(de.FIELDS)
    state.update(schema=de.SCHEMA,version=env('VERSION','v-fake'),token=token,pid=os.getpid(),create_time=psutil.Process().create_time(),
        phase='PREFLIGHT',started_ts=time.time(),log_name=de.find_log(token,control))
    def publish(phase,**fields):
        state.update(fields,phase=phase);ok=write_once(target,state);say(phase);return ok
    def publish_final(phase,**fields):
        state.update(fields,phase=phase);ok=write_final(target,state);say(phase);return ok
    if not publish_final('PREFLIGHT'):return de.EXIT_FAILED
    time.sleep(float(env('START_DELAY','.2')))
    if env('FAIL'):
        say('START BLOCKED:',env('FAIL'));publish_final('FAILED',exit_code=de.EXIT_FAILED,error_code=env('FAIL'),finished_ts=time.time());return de.EXIT_FAILED
    publish('STARTING')
    try:server=serve(port,dict(version=env('VERSION','v-fake'),database_identity=identity(env('DB','fake.db')),pid=os.getpid(),launch_id=None))
    except OSError as exc:
        publish_final('FAILED',exit_code=de.EXIT_FAILED,error_code='OSError',finished_ts=time.time());say('port',port,type(exc).__name__);return de.EXIT_FAILED
    pending=not publish('RUNNING',running_ts=time.time())
    while not stop.exists():
        if pending:pending=not publish('RUNNING')
        time.sleep(.05)
    publish('STOP_REQUESTED',stop_requested_ts=time.time())
    delay=float(env('STOP_DELAY','.5'));time.sleep(delay/2)
    server.shutdown();server.server_close();say('dashboard stopped')
    time.sleep(delay/2)
    code=int(env('EXIT','0'))
    print('SQLITE SHUTDOWN AUDIT: writers_finished=True integrity_before_after_ok=True',flush=True)  # as launcher.py prints it
    publish_final('STOPPED',exit_code=code,finished_ts=time.time());return code

def main(argv=None):
    parser=argparse.ArgumentParser(prog='fake_engine.py')
    sub=parser.add_subparsers(dest='command',required=True)
    def token(value):
        if not de.valid_token(value):raise argparse.ArgumentTypeError('token must be 32 lowercase hex characters')
        return value
    sub.add_parser('run').add_argument('--token',required=True,type=token)
    return run(parser.parse_args(argv).token)

if __name__=='__main__':raise SystemExit(main())
