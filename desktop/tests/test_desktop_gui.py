"""WP3 GUI shell with fake pywebview/pystray/Pillow (spec 6.2): window URL/html, storage_path and menu;
closing while the bot runs cancels and hides; 'Выйти из окна' never writes a stop file; 'Остановить…'
refused does nothing, accepted writes exactly one stop file; tray icon follows the state; js_api takes no
arguments; status.html/log.html have no external URL; app_main order, dev override and failure paths."""
import inspect,json,re,struct,subprocess,sys,threading,time
from pathlib import Path
import pytest
from fakes import (forbid_live,ROOT,TOKEN,make_layout,write_state,Procs,Health,FakePopen,Clock,controller,wait_for,C)
import fake_gui
from desktop import window as W,tray as T,app_main,settings_store
DESKTOP=ROOT/'desktop'

class Info:
    def __init__(self):self.calls=[]
    def __call__(self,title,text):self.calls.append((title,text))

def make(tmp_path,monkeypatch,state=None,health=None,dev=False,procs=None,clock=None,popen=None,startfile=None):
    gui=fake_gui.install(monkeypatch)
    lay=make_layout(tmp_path)
    if state:write_state(lay,**state)
    h=health(lay) if callable(health) else (health or Health())
    ctl=controller(lay,procs=procs or Procs(),health=h,popen=popen or FakePopen(),clock=clock or Clock(),startfile=startfile)
    settings=settings_store.SettingsStore(lay.settings_file);settings.load();info=Info()
    shell=W.Shell(ctl,gui.webview,settings=settings,dev=dev,info=info)
    shell.tray=T.Tray(shell.show_window,shell.tray_stop,shell.quit_window,pystray=gui.pystray,spawn=lambda fn:fn())
    win=shell.create(screen=lambda:((0,0,3840,2100),(0,0,3840,2160)))
    return gui,lay,ctl,shell,win,h,info

def stops(lay):return sorted(p.name for p in lay.control.glob('stop_*')) if lay.control.exists() else []
def saved(lay,key):
    """Value in settings.json on disk, None while the file is missing or being replaced."""
    try:return json.loads(lay.settings_file.read_text(encoding='utf-8')).get(key)
    except (OSError,ValueError):return None
def same(lay):return Health().same(lay)

# ---- window creation ----
def test_stopped_window_shows_status_html_with_profile_storage(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    assert win.url is None and win.html==W.read_html(W.STATUS_HTML) and shell.view=='status'
    assert win.title=='PumpFun PAPER '+lay.version+' — остановлен'
    assert isinstance(win.js_api,W.Api) and win.kw['background_color']=='#0b0e11' and (win.kw['width'],win.kw['height'])==W.SIZE
    shell.tray.start();shell.run()
    start=gui.webview.started[-1]
    assert start['private_mode'] is False and start['storage_path']==str(lay.webview_storage) and start['gui']=='edgechromium'
    assert lay.webview_storage==lay.profile/'WebView2' and lay.webview_storage.is_dir()
    assert 'http_server' not in start and not start.get('debug')
    assert lay.profile.is_relative_to(tmp_path/'profile')  # %LOCALAPPDATA%\PumpfunPaperDesktop\<identity>, never app/data
    shell.stop_polling()

@pytest.mark.parametrize('state,health,kind',[(dict(phase='RUNNING'),None,'OWN_RUNNING'),(dict(phase='STOP_REQUESTED'),None,'OWN_STOP_REQUESTED'),(None,same,'EXTERNAL_SAME')])
def test_running_bot_loads_dashboard_url_directly(tmp_path,monkeypatch,state,health,kind):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=state,health=health)
    assert shell.status.kind==kind and win.url=='http://127.0.0.1:8215/' and win.html is None and shell.view=='terminal'

def test_native_menu_matches_spec(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    menu=[(m.title,[i.title for i in m.items]) for m in win.menu]
    assert menu==[('Бот',['Запустить','Остановить…']),('Отчёты',['Результаты (05)','Проверка (04)','Открыть папку results']),
        ('Сервис',['Настройки (02, консоль)','Сохранить данные (08, консоль)','Журнал','Открыть папку журналов']),
        ('Окно',['Терминал','Состояние','Свернуть в трей'])]
    for m in win.menu:
        for item in m.items:assert callable(item.function) and not inspect.signature(item.function).parameters

def test_unavailable_menu_action_shows_reason(tmp_path,monkeypatch):
    popen=FakePopen()
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'),popen=popen)
    item={i.title:i for m in win.menu for i in m.items}
    item['Результаты (05)'].function();item['Запустить'].function();item['Настройки (02, консоль)'].function()
    assert [t for t,_ in info.calls]==['Действие недоступно']*3 and all(m==C.TEXT['running'] for _,m in info.calls)
    assert popen.calls==[] and 'confirm' not in win.names()

# ---- view switching ----
def test_view_follows_state_and_progress(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    write_state(lay,phase='RUNNING');shell.poll()
    assert win.url==lay.url and win.names()[-2:]==['load_url','set_title'] and win.title.endswith('— работает')
    win.answers=[True];r=shell.do('stop',page=True)  # stop in progress: status page with the steps
    assert r['ok'] and shell.view=='status' and win.html is not None and r['status']['stop']['requested']
    write_state(lay,phase='STOPPED',exit_code=0,finished_ts=5.);shell.poll()
    assert shell.view=='status' and shell.status.kind=='STOPPED'
    write_state(lay,phase='RUNNING',token='ab'*16);shell.poll();assert shell.view=='terminal'
    shell.do('show_status');assert shell.view=='status' and shell.pinned_status
    shell.poll();assert shell.view=='status'  # stays on the status page while pinned
    shell.do('show_terminal');assert shell.view=='terminal' and not shell.pinned_status

def test_show_terminal_refused_when_not_running(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    r=shell.do('show_terminal',page=True)
    assert not r['ok'] and r['message']=='Терминал доступен, когда бот работает.' and shell.view=='status' and info.calls==[]

# ---- closing, tray, quit (2.6) ----
@pytest.mark.parametrize('state,health',[(dict(phase='RUNNING'),None),(dict(phase='STARTING'),None),(None,same)])
def test_closing_while_bot_runs_hides_to_tray(tmp_path,monkeypatch,state,health):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=state,health=health);shell.tray.start()
    assert win.user_close() is False and not win.destroyed
    # MINOR 6 round 2: to_tray() runs in its own thread and writes settings.json after hide(): wait for the file
    wait_for(lambda:not win.visible and shell.hidden and saved(lay,'tray_notice_shown') is True,5,.02,'hide + settings.json')
    assert 'hide' in win.names() and 'destroy' not in win.names() and stops(lay)==[]
    icon=gui.pystray.icons[0]
    assert icon.notes==[('PumpFun PAPER',C.TEXT['tray_notice'])] and shell.settings.get('tray_notice_shown') is True
    shell.hidden=False;win.visible=True;win.user_close();wait_for(lambda:not win.visible and shell.hidden,5,.02,'hide 2')
    assert len(icon.notes)==1  # the tray notice is shown once
    icon.item('Показать окно')(icon);assert win.visible and win.names()[-2:]==['show','restore'] and not shell.hidden

def test_closing_when_stopped_quits(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch);shell.tray.start()
    assert win.user_close() is True and win.destroyed and shell.allow_quit
    assert gui.pystray.icons[0].stopped==1 and stops(lay)==[]
    assert json.loads(lay.settings_file.read_text(encoding='utf-8'))['geometry']==dict(x=1280,y=640,width=1280,height=820)

def test_closing_during_background_action_hides(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    assert shell.do('results',page=True)['ok']
    assert win.user_close() is False;wait_for(lambda:not win.visible,5,.02,'hide')

@pytest.mark.parametrize('answer',[False,True])
def test_tray_quit_with_running_bot_never_writes_stop_file(tmp_path,monkeypatch,answer):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'));shell.tray.start()
    icon=gui.pystray.icons[0];win.answers=[answer]
    icon.item('Выйти из окна')(icon)
    assert win.calls[0][0]=='confirm' and win.calls[0][2]==C.TEXT['quit_confirm'] and stops(lay)==[]
    if answer:assert win.destroyed and shell.allow_quit and icon.stopped==1 and win.names().count('destroy')==1
    else:assert not win.destroyed and not shell.allow_quit and icon.stopped==0
    assert C.engine.read_state(lay.control)['phase']=='RUNNING'

def test_tray_quit_when_stopped_needs_no_confirmation(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch);shell.tray.start()
    icon=gui.pystray.icons[0];icon.item('Выйти из окна')(icon)
    assert 'confirm' not in win.names() and win.destroyed and stops(lay)==[]

# ---- stop (2.3) ----
def test_stop_refused_in_dialog_does_nothing(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'))
    win.answers=[False];r=shell.do('stop',page=True)
    assert win.calls[0]==('confirm','Остановить PAPER-бота',C.TEXT['stop_confirm'])
    assert r['ok'] is False and r.get('cancelled') and r['message'] is None and stops(lay)==[] and ctl._stop is None

@pytest.mark.parametrize('source',['page','menu','tray'])
def test_stop_accepted_writes_exactly_one_stop_file(tmp_path,monkeypatch,source):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'));shell.tray.start()
    win.answers=[True]
    if source=='page':win.js_api.stop()
    elif source=='menu':[i for m in win.menu for i in m.items if i.title=='Остановить…'][0].function()
    else:icon=gui.pystray.icons[0];icon.item('Остановить бота…')(icon)
    assert stops(lay)==['stop_'+TOKEN+'.request'] and win.names().count('confirm')==1
    # a second request is not possible: the stop is in progress
    r=shell.do('stop',page=True);assert not r['ok'] and r['message']==C.TEXT['stopping'] and win.names().count('confirm')==1
    assert stops(lay)==['stop_'+TOKEN+'.request'] and info.calls==[]

@pytest.mark.parametrize('state,health,reason',[(None,None,C.TEXT['not_running']),(None,same,C.TEXT['external_same']),(dict(phase='STARTING'),None,C.TEXT['starting'])])
def test_stop_unavailable_shows_no_confirmation(tmp_path,monkeypatch,state,health,reason):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=state,health=health)
    r=shell.do('stop')
    assert not r['ok'] and r['message']==reason and info.calls==[('Действие недоступно',reason)] and 'confirm' not in win.names() and stops(lay)==[]

def test_stop_progress_notification(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING',log_name=None));shell.tray.start()
    win.answers=[True];shell.do('stop',page=True)
    write_state(lay,phase='STOPPED',exit_code=0);shell.poll()
    assert gui.pystray.icons[0].notes[-1]==('PumpFun PAPER',C.TEXT['stop_ok'])

# ---- start / ENGINE_LOST ----
def test_start_spawns_host_and_shows_progress(tmp_path,monkeypatch):
    popen=FakePopen()
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,popen=popen)
    r=win.js_api.start()
    assert r['ok'] and len(popen.calls)==1 and popen.calls[0]['args'][-3:]==['run','--token',r['token']]
    assert r['status']['busy']=='start' and r['status']['tray']=='yellow' and shell.view=='status'

def test_engine_lost_needs_acknowledgement_before_start(tmp_path,monkeypatch):
    popen=FakePopen();procs=Procs();procs.rows.clear()
    monkeypatch.setattr(T.Tray,'NOTICE_DELAY',0)
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'),procs=procs,popen=popen);shell.tray.start()
    assert shell.status.kind=='ENGINE_LOST' and gui.pystray.icons[0].icon.path.name=='app_error.ico'
    assert ('PumpFun PAPER',C.TEXT['lost']) in gui.pystray.icons[0].notes  # seen before the tray started: shown on start
    win.answers=[False];r=shell.do('start',page=True)
    assert not r['ok'] and r['cancelled'] and popen.calls==[] and not shell.status.lost_acked
    win.answers=[True];r=shell.do('start',page=True)
    assert r['ok'] and r['acknowledged'] and popen.calls==[] and r['status']['lost_acked'] and r['status']['allowed']['start']==[True,None]
    r=shell.do('start',page=True);assert r['ok'] and len(popen.calls)==1

# ---- tray (2.5) ----
def test_tray_icon_tip_and_menu_follow_state(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch);shell.tray.start();shell.poll()
    icon=gui.pystray.icons[0]
    assert icon.detached==1 and icon.name=='PumpfunPaperDesktop'
    def check(kind,ico,text,stop):
        s=shell.status;assert s.kind==kind,s.kind
        assert icon.icon.path.name==ico and icon.title=='PumpFun PAPER — '+text
        assert icon.item('Состояние: ').text=='Состояние: '+text and icon.item('Состояние: ').enabled is False
        assert icon.item('Остановить бота…').enabled is stop
    check('STOPPED','app_idle.ico','остановлен',False)
    write_state(lay,phase='STARTING');shell.poll();check('OWN_STARTING','app_busy.ico','запуск',False)
    write_state(lay,phase='RUNNING');shell.poll();check('OWN_RUNNING','app_running.ico','работает',True)
    write_state(lay,phase='STOP_REQUESTED');shell.poll();check('OWN_STOP_REQUESTED','app_busy.ico','останавливается',False)
    write_state(lay,phase='FAILED',exit_code=1,error_code='X');shell.poll();check('STOPPED','app_error.ico','остановлен',False)
    (lay.control/C.STATE_NAME).unlink();h.foreign(lay,version='v0');shell.poll()
    check('EXTERNAL_FOREIGN','app_error.ico','порт занят другой установкой',False)
    h.same(lay);shell.poll();check('EXTERNAL_SAME','app_running.ico','работает (03_START)',False)
    assert icon.menu_updates>=6 and [i.text for i in icon.menu.items if i is not gui.pystray.Menu.SEPARATOR][0]=='Показать окно'
    assert icon.menu.items[0].default and not icon.menu.items[-1].default

def test_tray_assets_are_real_multi_size_icons():
    for state,name in T.ICONS.items():
        data=(DESKTOP/'assets'/name).read_bytes()
        reserved,kind,count=struct.unpack('<HHH',data[:6]);assert (reserved,kind)==(0,1) and count>=4,name
        sizes={(data[6+16*i] or 256) for i in range(count)}
        assert {16,24,32,48,256}<=sizes,(name,sizes)
    assert set(T.ICONS)=={'grey','green','yellow','red'} and set(T.ICONS.values())=={'app_idle.ico','app_running.ico','app_busy.ico','app_error.ico'}

def test_tray_loader_falls_back_to_drawing(tmp_path,monkeypatch):
    gui=fake_gui.install(monkeypatch)
    img=T.load_image('green',folder=tmp_path)
    assert img.path is None and img.size==(64,64)

# ---- js_api ----
def test_js_api_fixed_argumentless_methods(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    api=win.js_api
    public=[n for n in dir(api) if not n.startswith('_')]
    assert tuple(sorted(public))==tuple(sorted(C.JS_API))
    for name in public:
        fn=getattr(api,name);assert inspect.ismethod(fn) and not inspect.signature(fn).parameters,name
        with pytest.raises(TypeError):fn('C:\\Windows\\System32\\cmd.exe')
    log=W.LogApi(shell);assert sorted(n for n in dir(log) if not n.startswith('_'))==['get_log','open_logs','open_results_folder']
    for name in ('get_log','open_logs','open_results_folder'):assert not inspect.signature(getattr(log,name)).parameters
    s=api.get_status();json.dumps(s)
    assert s['kind']=='STOPPED' and s['paper'] is True and s['dev'] is False and s['title']==win.title

def test_js_api_folders_and_consoles(tmp_path,monkeypatch):
    opened=[];popen=FakePopen()
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,popen=popen,startfile=opened.append)
    r=win.js_api.open_results_folder();assert not r['ok'] and r['message'].startswith('Папка ещё не создана') and info.calls==[]
    lay.results.mkdir();assert win.js_api.open_results_folder()['ok'] and opened==[str(lay.results)]
    ev=lay.results/'run_1';ev.mkdir();ctl.last_action=dict(kind='results',evidence=str(ev),log='x')
    assert win.js_api.open_results_folder()['ok'] and opened[-1]==str(ev.resolve())
    ctl.last_action=dict(kind='results',evidence=str(tmp_path),log='x')  # outside results/: never opened
    assert win.js_api.open_results_folder()['ok'] and opened[-1]==str(lay.results)
    lay.logs.mkdir(parents=True);assert win.js_api.open_logs()['ok'] and opened[-1]==str(lay.logs)
    assert win.js_api.open_settings()['ok'] and win.js_api.open_preserve()['ok']
    assert [c['args'].endswith('02_SETTINGS.bat"') for c in popen.calls]==[True,False] and popen.calls[1]['args'].endswith('08_PRESERVE_DATA.bat"')
    r=win.js_api.run_check();assert r['ok'] and r['status']['busy']=='check'
    r=win.js_api.run_results();assert not r['ok'] and r['message']==C.TEXT['busy'].format(action='check')

def test_log_window(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='STOPPED',exit_code=0,log_name='engine_20261102_120000_01234567.log'))
    lay.logs.mkdir(parents=True);(lay.logs/'engine_20261102_120000_01234567.log').write_text('line 1\nSQLITE SHUTDOWN AUDIT: ok\n',encoding='utf-8')
    assert shell.do('show_log')['ok']
    lw=gui.webview.windows[-1];assert lw is shell.log_window and lw.title==W.LOG_TITLE and lw.html==W.read_html(W.LOG_HTML)
    r=lw.js_api.get_log();assert r['name']=='engine_20261102_120000_01234567.log' and 'SQLITE SHUTDOWN AUDIT: ok' in r['text']
    shell.do('show_log');assert len(gui.webview.windows)==2 and lw.names()[-2:]==['show','restore']
    lw.user_close();assert shell.log_window is None

# ---- static checks of the pages ----
@pytest.mark.parametrize('name',['status.html','log.html'])
def test_pages_are_offline(name):
    text=(DESKTOP/'ui'/name).read_text(encoding='utf-8')
    assert not re.search(r'https?:|//[a-z0-9]|\bsrc\s*=|<link|@import|url\(|<iframe|fetch\(|XMLHttpRequest|WebSocket|eval\(',text,re.I),name
    assert 'innerHTML' not in text and "default-src 'none'" in text and 'PAPER' in text
    calls=set(re.findall(r'data-api="([a-z_]+)"',text))|set(re.findall(r'\bp\.([a-z_]+)\(',text))
    allowed=set(C.JS_API) if name=='status.html' else {'get_log','open_logs','open_results_folder'}
    assert calls and calls<=allowed,calls-allowed

def test_status_page_buttons_match_actions():
    text=(DESKTOP/'ui'/'status.html').read_text(encoding='utf-8')
    pairs=dict(re.findall(r'data-api="([a-z_]+)" data-act="([a-z_]+)"',text))
    assert set(pairs)==set(C.JS_API)-{'get_status'} and set(pairs.values())<=set(C.ACTIONS)

# ---- dev mode and app_main ----
@pytest.fixture
def checkout(monkeypatch):
    """BLOCKER round 2: dev mode exists only in a developer checkout. Tests of dev mode say they run in one, so
    they behave the same in a ZIP/installed release (no .git), where 04_CHECK runs them."""
    monkeypatch.setattr(app_main,'dev_checkout',lambda root=None:True)

def test_dev_override_only_test_ports(tmp_path,checkout):
    assert app_main.make_layout({})[1] is False and app_main.make_layout({})[0].port==8188
    lay,dev=app_main.make_layout({app_main.DEV_PORT:'8212',app_main.DEV_DB:str(tmp_path/'t.db'),app_main.DEV_CONTROL:str(tmp_path/'c'),app_main.DEV_PROFILE:str(tmp_path/'p')})
    assert dev and lay.port==8212 and lay.url=='http://127.0.0.1:8212/' and lay.db==(tmp_path/'t.db').resolve() and lay.control==tmp_path/'c'
    assert lay.profile.parent==tmp_path/'p'
    for bad in ('8188','49190','8209','8220','x','80'):
        with pytest.raises(ValueError):app_main.make_layout({app_main.DEV_PORT:bad})
    lay,dev=app_main.make_layout({app_main.DEV_DB:str(tmp_path/'t.db')})  # without the port nothing is overridden
    assert not dev and lay.port==8188 and lay.db!=(tmp_path/'t.db').resolve()

def test_dev_mode_refuses_engine_actions(tmp_path,monkeypatch):
    popen=FakePopen()
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,dev=True,popen=popen)
    assert win.title.endswith(' [DEV]') and shell.page_status()['dev_text']==W.TEXT['dev']
    for name in ('start','run_results','run_check','open_settings','open_preserve'):
        r=getattr(win.js_api,name)();assert not r['ok'] and r['message']==W.TEXT['dev'],name
    assert popen.calls==[]

class FakeInstance:
    acquired=True;made=[]
    def __init__(self,identity):self.identity=identity;self.closed=False;self.listener=None;FakeInstance.made.append(self)
    def acquire(self):return FakeInstance.acquired
    def listen(self,callback):self.listener=callback
    def close(self):self.closed=True

def run_main(tmp_path,monkeypatch,acquired=True,dev=True,short=None,no_gui=False,on_ready=None,checkout=True,aumid=None):
    """app_main.main with fakes. checkout: what app_main.dev_checkout answers (never the real ROOT/.git, so the
    result is the same in a release folder); aumid: list that records the AppUserModelID calls (never the real one)."""
    from desktop import paths,single_instance,session_end
    gui=fake_gui.install(monkeypatch);asked=[]
    monkeypatch.setattr(app_main,'dev_checkout',lambda root=None:checkout)
    monkeypatch.setattr(app_main,'set_app_user_model_id',(aumid if aumid is not None else []).append)
    if no_gui:monkeypatch.setitem(sys.modules,'webview',None)
    FakeInstance.acquired=acquired;FakeInstance.made=[]
    monkeypatch.setattr(single_instance,'SingleInstance',FakeInstance)
    monkeypatch.setattr(paths,'short_path_problem',short or (lambda *a,**k:None))
    class NoHidden:
        def __init__(self,guard):self.guard=guard;self.updates=0
        def start(self):return 1
        def update(self,status):self.updates+=1
        def close(self):pass
    monkeypatch.setattr(session_end,'HiddenWindow',NoHidden);monkeypatch.setattr(session_end,'User32Api',lambda:None)
    order=[]
    real_start=gui.webview.start
    def start(**k):order.append(('start',gui.pystray.icons[0].detached));real_start(**k)
    gui.webview.start=start
    env={app_main.DEV_PORT:'8215',app_main.DEV_DB:str(tmp_path/'t.db'),app_main.DEV_CONTROL:str(tmp_path/'c'),app_main.DEV_PROFILE:str(tmp_path/'p')} if dev else {}
    def ask(text,yes):asked.append((text,yes));return False
    code=app_main.main(env=env,ask=ask,gui=None if no_gui else (gui.webview,gui.pystray),on_ready=on_ready)
    return code,gui,asked,order

def test_main_dev_run_order_and_cleanup(tmp_path,monkeypatch):
    ready=[]
    code,gui,asked,order=run_main(tmp_path,monkeypatch,on_ready=ready.append)
    assert code==0 and asked==[] and order==[('start',1)]  # tray run_detached before webview.start
    win=gui.webview.windows[0];assert win.title.endswith('[DEV]') and win.html is not None
    start=gui.webview.started[0];assert start['private_mode'] is False and Path(start['storage_path']).is_relative_to(tmp_path/'p')
    inst=FakeInstance.made[0];assert inst.closed and inst.listener is not None
    wait_for(lambda:ready,5,.02,'on_ready');shell=ready[0]
    assert shell.session is not None and gui.pystray.icons[0].stopped==1 and shell._halt.is_set()
    inst.listener();assert win.names()[-2:]==['show','restore']  # second instance -> show the first window
    assert not (tmp_path/'c').exists() or not list((tmp_path/'c').glob('stop_*'))

def test_main_second_instance_exits_zero(tmp_path,monkeypatch):
    code,gui,asked,order=run_main(tmp_path,monkeypatch,acquired=False)
    assert code==0 and gui.webview.windows==[] and gui.pystray.icons==[] and asked==[]

def test_main_refusals(tmp_path,monkeypatch):
    code,gui,asked,order=run_main(tmp_path,monkeypatch,short=lambda *a,**k:'DESKTOP_PATH_TOO_LONG: x')
    assert code==1 and asked==[('DESKTOP_PATH_TOO_LONG: x',False)] and gui.webview.windows==[] and FakeInstance.made==[]
    code=app_main.main(env={app_main.DEV_PORT:'8188'},ask=lambda t,y:asked.append(t))
    assert code==2 and asked[-1]==app_main.TEXT['dev_port']

def test_main_release_check_failure(tmp_path,monkeypatch):
    from desktop import release_check
    def verify(root=None):raise RuntimeError('DESKTOP_EXTRA_MODULE: desktop/evil.py')
    monkeypatch.setattr(release_check,'verify',verify)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    code,gui,asked,order=run_main(tmp_path,monkeypatch,dev=False)
    assert code==1 and 'DESKTOP_EXTRA_MODULE: desktop/evil.py' in asked[0][0] and gui.webview.windows==[] and FakeInstance.made==[]

def test_main_without_gui_offers_browser(tmp_path,monkeypatch):
    opened=[];import webbrowser
    monkeypatch.setattr(webbrowser,'open',opened.append)
    code,gui,asked,order=run_main(tmp_path,monkeypatch,no_gui=True)
    assert code==1 and asked[0][1] is True and 'http://127.0.0.1:8215/' in asked[0][0] and opened==[] and FakeInstance.made[0].closed

def test_shell_modules_import_no_gui_library():
    code=('import sys;sys.path.insert(0,sys.argv[1])\n'
        'import desktop.window,desktop.tray,desktop.app_main\n'
        'bad=[m for m in ("webview","pystray","PIL","clr","config","launcher","main","dotenv") if m in sys.modules]\n'
        'print("BAD",bad);raise SystemExit(1 if bad else 0)')
    result=subprocess.run([sys.executable,'-I','-c',code,str(ROOT)],capture_output=True,text=True,errors='replace',timeout=60)
    assert result.returncode==0,result.stdout+result.stderr

def test_geometry_restored_only_on_screen(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    assert win.kw['x']==(3840-1280)//2 and win.kw['y']==(2100-820)//2  # no saved geometry: centred
    small=lambda:((0,0,1280,760),(0,0,1280,800))  # 1600x1000 physical at 125 %
    assert shell.geometry(small)==dict(width=1203,height=714,x=38,y=23)
    assert shell.geometry(lambda:((0,0,700,400),(0,0,700,440)))==dict(width=720,height=480,x=0,y=0)
    assert shell.geometry(lambda:(_ for _ in ()).throw(OSError()))==dict(width=1280,height=820)
    shell.settings.update(geometry=dict(x=50,y=40,width=1000,height=700))
    big=lambda:((0,0,1920,1040),(0,0,1920,1080))
    assert shell.geometry(big)==dict(width=1000,height=700,x=50,y=40)
    assert shell.geometry(lambda:((0,0,40,40),(0,0,600,600)))==dict(width=600,height=600,x=50,y=40)
    shell.settings.update(geometry=dict(x=5000,y=40,width=1000,height=700))
    assert shell.geometry(big)==dict(width=1000,height=700)  # monitor gone: size only
    win.x=-32000;assert shell.save_geometry() is False

def test_requirements_pinned_with_hashes():
    text=(DESKTOP/'requirements-desktop.txt').read_text(encoding='utf-8')
    reqs=re.findall(r'^([A-Za-z0-9_.-]+)==([0-9][0-9A-Za-z.]*) \\\n    --hash=sha256:([0-9a-f]{64})$',text,re.M)
    lines=[l for l in text.splitlines() if l.strip() and not l.startswith('#')]
    assert len(lines)==2*len(reqs)  # every requirement is pinned and hashed, nothing else
    names={n.lower() for n,_,_ in reqs}
    assert {'pywebview','pythonnet','clr-loader','pystray','pillow','psutil','bottle','proxy-tools','typing-extensions','cffi','pycparser','six'}==names
    assert dict((n.lower(),v) for n,v,_ in reqs)['pywebview']=='6.2.1' and not names&{'pytest','pluggy','iniconfig'}

def test_unknown_action_is_refused(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    r=shell.do('rm_rf',page=True);assert not r['ok'] and r['message']=='Неизвестное действие.'


# ---- M1 ORPHAN_RUNNING in the shell ----
def test_orphan_running_menu_tray_and_view(tmp_path,monkeypatch):
    popen=FakePopen();procs=Procs();procs.rows.clear();opened=[]
    monkeypatch.setattr(T.Tray,'NOTICE_DELAY',0)
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING',log_name='engine_20261102_120000_01234567.log'),
        health=same,procs=procs,popen=popen,startfile=opened.append)
    shell.tray.start();icon=gui.pystray.icons[0]
    assert shell.status.kind=='ORPHAN_RUNNING' and shell.view=='status' and win.url is None  # the warning first, not the dashboard
    assert icon.icon.path.name=='app_error.ico' and icon.title=='PumpFun PAPER — бот работает без управляющего процесса'
    assert icon.item('Остановить бота…').enabled is False and icon.item('Состояние: ').text=='Состояние: бот работает без управляющего процесса'
    assert icon.notes.count(('PumpFun PAPER',C.TEXT['orphan']))==1
    snap=win.js_api.get_status();assert snap['orphan']==C.TEXT['orphan'] and snap['tray']=='red' and win.title.endswith('бот работает без управляющего процесса')
    item={i.title:i for m in win.menu for i in m.items}
    for name in ('Запустить','Остановить…','Результаты (05)','Проверка (04)','Настройки (02, консоль)','Сохранить данные (08, консоль)'):item[name].function()
    assert info.calls==[('Действие недоступно',C.TEXT['orphan'])]*6
    assert popen.calls==[] and stops(lay)==[] and 'confirm' not in win.names()
    for name in ('start','stop','run_results','run_check','open_settings','open_preserve'):
        r=getattr(win.js_api,name)();assert not r['ok'] and r['message']==C.TEXT['orphan'],name
    assert len(info.calls)==6 and popen.calls==[] and stops(lay)==[]  # the page gets the reason, no dialog
    item['Терминал'].function();assert shell.view=='terminal' and win.url==lay.url
    shell.poll();assert shell.view=='terminal'  # chosen by the user: stays
    item['Состояние'].function();assert shell.view=='status'
    lay.logs.mkdir(parents=True);(lay.logs/'engine_20261102_120000_01234567.log').write_text('x\n',encoding='utf-8');lay.results.mkdir()
    item['Журнал'].function();assert shell.log_window is not None
    item['Открыть папку журналов'].function();item['Открыть папку results'].function();assert opened==[str(lay.logs),str(lay.results)]
    for _ in range(3):shell.poll()
    assert icon.notes.count(('PumpFun PAPER',C.TEXT['orphan']))==1  # once on entry
    h.data=None;shell.poll();assert shell.status.kind=='ORPHAN_RUNNING'  # one miss: held
    shell.poll();assert shell.status.kind=='ENGINE_LOST' and shell.view=='status' and ('PumpFun PAPER',C.TEXT['lost']) in icon.notes
    assert icon.icon.path.name=='app_error.ico' and len(info.calls)==6

def test_orphan_close_hides_without_stop_file(tmp_path,monkeypatch):
    procs=Procs();procs.rows.clear()
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'),health=same,procs=procs);shell.tray.start()
    shell.shutting_down=lambda:False
    assert win.user_close() is False;wait_for(lambda:not win.visible,5,.02,'hide')
    assert stops(lay)==[] and not win.destroyed

# ---- M3 / MINOR 4 / MINOR 5: closing during log-off ----
class FakeSession:
    """HiddenWindow without a real window: update() feeds the guard like HiddenWindow.update."""
    def __init__(self,guard):self.guard=guard;self.updates=0
    def update(self,status):self.updates+=1;self.guard.observe(status)
def with_guard(shell,clock=None):
    from desktop import session_end as se
    class Api:
        def __init__(self):self.calls=[]
        def block(self,hwnd,text):self.calls.append(('block',text));return True
        def unblock(self,hwnd):self.calls.append(('unblock',));return True
        def priority(self,level=0):return True
    guard=se.SessionEndGuard(shell.ctl.stop_for_session_end,Api(),clock=clock or Clock());shell.session=FakeSession(guard);shell.shutting_down=lambda:False
    return guard,se

@pytest.mark.parametrize('state,health,busy',[(None,same,None),(None,None,'results'),(dict(phase='STOPPED',exit_code=0),None,None),
    (dict(phase='FAILED',exit_code=1),None,None)])
def test_logoff_lets_the_window_close_without_toast(tmp_path,monkeypatch,state,health,busy):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=state,health=health);shell.tray.start()
    guard,se=with_guard(shell)
    if busy:assert shell.do(busy,page=True)['ok']
    assert guard.on_query(1,se.ENDSESSION_LOGOFF)==1
    assert win.user_close() is True and win.destroyed and shell.allow_quit
    assert gui.pystray.icons[0].notes==[] and 'hide' not in win.names() and stops(lay)==[] and not shell.hidden
    assert shell.settings.get('tray_notice_shown') is False

@pytest.mark.parametrize('phase',['RUNNING','STARTING','PREFLIGHT','STOP_REQUESTED'])
def test_logoff_in_held_states_cancels_silently(tmp_path,monkeypatch,phase):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase=phase));shell.tray.start()
    guard,se=with_guard(shell)
    assert guard.on_query(1,se.ENDSESSION_LOGOFF)==0 and guard.blocking
    assert win.user_close() is False and not win.destroyed
    time.sleep(.1);assert win.visible and 'hide' not in win.names() and gui.pystray.icons[0].notes==[]
    assert stops(lay)==([] if phase=='STOP_REQUESTED' else ['stop_'+TOKEN+'.request'])

def test_closing_before_the_guard_query_uses_sm_shuttingdown(tmp_path,monkeypatch):
    '''The form may hear the log-off first: SM_SHUTTINGDOWN already says so; held states still get their stop file.'''
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='STARTING'));shell.tray.start()
    shell.session=None;shell.shutting_down=lambda:True
    assert win.user_close() is False and stops(lay)==['stop_'+TOKEN+'.request'] and gui.pystray.icons[0].notes==[] and 'hide' not in win.names()
    gui2,lay2,ctl2,shell2,win2,h2,info2=make(tmp_path/'b',monkeypatch,health=same);shell2.tray.start()
    shell2.session=None;shell2.shutting_down=lambda:True
    assert win2.user_close() is True and stops(lay2)==[]
    gui3,lay3,ctl3,shell3,win3,h3,info3=make(tmp_path/'c',monkeypatch,health=same)
    def boom():raise OSError('x')
    shell3.shutting_down=boom;assert shell3.session_mode() is None  # a failing probe never blocks closing logic

@pytest.mark.parametrize('phase',['RUNNING','STARTING'])
def test_restart_manager_closes_the_window_and_leaves_the_campaign(tmp_path,monkeypatch,phase):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase=phase));shell.tray.start()
    guard,se=with_guard(shell)
    assert guard.on_query(1,se.ENDSESSION_CLOSEAPP)==1
    assert win.user_close() is True and win.destroyed and stops(lay)==[] and gui.pystray.icons[0].notes==[]
    assert C.engine.read_state(lay.control)['phase']==phase

def test_cancelled_logoff_restores_close_to_tray(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,health=same);shell.tray.start()
    guard,se=with_guard(shell)
    guard.on_query(1,se.ENDSESSION_LOGOFF);guard.on_end(1,False,se.ENDSESSION_LOGOFF)
    assert win.user_close() is False;wait_for(lambda:not win.visible and gui.pystray.icons[0].notes,5,.02,'hide + notice')
    assert gui.pystray.icons[0].notes==[('PumpFun PAPER',C.TEXT['tray_notice'])]

# ---- MINOR 2: run that ends by itself; MINOR 3: slow start ----
def test_self_stop_notifies_and_stays_red_until_acknowledged(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'));shell.tray.start();icon=gui.pystray.icons[0]
    shell.hidden=True;win.visible=False
    write_state(lay,phase='STOPPED',exit_code=5,finished_ts=9.);shell.poll();shell.poll()
    text='Бот остановился сам (код 5). Откройте журнал.'
    assert icon.notes.count(('PumpFun PAPER',text))==1 and icon.icon.path.name=='app_error.ico' and shell.page_status()['self_stop']==text
    assert shell.settings.get('self_stop_acked') is None
    icon.item('Показать окно')(icon);shell.poll()
    assert icon.icon.path.name=='app_idle.ico' and shell.page_status()['self_stop'] is None
    assert json.loads(lay.settings_file.read_text(encoding='utf-8'))['self_stop_acked']==TOKEN
    # a new window session remembers the acknowledgement: no second notice, grey icon
    gui2=fake_gui.install(monkeypatch);ctl2=controller(lay)
    st=settings_store.SettingsStore(lay.settings_file);st.load()
    shell2=W.Shell(ctl2,gui2.webview,settings=st,info=Info())
    shell2.tray=T.Tray(shell2.show_window,shell2.tray_stop,shell2.quit_window,pystray=gui2.pystray,spawn=lambda fn:fn())
    shell2.create(screen=lambda:((0,0,1920,1040),(0,0,1920,1080)));shell2.tray.start();shell2.poll()
    assert gui2.pystray.icons[0].notes==[] and gui2.pystray.icons[0].icon.path.name=='app_idle.ico'

def test_self_stop_acknowledged_by_log_or_start(tmp_path,monkeypatch):
    popen=FakePopen()
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='STOPPED',exit_code=2),popen=popen);shell.tray.start()
    assert shell.status.self_stop and gui.pystray.icons[0].icon.path.name=='app_error.ico'
    shell.do('show_log');shell.poll();assert shell.status.self_stop is None and shell.settings.get('self_stop_acked')==TOKEN
    gui2,lay2,ctl2,shell2,win2,h2,info2=make(tmp_path/'b',monkeypatch,state=dict(phase='STOPPED',exit_code=2),popen=FakePopen())
    assert shell2.status.self_stop
    r=win2.js_api.start();assert r['ok'] and shell2.settings.get('self_stop_acked')==TOKEN and r['status']['self_stop'] is None

def test_slow_start_notifies_once_and_keeps_waiting(tmp_path,monkeypatch):
    clock=Clock();popen=FakePopen()
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,clock=clock,popen=popen);shell.tray.start();icon=gui.pystray.icons[0]
    token=win.js_api.start()['token'];write_state(lay,token=token,phase='PREFLIGHT')
    clock.advance(121);shell.poll();clock.advance(60);snap=shell.poll()
    assert icon.notes.count(('PumpFun PAPER',C.TEXT['start_slow']))==1 and snap['busy']=='start' and snap['start']['slow'] and not snap['start']['done']
    assert icon.icon.path.name=='app_busy.ico' and not popen.procs[0].killed

# ---- MINOR 6: dev mode only in a developer checkout ----
def test_dev_variables_ignored_without_git(tmp_path):
    env={app_main.DEV_PORT:'8212',app_main.DEV_DB:str(tmp_path/'t.db'),app_main.DEV_CONTROL:str(tmp_path/'c')}
    root=tmp_path/'release';root.mkdir();warnings=[]
    lay,dev=app_main.make_layout(env,root=root,warnings=warnings)
    assert not dev and lay.port==8188 and lay.db!=(tmp_path/'t.db').resolve() and len(warnings)==1
    assert 'PFDESK_DEV_DASHBOARD_PORT' in warnings[0] and 'PFDESK_DEV_DB' in warnings[0] and str(root) in warnings[0]
    w2=[];assert app_main.make_layout({app_main.DEV_PORT:'8188'},root=root,warnings=w2)[1] is False and len(w2)==1  # ignored, not even parsed
    assert app_main.make_layout({},root=root,warnings=w2)[1] is False and len(w2)==1  # no variables: no warning
    (root/'.git').write_text('gitdir: x\n',encoding='utf-8')  # a git worktree has a .git file
    lay,dev=app_main.make_layout(env,root=root);assert dev and lay.port==8212
    assert app_main.dev_checkout(ROOT) is (ROOT/'.git').exists()

def test_main_ignores_dev_mode_in_a_release_and_logs_it(tmp_path,monkeypatch):
    from desktop import release_check
    checked=[]
    def verify(root=None):checked.append(root);raise RuntimeError('CLEAN_RELEASE_FILE_CHANGED: app/x.py')
    monkeypatch.setattr(release_check,'verify',verify)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    code,gui,asked,order=run_main(tmp_path,monkeypatch,dev=True,checkout=False)
    assert code==1 and checked and 'CLEAN_RELEASE_FILE_CHANGED' in asked[0][0] and gui.webview.windows==[]  # release check ran
    logs_found=list((tmp_path/'local').rglob('window.log'));assert len(logs_found)==1
    text=logs_found[0].read_text(encoding='utf-8');assert 'DESKTOP WARNING: PFDESK_DEV_* ignored' in text and 'PFDESK_DEV_DASHBOARD_PORT' in text

# ---- E2E round 2: owned Yes/No confirmation, one at a time ----
class BlockingBox:
    """Confirmation that stays open until release(); records every opening (like a real modal box)."""
    def __init__(self,answer):self.answer=answer;self.opened=[];self.is_open=threading.Event();self.go=threading.Event()
    def __call__(self,window,title,text):
        self.opened.append((window,title,text));self.is_open.set()
        assert self.go.wait(5),'box never answered'
        return self.answer

@pytest.mark.parametrize('answer',[True,False])
def test_stop_confirmation_is_not_reentrant(tmp_path,monkeypatch,answer):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'));shell.tray.start()
    box=BlockingBox(answer);shell.confirm_box=box;first=[]
    t=threading.Thread(target=lambda:first.append(shell.do('stop')),daemon=True);t.start()
    assert box.is_open.wait(5) and box.opened[0][0] is win and box.opened[0][1]==W.TEXT['stop_title']
    # while it is open: menu, page, tray Stop and tray quit open nothing and write nothing
    r=[i for m in win.menu for i in m.items if i.title=='Остановить…'][0].function()
    r=shell.do('stop');assert not r['ok'] and r['asking'] and r['message']==W.TEXT['asking']
    r=win.js_api.stop();assert not r['ok'] and r['asking'] and r['message']==W.TEXT['asking']
    icon=gui.pystray.icons[0];icon.item('Остановить бота…')(icon)
    assert shell.quit_window() is False and not win.destroyed
    assert len(box.opened)==1 and stops(lay)==[] and info.calls==[]  # no 'unavailable' dialog either
    box.go.set();t.join(5);assert not t.is_alive()
    assert stops(lay)==(['stop_'+TOKEN+'.request'] if answer else [])
    assert first[0]['ok'] is answer and (first[0].get('cancelled') is (None if answer else True))
    if not answer:  # the box is free again: a new Stop asks again
        box2=BlockingBox(True);box2.go.set();shell.confirm_box=box2
        assert shell.do('stop',page=True)['ok'] and len(box2.opened)==1 and stops(lay)==['stop_'+TOKEN+'.request']

def test_shell_uses_native_yes_no_box_without_the_fake(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch)
    assert shell.confirm_box is gui.webview.PFDESK_CONFIRM
    del gui.webview.PFDESK_CONFIRM
    assert W.Shell(ctl,gui.webview,info=Info()).confirm_box is W.native_confirm

class FakeHandle:
    def __init__(self,value):self.value=value
    def ToInt64(self):return self.value
class NativeWindow:
    def __init__(self,handle=None,title='PumpFun PAPER x'):
        self.title=title
        if handle is not None:self.native=type('Form',(),{'Handle':FakeHandle(handle)})()

@pytest.mark.parametrize('result,expected',[(6,True),(7,False),(2,False),(0,False)])
def test_native_confirm_is_an_owned_yes_no_warning_box(result,expected):
    calls=[]
    def api(owner,text,title,flags):calls.append((owner,text,title,flags));return result
    assert W.native_confirm(NativeWindow(0x1234),'Остановить PAPER-бота','текст',api=api) is expected
    MB_YESNO,MB_ICONWARNING,MB_DEFBUTTON2,MB_SETFOREGROUND=0x4,0x30,0x100,0x10000
    assert calls==[(0x1234,'текст','Остановить PAPER-бота',MB_YESNO|MB_ICONWARNING|MB_DEFBUTTON2|MB_SETFOREGROUND)]
    assert W.IDYES==6 and not calls[0][3]&W.MB_TOPMOST

def test_native_confirm_owner_fallbacks():
    assert W.window_handle(NativeWindow(0x55))==0x55
    assert W.window_handle(NativeWindow(None,title='T'),find=lambda title:{'T':0x77}.get(title))==0x77  # no .native
    assert W.window_handle(NativeWindow(0,title='T'),find=lambda title:None) is None
    calls=[];api=lambda owner,text,title,flags:calls.append((owner,flags)) or 6
    assert W.native_confirm(None,'t','x',api=api) is True and calls==[(None,W.CONFIRM_FLAGS|W.MB_TOPMOST)]  # no owner: top-most
    assert W.native_confirm(None,'t','x',api=api,owner=0x99) is True and calls[-1]==(0x99,W.CONFIRM_FLAGS)

# ---- E2E round 2: own icon and AppUserModelID (not Python's) ----
def test_window_gets_the_app_icon(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch);shell.tray.start();shell.run()
    start=gui.webview.started[-1]
    assert start['icon']==str(DESKTOP/'assets'/'app_idle.ico') and Path(start['icon']).is_file() and W.APP_ICON.name=='app_idle.ico'
    shell.stop_polling()

def test_main_sets_app_user_model_id_before_any_window(tmp_path,monkeypatch):
    class Record(list):
        def append(self,value):super().append((value,len(sys.modules['webview'].windows),len(sys.modules['pystray'].icons)))
    calls=Record()
    code,gui,asked,order=run_main(tmp_path,monkeypatch,aumid=calls)
    assert code==0 and calls==[('PumpfunPaper.Desktop',0,0)] and C.APP_USER_MODEL_ID=='PumpfunPaper.Desktop'
    calls2=Record()
    code,gui,asked,order=run_main(tmp_path/'b',monkeypatch,short=lambda *a,**k:'DESKTOP_PATH_TOO_LONG: x',aumid=calls2)
    assert code==1 and calls2==[('PumpfunPaper.Desktop',0,0)]  # also the error boxes before the window

@pytest.mark.skipif(sys.platform!='win32',reason='shell32')
def test_set_app_user_model_id_real_call_in_a_child_process():
    code=('import sys,ctypes;sys.path.insert(0,sys.argv[1]);from desktop import app_main\n'
        'ok=app_main.set_app_user_model_id("PumpfunPaper.Desktop")\n'
        'p=ctypes.c_wchar_p();ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(p))\n'
        'print(ok,p.value)')
    r=subprocess.run([sys.executable,'-I','-c',code,str(ROOT)],capture_output=True,text=True,errors='replace',timeout=60)
    assert r.returncode==0 and r.stdout.split()==['True','PumpfunPaper.Desktop'],r.stdout+r.stderr

# ---- MINOR 2 round 2: a stray WM_QUERYENDSESSION does not turn a later X into a stop ----
def test_stray_query_end_session_expires_before_a_later_close(tmp_path,monkeypatch):
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch);shell.tray.start()
    clock=Clock();guard,se=with_guard(shell,clock)
    assert guard.on_query(1,se.ENDSESSION_LOGOFF)==1  # a third-party broadcast while idle; no WM_ENDSESSION follows
    write_state(lay,phase='RUNNING');shell.poll();assert shell.status.kind=='OWN_RUNNING'
    clock.advance(se.MODE_TTL_S);assert shell.session_mode() is None
    assert win.user_close() is False;wait_for(lambda:not win.visible and shell.hidden,5,.02,'hide')
    assert stops(lay)==[] and not win.destroyed and C.engine.read_state(lay.control)['phase']=='RUNNING'

def test_held_query_mode_ends_with_the_campaign(tmp_path,monkeypatch):
    from fakes import OTHER
    gui,lay,ctl,shell,win,h,info=make(tmp_path,monkeypatch,state=dict(phase='RUNNING'));shell.tray.start()
    clock=Clock();guard,se=with_guard(shell,clock)
    assert guard.on_query(1,se.ENDSESSION_LOGOFF)==0 and stops(lay)==['stop_'+TOKEN+'.request']
    write_state(lay,phase='STOPPED',exit_code=0);shell.poll();assert guard.mode() is None  # before the TTL
    write_state(lay,phase='RUNNING',token=OTHER);shell.poll();assert shell.status.kind=='OWN_RUNNING'
    assert win.user_close() is False;wait_for(lambda:not win.visible and shell.hidden,5,.02,'hide')
    assert stops(lay)==['stop_'+TOKEN+'.request']  # nothing for the new run
