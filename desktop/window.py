"""GUI shell over the WP2 Controller (spec 2.2-2.6): one pywebview window, native menu, js_api, close-to-tray,
tray, log window, notifications. pywebview is passed in (app_main imports it; tests pass fake_gui), so this
module imports no GUI library itself.

- Bot running (OWN_RUNNING, OWN_STOP_REQUESTED, EXTERNAL_SAME): the window loads http://127.0.0.1:<port>/
  directly (no iframe, own origin and localStorage). Otherwise, and while a start/stop is in progress or the
  user chose Окно -> Состояние, it shows desktop/ui/status.html passed as html= (no pywebview HTTP server).
  ORPHAN_RUNNING shows the status page with its warning; the dashboard only on Окно -> Терминал.
- js_api: fixed argument-less methods (constants.JS_API); paths, commands and URLs never come from a page.
- X: hide to tray while the bot runs or an action runs (closing returns False, hide() in another thread),
  else quit. Tray 'Выйти из окна' ends only this process: no stop file, no process is touched.
- Log-off/shutdown (M3): pywebview reports that FormClosing as 'closing' too. While the session-end guard (or
  SM_SHUTTINGDOWN) says a session end is running, 'closing' is allowed without toast or stop file, except in
  the states the guard holds (C.SESSION_HOLD_STATES), where it is cancelled silently. Restart Manager
  (ENDSESSION_CLOSEAPP) always lets the window close; the engine host is another process and keeps running.
- Stop: native confirmation, then exactly one stop_<token>.request (Controller.stop); never a kill.
- Confirmations (E2E round 2): user32.MessageBoxW Yes/No (No is the default button) owned by the main window, so it
  stays in front of it and the window is disabled while it is open; called from the menu/js_api/tray thread, never
  the UI thread. Only one confirmation at a time: a second request (e.g. Stop from the tray while the menu's Stop
  dialog is open) gets TEXT['asking'] and opens nothing. Tests replace the box through the GUI module
  (fake_gui sets webview.PFDESK_CONFIRM) or confirm_box=.
- Icon (E2E round 2): webview.start(icon=assets/app_idle.ico) so the window and taskbar do not show Python's icon."""
import threading,time
from pathlib import Path
from . import constants as C,logs,actions
from .controller import allowed,close_action,quit_needs_confirm,tray_state,tray_tip,title as state_title
from .session_end import MODE_LOGOFF,system_shutting_down
UI=Path(__file__).resolve().parent/'ui'
APP_ICON=Path(__file__).resolve().parent/'assets'/'app_idle.ico'
STATUS_HTML=UI/'status.html';LOG_HTML=UI/'log.html'
APP_TITLE='PumpFun PAPER'
LOG_TITLE='Журнал — PumpFun PAPER'
DEV_MARK=' [DEV]'
SIZE=(1280,820);MIN_SIZE=(720,480);LOG_SIZE=(980,640);BG='#0b0e11'
TEXT=dict(
    dev='Режим разработки (PFDESK_DEV_DASHBOARD_PORT): запуск движка, 05/04 и консоли 02/08 отключены.',
    stop_title='Остановить PAPER-бота',
    quit_title='Выйти из окна',
    lost_title='Движок завершился без итога',
    lost_ack='\n\nOK — подтвердить предупреждение. После этого «Запустить» станет доступна.',
    lost_acked='Предупреждение подтверждено. «Запустить» доступна.',
    unavailable='Действие недоступно',
    no_log='Журнала ещё нет.',
    started='Запуск начат.',
    stop_sent='Запрос остановки записан.',
    asking='Окно подтверждения уже открыто: ответьте в нём.',
)
# Native menu (spec 2.2): (menu, ((item, action), ...)). Actions go through Shell.do and the 2.4 matrix.
MENU=(('Бот',(('Запустить','start'),('Остановить…','stop'))),
    ('Отчёты',(('Результаты (05)','results'),('Проверка (04)','check'),('Открыть папку results','open_results'))),
    ('Сервис',(('Настройки (02, консоль)','settings'),('Сохранить данные (08, консоль)','preserve'),('Журнал','show_log'),('Открыть папку журналов','open_logs'))),
    ('Окно',(('Терминал','show_terminal'),('Состояние','show_status'),('Свернуть в трей','to_tray'))))
ENGINE_ACTIONS=('start','results','check','settings','preserve')  # refused in dev mode
NOTIFY_EVENTS=('engine_lost','orphan','self_stopped','start_slow','stop_done','stop_slow','start_failed','action_done')

def read_html(path):return Path(path).read_text(encoding='utf-8')

class Api:
    """js_api of status.html. Every public attribute is an argument-less method; the shell is private
    (pywebview does not expose names starting with '_')."""
    def __init__(self,shell):self._shell=shell
    def get_status(self):return self._shell.page_status()
    def start(self):return self._shell.do('start',page=True)
    def stop(self):return self._shell.do('stop',page=True)
    def run_results(self):return self._shell.do('results',page=True)
    def run_check(self):return self._shell.do('check',page=True)
    def open_settings(self):return self._shell.do('settings',page=True)
    def open_preserve(self):return self._shell.do('preserve',page=True)
    def open_results_folder(self):return self._shell.do('open_results',page=True)
    def open_logs(self):return self._shell.do('open_logs',page=True)
    def show_terminal(self):return self._shell.do('show_terminal',page=True)

class LogApi:
    """js_api of log.html: the tail of the log chosen by the controller, and the two fixed folders."""
    def __init__(self,shell):self._shell=shell
    def get_log(self):return self._shell.log_view()
    def open_results_folder(self):return self._shell.do('open_results',page=True)
    def open_logs(self):return self._shell.do('open_logs',page=True)

MB_YESNO=0x4;MB_ICONWARNING=0x30;MB_DEFBUTTON2=0x100;MB_SETFOREGROUND=0x10000;MB_TOPMOST=0x40000;IDYES=6
CONFIRM_FLAGS=MB_YESNO|MB_ICONWARNING|MB_DEFBUTTON2|MB_SETFOREGROUND

def window_handle(window,find=None):
    """Native HWND of a pywebview (winforms) window: window.native is the BrowserForm, Handle an IntPtr. If that
    fails (no form yet, a cross-thread refusal), the visible top-level window of this process with the same title."""
    try:
        handle=window.native.Handle
        value=int(handle.ToInt64()) if hasattr(handle,'ToInt64') else int(handle)
        if value:return value
    except Exception:pass
    try:return (find or own_window_by_title)(str(window.title))
    except Exception:return None

def own_window_by_title(title):
    """First visible top-level window of this process whose title is `title`, else None."""
    import ctypes,os
    from ctypes import wintypes as WT
    u=ctypes.WinDLL('user32',use_last_error=True);found=[]
    PROC=ctypes.WINFUNCTYPE(WT.BOOL,WT.HWND,WT.LPARAM)
    u.GetWindowTextW.argtypes=(WT.HWND,WT.LPWSTR,ctypes.c_int);u.GetWindowThreadProcessId.argtypes=(WT.HWND,ctypes.POINTER(WT.DWORD))
    u.IsWindowVisible.argtypes=(WT.HWND,);u.EnumWindows.argtypes=(PROC,WT.LPARAM)
    def each(hwnd,_):
        pid=WT.DWORD();u.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value==os.getpid() and u.IsWindowVisible(hwnd):
            buf=ctypes.create_unicode_buffer(512);u.GetWindowTextW(hwnd,buf,512)
            if buf.value==title:found.append(hwnd);return False
        return True
    u.EnumWindows(PROC(each),0)
    return int(found[0]) if found else None

def native_confirm(window,title,text,api=None,owner=None):
    """Yes/No MessageBoxW (warning icon, No is the default) owned by the main window; True only on IDYES.
    Blocks the calling thread, which must not be the UI thread. Without an owner handle it is top-most instead."""
    import ctypes
    from ctypes import wintypes as WT
    if api is None:
        api=ctypes.WinDLL('user32',use_last_error=True).MessageBoxW
        api.argtypes=(WT.HWND,WT.LPCWSTR,WT.LPCWSTR,WT.UINT);api.restype=ctypes.c_int
    if owner is None and window is not None:owner=window_handle(window)
    flags=CONFIRM_FLAGS|(0 if owner else MB_TOPMOST)
    return api(owner,str(text),str(title),flags)==IDYES

def message_box(title,text):
    """Plain information box (pywebview has only OK/Cancel confirmations)."""
    import ctypes
    MB_ICONINFORMATION=0x40;MB_SETFOREGROUND=0x10000;MB_TOPMOST=0x40000
    ctypes.windll.user32.MessageBoxW(None,str(text),str(title),MB_ICONINFORMATION|MB_SETFOREGROUND|MB_TOPMOST)

class Shell:
    def __init__(self,controller,webview,settings=None,tray=None,dev=False,info=None,session=None,poll_s=C.POLL_S,shutting_down=None,confirm_box=None):
        self.ctl=controller;self.layout=controller.layout;self.webview=webview;self.settings=settings;self.tray=tray;self.dev=dev
        self.info=info or message_box;self.session=session;self.poll_s=poll_s;self.shutting_down=shutting_down or system_shutting_down
        self.window=None;self.log_window=None;self.view=None;self.pinned_status=False;self.terminal_chosen=False;self.allow_quit=False
        self.status=None;self.snap=None;self.title=None;self.hidden=False
        self.status_html=read_html(STATUS_HTML);self.log_html=read_html(LOG_HTML)
        self._lock=threading.RLock();self._halt=threading.Event();self._thread=None
        self.confirm_box=confirm_box or getattr(webview,'PFDESK_CONFIRM',None) or native_confirm
        self._asking=threading.Lock()  # held while a confirmation is open
        acked=settings.get('self_stop_acked') if settings is not None else None
        if acked:self.ctl.ack_self_stop(acked)  # acknowledged in an earlier window session

    # ---- window ----
    def menu(self):
        from webview.menu import Menu,MenuAction
        return [Menu(name,[MenuAction(label,self._menu_action(action)) for label,action in items]) for name,items in MENU]
    def _menu_action(self,action):
        def run():self.do(action)
        run.__name__='menu_'+action;return run
    def geometry(self,screen=None):
        """Saved size/position (the position only when it lies on the virtual screen), else SIZE centred on
        the primary work area. Sizes never exceed the screen (small/125 % screens: 1280x800 logical)."""
        geo=self.settings.get('geometry') if self.settings else None
        try:work,virt=(screen or screens)()
        except Exception:work=virt=None
        if geo:
            out=dict(width=geo['width'],height=geo['height'])
            if virt:
                out.update(width=min(out['width'],virt[2]),height=min(out['height'],virt[3]))
                if virt[0]<=geo['x']<virt[0]+virt[2]-100 and virt[1]<=geo['y']<virt[1]+virt[3]-60:out.update(x=geo['x'],y=geo['y'])
            return out
        if not work:return dict(width=SIZE[0],height=SIZE[1])
        width=max(MIN_SIZE[0],min(SIZE[0],int(work[2]*.94)));height=max(MIN_SIZE[1],min(SIZE[1],int(work[3]*.94)))
        return dict(width=width,height=height,x=work[0]+max(0,(work[2]-width)//2),y=work[1]+max(0,(work[3]-height)//2))
    def create(self,screen=None):
        snap=self.poll(apply=False);s=self.status
        kw=dict(js_api=Api(self),menu=self.menu(),min_size=MIN_SIZE,background_color=BG,text_select=True,**self.geometry(screen))
        if self.want_terminal(s):kw['url']=self.layout.url;self.view='terminal'
        else:kw['html']=self.status_html;self.view='status'
        self.title=self.make_title(s)
        self.window=self.webview.create_window(self.title,**kw)
        self.window.events.closing+=self.on_closing
        self.window.events.closed+=self.on_closed
        self.apply(s)  # view and title already match: only the tray follows the first state
        return self.window
    def run(self,gui='edgechromium'):
        """Blocks in the GUI loop (main thread) until the main window is gone. Spike workaround 2:
        private_mode=False + storage_path, so the terminal keeps its localStorage tabs."""
        storage=self.layout.webview_storage;storage.mkdir(parents=True,exist_ok=True)
        icon=str(APP_ICON) if APP_ICON.is_file() else None
        self.webview.start(func=self.start_polling,gui=gui,private_mode=False,storage_path=str(storage),debug=False,icon=icon)
        self._halt.set()

    # ---- polling and view ----
    def start_polling(self):
        if self._thread is None:
            self._thread=threading.Thread(target=self._loop,name='desktop-poll',daemon=True);self._thread.start()
        return self._thread
    def _loop(self):
        while not self._halt.is_set():
            try:self.poll()
            except Exception:pass  # a poll error must never end the window; the next poll retries
            self._halt.wait(self.poll_s)
    def stop_polling(self):
        self._halt.set()
        if self._thread is not None and self._thread is not threading.current_thread():self._thread.join(5)
    def poll(self,apply=True):
        with self._lock:
            snap=self.ctl.snapshot();s=self.ctl.status
            snap.update(dev=self.dev,dev_text=TEXT['dev'] if self.dev else None,title=self.make_title(s))
            self.status=s
            if not s.terminal:self.pinned_status=False;self.terminal_chosen=False
            if apply and self.window is not None:self.apply(s)
            snap['view']=self.view;self.snap=snap
            events=self.ctl.drain_events()
        for name,payload in events:self.on_event(name,payload)
        if self.session is not None:
            try:self.session.update(s)
            except Exception:pass
        return snap
    def want_terminal(self,s):
        if not s.terminal or self.pinned_status or s.busy in ('start','stop'):return False
        return s.kind in C.AUTO_TERMINAL_STATES or self.terminal_chosen
    def make_title(self,s):return state_title(s,self.layout.version)+(DEV_MARK if self.dev else '')
    def apply(self,s):
        want='terminal' if self.want_terminal(s) else 'status'
        if want!=self.view:
            if want=='terminal':self.window.load_url(self.layout.url)
            else:self.window.load_html(self.status_html)
            self.view=want
        t=self.make_title(s)
        if t!=self.title:self.window.set_title(t);self.title=t
        if self.tray is not None:self.tray.update(tray_state(s),tray_tip(s),s.text,allowed(s,'stop')[0])
    def on_event(self,name,payload):
        if self.tray is None:return
        if name=='action_done' and isinstance(payload,dict):payload=payload.get('text')
        if name in NOTIFY_EVENTS and payload:self.tray.notify(str(payload))
    def page_status(self):
        with self._lock:snap=self.snap if self.snap is not None else self.poll(apply=False)
        return dict(snap)

    # ---- actions ----
    def do(self,action,page=False):
        """Every menu item, tray item and js_api method lands here. Returns a JSON-safe dict for the page;
        from the menu an unavailable action shows a dialog with the reason (spec 2.2)."""
        handler=getattr(self,'_do_'+action,None)
        if handler is None:result=dict(ok=False,message='Неизвестное действие.')
        elif self.dev and action in ENGINE_ACTIONS:result=dict(ok=False,message=TEXT['dev'])
        else:
            try:result=handler()
            except Exception as exc:result=dict(ok=False,message=type(exc).__name__+': '+str(exc)[:200])
        result=dict(result or {},action=action)
        if not page and not result.get('ok') and result.get('message') and not result.get('asking'):self.info(TEXT['unavailable'],result['message'])
        try:result['status']=self.poll()
        except Exception:result['status']=self.snap
        return result
    def _check(self,action):
        ok,reason=self.ctl.can(action)
        return None if ok else dict(ok=False,message=reason)
    def confirm(self,title,text):
        """True on Yes, False on No, None when another confirmation is already open (nothing is shown)."""
        if self.window is None:return False
        if not self._asking.acquire(blocking=False):return None
        try:return bool(self.confirm_box(self.window,title,text))
        finally:self._asking.release()
    @staticmethod
    def _asking_result():return dict(ok=False,message=TEXT['asking'],asking=True)
    def _do_start(self):
        s=self.ctl.refresh()
        if s.kind==C.ENGINE_LOST and not s.lost_acked:
            answer=self.confirm(TEXT['lost_title'],(s.lost_text or C.TEXT['lost'])+TEXT['lost_ack'])
            if answer is None:return self._asking_result()
            if not answer:return dict(ok=False,message=None,cancelled=True)
            r=self.ctl.confirm_lost();return dict(ok=r['ok'],message=TEXT['lost_acked'] if r['ok'] else r['message'],acknowledged=r['ok'])
        r=self.ctl.start()
        if r.get('ok'):
            self.pinned_status=False;self.terminal_chosen=False;self._persist_ack(r.get('self_stop_acked'))
            r=dict(r,message=TEXT['started'])
        return r
    def _do_stop(self):
        """Confirmation first; 'No' does nothing, 'Yes' writes exactly one stop file."""
        blocked=self._check('stop')
        if blocked:return blocked
        answer=self.confirm(TEXT['stop_title'],C.TEXT['stop_confirm'])
        if answer is None:return self._asking_result()  # re-entry: the open dialog decides, no second one
        if not answer:return dict(ok=False,message=None,cancelled=True)
        r=self.ctl.stop()
        if r.get('ok'):r=dict(r,message=TEXT['stop_sent'])
        return r
    def _do_results(self):return self.ctl.run('results')
    def _do_check(self):return self.ctl.run('check')
    def _do_settings(self):return self.ctl.open_console('settings')
    def _do_preserve(self):return self.ctl.open_console('preserve')
    def _do_open_results(self):
        """results/, or the evidence folder the last 05 printed when it lies inside results/."""
        last=self.ctl.last_action or {}
        target=last.get('evidence') if last.get('kind')=='results' else None
        if target:
            try:
                path=Path(target).resolve()
                if path.is_dir() and path.is_relative_to(self.layout.results.resolve()):
                    error=actions.open_folder(path,getattr(self.ctl,'_startfile',None));return dict(ok=error is None,message=error)
            except (OSError,ValueError):pass
        return self.ctl.open_folder('open_results')
    def _do_open_logs(self):return self.ctl.open_folder('open_logs')
    def _do_show_terminal(self):
        blocked=self._check('show_terminal')
        if blocked:return blocked
        self.pinned_status=False;self.terminal_chosen=True;self.show_window();return dict(ok=True,message=None)
    def _do_show_status(self):
        self.pinned_status=bool(self.status and self.status.terminal);self.terminal_chosen=False;self.show_window()
        if self.window is not None and self.view!='status':self.window.load_html(self.status_html);self.view='status'
        return dict(ok=True,message=None)
    def _do_to_tray(self):self.to_tray();return dict(ok=True,message=None)
    def _do_show_log(self):
        self.ack_self_stop()
        if self.log_window is not None:
            self.log_window.show();self.log_window.restore();return dict(ok=True,message=None)
        w=self.webview.create_window(LOG_TITLE,html=self.log_html,js_api=LogApi(self),width=LOG_SIZE[0],height=LOG_SIZE[1],
            min_size=MIN_SIZE,background_color=BG,text_select=True)
        def closed():self.log_window=None
        w.events.closed+=closed;self.log_window=w;return dict(ok=True,message=None)
    def log_view(self):
        """Tail of the current log (running action/start first, else the last engine log)."""
        path=self.ctl.latest_log();snap=self.page_status()
        if path is None:return dict(name=None,text=TEXT['no_log'],action=snap.get('action'),warning=snap.get('warning'))
        path=Path(path)
        return dict(name=path.name,text=logs.tail_lines(path,400),action=snap.get('action'),warning=snap.get('warning'),
            size=_size(path))

    # ---- close, tray, quit (spec 2.6) ----
    def on_closing(self):
        """pywebview 'closing' (UI thread). window.destroy() fires it too, hence allow_quit (spike workaround 3)."""
        if self.allow_quit:return True
        mode=self.session_mode()
        if mode is not None:return self.closing_for_session_end(mode)
        s=self.ctl.refresh()
        if close_action(s)=='hide':
            threading.Thread(target=self.to_tray,name='desktop-hide',daemon=True).start()
            return False
        self.allow_quit=True;self.save_geometry();self.stop_polling_async()
        return True
    def session_mode(self):
        """MODE_LOGOFF / MODE_CLOSEAPP while Windows ends the session or Restart Manager closes the app, else None."""
        guard=getattr(self.session,'guard',None)
        try:mode=guard.mode() if guard is not None else None
        except Exception:mode=None
        if mode is None:
            try:mode=MODE_LOGOFF if self.shutting_down() else None
            except Exception:mode=None
        return mode
    def closing_for_session_end(self,mode):
        """Log-off: cancel (silently: no hide, no toast) only where the guard holds the session, and make sure the
        stop file exists there (this 'closing' may come before the guard's own query). Everything else, and Restart
        Manager always: let the window process end; no stop file, the engine host keeps running."""
        if mode==MODE_LOGOFF:
            try:s=self.ctl.refresh()
            except Exception:s=None
            if s is not None and s.kind in C.SESSION_HOLD_STATES:
                try:self.ctl.stop_for_session_end()
                except Exception:pass
                return False
        self.allow_quit=True;self.save_geometry();self.stop_polling_async()
        return True
    def stop_polling_async(self):self._halt.set()
    def on_closed(self):
        self._halt.set()
        if self.tray is not None:self.tray.stop()
        if self.log_window is not None:
            try:self.log_window.destroy()
            except Exception:pass
    def to_tray(self):
        self.save_geometry()
        if self.window is not None:self.window.hide()
        self.hidden=True
        if self.settings is not None and not self.settings.get('tray_notice_shown'):
            if self.tray is not None:self.tray.notify(C.TEXT['tray_notice'])
            self.settings.update(tray_notice_shown=True)
    def show_window(self):
        """Tray 'Показать окно', second instance, Терминал/Состояние: the user looks, so a self-stop is acknowledged."""
        self.ack_self_stop()
        if self.window is None:return
        self.window.show();self.window.restore();self.hidden=False
    def ack_self_stop(self):
        """Acknowledge a run that ended by itself (MINOR 2): the tray leaves red; remembered in settings.json."""
        try:token=self.ctl.ack_self_stop()
        except Exception:return None
        self._persist_ack(token);return token
    def _persist_ack(self,token):
        if token and self.settings is not None:self.settings.update(self_stop_acked=token)
    def quit_window(self):
        """Tray 'Выйти из окна': confirmation while the bot runs; ends only the window process."""
        s=self.ctl.refresh()
        if quit_needs_confirm(s) and not self.confirm(TEXT['quit_title'],C.TEXT['quit_confirm']):return False
        self.allow_quit=True;self.save_geometry();self._halt.set()
        if self.tray is not None:self.tray.stop()
        if self.log_window is not None:
            try:self.log_window.destroy()
            except Exception:pass
        if self.window is not None:self.window.destroy()
        return True
    def tray_stop(self):return self.do('stop')
    def save_geometry(self):
        if self.settings is None or self.window is None or self.hidden:return False
        try:geo=dict(x=int(self.window.x),y=int(self.window.y),width=int(self.window.width),height=int(self.window.height))
        except Exception:return False
        if geo['x']<=-30000 or geo['y']<=-30000:return False  # minimized: keep the last real geometry
        return self.settings.update(geometry=geo)

def _size(path):
    try:return path.stat().st_size
    except OSError:return None

def screens():
    """(primary work area, virtual screen) as (left, top, width, height) in logical (96 dpi) units: the
    process may or may not be DPI-aware yet, so values are scaled by the DPI Windows reports to it."""
    import ctypes
    from ctypes import wintypes as WT
    u=ctypes.windll.user32
    try:scale=u.GetDpiForSystem()/96.
    except AttributeError:scale=1.
    r=WT.RECT();u.SystemParametersInfoW(0x30,0,ctypes.byref(r),0)  # SPI_GETWORKAREA
    m=u.GetSystemMetrics;f=lambda v:int(round(v/scale))
    return (f(r.left),f(r.top),f(r.right-r.left),f(r.bottom-r.top)),tuple(f(m(i)) for i in (76,77,78,79))
