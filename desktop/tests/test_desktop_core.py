"""WP2 desktop core: contract import, layout/identity, release_check, short-path preflight, state detection
(spec 2.1), action matrix (2.4), start/stop progress without kill, tray/close policy, logs, settings."""
import ast,hashlib,importlib.util,json,os,shutil,subprocess,sys
from pathlib import Path
import pytest
from fakes import (forbid_live,ROOT,TOKEN,OTHER,VERSION,make_layout,write_state,Procs,Health,FakePopen,Clock,Lineage,controller,C)
from desktop import paths,release_check,logs,controller as ctl_mod,settings_store
from desktop.controller import allowed,tray_state,close_action,quit_needs_confirm,title,Status
APP=ROOT/'app'
WIN=os.name=='nt'

def load(name):
    spec=importlib.util.spec_from_file_location('pfdesk_'+name,APP/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

# ---- contract and layout ----
def test_contract_comes_from_the_engine_host():
    de=load('desktop_engine')
    for name in ('SCHEMA','STATE_NAME','STOP_TEMPLATE','PHASES','ACTIVE_PHASES','TOKEN_RE','FIELDS','LOG_TEMPLATE','CLEAR_ENV','CLEAN_DB'):
        assert getattr(C,name)==getattr(de,name),name
    assert Path(C.engine.__file__).resolve()==(APP/'desktop_engine.py').resolve()
    assert C.STATES==('OWN_PREFLIGHT','OWN_STARTING','OWN_RUNNING','OWN_STOP_REQUESTED','ENGINE_LOST','ORPHAN_RUNNING','EXTERNAL_SAME','EXTERNAL_FOREIGN','STOPPED')
    assert C.APP_MUTEX=='PumpfunPaperDesktopApp' and not hasattr(C,'ENGINE_SET_ENV')
    assert C.ENGINE_CLEAR_ENV==('PYTHONPATH','PYTHONHOME','DATABASE_PATH','DASHBOARD_PORT','EXECUTION_MODE')
    assert C.DETACHED_FLAGS==0x08000200 and C.JS_API==('get_status','start','stop','run_results','run_check','open_settings','open_preserve','open_results_folder','open_logs','show_terminal')

def test_core_imports_no_gui_library_and_no_bot_module():
    code=('import sys;sys.path.insert(0,sys.argv[1])\n'
        'import desktop.constants,desktop.paths,desktop.release_check,desktop.engine_client,desktop.controller,desktop.actions,'
        'desktop.logs,desktop.single_instance,desktop.session_end,desktop.settings_store\n'
        'bad=[m for m in ("webview","pystray","PIL","clr","config","launcher","main","dotenv","psutil") if m in sys.modules]\n'
        'print("BAD",bad);raise SystemExit(1 if bad else 0)')
    result=subprocess.run([sys.executable,'-I','-c',code,str(ROOT)],capture_output=True,text=True,errors='replace',timeout=60)
    assert result.returncode==0,result.stdout+result.stderr

def test_identity_matches_system_health():
    system_health=load('system_health')
    for db in (APP/'data'/'clean_0762.db',Path('relative/x.db'),Path('C:/Some Folder/DB.sqlite')):
        assert paths.identity(db)==system_health.identity(db)

def test_layout_defaults(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    lay=paths.Layout(root=tmp_path,version='v1')
    assert lay.db==(tmp_path/'app'/'data'/'clean_0762.db').resolve() and lay.control==tmp_path.resolve()/'app'/'data'/'desktop'
    assert lay.logs==lay.control/'logs' and lay.url=='http://127.0.0.1:8188/' and lay.port==8188
    assert lay.profile==tmp_path/'local'/'PumpfunPaperDesktop'/lay.identity and lay.webview_storage==lay.profile/'WebView2'
    assert lay.settings_file==lay.profile/'settings.json' and 'data' not in lay.settings_file.relative_to(tmp_path).parts[:2]
    assert lay.venv_python==tmp_path.resolve()/'.venv'/'Scripts'/'python.exe' and lay.engine_script==tmp_path.resolve()/'app'/'desktop_engine.py'
    with pytest.raises(ValueError):paths.Layout(root=tmp_path,port=49190)
    (tmp_path/'BUILD_MANIFEST.json').write_text(json.dumps(dict(version='v9',files={})),encoding='utf-8')
    assert paths.Layout(root=tmp_path).version=='v9' and paths.manifest_version(tmp_path/'missing') is None

# ---- release check (EXTRA_MODULE for desktop/**) ----
def build_release(tmp_path):
    root=tmp_path/'rel';(root/'app').mkdir(parents=True);(root/'desktop'/'tests').mkdir(parents=True)
    shutil.copy2(APP/'release_guard.py',root/'app'/'release_guard.py')
    (root/'desktop'/'__init__.py').write_text('',encoding='utf-8');(root/'desktop'/'tests'/'test_desktop_x.py').write_text('x=1\n',encoding='utf-8')
    files={}
    for rel in ('app/release_guard.py','desktop/__init__.py','desktop/tests/test_desktop_x.py'):
        files[rel]=hashlib.sha256((root/rel).read_bytes()).hexdigest()
    guard=release_check.release_guard(root)
    (root/'BUILD_MANIFEST.json').write_text(json.dumps(dict(version=guard.VERSION,build_revision='T',files=files)),encoding='utf-8')
    return root

def test_release_check_refuses_extra_desktop_module(tmp_path):
    root=build_release(tmp_path)
    assert release_check.verify(root)['version']==release_check.release_guard(root).VERSION
    (root/'desktop'/'__pycache__').mkdir();(root/'desktop'/'__pycache__'/'x.py').write_text('',encoding='utf-8')
    release_check.verify(root)  # __pycache__ is ignored
    (root/'desktop'/'extra.py').write_text('',encoding='utf-8')
    with pytest.raises(RuntimeError,match='^DESKTOP_EXTRA_MODULE: desktop/extra.py$'):release_check.verify(root)
    (root/'desktop'/'extra.py').unlink();(root/'app'/'extra.py').write_text('',encoding='utf-8')
    with pytest.raises(RuntimeError,match='CLEAN_RELEASE_EXTRA_MODULE'):release_check.verify(root)
    (root/'app'/'extra.py').unlink();(root/'desktop'/'__init__.py').write_text('changed',encoding='utf-8')
    with pytest.raises(RuntimeError,match='CLEAN_RELEASE_FILE_CHANGED'):release_check.verify(root)
    with pytest.raises(RuntimeError,match='CLEAN_RELEASE_MANIFEST_MISSING'):release_check.check_desktop(tmp_path)

# ---- short-path preflight (spike workaround 1) ----
def test_short_path_preflight(tmp_path,monkeypatch):
    assert paths.short_path_problem('C:/pf/v',scan=False) is None
    long='C:/'+'x'*200+'/v'
    problem=paths.short_path_problem(long,scan=False)
    assert problem.startswith('DESKTOP_PATH_TOO_LONG') and 'WebView2Loader.dll' in problem and '%LOCALAPPDATA%' in problem
    # Limits relative to this temp folder: a .pyd over its (lower) limit is refused, a .dll of the same length loads.
    venv=tmp_path/'v';site=venv/'Lib'/'site-packages';site.mkdir(parents=True)
    loader=len(str(venv.joinpath(*C.WEBVIEW2_LOADER)))
    monkeypatch.setattr(C,'DLL_PATH_MAX',loader+20);monkeypatch.setattr(C,'PYD_PATH_MAX',loader+5)
    assert paths.short_path_problem(venv) is None
    pyd=site/('p'*(loader+10-len(str(site))-5)+'.pyd');pyd.write_bytes(b'')
    assert len(str(pyd))==loader+10 and str(pyd) in paths.short_path_problem(venv)
    pyd.rename(pyd.with_suffix('.dll'))
    assert paths.short_path_problem(venv) is None

@pytest.mark.skipif(not WIN,reason='junctions are Windows only')
def test_short_path_preflight_refuses_junction(tmp_path):
    import _winapi
    real=tmp_path/'real';real.mkdir();link=tmp_path/'j'
    _winapi.CreateJunction(str(real),str(link))
    try:assert paths.short_path_problem(link/'venv',scan=False).startswith('DESKTOP_PATH_LINK')
    finally:os.rmdir(link)

# ---- state detection (2.1) ----
SCENARIOS=[
    # (state fields or None, process alive, health: None|'same'|dict override, expected kind)
    (None,False,None,'STOPPED'),
    (dict(phase='PREFLIGHT'),True,None,'OWN_PREFLIGHT'),
    (dict(phase='STARTING'),True,None,'OWN_STARTING'),
    (dict(phase='RUNNING'),True,'same','OWN_RUNNING'),
    (dict(phase='STOP_REQUESTED'),True,None,'OWN_STOP_REQUESTED'),
    (dict(phase='RUNNING'),False,'same','ORPHAN_RUNNING'),  # host dead, our dashboard still answers (M1)
    (dict(phase='STARTING'),False,'same','ORPHAN_RUNNING'),
    (dict(phase='RUNNING'),False,None,'ENGINE_LOST'),
    (dict(phase='RUNNING'),False,dict(version='v0.0.1'),'ENGINE_LOST'),  # a foreign dashboard is not our orphan
    (dict(phase='STARTING'),False,None,'ENGINE_LOST'),
    (dict(phase='STOPPED',exit_code=0),False,'same','EXTERNAL_SAME'),
    (None,False,'same','EXTERNAL_SAME'),
    (None,False,dict(version='v0.0.1'),'EXTERNAL_FOREIGN'),
    (None,False,dict(database_identity='0'*20),'EXTERNAL_FOREIGN'),
    (dict(phase='FAILED',exit_code=1,error_code='RuntimeError: X'),False,None,'STOPPED'),
    (dict(phase='RUNNING',schema='OTHER'),True,None,'STOPPED'),
]
@pytest.mark.parametrize('fields,alive,health,expected',SCENARIOS)
def test_state_detection_table(tmp_path,fields,alive,health,expected):
    lay=make_layout(tmp_path)
    if fields is not None:write_state(lay,**fields)
    h=Health()
    if health=='same':h.same(lay)
    elif isinstance(health,dict):h.foreign(lay,**health)
    procs=Procs()
    if not alive:procs.rows={}
    ctl=controller(lay,procs=procs,health=h)
    s=ctl.refresh()
    assert s.kind==expected
    if s.own:assert h.calls==0  # an own campaign is identified by the state file, not by the port

def test_pid_reuse_is_engine_lost_with_real_process_table(tmp_path):
    import psutil
    lay=make_layout(tmp_path);me=psutil.Process()
    write_state(lay,pid=os.getpid(),create_time=me.create_time())
    from desktop.engine_client import EngineClient
    from desktop.controller import Controller
    ctl=Controller(lay,client=EngineClient(lay,health_get=Health(),popen=FakePopen()),clock=Clock())
    assert ctl.refresh().kind=='OWN_RUNNING'
    write_state(lay,pid=os.getpid(),create_time=me.create_time()-50)
    assert ctl.refresh().kind=='ENGINE_LOST'

def test_engine_lost_needs_confirmation_and_notifies_once(tmp_path):
    lay=make_layout(tmp_path);write_state(lay,phase='RUNNING');procs=Procs();procs.rows={}
    ctl=controller(lay,procs=procs)
    s=ctl.refresh();assert s.kind=='ENGINE_LOST' and not s.lost_acked
    assert allowed(s,'start')==(False,C.TEXT['lost_confirm'])
    assert allowed(s,'results')==(True,None) and allowed(s,'check')==(True,None) and allowed(s,'settings')==(True,None)
    ctl.refresh()
    assert [e for e,_ in ctl.drain_events()]==['engine_lost']
    assert ctl.confirm_lost()['ok'] and allowed(ctl.refresh(),'start')==(True,None)
    write_state(lay,phase='RUNNING',token=OTHER,pid=99)  # another lost run needs a new confirmation
    assert allowed(ctl.refresh(),'start')[0] is False and [e for e,_ in ctl.drain_events()]==['engine_lost']
    assert tray_state(ctl.refresh())=='red'

# ---- matrix 2.4 ----
def status(kind,busy=None,lost_acked=False,phase=None):
    return Status(kind,dict(phase=phase) if phase else {},busy=busy,lost_acked=lost_acked)
ALWAYS=('preserve','open_results','open_logs','show_log')
def test_action_matrix():
    for kind in C.STATES:
        for busy in (None,'results','check','start'):
            s=status(kind,busy)
            for a in ALWAYS:assert allowed(s,a)==((False,C.TEXT['orphan']) if kind=='ORPHAN_RUNNING' and a=='preserve' else (True,None)),(kind,a)
            assert allowed(s,'stop')[0]==(kind=='OWN_RUNNING')
            idle=kind in ('STOPPED','ENGINE_LOST') and busy is None
            for a in ('results','check','settings'):assert allowed(s,a)[0]==idle,(kind,busy,a)
            assert allowed(s,'start')[0]==(kind=='STOPPED' and busy is None)
            assert allowed(status(kind,busy,lost_acked=True),'start')[0]==(kind in ('STOPPED','ENGINE_LOST') and busy is None)
            assert allowed(s,'show_terminal')[0]==(kind in ('OWN_RUNNING','OWN_STOP_REQUESTED','EXTERNAL_SAME','ORPHAN_RUNNING'))
    assert allowed(status('EXTERNAL_SAME'),'stop')==(False,'Бот запущен через 03_START.bat: остановите его Ctrl+C в окне 03_START.')
    assert allowed(status('OWN_RUNNING',busy='stop'),'stop')==(False,C.TEXT['stopping'])
    assert allowed(status('STOPPED',busy='check'),'results')==(False,'Уже выполняется действие: check.')
    assert allowed(status('STOPPED'),'nonsense')[0] is False

def test_orphan_matrix_allows_only_viewing():
    '''M1: start, stop, 05, 04, 02, 08 forbidden (the launcher died with the host); terminal, logs, folders allowed.'''
    for lost_acked in (False,True):
        s=status('ORPHAN_RUNNING',lost_acked=lost_acked)
        for a in ('start','stop','results','check','settings','preserve'):assert allowed(s,a)==(False,C.TEXT['orphan']),a
        for a in ('show_terminal','show_log','open_logs','open_results'):assert allowed(s,a)==(True,None),a
    assert C.TEXT['orphan']==('Управляющий процесс бота завершился, а процессы бота ещё работают. Запуск, 05, 04 и настройки недоступны, '
        'пока они не завершатся. Перезагрузите компьютер, затем запустите 09_RECOVERY_CHECK.')
    s=status('ORPHAN_RUNNING')
    assert tray_state(s)=='red' and close_action(s)=='hide' and not quit_needs_confirm(s) and s.terminal

def test_tray_close_title_policy():
    assert tray_state(status('STOPPED'))=='grey' and tray_state(status('OWN_RUNNING'))=='green' and tray_state(status('EXTERNAL_SAME'))=='green'
    for k in ('OWN_PREFLIGHT','OWN_STARTING','OWN_STOP_REQUESTED'):assert tray_state(status(k))=='yellow'
    assert tray_state(status('STOPPED',busy='results'))=='yellow'
    for s in (status('ENGINE_LOST'),status('ORPHAN_RUNNING'),status('EXTERNAL_FOREIGN'),Status('STOPPED',dict(phase='FAILED')),
            Status('STOPPED',dict(phase='STOPPED'),self_stop=dict(token=TOKEN,code=5,text='x'))):assert tray_state(s)=='red'
    for k in ('OWN_RUNNING','OWN_STARTING','OWN_STOP_REQUESTED','EXTERNAL_SAME'):assert close_action(status(k))=='hide' and quit_needs_confirm(status(k))
    assert close_action(status('STOPPED',busy='check'))=='hide'
    for k in ('STOPPED','ENGINE_LOST','EXTERNAL_FOREIGN'):assert close_action(status(k))=='quit' and not quit_needs_confirm(status(k))
    assert title(status('OWN_RUNNING'),'v0.7.101.4')=='PumpFun PAPER v0.7.101.4 — работает'

# ---- start (2.3) ----
def test_start_spawns_detached_host_with_clean_env(tmp_path,monkeypatch):
    for key in ('PYTHONPATH','PYTHONHOME','DATABASE_PATH','DASHBOARD_PORT','EXECUTION_MODE'):monkeypatch.setenv(key,'bad')
    for key in ('PYTHONUTF8','PYTHONIOENCODING','PYTHONUNBUFFERED'):monkeypatch.delenv(key,raising=False)
    lay=make_layout(tmp_path);pop=FakePopen();h=Health();ctl=controller(lay,popen=pop,health=h)
    result=ctl.start();assert result['ok'],result
    call,=pop.calls;token=result['token']
    assert call['args']==[str(lay.venv_python),'-I',str(lay.engine_script),'run','--token',token]
    assert C.engine.valid_token(token)
    env=call['env']
    assert not {k.upper() for k in env}&{'PYTHONPATH','PYTHONHOME','DATABASE_PATH','DASHBOARD_PORT','EXECUTION_MODE'}
    assert not {k.upper() for k in env}&{'PYTHONUTF8','PYTHONIOENCODING','PYTHONUNBUFFERED'}  # MINOR 1: as 03_START.bat
    assert call['creationflags']==(0x08000200 if WIN else 0) and call['stdin']==subprocess.DEVNULL and call['stderr']==subprocess.STDOUT
    assert call['cwd']==str(lay.root) and call['close_fds'] is True
    log=Path(call['stdout'].name)
    assert log.parent==lay.logs and logs.FAMILIES['engine'].fullmatch(log.name) and log.name.endswith('_'+token[:8]+'.log')
    assert call['stdout'].closed  # the window keeps no handle to the log
    s=ctl.refresh();assert s.busy=='start' and tray_state(s)=='yellow' and allowed(s,'start')==(False,C.TEXT['starting'])
    assert ctl.start()['ok'] is False and len(pop.calls)==1
    write_state(lay,token=token,phase='PREFLIGHT');s=ctl.refresh();assert s.kind=='OWN_PREFLIGHT' and s.start['phase']=='PREFLIGHT'
    write_state(lay,token=token,phase='RUNNING');s=ctl.refresh()
    assert s.kind=='OWN_RUNNING' and not s.start['done']  # RUNNING without a matching /health is not ready yet
    h.same(lay);s=ctl.refresh()
    assert s.start['done'] and s.start['ok'] and s.busy is None and ('start_done','Бот работает.') in ctl.drain_events()
    assert not list(lay.control.glob('stop_*.request'))

def test_start_requires_venv(tmp_path):
    lay=make_layout(tmp_path,venv=False);pop=FakePopen();ctl=controller(lay,popen=pop)
    assert ctl.start()==dict(ok=False,message='Не найден .venv. Запустите 01_INSTALL.bat в этой папке.') and not pop.calls

def test_start_failed_shows_error_code_and_log_tail(tmp_path):
    lay=make_layout(tmp_path);pop=FakePopen();ctl=controller(lay,popen=pop)
    token=ctl.start()['token'];log=Path(pop.calls[0]['stdout'].name)
    log.write_text('DESKTOP ENGINE: PREFLIGHT\nDESKTOP ENGINE: FAILED: RuntimeError: CLEAN_RELEASE_FILE_CHANGED: app/x.py\n',encoding='utf-8')
    write_state(lay,token=token,phase='FAILED',exit_code=1,error_code='RuntimeError: CLEAN_RELEASE_FILE_CHANGED: app/x.py',log_name=log.name)
    s=ctl.refresh()
    assert s.start['done'] and not s.start['ok'] and s.start['error_code'].startswith('RuntimeError: CLEAN_RELEASE_FILE_CHANGED')
    assert 'FAILED: RuntimeError' in s.start['log_tail'] and s.kind=='STOPPED' and tray_state(s)=='red'
    assert [e for e,_ in ctl.drain_events()]==['start_failed']

def test_start_host_exit_before_state_and_timeout_never_kill(tmp_path):
    lay=make_layout(tmp_path);pop=FakePopen(code=3);ctl=controller(lay,popen=pop)
    ctl.start();s=ctl.refresh()
    assert s.start['done'] and s.start['message']==C.TEXT['already_running']
    clock=Clock();pop=FakePopen();ctl=controller(lay,popen=pop,clock=clock)
    ctl.start();clock.advance(119);assert not ctl.refresh().start['done']
    clock.advance(2);s=ctl.refresh()  # MINOR 3: no state yet but the host lives: slow, never failed on time
    assert not s.start['done'] and s.start['slow'] and s.start['message']==C.TEXT['start_slow'] and not pop.procs[0].killed

def test_start_slow_keeps_waiting_until_host_dies_or_fails(tmp_path):
    '''MINOR 3: after 120 s in PREFLIGHT/STARTING the start is shown as slow (one event) and keeps waiting.'''
    lay=make_layout(tmp_path);pop=FakePopen();clock=Clock();h=Health();procs=Procs();ctl=controller(lay,popen=pop,clock=clock,health=h,procs=procs)
    token=ctl.start()['token'];write_state(lay,token=token,phase='PREFLIGHT')
    clock.advance(121);s=ctl.refresh()
    assert s.kind=='OWN_PREFLIGHT' and not s.start['done'] and s.start['slow'] and s.busy=='start'
    assert C.TEXT['start_slow']=='Запуск идёт дольше обычного (проверки на большой базе).'
    assert ctl.drain_events()==[('start_slow',C.TEXT['start_slow'])] and json.loads(json.dumps(ctl.snapshot()))['start']['slow']
    write_state(lay,token=token,phase='STARTING');clock.advance(600);s=ctl.refresh()
    assert not s.start['done'] and ctl.drain_events()==[]  # one notice only
    write_state(lay,token=token,phase='RUNNING');h.same(lay);s=ctl.refresh()
    assert s.start['done'] and s.start['ok']
    # host dies in STARTING after the slow notice: failed
    ctl2=controller(make_layout(tmp_path/'b'),popen=FakePopen(),clock=clock,procs=procs);lay2=ctl2.layout
    token=ctl2.start()['token'];write_state(lay2,token=token,phase='STARTING');clock.advance(200);assert not ctl2.refresh().start['done']
    procs.rows={};s=ctl2.refresh();assert s.kind=='ENGINE_LOST' and s.start['done'] and not s.start['ok'] and s.start['message']==C.TEXT['lost']
    # FAILED after the slow notice: failed with its code
    ctl3=controller(make_layout(tmp_path/'c'),popen=FakePopen(),clock=clock);lay3=ctl3.layout
    token=ctl3.start()['token'];clock.advance(300);assert ctl3.refresh().start['slow']
    write_state(lay3,token=token,phase='FAILED',exit_code=1,error_code='RuntimeError: X');s=ctl3.refresh()
    assert s.start['done'] and not s.start['ok'] and s.start['error_code']=='RuntimeError: X'

# ---- stop (2.3) ----
def running(tmp_path,**kw):
    lay=make_layout(tmp_path);write_state(lay,phase='RUNNING',log_name='engine_20261102_101112_01234567.log')
    lay.logs.mkdir(parents=True,exist_ok=True);(lay.logs/'engine_20261102_101112_01234567.log').write_text('x\n',encoding='utf-8')
    h=Health().same(lay);clock=Clock();return lay,h,clock,controller(lay,health=h,clock=clock,**kw)

def test_stop_progress_writes_one_stop_file_and_never_kills(tmp_path):
    lay,h,clock,ctl=running(tmp_path)
    s=ctl.refresh();assert s.kind=='OWN_RUNNING' and allowed(s,'stop')==(True,None)
    r=ctl.stop();assert r['ok']
    assert sorted(p.name for p in lay.control.glob('stop_*'))==['stop_'+TOKEN+'.request']
    s=ctl.refresh();assert s.stop['requested'] and not s.stop['accepted'] and s.busy=='stop'
    assert allowed(s,'stop')==(False,C.TEXT['stopping'])
    assert ctl.stop()['ok'] is False and len(list(lay.control.glob('stop_*')))==1
    write_state(lay,phase='STOP_REQUESTED',stop_requested_ts=5.,log_name='engine_20261102_101112_01234567.log')
    s=ctl.refresh();assert s.kind=='OWN_STOP_REQUESTED' and s.stop['accepted'] and not s.stop['panel_down']
    h.data=None;s=ctl.refresh();assert s.stop['panel_down'] and not s.stop['done']
    clock.advance(181);s=ctl.refresh()
    assert s.stop['slow'] and not s.stop['done'] and s.stop['message']==C.TEXT['stop_slow']
    assert [e for e,_ in ctl.drain_events()]==['stop_slow']
    (lay.logs/'engine_20261102_101112_01234567.log').write_text('x\nSQLITE SHUTDOWN AUDIT: writers_finished=True integrity_before_after_ok=True\nend\n',encoding='utf-8')
    write_state(lay,phase='STOPPED',exit_code=0,log_name='engine_20261102_101112_01234567.log')
    s=ctl.refresh()
    assert s.kind=='STOPPED' and s.stop['done'] and s.stop['ok'] and s.stop['exit_code']==0 and s.stop['message']==C.TEXT['stop_ok']
    assert s.stop['audit']=='SQLITE SHUTDOWN AUDIT: writers_finished=True integrity_before_after_ok=True'
    assert [e for e,_ in ctl.drain_events()]==['stop_done'] and len(list(lay.control.glob('stop_*')))==1

def test_stop_nonzero_code_and_lost_engine(tmp_path):
    lay,h,clock,ctl=running(tmp_path)
    ctl.stop();write_state(lay,phase='STOPPED',exit_code=1);s=ctl.refresh()
    assert s.stop['done'] and not s.stop['ok'] and s.stop['message']==C.TEXT['stop_bad'].format(code=1) and 'см. журнал' in s.stop['message']
    lay2,h2,clock2,ctl2=running(tmp_path/'b')
    ctl2.stop();ctl2.client._alive=lambda st:False;h2.data=None;s=ctl2.refresh()
    assert s.kind=='ENGINE_LOST' and s.stop['done'] and s.stop['message']==C.TEXT['lost']
    lay3,h3,clock3,ctl3=running(tmp_path/'c')  # host died during the stop, its dashboard still answers
    ctl3.stop();ctl3.client._alive=lambda st:False;s=ctl3.refresh()
    assert s.kind=='ORPHAN_RUNNING' and s.stop['done'] and not s.stop['ok'] and s.stop['message']==C.TEXT['orphan']

def test_stop_is_refused_outside_own_running(tmp_path):
    lay=make_layout(tmp_path);h=Health().same(lay);ctl=controller(lay,health=h)
    assert ctl.stop()==dict(ok=False,message=C.TEXT['external_same'])
    h.data=None;ctl.refresh()  # the first miss is held (hysteresis), the second one is STOPPED
    assert ctl.stop()==dict(ok=False,message=C.TEXT['not_running'])
    write_state(lay,phase='STARTING');assert ctl.stop()==dict(ok=False,message=C.TEXT['starting'])
    assert not list(lay.control.glob('stop_*'))

def test_dropping_controller_writes_nothing(tmp_path):
    import gc
    lay,h,clock,ctl=running(tmp_path);ctl.refresh();before=sorted(p.name for p in lay.control.iterdir())
    del ctl;gc.collect()
    assert sorted(p.name for p in lay.control.iterdir())==before

def test_session_end_hook_on_controller(tmp_path):
    lay,h,clock,ctl=running(tmp_path)
    assert ctl.stop_for_session_end() is True and len(list(lay.control.glob('stop_*')))==1
    assert ctl.stop_for_session_end() is True and len(list(lay.control.glob('stop_*')))==1
    write_state(lay,phase='STOP_REQUESTED');assert ctl.stop_for_session_end() is True
    for fields in (dict(phase='STOPPED',exit_code=0),dict(phase='FAILED',exit_code=1)):
        lay2=make_layout(tmp_path/fields['phase']);write_state(lay2,**fields)
        assert controller(lay2,health=Health().same(lay2)).stop_for_session_end() is False and not list(lay2.control.glob('stop_*'))
    for phase in ('PREFLIGHT','STARTING'):  # MINOR 4: held like OWN_RUNNING, one stop file
        lay3=make_layout(tmp_path/phase);write_state(lay3,phase=phase);c=controller(lay3)
        assert c.stop_for_session_end() is True and c.stop_for_session_end() is True
        assert [p.name for p in lay3.control.glob('stop_*')]==['stop_'+TOKEN+'.request']
    lay4=make_layout(tmp_path/'orphan');write_state(lay4,phase='RUNNING');procs=Procs();procs.rows={}
    c=controller(lay4,procs=procs,health=Health().same(lay4));assert c.refresh().kind=='ORPHAN_RUNNING'
    assert c.stop_for_session_end() is False and not list(lay4.control.glob('stop_*'))  # orphans are not held

def test_snapshot_is_json_and_complete(tmp_path):
    lay,h,clock,ctl=running(tmp_path);ctl.stop()
    snap=json.loads(json.dumps(ctl.snapshot()))
    assert snap['kind']=='OWN_RUNNING' and snap['paper'] is True and snap['tray']=='yellow' and snap['version']==VERSION
    assert set(snap['allowed'])==set(C.ACTIONS) and snap['stop']['requested'] and snap['title'].startswith('PumpFun PAPER '+VERSION)

def test_log_size_warning(tmp_path,monkeypatch):
    lay,h,clock,ctl=running(tmp_path)
    monkeypatch.setattr(logs,'too_big',lambda p,limit=None:Path(p).stat().st_size>1)
    assert ctl.refresh().warning==C.TEXT['log_big'].format(name='engine_20261102_101112_01234567.log')

# ---- M1 orphans, MINOR 13 hysteresis, MINOR 2 self-stop ----
def test_orphan_running_after_host_death_and_back_to_engine_lost(tmp_path):
    lay=make_layout(tmp_path);write_state(lay,phase='RUNNING');procs=Procs();h=Health().same(lay);pop=FakePopen()
    ctl=controller(lay,procs=procs,health=h,popen=pop)
    s=ctl.refresh();assert s.kind=='OWN_RUNNING' and h.calls==0
    procs.rows={}  # only the host died: dashboard, main.py, monitor keep running
    s=ctl.refresh();assert s.kind=='ORPHAN_RUNNING' and h.calls==1 and s.terminal and tray_state(s)=='red'
    assert ctl.drain_events()==[('orphan',C.TEXT['orphan'])]
    for _ in range(3):assert ctl.refresh().kind=='ORPHAN_RUNNING'
    assert ctl.drain_events()==[]  # notified once on entry
    snap=json.loads(json.dumps(ctl.snapshot()))
    assert snap['orphan']==C.TEXT['orphan'] and snap['lost'] is None and snap['allowed']['start']==[False,C.TEXT['orphan']]
    assert ctl.start()['ok'] is False and ctl.run('results')['ok'] is False and ctl.run('check')['ok'] is False
    assert ctl.open_console('settings')['ok'] is False and ctl.open_console('preserve')['ok'] is False and ctl.stop()['ok'] is False
    assert ctl.confirm_lost()['ok'] is False and pop.calls==[] and not list(lay.control.glob('stop_*'))
    h.data=None
    assert ctl.refresh().kind=='ORPHAN_RUNNING'  # first miss: held (hysteresis)
    s=ctl.refresh();assert s.kind=='ENGINE_LOST' and not s.lost_acked
    assert [e for e,_ in ctl.drain_events()]==['engine_lost'] and allowed(s,'start')==(False,C.TEXT['lost_confirm'])
    h.same(lay);assert ctl.refresh().kind=='ORPHAN_RUNNING' and ctl.drain_events()==[]  # same run: no second notice

def test_health_hysteresis_external_same(tmp_path):
    '''MINOR 13: one missed /health (0.5 s timeout) never turns EXTERNAL_SAME into STOPPED (Start enabled).'''
    lay=make_layout(tmp_path);h=Health().same(lay);ctl=controller(lay,health=h)
    assert ctl.refresh().kind=='EXTERNAL_SAME'
    for _ in range(3):  # isolated misses keep it
        h.data=None;s=ctl.refresh();assert s.kind=='EXTERNAL_SAME' and allowed(s,'start')[0] is False and s.health['_port']==lay.port
        h.same(lay);assert ctl.refresh().kind=='EXTERNAL_SAME'
    h.data=None;assert ctl.refresh().kind=='EXTERNAL_SAME'
    s=ctl.refresh();assert s.kind=='STOPPED' and allowed(s,'start')==(True,None)  # two misses in a row
    assert ctl.refresh().kind=='STOPPED'
    h.same(lay);assert ctl.refresh().kind=='EXTERNAL_SAME'  # an answer switches back at once
    h.foreign(lay,version='v0');assert ctl.refresh().kind=='EXTERNAL_FOREIGN'  # an answer is never held
    h.data=None;assert ctl.refresh().kind=='STOPPED'  # FOREIGN does not enable Start: no hysteresis needed
    write_state(lay,phase='RUNNING');h.same(lay);ctl2=controller(lay,health=h)
    assert ctl2.refresh().kind=='OWN_RUNNING'  # state-file changes are never delayed

def test_run_that_stops_by_itself_is_red_until_acknowledged(tmp_path):
    lay=make_layout(tmp_path);write_state(lay,phase='RUNNING');ctl=controller(lay)
    assert ctl.refresh().kind=='OWN_RUNNING'
    write_state(lay,phase='STOPPED',exit_code=5,finished_ts=9.);s=ctl.refresh()
    text='Бот остановился сам (код 5). Откройте журнал.'
    assert s.kind=='STOPPED' and s.self_stop==dict(token=TOKEN,code=5,text=text) and tray_state(s)=='red'
    assert ctl.drain_events()==[('self_stopped',text)] and ctl.snapshot()['self_stop']==text
    assert ctl.refresh().self_stop and ctl.drain_events()==[]  # once
    assert ctl.ack_self_stop()==TOKEN and ctl.status.self_stop is None
    s=ctl.refresh();assert s.self_stop is None and tray_state(s)=='grey' and ctl.ack_self_stop() is None
    # a new controller remembers the acknowledgement only when told (the shell keeps it in settings.json)
    fresh=controller(lay);assert fresh.refresh().self_stop and [e for e,_ in fresh.drain_events()]==['self_stopped']
    other=controller(lay);other.ack_self_stop(TOKEN);assert other.refresh().self_stop is None and other.drain_events()==[]
    assert other.ack_self_stop('bad') is None

@pytest.mark.parametrize('case',['code0','window_stop','stop_requested_ts','failed','start_failed'])
def test_not_a_self_stop(tmp_path,case):
    lay=make_layout(tmp_path);write_state(lay,phase='RUNNING');pop=FakePopen();ctl=controller(lay,popen=pop)
    if case=='window_stop':ctl.stop();write_state(lay,phase='STOPPED',exit_code=1)
    elif case=='code0':write_state(lay,phase='STOPPED',exit_code=0)
    elif case=='stop_requested_ts':write_state(lay,phase='STOPPED',exit_code=1,stop_requested_ts=5.)  # e.g. a stop file of an earlier window
    elif case=='failed':write_state(lay,phase='FAILED',exit_code=1,error_code='X')
    else:
        write_state(lay,phase='STOPPED',exit_code=0);ctl.refresh();token=ctl.start()['token']
        write_state(lay,token=token,phase='STOPPED',exit_code=4)
    s=ctl.refresh();assert s.kind=='STOPPED' and s.self_stop is None
    assert 'self_stopped' not in [e for e,_ in ctl.drain_events()]

def test_start_acknowledges_self_stop(tmp_path):
    lay=make_layout(tmp_path);write_state(lay,phase='STOPPED',exit_code=3);ctl=controller(lay)
    assert ctl.refresh().self_stop
    r=ctl.start();assert r['ok'] and r['self_stop_acked']==TOKEN
    pop=ctl.client._popen;pop.procs[0].code=3  # host exits before its state: the old state reappears
    s=ctl.refresh();assert s.self_stop is None and s.start['done']

# ---- logs ----
def test_log_rotation_only_own_patterns(tmp_path):
    folder=tmp_path/'logs';folder.mkdir()
    own=['engine_202611%02d_101112_0123456%d.log'%(d,d%10) for d in range(1,26)]+['results_202611%02d_000000.log'%d for d in range(1,23)]+['check_20261101_000000.log']
    foreign=['engine_notes.txt','engine_2026_x.log','results.log','other_20261101_000000.log','ENGINE_20261101_101112_01234567.LOG']
    for n in own+foreign:(folder/n).write_text('x',encoding='utf-8')
    protect='engine_20261101_101112_01234561.log'
    removed=logs.rotate(folder,protect=[protect])
    left={p.name for p in folder.iterdir()}
    assert set(foreign)<=left and protect in left and 'check_20261101_000000.log' in left
    assert len([n for n in left if logs.FAMILIES['engine'].fullmatch(n)])==21
    assert len([n for n in left if n.startswith('results_')])==20 and set(removed)<=set(own)
    assert 'results_20261101_000000.log' in removed and 'results_20261122_000000.log' in left

def test_log_helpers(tmp_path):
    p=tmp_path/'a.log'
    p.write_bytes(('line\n'*10).encode()+'Ошибка \xff\n'.encode('utf-8')+b'\xff\xfe broken\n')
    assert logs.tail_lines(p,2).endswith('broken') and 'Ошибка' in logs.read_tail(p)
    assert logs.read_tail(tmp_path/'none.log')==''
    assert len(logs.read_tail(p,8))<=8
    text='x\nPRESERVED EVIDENCE: C:\\r\\results\\preservation_1\\data.zip\nFULL EVIDENCE FOLDER: C:\\r\\results\\20261102_101010\n'
    assert logs.evidence(text)==Path('C:\\r\\results\\20261102_101010')
    assert logs.evidence('PRESERVED EVIDENCE: C:\\r\\results\\preservation_1\\data.zip\nREPORT FAILED')==Path('C:\\r\\results\\preservation_1')
    assert logs.evidence('nothing') is None
    assert logs.too_big(p,limit=10) and not logs.too_big(p) and not logs.too_big(tmp_path/'none')
    assert logs.action_name('check',0).startswith('check_') and logs.FAMILIES['check'].fullmatch(logs.action_name('check'))
    with pytest.raises(ValueError):logs.action_name('settings')
    stream=logs.new_log(tmp_path/'n','check_20261102_101112.log');stream.close()
    stream=logs.new_log(tmp_path/'n','check_20261102_101112.log');stream.close()  # same second: never overwrites
    assert len(list((tmp_path/'n').iterdir()))==2

def test_log_decoding_per_line_mixed_encodings(tmp_path,monkeypatch):
    '''MINOR 1: the host's children write ANSI (cp1251), the window/04 UTF-8; each line is decoded on its own.'''
    monkeypatch.setattr(logs,'ANSI','cp1251')
    utf='DESKTOP ENGINE: PREFLIGHT ✓ Проверка';ansi='OSError: [WinError 5] Отказано в доступе: C:\\Пользователи\\x.db'
    p=tmp_path/'mixed.log';p.write_bytes(utf.encode('utf-8')+b'\r\n'+ansi.encode('cp1251')+b'\r\n'+b'tail \x98 end\n')
    lines=logs.read_tail(p).splitlines()
    assert lines[0]==utf and lines[1]==ansi and lines[2]=='tail \ufffd end'  # 0x98 is undefined in cp1251: replaced
    assert logs.tail_lines(p,2).splitlines()[0]==ansi
    assert logs.decode_line('Ж'.encode('cp1251'),'cp1251')=='Ж' and logs.decode_line('Ж'.encode('utf-8'),'cp1251')=='Ж'
    assert logs.decode_line(b'\xc6','no-such-codec')=='\ufffd'
    text=logs.read_tail(p)+'\n'+('FULL EVIDENCE FOLDER: C:\\Пользователи\\r\\20261102'.encode('cp1251')).decode('cp1251')
    assert logs.evidence(text)==Path('C:\\Пользователи\\r\\20261102')
    big=tmp_path/'big.log';big.write_bytes(b'x'*10+'Яблоко\n'.encode('utf-8')+ansi.encode('cp1251')+b'\n')
    assert logs.read_tail(big,len(ansi)+3).strip()==ansi  # a cut first line is dropped, not mis-decoded
    assert logs.ansi_encoding() and logs.ANSI=='cp1251'

# ---- settings store ----
def test_settings_store(tmp_path):
    path=tmp_path/'prof'/'settings.json';store=settings_store.SettingsStore(path)
    assert store.load()==settings_store.DEFAULTS
    assert store.update(geometry=dict(x=10,y=20,width=100,height=5000),tray_notice_shown=True)
    again=settings_store.SettingsStore(path).load()
    assert again==dict(geometry=dict(x=10,y=20,width=480,height=5000),tray_notice_shown=True,console_mode='hidden',self_stop_acked=None)
    assert store.update(self_stop_acked=TOKEN) and settings_store.SettingsStore(path).load()['self_stop_acked']==TOKEN
    store.update(self_stop_acked='../x');assert store.get('self_stop_acked') is None
    with pytest.raises(KeyError):store.update(api_key='x')
    store.update(console_mode='visible');assert store.get('console_mode')=='hidden'
    path.write_text('{broken',encoding='utf-8');assert settings_store.SettingsStore(path).load()==settings_store.DEFAULTS
    path.write_text(json.dumps(dict(geometry=dict(x='1',y=2,width=3,height=4),tray_notice_shown='yes',extra=1)),encoding='utf-8')
    assert settings_store.SettingsStore(path).load()==settings_store.DEFAULTS
    path.write_text(' '*(70*1024),encoding='utf-8');assert settings_store.SettingsStore(path).load()==settings_store.DEFAULTS
    assert not [p for p in path.parent.iterdir() if p.name.startswith('.settings.')]
    blocked=settings_store.SettingsStore(tmp_path/'file'/'settings.json');(tmp_path/'file').write_text('x');assert blocked.save() is False

# ---- M1 round 2: ORPHAN_RUNNING only for a dashboard that descends from the dead host ----
@pytest.mark.parametrize('verdict,kind',[(True,'ORPHAN_RUNNING'),(False,'EXTERNAL_SAME'),(None,'EXTERNAL_SAME'),(RuntimeError('x'),'EXTERNAL_SAME')])
def test_dead_host_with_same_dashboard_uses_lineage(tmp_path,capsys,verdict,kind):
    lay=make_layout(tmp_path);write_state(lay,phase='RUNNING');procs=Procs();procs.rows={};h=Health().same(lay)
    lineage=Lineage(verdict);ctl=controller(lay,procs=procs,health=h,lineage=lineage)
    s=ctl.refresh();assert s.kind==kind and lineage.calls==[(4242,1,8215)]
    events=[e for e,_ in ctl.drain_events()]
    assert events==(['orphan'] if kind=='ORPHAN_RUNNING' else [])
    if kind=='EXTERNAL_SAME':
        assert allowed(s,'stop')==(False,C.TEXT['external_same']) and allowed(s,'show_terminal')==(True,None)
        assert allowed(s,'start')==(False,C.TEXT['external_same']) and close_action(s)=='hide' and tray_state(s)=='green'
        snap=ctl.snapshot();assert snap['orphan'] is None and snap['kind']=='EXTERNAL_SAME'
    for _ in range(3):assert ctl.refresh().kind==kind
    # a definite verdict is cached per run; an undeterminable one is asked again but logged only once
    assert len(lineage.calls)==(1 if verdict in (True,False) else 5)
    assert len(ctl.lineage_log)==1 and ctl.lineage_log[0].startswith('DESKTOP LINEAGE: ')
    err=capsys.readouterr().err
    assert err.count('DESKTOP LINEAGE')==(0 if verdict is True else 1)

def test_stale_state_then_03_start_is_external_not_orphan(tmp_path):
    '''MAJOR-1 round 2: the window was force-quit during a log-off and left engine_state.json in an active phase
    with a dead host; then the user starts the bot with 03_START.bat. Its dashboard answers with this
    installation's identity but does not descend from the recorded host: EXTERNAL_SAME (Ctrl+C in 03_START),
    no "reboot" warning, no blocked folders, and the graceful path stays visible.'''
    lay=make_layout(tmp_path);write_state(lay,phase='RUNNING');procs=Procs();procs.rows={};h=Health()
    lineage=Lineage(False,'chain from /health pid 30 ends without the host');pop=FakePopen()
    ctl=controller(lay,procs=procs,health=h,lineage=lineage,popen=pop)
    s=ctl.refresh();assert s.kind=='ENGINE_LOST'  # nothing answers yet: the stale file means a lost engine
    h.same(lay);h.data['pid']=30
    s=ctl.refresh();assert s.kind=='EXTERNAL_SAME' and s.terminal and ctl.snapshot()['orphan'] is None
    assert ctl.stop()=={'ok':False,'message':C.TEXT['external_same']} and ctl.start()['ok'] is False and pop.calls==[]
    assert ctl.stop_for_session_end() is False and not list(lay.control.glob('stop_*'))
    assert 'external (03_START)' in ctl.lineage_log[0] and 'ends without the host' in ctl.lineage_log[0]
    h.data=None;assert ctl.refresh().kind=='EXTERNAL_SAME'  # one miss: held (hysteresis)
    assert ctl.refresh().kind=='ENGINE_LOST'  # 03_START ended: the stale active state is a lost engine again
