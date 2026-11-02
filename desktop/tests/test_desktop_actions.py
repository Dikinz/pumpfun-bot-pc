"""WP2 actions (spec 2.3): exact argv/env, 04 codes, 05 evidence folder, one background action at a time,
04/05/02 refused while a campaign runs (decision 8), visible consoles for 02/08, fixed folders only."""
import json,os,subprocess,sys
from pathlib import Path
import pytest
from fakes import forbid_live,TOKEN,make_layout,write_state,Health,FakePopen,Clock,controller,C
from desktop import actions,logs
from desktop.controller import allowed
WIN=os.name=='nt'

def test_engine_argv_and_env_exact(tmp_path):
    lay=make_layout(tmp_path)
    assert actions.engine_argv(lay,TOKEN)==[str(lay.root/'.venv'/'Scripts'/'python.exe'),'-I',str(lay.root/'app'/'desktop_engine.py'),'run','--token',TOKEN]
    with pytest.raises(ValueError):actions.engine_argv(lay,'x')
    base=dict(PATH='p',PythonPath='a',PYTHONHOME='b',DATABASE_PATH='c',DASHBOARD_PORT='d',execution_mode='live',KEEP='k',PYTHONUTF8='0')
    # MINOR 1: nothing is added; what the user's environment has passes on exactly as through 03_START.bat
    assert actions.engine_env(base)==dict(PATH='p',KEEP='k',PYTHONUTF8='0')
    assert actions.engine_env(dict(PATH='p'))==dict(PATH='p')

def test_background_argv_and_env_exact(tmp_path):
    lay=make_layout(tmp_path);py=str(lay.venv_python);boot=str(lay.root/'app'/'bootstrap.py')
    assert actions.action_argv(lay,'results')==[py,'-I',boot,'results'] and actions.action_argv(lay,'check')==[py,'-I',boot,'check']
    with pytest.raises(ValueError):actions.action_argv(lay,'start')
    base=dict(PATH='p',PYTHONPATH='a',PYTHONHOME='b',DATABASE_PATH='keep')
    assert actions.action_env('results',base)==dict(PATH='p',DATABASE_PATH='keep')  # as 05_RESULTS.bat: nothing added
    assert actions.action_env('check',base)==dict(PATH='p',DATABASE_PATH='keep',PYTHONUTF8='1')  # 04_CHECK.bat: set PYTHONUTF8=1
    with pytest.raises(ValueError):actions.action_env('start',base)

CHILD='import json,os,sys;print(json.dumps(dict(utf8=sys.flags.utf8_mode,keys=sorted(k for k in os.environ if k.upper().startswith("PYTHON")))))'
def child_view(env):
    '''A real child started like the launcher's children (main.py, dashboard, monitor): without -I, so it reads env.'''
    r=subprocess.run([sys.executable,'-c',CHILD],env=env,capture_output=True,text=True,errors='replace',timeout=60)
    assert r.returncode==0,r.stderr
    return json.loads(r.stdout)

@pytest.mark.parametrize('build',['engine','results'])
def test_real_child_gets_bat_environment_without_utf8_mode(monkeypatch,build):
    for key in ('PYTHONUTF8','PYTHONIOENCODING','PYTHONUNBUFFERED','PYTHONPATH','PYTHONHOME'):monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('PYTHONPATH','C:\\nowhere')
    env=actions.engine_env() if build=='engine' else actions.action_env('results')
    assert not {'PYTHONUTF8','PYTHONIOENCODING','PYTHONUNBUFFERED','PYTHONPATH','PYTHONHOME'}&{k.upper() for k in env}
    seen=child_view(env)
    assert seen['utf8']==0 and not {'PYTHONUTF8','PYTHONIOENCODING','PYTHONPATH','PYTHONHOME'}&set(seen['keys']),seen

def test_real_child_of_check_matches_04_check_bat(monkeypatch):
    for key in ('PYTHONUTF8','PYTHONIOENCODING','PYTHONUNBUFFERED'):monkeypatch.delenv(key,raising=False)
    env=actions.action_env('check');assert env['PYTHONUTF8']=='1' and 'PYTHONIOENCODING' not in {k.upper() for k in env}
    seen=child_view(env);assert seen['utf8']==1 and 'PYTHONIOENCODING' not in seen['keys']

def test_results_log_with_ansi_and_utf8_lines_finds_cyrillic_evidence(tmp_path,monkeypatch):
    '''05 output without UTF-8 mode is ANSI (cp1251 on Russian Windows); window/04 lines are UTF-8.'''
    monkeypatch.setattr(logs,'ANSI','cp1251')
    lay=make_layout(tmp_path);pop=FakePopen();ctl=controller(lay,popen=pop,health=Health())
    log=Path(ctl.run('results')['log'])
    folder='C:\\Пользователи\\Иван\\results\\20261102_120000'
    log.write_bytes('Отчёт готов ✓\r\n'.encode('utf-8')+('OSError: [WinError 5] Отказано в доступе\r\nFULL EVIDENCE FOLDER: '+folder+'\r\n').encode('cp1251'))
    pop.procs[0].code=0;s=ctl.refresh()
    assert s.action['evidence']==folder and 'Отказано в доступе' in logs.read_tail(log) and 'Отчёт готов ✓' in logs.read_tail(log)

def test_console_commands_exact(tmp_path,monkeypatch):
    lay=make_layout(tmp_path);monkeypatch.setenv('ComSpec','C:\\Windows\\system32\\cmd.exe')
    assert actions.console_command(lay,'settings')=='"C:\\Windows\\system32\\cmd.exe" /c "'+str(lay.root/'02_SETTINGS.bat')+'"'
    assert actions.console_command(lay,'preserve')=='"C:\\Windows\\system32\\cmd.exe" /c "'+str(lay.root/'08_PRESERVE_DATA.bat')+'"'
    monkeypatch.setenv('ComSpec','C:\\evil\\x.exe');monkeypatch.setenv('SystemRoot','C:\\Windows')
    assert actions.console_command(lay,'settings').startswith('"C:\\Windows\\System32\\cmd.exe" /c ')
    pop=FakePopen();monkeypatch.setenv('PYTHONPATH','x')
    actions.open_console(lay,'settings',popen=pop);call,=pop.calls
    assert call['args']==actions.console_command(lay,'settings') and call['creationflags']==(C.CREATE_NEW_CONSOLE if WIN else 0)
    assert 'PYTHONPATH' not in {k.upper() for k in call['env']} and 'stdout' not in call  # the console is the user's, nothing captured
    with pytest.raises(ValueError):actions.open_console(lay,'results',popen=pop)

def test_check_codes_like_04_check_bat():
    assert actions.check_outcome(0)==('PASSED','Technical tests and device time check PASSED.')
    status,text=actions.check_outcome(2)
    assert status=='TIME_WARNING' and text.splitlines()==['Technical tests passed, but time check needs attention.',
        'See app\\data\\time_check.txt. Keep this report before the next test.','No clock correction was performed. Trading tolerance is unchanged.']
    for code in (1,3,-1,255):assert actions.check_outcome(code)==('FAILED','Technical check failed. Keep this window and copy the error text.')
    bat=(Path(__file__).resolve().parents[2]/'04_CHECK.bat').read_text(encoding='utf-8')
    for code in (0,2,1):
        for line in actions.check_outcome(code)[1].splitlines():assert 'echo '+line in bat

def test_results_background_parses_evidence(tmp_path):
    lay=make_layout(tmp_path);pop=FakePopen();clock=Clock();ctl=controller(lay,popen=pop,clock=clock,health=Health())
    r=ctl.run('results');assert r['ok']
    call,=pop.calls;log=Path(r['log'])
    assert call['args']==actions.action_argv(lay,'results') and call['creationflags']==(C.DETACHED_FLAGS if WIN else 0) and call['stdin']==subprocess.DEVNULL
    assert logs.FAMILIES['results'].fullmatch(log.name) and log.parent==lay.logs and call['stdout'].closed
    s=ctl.refresh();assert s.busy=='results' and s.action['done'] is False and allowed(s,'start')[0] is False and allowed(s,'check')==(False,C.TEXT['busy'].format(action='results'))
    assert ctl.run('check')['ok'] is False and ctl.start()['ok'] is False and len(pop.calls)==1
    assert allowed(s,'preserve')==(True,None)
    log.write_text('PRESERVED EVIDENCE: C:\\r\\results\\preservation_1\\db.zip\nFULL EVIDENCE FOLDER: C:\\r\\results\\20261102_120000\n',encoding='utf-8')
    pop.procs[0].code=0;s=ctl.refresh()
    assert s.busy is None and s.action['done'] and s.action['status']=='OK' and s.action['evidence']=='C:\\r\\results\\20261102_120000'
    assert [e for e,_ in ctl.drain_events()]==['action_done'] and ctl.latest_log()==log

def test_check_background_codes(tmp_path):
    for code,status in ((0,'PASSED'),(2,'TIME_WARNING'),(1,'FAILED')):
        lay=make_layout(tmp_path/str(code));pop=FakePopen(code=code);ctl=controller(lay,popen=pop)
        r=ctl.run('check');s=ctl.refresh()
        assert s.action['status']==status and s.action['code']==code and s.action['evidence'] is None
        assert pop.calls[0]['env']['PYTHONUTF8']=='1' and not pop.procs[0].killed

@pytest.mark.parametrize('kind,health,fields',[('OWN_RUNNING',None,dict(phase='RUNNING')),('EXTERNAL_SAME','same',None),('OWN_STARTING',None,dict(phase='STARTING')),('EXTERNAL_FOREIGN','foreign',None)])
def test_results_check_settings_refused_while_bot_runs(tmp_path,kind,health,fields):
    lay=make_layout(tmp_path);h=Health()
    if health=='same':h.same(lay)
    if health=='foreign':h.foreign(lay,version='v-other')
    if fields:write_state(lay,**fields)
    pop=FakePopen();ctl=controller(lay,popen=pop,health=h)
    assert ctl.refresh().kind==kind
    for name in ('results','check'):assert ctl.run(name)['ok'] is False
    assert ctl.open_console('settings')['ok'] is False and not pop.calls
    assert ctl.open_console('preserve')['ok'] is True and len(pop.calls)==1  # 08 works with an old folder, always allowed

def test_open_folders_only_fixed_paths(tmp_path):
    lay=make_layout(tmp_path);opened=[];ctl=controller(lay,startfile=opened.append)
    assert ctl.open_folder('open_results')==dict(ok=False,message='Папка ещё не создана: '+str(lay.results)) and not opened
    lay.results.mkdir();lay.logs.mkdir(parents=True)
    assert ctl.open_folder('open_results')['ok'] and ctl.open_folder('open_logs')['ok'] and opened==[str(lay.results),str(lay.logs)]
    with pytest.raises(KeyError):ctl.open_folder('C:\\Windows')

def test_latest_log_prefers_running_action_then_state(tmp_path):
    lay=make_layout(tmp_path);ctl=controller(lay)
    assert ctl.latest_log() is None
    write_state(lay,phase='STOPPED',exit_code=0,log_name='engine_20261102_101112_01234567.log')
    assert controller(lay).latest_log()==lay.logs/'engine_20261102_101112_01234567.log'
