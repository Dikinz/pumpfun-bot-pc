"""Window entry point: .venv-ui\\Scripts\\pythonw.exe -I desktop\\app_main.py (16_DESKTOP.bat / shortcut).

Order (spec 1, 2.5, 2.6, spike workarounds): sys.path (ROOT, app/; -I leaves the script folder out) ->
explicit AppUserModelID 'PumpfunPaper.Desktop' (before any window or MessageBox, so the taskbar button and the
notifications are not grouped under Python's identity) -> layout ->
window log in the profile (pythonw has no stdout) -> short-path preflight (before pywebview/pythonnet are
imported) -> release check (release_guard.verify + DESKTOP_EXTRA_MODULE) -> single instance (a second
instance only signals the first and exits 0) -> pywebview/pystray import -> tray run_detached -> GUI loop.
Any failure before the window shows a MessageBox; the campaign is never touched by the window ending.

Development/test only (documented in docs/DESKTOP_SPIKE_RU.md): PFDESK_DEV_DASHBOARD_PORT=<8210..8219>
points the window at a test dashboard instead of 8188. Only together with it are read PFDESK_DEV_DB
(database whose identity /health must report), PFDESK_DEV_CONTROL (engine_state/stop/log folder) and
PFDESK_DEV_PROFILE (WebView2 profile/settings base). Dev mode skips the release check, marks the title
[DEV] and refuses engine start, 05/04 and consoles 02/08. Any other port value refuses to start.
The PFDESK_DEV_* variables count only in a developer checkout (ROOT/.git exists; a release has none, MINOR 6);
elsewhere they are ignored, the release check runs, and a warning goes to <profile>/window.log."""
import os,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
DEV_PORT='PFDESK_DEV_DASHBOARD_PORT';DEV_DB='PFDESK_DEV_DB';DEV_CONTROL='PFDESK_DEV_CONTROL';DEV_PROFILE='PFDESK_DEV_PROFILE'
DEV_VARS=(DEV_PORT,DEV_DB,DEV_CONTROL,DEV_PROFILE)
DEV_PORTS=range(8210,8220)
WINDOW_LOG='window.log';WINDOW_LOG_MAX=1<<20
TEXT=dict(
    title='PumpFun PAPER',
    dev_port='PFDESK_DEV_DASHBOARD_PORT должен быть портом 8210–8219 (тестовая панель). Окно не запущено.',
    release='Проверка сборки не пройдена, окно не запущено:\n\n{error}\n\nРаспакуйте полный ZIP в новую папку или переустановите приложение.',
    no_gui='Не удалось загрузить оконные компоненты (pywebview/WebView2):\n\n{error}\n\nПереустановите окружение окна: удалите папку .venv-ui и запустите 16_DESKTOP.bat. '
        'Если не установлен Microsoft Edge WebView2 Runtime, установите его.\nКампания не затронута.\n\nОткрыть терминал {url} в системном браузере?',
    dev_ignored='PFDESK_DEV_* ignored: no .git in {root} (not a developer checkout); release mode, release check runs: {names}',
    crashed='Окно завершилось с ошибкой:\n\n{error}\n\nКампания не затронута. Открыть терминал {url} в системном браузере?',
)

def setup_path():
    if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
    if str(APP) not in sys.path:sys.path.append(str(APP))

def dev_checkout(root=None):
    """True only in a developer checkout: ROOT/.git (a folder, or a file in a git worktree). Releases have none."""
    return (Path(root or ROOT)/'.git').exists()

def set_app_user_model_id(app_id):
    """shell32.SetCurrentProcessExplicitAppUserModelID: True on S_OK. Never raises (older/odd systems keep
    Python's identity, nothing else changes)."""
    try:
        import ctypes
        fn=ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes=(ctypes.c_wchar_p,);fn.restype=ctypes.c_long
        return fn(str(app_id))==0
    except Exception:return False

def make_layout(env=None,root=None,warnings=None):
    """(Layout, dev). The dashboard URL changes only through PFDESK_DEV_DASHBOARD_PORT in 8210-8219, and only in a
    developer checkout; otherwise the variables are ignored and a warning is appended to `warnings`."""
    from desktop.paths import Layout
    env=os.environ if env is None else env
    names=[name for name in DEV_VARS if env.get(name)]
    if names and not dev_checkout(root):
        if warnings is not None:warnings.append(TEXT['dev_ignored'].format(root=Path(root or ROOT),names=', '.join(names)))
        return Layout(),False
    raw=env.get(DEV_PORT)
    if raw is None or raw=='':return Layout(),False
    try:port=int(raw)
    except ValueError:raise ValueError(TEXT['dev_port'])
    if port not in DEV_PORTS:raise ValueError(TEXT['dev_port'])
    kw=dict(port=port)
    if env.get(DEV_DB):kw['db']=Path(env[DEV_DB])
    if env.get(DEV_CONTROL):kw['control']=Path(env[DEV_CONTROL])
    if env.get(DEV_PROFILE):kw['profile_base']=Path(env[DEV_PROFILE])
    return Layout(**kw),True

def window_log(folder,text):
    """Append one line to <profile>/window.log (also when python.exe, not pythonw, runs the window)."""
    try:
        folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
        with open(folder/WINDOW_LOG,'a',encoding='utf-8',errors='backslashreplace') as stream:
            stream.write(time.strftime('%Y-%m-%d %H:%M:%S')+' DESKTOP WARNING: '+str(text)+'\n')
        return True
    except OSError:return False

def redirect_output(folder):
    """pythonw: sys.stdout/stderr are None; send them (and pywebview's logging) to <profile>/window.log."""
    if sys.stdout is not None and sys.stderr is not None:return None
    try:
        folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);path=folder/WINDOW_LOG
        mode='w' if path.exists() and path.stat().st_size>WINDOW_LOG_MAX else 'a'
        stream=open(path,mode,encoding='utf-8',errors='backslashreplace',buffering=1)
    except OSError:return None
    if sys.stdout is None:sys.stdout=stream
    if sys.stderr is None:sys.stderr=stream
    return stream

def message(text,ask=False):
    """MessageBox (no GUI library needed). ask=True: Yes/No, returns True on Yes."""
    try:
        import ctypes
        MB_YESNO=0x4;MB_ICONWARNING=0x30;MB_SETFOREGROUND=0x10000;MB_TOPMOST=0x40000;IDYES=6
        flags=MB_ICONWARNING|MB_SETFOREGROUND|MB_TOPMOST|(MB_YESNO if ask else 0)
        return ctypes.windll.user32.MessageBoxW(None,str(text),TEXT['title'],flags)==IDYES
    except Exception:
        print(text,file=sys.stderr);return False

def offer_browser(text,url,ask=message):
    if ask(text,True):
        import webbrowser;webbrowser.open(url)
        return True
    return False

def main(env=None,on_ready=None,ask=message,gui=None):
    """Returns the process exit code. on_ready(shell) runs in a thread once the window is shown (tests/smoke)."""
    setup_path()
    from desktop import paths,release_check,settings_store,single_instance,session_end,window,tray as tray_mod
    from desktop.controller import Controller
    from desktop.constants import APP_USER_MODEL_ID
    set_app_user_model_id(APP_USER_MODEL_ID)
    warnings=[]
    try:layout,dev=make_layout(env,warnings=warnings)
    except ValueError as exc:ask(str(exc),False);return 2
    for text in warnings:window_log(layout.profile,text)
    redirect_output(layout.profile)
    problem=paths.short_path_problem()
    if problem:ask(problem,False);return 1
    if not dev:
        try:release_check.verify(ROOT)
        except Exception as exc:ask(TEXT['release'].format(error=str(exc)[:400]),False);return 1
    instance=single_instance.SingleInstance(layout.identity)
    if not instance.acquire():return 0
    shell=hidden=None
    try:
        try:
            if gui is None:
                import webview,pystray
            else:webview,pystray=gui
        except Exception as exc:
            offer_browser(TEXT['no_gui'].format(error=type(exc).__name__+': '+str(exc)[:300],url=layout.url),layout.url,ask);return 1
        settings=settings_store.SettingsStore(layout.settings_file);settings.load()
        controller=Controller(layout)
        shell=window.Shell(controller,webview,settings=settings,dev=dev)
        shell.tray=tray_mod.Tray(shell.show_window,shell.tray_stop,shell.quit_window,pystray=pystray)
        try:
            hidden=session_end.HiddenWindow(session_end.SessionEndGuard(controller.stop_for_session_end,session_end.User32Api()))
            hidden.start();shell.session=hidden
        except Exception:hidden=None  # without it only the log-off hold is missing; the window still works
        instance.listen(shell.show_window)
        shell.create()
        if on_ready is not None:
            def ready():
                shell.window.events.shown.wait(60);on_ready(shell)
            threading.Thread(target=ready,name='desktop-on-ready',daemon=True).start()
        shell.tray.start()  # spike workaround 4: run_detached before webview.start
        try:shell.run()
        except Exception as exc:
            offer_browser(TEXT['crashed'].format(error=type(exc).__name__+': '+str(exc)[:300],url=layout.url),layout.url,ask);return 1
        return 0
    finally:
        if shell is not None:
            shell.stop_polling()
            if shell.tray is not None:shell.tray.stop()
        if hidden is not None:hidden.close()
        instance.close()

if __name__=='__main__':
    raise SystemExit(main())
