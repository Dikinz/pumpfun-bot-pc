"""WP2 process integration without the market (spec 6.3). The engine is desktop/tests/fake_engine.py,
never the real host; servers use ports 8210-8219 only; every process started here is ended here.
  - start -> RUNNING; dropping the controller (window closed) leaves the engine running; a new controller
    attaches as OWN; stop -> STOPPED with code and audit line; kill of the fake -> ENGINE_LOST;
  - the real app/dashboard/server.py on a test port with a temporary DB -> EXTERNAL_SAME / EXTERNAL_FOREIGN;
  - Windows: CTRL_BREAK reaches a CREATE_NEW_PROCESS_GROUP child inside a CREATE_NO_WINDOW console."""
import gc,json,os,subprocess,sys,time
from pathlib import Path
import pytest
from fakes import forbid_live,ROOT,free_port,make_layout,wait_for,C
from desktop.controller import Controller,allowed
from desktop.engine_client import EngineClient,get_health
from desktop.paths import Layout
FAKE=Path(__file__).resolve().with_name('fake_engine.py')
APP=ROOT/'app'
WIN=os.name=='nt'

class Engines:
    """Starts the fake engine for a layout and guarantees cleanup of everything it started."""
    def __init__(self,monkeypatch,layout,**env):
        self.layout=layout;self.spawned=[]
        values=dict(CONTROL=str(layout.control),PORT=str(layout.port),DB=str(layout.db),VERSION=layout.version,START_DELAY='.2',STOP_DELAY='.6')
        values.update(env)
        for k,v in values.items():monkeypatch.setenv('PFDESK_FAKE_'+k,v)
    def cmd(self,token):return [sys.executable,'-I',str(FAKE),'run','--token',token]
    def controller(self):
        ctl=Controller(self.layout,engine_cmd=self.cmd,clock=time.monotonic)
        real=ctl.client.spawn
        def spawn(*a,**k):
            s=real(*a,**k);self.spawned.append(s);return s
        ctl.client.spawn=spawn;return ctl
    def cleanup(self):
        import psutil
        pids=set()
        for s in self.spawned:
            pids.add(s.proc.pid)
            try:pids.update(c.pid for c in psutil.Process(s.proc.pid).children(recursive=True))
            except psutil.Error:pass
        state=C.engine.read_state(self.layout.control)
        if state.get('pid') and C.engine.alive(state):pids.add(state['pid'])
        for pid in pids:
            try:
                p=psutil.Process(pid)
                if pid==state.get('pid') or any('fake_engine.py' in part for part in p.cmdline()) or pid in {s.proc.pid for s in self.spawned}:p.kill()
            except psutil.Error:pass
        for s in self.spawned:
            try:s.proc.wait(10)
            except Exception:pass

@pytest.fixture
def engines(tmp_path,monkeypatch):
    made=[]
    def make(**env):
        lay=make_layout(tmp_path/('e'+str(len(made))),port=free_port());e=Engines(monkeypatch,lay,**env);made.append(e);return e
    yield make
    for e in made:e.cleanup()

def poll(ctl,predicate,timeout=30,what='state'):return wait_for(lambda:(lambda s:s if predicate(s) else None)(ctl.refresh()),timeout,.1,what)

@pytest.mark.filterwarnings('ignore::ResourceWarning')
def test_start_detach_reattach_stop(engines):
    import psutil
    e=engines();lay=e.layout;ctl=e.controller()
    assert ctl.refresh().kind=='STOPPED'
    r=ctl.start();assert r['ok'],r
    s=poll(ctl,lambda s:s.start and s.start['done'],what='start done')
    assert s.start['ok'],s.start
    assert s.kind=='OWN_RUNNING' and s.terminal and ctl.client.health_kind(get_health(lay.port))=='EXTERNAL_SAME'
    state=s.state;assert state['token']==r['token'] and state['log_name']==Path(r['log']).name
    engine_pid=state['pid'];assert psutil.Process(engine_pid).is_running()
    # window closes: the controller object goes away, the campaign does not
    del ctl,s;gc.collect();time.sleep(1.)
    assert psutil.Process(engine_pid).is_running() and not list(lay.control.glob('stop_*'))
    assert C.engine.read_state(lay.control)['phase']=='RUNNING'
    # window reopened: attaches as OWN by engine_state.json
    ctl=e.controller();s=ctl.refresh()
    assert s.kind=='OWN_RUNNING' and allowed(s,'stop')==(True,None) and allowed(s,'start')[0] is False
    assert ctl.stop()['ok'] and [p.name for p in lay.control.glob('stop_*')]==['stop_'+r['token']+'.request']
    seen=set()
    def done(s):
        seen.update(k for k in ('accepted','panel_down') if s.stop[k]);return s.stop['done']
    s=poll(ctl,done,what='stop done')
    assert s.stop['ok'] and s.stop['exit_code']==0 and seen=={'accepted','panel_down'}
    assert s.stop['audit']=='SQLITE SHUTDOWN AUDIT: writers_finished=True integrity_before_after_ok=True'
    assert s.kind=='STOPPED' and s.state['phase']=='STOPPED' and get_health(lay.port) is None
    wait_for(lambda:not psutil.pid_exists(engine_pid) or not psutil.Process(engine_pid).is_running(),15,.1,'engine exit')
    log=(lay.logs/s.state['log_name']).read_text(encoding='utf-8')
    assert 'FAKE ENGINE: RUNNING' in log and 'FAKE ENGINE: STOPPED' in log

@pytest.mark.filterwarnings('ignore::ResourceWarning')
def test_killed_engine_is_engine_lost(engines):
    import psutil
    e=engines();lay=e.layout;ctl=e.controller()
    ctl.start();s=poll(ctl,lambda s:s.start['done'],what='start done');assert s.start['ok']
    psutil.Process(s.state['pid']).kill()
    s=poll(ctl,lambda s:s.kind=='ENGINE_LOST',timeout=15,what='ENGINE_LOST')
    assert allowed(s,'start')==(False,C.TEXT['lost_confirm']) and allowed(s,'results')==(True,None)
    assert ('engine_lost',C.TEXT['lost']) in ctl.drain_events() and not list(lay.control.glob('stop_*'))
    assert ctl.confirm_lost()['ok'] and allowed(ctl.refresh(),'start')==(True,None)

@pytest.mark.filterwarnings('ignore::ResourceWarning')
def test_failed_preflight_reports_error_and_log(engines):
    e=engines(FAIL='RuntimeError: CLEAN_RELEASE_MANIFEST_MISSING: extract the complete ZIP into a new folder');ctl=e.controller()
    ctl.start();s=poll(ctl,lambda s:s.start['done'],what='start failed')
    assert not s.start['ok'],s.start
    assert str(s.start['error_code']).startswith('RuntimeError: CLEAN_RELEASE_MANIFEST_MISSING'),s.start
    assert 'START BLOCKED' in s.start['log_tail'] and s.kind=='STOPPED' and s.failed

@pytest.mark.filterwarnings('ignore::ResourceWarning')
def test_second_host_start_is_already_running(engines):
    e=engines();ctl=e.controller()
    first=ctl.start();s=poll(ctl,lambda s:s.start['done'],what='start');assert s.start['ok']
    # a second host for the same folder (e.g. started by hand) leaves the running one untouched
    r=subprocess.run(e.cmd('ab'*16),capture_output=True,text=True,errors='replace',timeout=60)
    assert r.returncode==C.EXIT_ALREADY_RUNNING and 'ALREADY_RUNNING' in r.stdout
    assert ctl.refresh().state['token']==first['token'] and ctl.refresh().kind=='OWN_RUNNING'
    ctl.stop();poll(ctl,lambda s:s.stop['done'],what='stop')

def bot_python():
    for candidate in (sys.executable,os.environ.get('PFDESK_BOT_PYTHON'),str(ROOT/'.venv'/'Scripts'/'python.exe')):
        if candidate and Path(candidate).is_file():
            r=subprocess.run([candidate,'-I','-c','import dotenv,psutil'],capture_output=True,timeout=60)
            if r.returncode==0:return candidate
    return None

def test_real_dashboard_identity_same_and_foreign(tmp_path):
    python=bot_python()
    if python is None:pytest.skip('no interpreter with the bot dependencies (set PFDESK_BOT_PYTHON)')
    port=free_port();db=tmp_path/'test.db'
    env=dict(os.environ,DATABASE_PATH=str(db),DASHBOARD_PORT=str(port));env.pop('PYTHONPATH',None);env.pop('EXECUTION_MODE',None)
    with (tmp_path/'dashboard.log').open('w+') as log:
        p=subprocess.Popen([python,str(APP/'dashboard'/'server.py')],cwd=tmp_path,env=env,stdout=log,stderr=log)
        try:
            def ready():
                if p.poll() is not None:log.seek(0);pytest.fail('dashboard exited: '+log.read())
                return get_health(port)
            data=wait_for(ready,30,.2,'dashboard')
            guard=__import__('desktop.release_check',fromlist=['x']).release_guard()
            assert data['version']==guard.VERSION
            same=Layout(root=tmp_path/'inst',db=db,port=port,version=guard.VERSION)
            s=Controller(same).refresh()
            assert s.kind=='EXTERNAL_SAME' and s.terminal and allowed(s,'start')[0] is False and allowed(s,'stop')==(False,C.TEXT['external_same'])
            for other in (Layout(root=tmp_path/'inst',db=tmp_path/'other.db',port=port,version=guard.VERSION),
                    Layout(root=tmp_path/'inst',db=db,port=port,version='v0.0.0')):
                s=Controller(other).refresh()
                assert s.kind=='EXTERNAL_FOREIGN' and allowed(s,'start')[0] is False and allowed(s,'results')[0] is False and tray_red(s)
        finally:
            p.terminate();p.wait(15)
    assert get_health(port) is None

def tray_red(s):
    from desktop.controller import tray_state
    return tray_state(s)=='red'

HELPER=r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
work=Path(sys.argv[1]);ready=work/'ready'
CHILD="""
import signal,sys,time
from pathlib import Path
work=Path(sys.argv[1])
def stop(*a):
    (work/'got').write_text('SIGBREAK');raise SystemExit(0)
signal.signal(signal.SIGBREAK,stop)
(work/'ready').write_text('1')
for _ in range(600):time.sleep(.05)
raise SystemExit(5)
"""
child=subprocess.Popen([sys.executable,'-I','-c',CHILD,str(work)],creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
deadline=time.monotonic()+30
while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
import ctypes
console=ctypes.windll.kernel32.GetConsoleWindow()
os.kill(child.pid,signal.CTRL_BREAK_EVENT)
try:code=child.wait(20)
except subprocess.TimeoutExpired:child.kill();code='TIMEOUT'
(work/'result.json').write_text(json.dumps(dict(code=code,console_visible=bool(console and ctypes.windll.user32.IsWindowVisible(console)))))
raise SystemExit(0 if code==0 else 1)
'''
@pytest.mark.skipif(not WIN,reason='Windows console control events')
def test_ctrl_break_reaches_child_in_hidden_console(tmp_path):
    work=tmp_path/'w';work.mkdir();script=tmp_path/'helper.py';script.write_text(HELPER,encoding='utf-8')
    r=subprocess.run([sys.executable,'-I',str(script),str(work)],creationflags=C.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,capture_output=True,text=True,errors='replace',timeout=90)
    result=json.loads((work/'result.json').read_text())
    assert r.returncode==0 and result['code']==0,(r.stdout,r.stderr,result)
    assert (work/'got').read_text()=='SIGBREAK' and result['console_visible'] is False

def test_fake_engine_writes_like_the_real_host(tmp_path,monkeypatch):
    '''M2: final phases through the real desktop_engine.publish_durable with its own timeout, others one attempt.'''
    import importlib.util
    spec=importlib.util.spec_from_file_location('pfdesk_fake_engine',FAKE);fake=importlib.util.module_from_spec(spec);spec.loader.exec_module(fake)
    calls=[]
    monkeypatch.setattr(fake.de,'publish_durable',lambda path,payload,timeout_s=None:calls.append((Path(path),payload['phase'],timeout_s)))
    target=tmp_path/C.STATE_NAME
    assert fake.write_final(target,dict(phase='STOPPED')) and calls==[(target,'STOPPED',fake.de.FINAL_PUBLISH_TIMEOUT_S)]
    assert fake.de.FINAL_PUBLISH_TIMEOUT_S==3.0 and Path(fake.de.__file__).resolve()==(APP/'desktop_engine.py').resolve()
    assert fake.write_once(target,dict(phase='RUNNING')) and json.loads(target.read_text(encoding='utf-8'))==dict(phase='RUNNING')
    def denied(path,payload,timeout_s=None):raise PermissionError(5,'denied')
    monkeypatch.setattr(fake.de,'publish_durable',denied)
    assert fake.write_final(target,dict(phase='FAILED')) is False
