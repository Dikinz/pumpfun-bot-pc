"""WP1 desktop engine host: contract, phases, stop protocol, preflight parity. Never starts the market:
launcher.start is always a fake here and real-process runs use a copy without BUILD_MANIFEST."""
import ast,inspect,json,os,shutil,subprocess,sys,time,types
from pathlib import Path
import pytest
import config,launcher,state_io,release_guard
import desktop_engine as de
from test_primary_integrity import fresh  # launcher.start harness database
APP=Path(de.__file__).resolve().parent
TOKEN='0123456789abcdef'*2
OTHER='fedcba9876543210'*2

@pytest.fixture
def control(tmp_path,monkeypatch):
    folder=tmp_path/'desktop';monkeypatch.setattr(de,'CONTROL_DIR',folder);return folder

@pytest.fixture
def phases(monkeypatch):
    seen=[];original=state_io.atomic_json
    def record(path,data):seen.append(dict(data));return original(path,data)
    monkeypatch.setattr(state_io,'atomic_json',record);return seen

def own_state(**fields):
    import psutil
    state=dict.fromkeys(de.FIELDS);state.update(schema=de.SCHEMA,version=config.VERSION,token=OTHER,pid=os.getpid(),
        create_time=psutil.Process(os.getpid()).create_time(),phase='RUNNING',started_ts=1.,running_ts=2.);state.update(fields);return state

def fake_start(monkeypatch,script):
    calls=[]
    def start(stop_condition=None,open_browser=True):
        calls.append(dict(open_browser=open_browser));return script(stop_condition)
    monkeypatch.setattr(launcher,'start',start);monkeypatch.setattr(de,'preflight',lambda:(launcher,config));return calls

def launcher_loop(stop_condition,steps):
    """The launcher's own contract: True from stop_condition -> KeyboardInterrupt -> graceful stop, code 0."""
    try:
        for step in steps:
            if step:step()
            if stop_condition():raise KeyboardInterrupt
    except KeyboardInterrupt:return 0
    return 1

def read(control):return json.loads((control/de.STATE_NAME).read_text(encoding='utf-8'))

def test_contract_constants_are_the_window_source_of_truth():
    assert de.SCHEMA=='DESKTOP_ENGINE_V1' and de.STATE_NAME=='engine_state.json' and de.STOP_TEMPLATE=='stop_{token}.request'
    assert de.CONTROL_DIR==APP/'data'/'desktop' and de.TOKEN_RE=='[0-9a-f]{32}'
    assert de.PHASES==('PREFLIGHT','STARTING','RUNNING','STOP_REQUESTED','STOPPED','FAILED')
    assert de.ACTIVE_PHASES==('PREFLIGHT','STARTING','RUNNING','STOP_REQUESTED')
    assert de.FIELDS==('schema','version','token','pid','create_time','phase','started_ts','running_ts','stop_requested_ts','finished_ts','exit_code','error_code','log_name')
    assert de.stop_path(TOKEN,Path('x'))==Path('x')/('stop_'+TOKEN+'.request')
    for bad in (TOKEN.upper(),TOKEN[:31],TOKEN+'0','../'+TOKEN[3:],None):
        assert not de.valid_token(bad)
        with pytest.raises(ValueError):de.stop_path(bad)
    name=de.log_name(TOKEN,time.mktime((2026,11,2,10,11,12,0,0,-1)))
    assert name=='engine_20261102_101112_01234567.log'

def test_top_level_imports_are_stdlib_only():
    tree=ast.parse((APP/'desktop_engine.py').read_text(encoding='utf-8'))
    names=[a.name for n in tree.body if isinstance(n,ast.Import) for a in n.names]+[n.module for n in tree.body if isinstance(n,ast.ImportFrom)]
    assert names and all(n.split('.')[0] in sys.stdlib_module_names for n in names),names

def test_import_has_no_side_effects_in_a_fresh_interpreter(tmp_path):
    code=('import os,sys,json;sys.path.insert(0,sys.argv[1]);before=set(sys.modules);cwd=os.getcwd();path=list(sys.path)\n'
          'import desktop_engine\n'
          'new=sorted(m for m in set(sys.modules)-before if m.split(".")[0] not in sys.stdlib_module_names)\n'
          'print(json.dumps(dict(new=new,cwd=os.getcwd()==cwd,path=sys.path==path)))')
    out=subprocess.run([sys.executable,'-I','-c',code,str(APP)],cwd=tmp_path,capture_output=True,text=True,timeout=60)
    assert out.returncode==0,out.stderr
    assert json.loads(out.stdout)==dict(new=['desktop_engine'],cwd=True,path=True)

def test_launcher_signature_and_single_guarded_browser_open():
    params=inspect.signature(launcher.start).parameters
    assert list(params)==['stop_condition','open_browser'] and params['stop_condition'].default is None and params['open_browser'].default is True
    tree=ast.parse((APP/'launcher.py').read_text(encoding='utf-8'))
    parents={c:p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    opens=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and ast.unparse(n.func)=='webbrowser.open']
    assert len(opens)==1
    node=opens[0]
    while not isinstance(node,ast.If):node=parents[node]
    assert ast.unparse(node.test)=='open_browser' and opens[0] in list(ast.walk(ast.Module(body=node.body,type_ignores=[])))
    # 03_START (bootstrap -> launcher.main -> start()) keeps the default browser.
    main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    starts=[n for n in ast.walk(main) if isinstance(n,ast.Call) and ast.unparse(n.func)=='start']
    assert len(starts)==1 and not starts[0].args and not starts[0].keywords

def test_paper_fault_test_still_uses_default_browser():
    tree=ast.parse((APP/'paper_fault_test_v101.py').read_text(encoding='utf-8'))
    starts=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and ast.unparse(n.func)=='launcher.start']
    assert len(starts)==1 and [k.arg for k in starts[0].keywords]==['stop_condition'] and not starts[0].args
    engine=ast.parse((APP/'desktop_engine.py').read_text(encoding='utf-8'))
    calls=[n for n in ast.walk(engine) if isinstance(n,ast.Call) and ast.unparse(n.func)=='launcher.start']
    assert len(calls)==1 and {k.arg:ast.unparse(k.value) for k in calls[0].keywords}=={'stop_condition':'stop_condition','open_browser':'False'}

@pytest.mark.parametrize('open_browser',[None,True,False])
def test_launcher_start_opens_browser_only_when_asked(fresh,monkeypatch,open_browser):
    # Same fakes as test_launcher_monitor_failure_stops_all_children: no port bind, no real child.
    monkeypatch.setattr(release_guard,'local_database',lambda *a:None);monkeypatch.setattr(release_guard,'available',lambda *a:None)
    class Child:
        pid=99999999;stdin=None
        def __init__(self,code=None):self.returncode=code
        def poll(self):return self.returncode
        def send_signal(self,_):self.returncode=0
        def terminate(self):self.returncode=-15
        def kill(self):self.returncode=-9
        def wait(self,timeout=None):return self.returncode
    children=iter([Child(),Child(),Child(7)]);opened=[]
    monkeypatch.setattr(launcher,'call',lambda *a:0);monkeypatch.setattr(launcher,'setup_env',lambda:None)
    monkeypatch.setattr(launcher,'wait_dashboard',lambda *a,**k:None)
    monkeypatch.setattr(launcher.subprocess,'Popen',lambda *a,**k:next(children))
    monkeypatch.setattr(launcher.webbrowser,'open',lambda *a:opened.append(a) or True)
    code=launcher.start() if open_browser is None else launcher.start(open_browser=open_browser)
    assert code==1
    assert opened==([] if open_browser is False else [('http://127.0.0.1:'+str(config.PORT),)])

def test_run_publishes_phases_and_obeys_only_its_own_stop_file(control,phases,monkeypatch):
    log=de.log_name(TOKEN);(control/'logs').mkdir(parents=True);(control/'logs'/log).write_text('')
    seen={}
    def script(stop_condition):
        def foreign():(control/de.STOP_TEMPLATE.format(token=OTHER)).write_text('')
        def check_running():seen['running']=read(control)
        def own():de.stop_path(TOKEN).write_text('')
        return launcher_loop(stop_condition,[None,check_running,foreign,None,own])
    calls=fake_start(monkeypatch,script)
    assert de.run(TOKEN)==0
    assert calls==[dict(open_browser=False)]
    assert [p['phase'] for p in phases]==['PREFLIGHT','STARTING','RUNNING','STOP_REQUESTED','STOPPED']
    assert seen['running']['phase']=='RUNNING' and seen['running']['running_ts'] is not None
    final=read(control)
    assert list(final)==list(de.FIELDS) and final['schema']==de.SCHEMA and final['version']==config.VERSION
    assert final['token']==TOKEN and final['pid']==os.getpid() and final['create_time']==de.own_create_time()
    assert final['phase']=='STOPPED' and final['exit_code']==0 and final['error_code'] is None
    assert final['started_ts']<=final['running_ts']<=final['stop_requested_ts']<=final['finished_ts']
    assert final['log_name']==log and de.alive(final)

@pytest.mark.parametrize('code',[0,1,5])
def test_exit_code_is_the_launcher_code(control,phases,monkeypatch,code):
    fake_start(monkeypatch,lambda stop_condition:(stop_condition(),code)[1])
    assert de.run(TOKEN)==code
    final=read(control);assert final['phase']=='STOPPED' and final['exit_code']==code and final['log_name'] is None
    assert [p['phase'] for p in phases]==['PREFLIGHT','STARTING','RUNNING','STOPPED']

def test_launcher_failure_before_loop_is_stopped_with_its_code(control,phases,monkeypatch):
    fake_start(monkeypatch,lambda stop_condition:1)
    assert de.run(TOKEN)==1 and read(control)['phase']=='STOPPED' and read(control)['running_ts'] is None

def test_state_write_failure_never_stops_the_campaign(control,phases,monkeypatch):
    original=state_io.atomic_json;failures=[]
    def flaky(path,data):
        if data['phase']=='RUNNING' and not failures:failures.append(1);raise PermissionError(13,'locked by reader')
        return original(path,data)
    monkeypatch.setattr(state_io,'atomic_json',flaky)
    results=[]
    def script(stop_condition):
        results.append(stop_condition());results.append(read(control)['phase'])
        results.append(stop_condition());results.append(read(control)['phase']);return 0
    fake_start(monkeypatch,script)
    assert de.run(TOKEN)==0 and results==[False,'STARTING',False,'RUNNING'] and failures==[1]

def hold_open(path,release):
    """A window-like reader: plain open() for reading. On Windows os.replace onto the open file fails
    (WinError 5) until the handle is closed; `release` is the hold time in seconds or an Event."""
    import threading
    opened=threading.Event()
    def reader():
        with open(path,'rb'):
            opened.set()
            if isinstance(release,threading.Event):release.wait(30)
            else:time.sleep(release)
    thread=threading.Thread(target=reader,daemon=True);thread.start()
    assert opened.wait(10);return thread

def test_durable_constants_and_signature():
    assert de.FINAL_PUBLISH_TIMEOUT_S==3.0 and 0<de.PUBLISH_RETRY_S<=.1
    params=inspect.signature(de.publish_durable).parameters
    assert list(params)==['path','payload','timeout_s'] and params['timeout_s'].default==de.FINAL_PUBLISH_TIMEOUT_S

def test_reader_holding_the_state_file_loses_neither_preflight_nor_stopped(control,monkeypatch):
    control.mkdir();(control/de.STATE_NAME).write_text(json.dumps(own_state(phase='STOPPED')))
    original=state_io.atomic_json;attempts=[];readers={}
    def write(path,data):
        attempts.append(data['phase'])
        if data['phase'] in ('PREFLIGHT','STOPPED') and data['phase'] not in readers:readers[data['phase']]=hold_open(path,.7)
        return original(path,data)
    monkeypatch.setattr(state_io,'atomic_json',write)
    seen={}
    fake_start(monkeypatch,lambda stop_condition:(stop_condition(),5)[1])
    monkeypatch.setattr(de,'preflight',lambda:(seen.setdefault('preflight',read(control)),(launcher,config))[1])
    started=time.monotonic()
    assert de.run(TOKEN)==5
    elapsed=time.monotonic()-started
    for thread in readers.values():thread.join(10)
    assert sorted(readers)==['PREFLIGHT','STOPPED']
    assert seen['preflight']['phase']=='PREFLIGHT' and seen['preflight']['token']==TOKEN
    final=read(control);assert final['phase']=='STOPPED' and final['exit_code']==5 and final['token']==TOKEN
    if os.name=='nt':
        # The reader really blocked both writes, longer than state_io's own ~35 ms retry.
        assert attempts.count('PREFLIGHT')>1 and attempts.count('STOPPED')>1 and elapsed>=1.2,(attempts,elapsed)

def test_publish_durable_gives_up_after_the_timeout(tmp_path,monkeypatch):
    path=tmp_path/de.STATE_NAME;path.write_text('{"old": 1}');calls=[]
    def blocked(p,data):calls.append(1);raise PermissionError(13,'held by a reader')
    monkeypatch.setattr(state_io,'atomic_json',blocked)
    started=time.monotonic()
    with pytest.raises(PermissionError):de.publish_durable(path,dict(phase='STOPPED'),timeout_s=.4)
    elapsed=time.monotonic()-started
    assert .4<=elapsed<2 and len(calls)>2 and path.read_text()=='{"old": 1}'

@pytest.mark.skipif(os.name!='nt',reason='an open reader blocks os.replace only on Windows')
def test_publish_durable_real_reader_held_forever_does_not_hang(tmp_path):
    import threading
    path=tmp_path/de.STATE_NAME;path.write_text('{"old": 1}');release=threading.Event()
    thread=hold_open(path,release)
    try:
        started=time.monotonic()
        with pytest.raises(OSError):de.publish_durable(path,dict(phase='STOPPED'),timeout_s=.5)
        elapsed=time.monotonic()-started
    finally:release.set();thread.join(10)
    assert .5<=elapsed<2.5 and path.read_text()=='{"old": 1}'
    assert not [p for p in tmp_path.iterdir() if p.suffix=='.tmp']
    de.publish_durable(path,dict(phase='STOPPED'),timeout_s=.5);assert json.loads(path.read_text())==dict(phase='STOPPED')

def test_publish_durable_never_retries_serialization_errors(tmp_path,monkeypatch):
    original=state_io.atomic_json;calls=[]
    monkeypatch.setattr(state_io,'atomic_json',lambda p,d:(calls.append(1),original(p,d))[1])
    with pytest.raises(ValueError):de.publish_durable(tmp_path/de.STATE_NAME,dict(x=float('nan')),timeout_s=5)
    assert calls==[1] and not (tmp_path/de.STATE_NAME).exists()

def blocked_phase(monkeypatch,phase):
    original=state_io.atomic_json;blocked=[]
    def write(path,data):
        if data['phase']==phase:blocked.append(1);raise PermissionError(13,'held by a reader')
        return original(path,data)
    monkeypatch.setattr(state_io,'atomic_json',write);monkeypatch.setattr(de,'FINAL_PUBLISH_TIMEOUT_S',.3)
    return blocked

@pytest.mark.parametrize('code',[0,5])
def test_lost_stopped_write_keeps_the_launcher_exit_code(control,monkeypatch,capsys,code):
    blocked=blocked_phase(monkeypatch,'STOPPED')
    fake_start(monkeypatch,lambda stop_condition:(stop_condition(),code)[1])
    started=time.monotonic()
    assert de.run(TOKEN)==code and time.monotonic()-started<3 and len(blocked)>1
    assert read(control)['phase']=='RUNNING'
    assert 'DESKTOP ENGINE: STATE WRITE FAILED: STOPPED PermissionError' in capsys.readouterr().err

def test_lost_failed_write_keeps_exit_failed(control,monkeypatch,capsys):
    blocked=blocked_phase(monkeypatch,'FAILED')
    def script(stop_condition):raise RuntimeError('PORT_IN_USE: 8188; stop the previous bot in its own window, then start this folder')
    fake_start(monkeypatch,script)
    assert de.run(TOKEN)==de.EXIT_FAILED and len(blocked)>1 and read(control)['phase']=='STARTING'
    assert 'STATE WRITE FAILED: FAILED PermissionError' in capsys.readouterr().err

def test_lost_preflight_write_aborts_before_launch(control,monkeypatch,capsys):
    blocked=blocked_phase(monkeypatch,'PREFLIGHT')
    monkeypatch.setattr(de,'preflight',lambda:pytest.fail('preflight ran without a published PREFLIGHT'))
    assert de.run(TOKEN)==de.EXIT_FAILED and len(blocked)>1 and not (control/de.STATE_NAME).exists()
    assert 'STATE WRITE FAILED: PREFLIGHT PermissionError' in capsys.readouterr().err
    again=de.host_lock(control);assert again is not None;again.close()

def test_stale_stop_files_removed_but_not_foreign_names(control,phases,monkeypatch):
    control.mkdir()
    for name in ('stop_'+OTHER+'.request','stop_'+TOKEN+'.request','stop_notatoken.request','keep.txt'):(control/name).write_text('')
    calls=fake_start(monkeypatch,lambda stop_condition:launcher_loop(stop_condition,[None,None]))
    # The own-token request is not stale: the campaign stops before launch (MINOR-4).
    assert de.run(TOKEN)==0 and read(control)['phase']=='STOPPED' and read(control)['stop_requested_ts'] is not None and calls==[]
    assert sorted(p.name for p in control.iterdir() if p.name!=de.STATE_NAME and p.name!=de.LOCK_NAME)==['keep.txt','stop_'+TOKEN+'.request','stop_notatoken.request']

def test_stop_requested_during_preflight_never_launches(control,phases,monkeypatch):
    # MINOR-4: Windows logoff writes the stop file while the host is in PREFLIGHT.
    calls=fake_start(monkeypatch,lambda stop_condition:pytest.fail('launcher started after a stop request'))
    def preflight():
        assert read(control)['phase']=='PREFLIGHT';de.stop_path(TOKEN).write_text('');return launcher,config
    monkeypatch.setattr(de,'preflight',preflight)
    assert de.run(TOKEN)==0 and calls==[]
    assert [p['phase'] for p in phases]==['PREFLIGHT','STOPPED']
    final=read(control)
    assert list(final)==list(de.FIELDS) and final['phase']=='STOPPED' and final['exit_code']==0 and final['error_code'] is None
    assert final['version']==config.VERSION and final['token']==TOKEN and final['running_ts'] is None
    assert final['started_ts']<=final['stop_requested_ts']<=final['finished_ts']
    # The stop file is kept as after a normal stop (the next host removes it as stale); the host lock is released.
    assert de.stop_path(TOKEN).exists()
    again=de.host_lock(control);assert again is not None;again.close()

def test_no_stop_file_after_preflight_launches_unchanged(control,phases,monkeypatch):
    calls=fake_start(monkeypatch,lambda stop_condition:(stop_condition(),0)[1])
    monkeypatch.setattr(de,'preflight',lambda:(launcher,config))
    assert de.run(TOKEN)==0 and calls==[dict(open_browser=False)]
    assert [p['phase'] for p in phases]==['PREFLIGHT','STARTING','RUNNING','STOPPED']
    final=read(control);assert final['stop_requested_ts'] is None and final['running_ts'] is not None
    again=de.host_lock(control);assert again is not None;again.close()

def test_already_running_leaves_the_living_host_untouched(control,monkeypatch):
    control.mkdir();state=own_state();(control/de.STATE_NAME).write_text(json.dumps(state))
    (control/('stop_'+OTHER+'.request')).write_text('');before=(control/de.STATE_NAME).read_bytes()
    monkeypatch.setattr(de,'preflight',lambda:pytest.fail('preflight ran over a living host'))
    assert de.alive(state) and de.run(TOKEN)==de.EXIT_ALREADY_RUNNING
    assert (control/de.STATE_NAME).read_bytes()==before and (control/('stop_'+OTHER+'.request')).exists()

def test_held_host_lock_is_already_running(control,monkeypatch):
    control.mkdir();held=de.host_lock(control)
    try:
        assert held is not None and de.host_lock(control) is None
        monkeypatch.setattr(de,'preflight',lambda:pytest.fail('second host started'))
        assert de.run(TOKEN)==de.EXIT_ALREADY_RUNNING and not (control/de.STATE_NAME).exists()
    finally:held.close()
    again=de.host_lock(control);assert again is not None;again.close()

@pytest.mark.parametrize('phase,delta',[('RUNNING',5.),('STOPPED',0.),('FAILED',0.)])
def test_dead_or_finished_previous_host_does_not_block(control,phases,monkeypatch,phase,delta):
    control.mkdir();state=own_state(phase=phase);state['create_time']+=delta
    (control/de.STATE_NAME).write_text(json.dumps(state))
    fake_start(monkeypatch,lambda stop_condition:0)
    assert de.run(TOKEN)==0 and read(control)['token']==TOKEN

def test_alive_rejects_reuse_and_garbage():
    state=own_state()
    assert de.alive(state) and not de.alive(dict(state,create_time=state['create_time']+1))
    for bad in (dict(state,pid=None),dict(state,pid=True),dict(state,pid=-1),dict(state,create_time='x'),dict(state,pid=2**40),{}):
        assert not de.alive(bad)
    assert de.active(dict(phase='STOP_REQUESTED')) and not de.active(dict(phase='STOPPED')) and not de.active({})

@pytest.mark.parametrize('exc,expected',[
    (RuntimeError('PORT_IN_USE: 8188; stop the previous bot in its own window, then start this folder'),'RuntimeError: PORT_IN_USE: 8188; stop the previous bot in its own window, then start this folder'),
    (RuntimeError('X'*500),'RuntimeError: '+'X'*200),
    (ValueError('text of other exception types is never published'),'ValueError'),
    (KeyboardInterrupt(),'KeyboardInterrupt')])
def test_launcher_exception_is_failed(control,phases,monkeypatch,exc,expected):
    def script(stop_condition):raise exc
    fake_start(monkeypatch,script)
    assert de.run(TOKEN)==de.EXIT_FAILED
    final=read(control);assert final['phase']=='FAILED' and final['error_code']==expected and final['exit_code']==1 and final['finished_ts']
    assert [p['phase'] for p in phases]==['PREFLIGHT','STARTING','FAILED']

@pytest.fixture
def guarded(monkeypatch):
    """Real preflight with harmless guards: no manifest walk, no chdir, no .env write."""
    events=[]
    monkeypatch.setattr(release_guard,'verify',lambda root:events.append('verify'))
    monkeypatch.setattr(release_guard,'local_database',lambda app,db:(Path(app)/de.CLEAN_DB).resolve())
    monkeypatch.setattr(sys,'prefix',str(de.ROOT/'.venv'));monkeypatch.setattr(os,'chdir',lambda path:events.append('chdir'))
    monkeypatch.setattr(launcher,'setup_env',lambda:events.append('setup_env'))
    return events

def test_preflight_error_is_failed_without_launch(control,phases,monkeypatch,guarded):
    monkeypatch.setattr(sys,'prefix',str(de.ROOT/'other_env'))
    monkeypatch.setattr(launcher,'start',lambda **kw:pytest.fail('market launched after failed preflight'))
    assert de.run(TOKEN)==1
    final=read(control);assert final['phase']=='FAILED' and final['error_code']=='RuntimeError: CLEAN_ENVIRONMENT_MISMATCH: run 01_INSTALL.bat in this folder'
    assert [p['phase'] for p in phases]==['PREFLIGHT','FAILED'] and final['version']==release_guard.VERSION

def test_non_paper_mode_is_paper_only(control,phases,monkeypatch,guarded):
    monkeypatch.setattr(config,'EXECUTION_MODE','live')
    monkeypatch.setattr(launcher,'start',lambda **kw:pytest.fail('non-paper launch'))
    assert de.run(TOKEN)==1 and read(control)['error_code']=='RuntimeError: PAPER_ONLY: EXECUTION_MODE must remain paper'

def test_config_import_refusal_is_paper_only(monkeypatch,guarded):
    import dotenv
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *a,**k:False)
    monkeypatch.delitem(sys.modules,'config');monkeypatch.setenv('EXECUTION_MODE','live')
    with pytest.raises(RuntimeError,match='^PAPER_ONLY: EXECUTION_MODE must remain paper$'):de.preflight()
    assert 'config' not in sys.modules

@pytest.mark.parametrize('scenario',['ok','verify','prefix','database'])
def test_preflight_parity_with_bootstrap_start(monkeypatch,scenario):
    import bootstrap
    probe=('PYTHONPATH','PYTHONHOME','DATABASE_PATH','DASHBOARD_PORT','EXECUTION_MODE','PFDESK_PARITY_PROBE')
    def harness(verify_attr,local_attr):
        events=[]
        for key in probe:monkeypatch.setenv(key,'paper' if key=='EXECUTION_MODE' else 'stale')
        def verify(root):
            events.append(('verify',Path(root).resolve()))
            if scenario=='verify':raise RuntimeError('CLEAN_RELEASE_FILE_CHANGED: app/x.py; extract a fresh full ZIP')
        def local(app,db):
            events.append(('local_database',Path(app).resolve(),db))
            return (Path(app)/('data/other.db' if scenario=='database' else 'data/clean_0762.db')).resolve()
        monkeypatch.setattr(*verify_attr,verify);monkeypatch.setattr(*local_attr,local)
        monkeypatch.setattr(os,'chdir',lambda path:events.append(('chdir',Path(path).resolve())))
        monkeypatch.setattr(launcher,'setup_env',lambda:events.append(('setup_env',tuple(k for k in probe if k in os.environ))))
        return events
    monkeypatch.setattr(sys,'prefix',str(de.ROOT/('elsewhere' if scenario=='prefix' else '.venv')))
    def outcome(fn,events):
        try:result=('ok',fn())
        except RuntimeError as exc:result=('error',str(exc))
        return result,list(events)
    monkeypatch.setattr(sys,'argv',['bootstrap.py','start'])
    monkeypatch.setattr(launcher,'main',lambda:'launched')
    boot_events=harness((bootstrap,'verify'),(bootstrap,'local_database'))
    boot=outcome(bootstrap.main,boot_events)
    engine_events=harness((release_guard,'verify'),(release_guard,'local_database'))
    engine=outcome(lambda:'launched' if de.preflight()==(launcher,config) else 'wrong',engine_events)
    assert boot==engine
    assert boot[0][0]==('ok' if scenario=='ok' else 'error')
    if scenario=='ok':
        assert [e[0] for e in boot[1]]==['verify','chdir','setup_env','local_database']
        assert dict((e[0],e[1:]) for e in boot[1])['setup_env']==(('EXECUTION_MODE','PFDESK_PARITY_PROBE'),)
    # The cleared keys are bootstrap's own literal tuple.
    tree=ast.parse((APP/'bootstrap.py').read_text(encoding='utf-8'))
    loops=[n for n in ast.walk(tree) if isinstance(n,ast.For) and 'os.environ.pop' in ast.unparse(n)]
    assert len(loops)==1 and ast.literal_eval(loops[0].iter)==de.CLEAR_ENV

@pytest.mark.parametrize('argv',[[],['run'],['run','--token',TOKEN.upper()],['run','--token','abc'],['start','--token',TOKEN],['run','--token',TOKEN,'--extra']])
def test_cli_accepts_only_run_with_a_token(control,monkeypatch,argv):
    monkeypatch.setattr(de,'run',lambda token:pytest.fail('run reached'))
    with pytest.raises(SystemExit) as exc:de.main(argv)
    assert exc.value.code==de.EXIT_USAGE and not control.exists()

def test_cli_run_reaches_run_with_the_token(monkeypatch):
    monkeypatch.setattr(de,'run',lambda token:('ran',token))
    assert de.main(['run','--token',TOKEN])==('ran',TOKEN)

def copy_host(tmp_path):
    app=tmp_path/'r'/'app';app.mkdir(parents=True)
    for name in ('desktop_engine.py','state_io.py','release_guard.py'):shutil.copy2(APP/name,app/name)
    return app

def test_real_process_without_manifest_fails_before_any_launch(tmp_path):
    app=copy_host(tmp_path);control=app/'data'/'desktop'
    log=de.log_name(TOKEN);(control/'logs').mkdir(parents=True);(control/'logs'/log).write_text('')
    (control/('stop_'+OTHER+'.request')).write_text('')
    env={k:v for k,v in os.environ.items() if k not in de.CLEAR_ENV}
    out=subprocess.run([sys.executable,'-I',str(app/'desktop_engine.py'),'run','--token',TOKEN],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=120)
    assert out.returncode==1,out.stdout+out.stderr
    state=json.loads((control/de.STATE_NAME).read_text(encoding='utf-8'))
    assert state['phase']=='FAILED' and state['error_code']=='RuntimeError: CLEAN_RELEASE_MANIFEST_MISSING: extract the complete ZIP into a new folder'
    assert state['token']==TOKEN and state['log_name']==log and type(state['pid']) is int and state['create_time']
    assert not de.alive(state) and not (control/('stop_'+OTHER+'.request')).exists()
    assert 'DESKTOP ENGINE: FAILED' in out.stdout and 'Traceback' not in out.stderr

def test_real_process_already_running_exit_code(tmp_path):
    app=copy_host(tmp_path);control=app/'data'/'desktop';control.mkdir(parents=True)
    (control/de.STATE_NAME).write_text(json.dumps(own_state()));before=(control/de.STATE_NAME).read_bytes()
    out=subprocess.run([sys.executable,'-I',str(app/'desktop_engine.py'),'run','--token',TOKEN],cwd=tmp_path,capture_output=True,text=True,timeout=120)
    assert out.returncode==de.EXIT_ALREADY_RUNNING and 'ALREADY_RUNNING' in out.stdout
    assert (control/de.STATE_NAME).read_bytes()==before

def test_real_process_bad_token_writes_nothing(tmp_path):
    app=copy_host(tmp_path)
    out=subprocess.run([sys.executable,'-I',str(app/'desktop_engine.py'),'run','--token','nothex'],cwd=tmp_path,capture_output=True,text=True,timeout=120)
    assert out.returncode==de.EXIT_USAGE and not (app/'data').exists()
