"""Exact argv/env of every window action (spec 2.3) and the runners. Paths come only from the Layout:
the page (js_api) can never pass a path, command or URL."""
import os,subprocess,time
from pathlib import Path
from . import constants as C,logs

def _clean(base,remove,add=()):
    env={k:v for k,v in dict(os.environ if base is None else base).items() if k.upper() not in {r.upper() for r in remove}}
    env.update(add);return env

def engine_env(base=None):
    """Host environment: the window's own minus PYTHONPATH/PYTHONHOME/DATABASE_PATH/DASHBOARD_PORT/EXECUTION_MODE.
    Nothing is added (MINOR 1): main.py, dashboard and monitor inherit it exactly as under 03_START.bat."""
    return _clean(base,C.ENGINE_CLEAR_ENV)
def engine_argv(layout,token):
    if not C.engine.valid_token(token):raise ValueError('DESKTOP_TOKEN_INVALID')
    return [str(layout.venv_python),'-I',str(layout.engine_script),'run','--token',token]

def action_argv(layout,kind):
    if kind not in C.BACKGROUND:raise ValueError('DESKTOP_ACTION_UNKNOWN')
    return [str(layout.venv_python),'-I',str(layout.bootstrap),kind]
def action_env(kind,base=None):
    """05/04 exactly as their .bat: PYTHONPATH/PYTHONHOME cleared; only 04 sets PYTHONUTF8=1 (04_CHECK.bat does).
    The log therefore mixes UTF-8 and ANSI lines; logs.read_tail decodes each line on its own."""
    if kind not in C.BACKGROUND:raise ValueError('DESKTOP_ACTION_UNKNOWN')
    return _clean(base,C.CONSOLE_CLEAR_ENV,C.CHECK_SET_ENV if kind=='check' else ())

CONSOLE_BATS=dict(settings='02_SETTINGS.bat',preserve='08_PRESERVE_DATA.bat')
def comspec():
    value=os.environ.get('ComSpec')
    return value if value and Path(value).name.lower()=='cmd.exe' else str(Path(os.environ.get('SystemRoot','C:\\Windows'))/'System32'/'cmd.exe')
def console_command(layout,kind):
    """cmd.exe /c "<ROOT>\\02_SETTINGS.bat" in a new visible console; keys are typed there (getpass)."""
    return '"'+comspec()+'" /c "'+str(layout.bat(CONSOLE_BATS[kind]))+'"'

def check_outcome(code):
    return C.CHECK_TEXT.get(code,C.CHECK_FAILED)
def results_outcome(code):
    return ('OK','Результаты собраны.') if code==0 else ('FAILED','Failed. Keep this window and copy the error text.')

class Background:
    """05 or 04 with a hidden console, output into logs/<kind>_<stamp>.log. Detached like the host, so it
    continues if the window closes; the window only polls it, never kills it."""
    def __init__(self,layout,kind,popen=None,clock=time.monotonic):
        if kind not in C.BACKGROUND:raise ValueError('DESKTOP_ACTION_UNKNOWN')
        self.layout=layout;self.kind=kind;self._popen=popen;self.clock=clock;self.proc=None;self.log=None;self.result=None;self.started=None
    def start(self):
        logs.rotate(self.layout.logs)
        stream=logs.new_log(self.layout.logs,logs.action_name(self.kind));self.log=Path(stream.name)
        try:
            popen=self._popen or subprocess.Popen
            self.proc=popen(action_argv(self.layout,self.kind),cwd=str(self.layout.root),env=action_env(self.kind),stdin=subprocess.DEVNULL,
                stdout=stream,stderr=subprocess.STDOUT,creationflags=C.DETACHED_FLAGS if os.name=='nt' else 0,close_fds=True)
        finally:stream.close()
        self.started=self.clock();return self
    def poll(self):
        """None while running, else (and from then on) the result dict."""
        if self.result is not None or self.proc is None:return self.result
        code=self.proc.poll()
        if code is None:return None
        text=logs.read_tail(self.log,256*1024)
        status,message=(check_outcome if self.kind=='check' else results_outcome)(code)
        self.result=dict(kind=self.kind,code=code,status=status,text=message,log=str(self.log),
            evidence=str(logs.evidence(text)) if self.kind=='results' and logs.evidence(text) else None,elapsed=self.clock()-self.started)
        return self.result

def open_console(layout,kind,popen=None):
    if kind not in CONSOLE_BATS:raise ValueError('DESKTOP_ACTION_UNKNOWN')
    popen=popen or subprocess.Popen
    return popen(console_command(layout,kind),cwd=str(layout.root),env=_clean(None,C.CONSOLE_CLEAR_ENV),
        creationflags=C.CREATE_NEW_CONSOLE if os.name=='nt' else 0,close_fds=True)

def open_folder(path,startfile=None):
    """Explorer on a fixed folder (results or logs); never a path from the page."""
    path=Path(path)
    if not path.is_dir():return 'Папка ещё не создана: '+str(path)
    (startfile or os.startfile)(str(path));return None
