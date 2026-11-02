"""Windows log-off / shutdown (spec 2.6). A hidden top-level window (message-only windows do not get the
broadcast) receives WM_QUERYENDSESSION. While this window's own campaign starts, runs or stops
(C.SESSION_HOLD_STATES) it writes the stop file (graceful stop starts at once; in PREFLIGHT/STARTING the host
honours it once its stop_condition runs), registers ShutdownBlockReasonCreate and answers FALSE; after STOPPED
the reason is destroyed. SetProcessShutdownParameters raises the window's shutdown priority.
Every query also sets a thread-safe session-end mode the shell reads in 'closing' (M3): pywebview reports any
FormClosing, including the one of a log-off, so the shell must not cancel it (and must not show the tray toast)
except in the states the guard holds. lParam with ENDSESSION_CLOSEAPP (Restart Manager, e.g. the installer)
is not a log-off (MINOR 5): the campaign is not stopped, the window process may close, the host keeps running.
The mode is not sticky (MINOR 2 round 2): a WM_QUERYENDSESSION not followed by WM_ENDSESSION (a third-party
broadcast) expires MODE_TTL_S after the query on the monotonic clock; one that held the own campaign (its stop file
is already written) is also cleared as soon as the controller reports STOPPED (leaves the held states). WM_ENDSESSION(TRUE) keeps the mode
(the session really ends), WM_ENDSESSION(FALSE) clears it. A real log-off answers within seconds: Windows sends the
query and the form's own FormClosing together, and a held log-off the user cancels ends with WM_ENDSESSION(FALSE).
Limitation: "Shut down anyway" lets Windows end the engine; then 09_RECOVERY_CHECK / 05_RESULTS apply.
user32/kernel32 are injectable; the block calls run on the window's own thread as Windows requires."""
import itertools,threading,time
from . import constants as C
WM_DESTROY=0x0002;WM_CLOSE=0x0010;WM_QUERYENDSESSION=0x0011;WM_ENDSESSION=0x0016;WM_APP=0x8000
WM_RELEASE=WM_APP+0x51
ENDSESSION_CLOSEAPP=0x00000001;ENDSESSION_CRITICAL=0x40000000;ENDSESSION_LOGOFF=0x80000000
HOLD_KINDS=C.SESSION_HOLD_STATES
MODE_LOGOFF='logoff';MODE_CLOSEAPP='closeapp'
SM_SHUTTINGDOWN=0x2000
MODE_TTL_S=60.  # a query not followed by WM_ENDSESSION stops counting after this long
_names=itertools.count(1)

def system_shutting_down():
    """GetSystemMetrics(SM_SHUTTINGDOWN): fallback for a 'closing' that arrives before the guard's query."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN))
    except Exception:return False

class SessionEndGuard:
    """Pure decision logic. should_block() is Controller.stop_for_session_end: it writes the stop file in
    PREFLIGHT/STARTING/RUNNING and returns True while the own campaign starts, runs or is already stopping."""
    def __init__(self,should_block,api,clock=time.monotonic,ttl=MODE_TTL_S):
        self.should_block=should_block;self.api=api;self.blocking=False;self.queries=0;self.clock=clock;self.ttl=ttl
        self._mode=None;self._mode_t=None;self._ended=False;self._held=False;self._mode_lock=threading.Lock()
    def mode(self):
        """None, MODE_LOGOFF (log-off/shutdown in progress) or MODE_CLOSEAPP (Restart Manager closes the app).
        A query expires MODE_TTL_S after it unless WM_ENDSESSION(TRUE) confirmed the session end; one that held the
        own campaign (its stop file is written) ends earlier once that campaign is STOPPED (observe)."""
        with self._mode_lock:
            if self._mode is not None and not self._ended and self.clock()-self._mode_t>=self.ttl:self._clear()
            return self._mode
    def _clear(self):self._mode=None;self._mode_t=None;self._ended=False;self._held=False
    def observe(self,status):
        """Every poll (HiddenWindow.update): once the campaign a query held has ended (STOPPED, or any state outside
        C.SESSION_HOLD_STATES, the same test as needs_release), the mode is cleared, so an X in a later run hides to
        the tray again instead of acting as a log-off. A query that held nothing (the window was idle) is left to the
        TTL: clearing it on STOPPED could turn the log-off's own 'closing' into a hide while a 05/04 runs."""
        if status is not None and status.kind not in HOLD_KINDS:
            with self._mode_lock:
                if self._mode is not None and self._held and not self._ended:self._clear()
    @property
    def session_ending(self):return self.mode() is not None
    def on_query(self,hwnd,lparam=0):
        """Return value for WM_QUERYENDSESSION: 1 lets Windows continue, 0 holds it with a reason."""
        self.queries+=1
        closeapp=bool(int(lparam or 0)&ENDSESSION_CLOSEAPP)
        with self._mode_lock:
            if not closeapp:self._mode=MODE_LOGOFF
            elif self._mode is None:self._mode=MODE_CLOSEAPP
            self._mode_t=self.clock()
        if closeapp:return 1  # Restart Manager: only the window process goes; the campaign is not stopped
        try:hold=bool(self.should_block())
        except Exception:hold=False  # never block Windows because of a window error
        if not hold:return 1
        with self._mode_lock:
            if self._mode is not None:self._held=True
        if not self.blocking:self.api.block(hwnd,C.TEXT['shutdown_block']);self.blocking=True
        return 0
    def on_end(self,hwnd,ending,lparam=0):
        """WM_ENDSESSION: ending=False means the log-off was cancelled, so the window behaves normally again.
        A stop already requested stays requested and its block reason stays until STOPPED."""
        with self._mode_lock:
            if not ending:self._clear()
            else:
                if self._mode is None:self._mode=MODE_CLOSEAPP if int(lparam or 0)&ENDSESSION_CLOSEAPP else MODE_LOGOFF
                self._ended=True;self._mode_t=self.clock()
        return 0
    def needs_release(self,status):return self.blocking and status is not None and status.kind not in HOLD_KINDS
    def release(self,hwnd):
        if self.blocking:self.api.unblock(hwnd);self.blocking=False

class User32Api:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as W
        u=ctypes.WinDLL('user32',use_last_error=True);k=ctypes.WinDLL('kernel32',use_last_error=True)
        u.ShutdownBlockReasonCreate.argtypes=(W.HWND,W.LPCWSTR);u.ShutdownBlockReasonCreate.restype=W.BOOL
        u.ShutdownBlockReasonDestroy.argtypes=(W.HWND,);u.ShutdownBlockReasonDestroy.restype=W.BOOL
        k.SetProcessShutdownParameters.argtypes=(W.DWORD,W.DWORD);k.SetProcessShutdownParameters.restype=W.BOOL
        self.u=u;self.k=k
    def block(self,hwnd,text):return bool(self.u.ShutdownBlockReasonCreate(hwnd,text))
    def unblock(self,hwnd):return bool(self.u.ShutdownBlockReasonDestroy(hwnd))
    def priority(self,level=C.SHUTDOWN_PRIORITY):return bool(self.k.SetProcessShutdownParameters(level,0))

class HiddenWindow:
    """Real hidden top-level window with its own message loop thread. start() returns once hwnd exists."""
    def __init__(self,guard,set_priority=True):
        self.guard=guard;self.set_priority=set_priority;self.hwnd=None;self._ready=threading.Event();self._thread=None;self.error=None
    def start(self,timeout=5):
        if self.set_priority:
            try:self.guard.api.priority()
            except Exception:pass
        self._thread=threading.Thread(target=self._run,name='desktop-session-end',daemon=True);self._thread.start()
        self._ready.wait(timeout)
        if self.error:raise OSError(self.error)
        return self.hwnd
    def _run(self):
        import ctypes
        from ctypes import wintypes as W
        u=ctypes.WinDLL('user32',use_last_error=True);k=ctypes.WinDLL('kernel32',use_last_error=True)
        LRESULT=ctypes.c_ssize_t
        WNDPROC=ctypes.WINFUNCTYPE(LRESULT,W.HWND,W.UINT,W.WPARAM,W.LPARAM)
        class WNDCLASSW(ctypes.Structure):
            _fields_=[('style',W.UINT),('lpfnWndProc',WNDPROC),('cbClsExtra',ctypes.c_int),('cbWndExtra',ctypes.c_int),('hInstance',W.HINSTANCE),
                ('hIcon',W.HICON),('hCursor',W.HANDLE),('hbrBackground',W.HBRUSH),('lpszMenuName',W.LPCWSTR),('lpszClassName',W.LPCWSTR)]
        u.DefWindowProcW.argtypes=(W.HWND,W.UINT,W.WPARAM,W.LPARAM);u.DefWindowProcW.restype=LRESULT
        u.RegisterClassW.argtypes=(ctypes.POINTER(WNDCLASSW),);u.RegisterClassW.restype=W.ATOM
        u.CreateWindowExW.argtypes=(W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,W.HWND,W.HMENU,W.HINSTANCE,W.LPVOID)
        u.CreateWindowExW.restype=W.HWND
        u.GetMessageW.argtypes=(ctypes.POINTER(W.MSG),W.HWND,W.UINT,W.UINT);u.GetMessageW.restype=W.BOOL
        u.TranslateMessage.argtypes=(ctypes.POINTER(W.MSG),);u.DispatchMessageW.argtypes=(ctypes.POINTER(W.MSG),);u.DispatchMessageW.restype=LRESULT
        u.DestroyWindow.argtypes=(W.HWND,);u.PostQuitMessage.argtypes=(ctypes.c_int,)
        u.UnregisterClassW.argtypes=(W.LPCWSTR,W.HINSTANCE)
        k.GetModuleHandleW.argtypes=(W.LPCWSTR,);k.GetModuleHandleW.restype=W.HMODULE
        guard=self.guard
        def proc(hwnd,msg,wparam,lparam):
            try:
                if msg==WM_QUERYENDSESSION:return guard.on_query(hwnd,lparam)
                if msg==WM_ENDSESSION:return guard.on_end(hwnd,bool(wparam),lparam)
                if msg==WM_RELEASE:guard.release(hwnd);return 0
                if msg==WM_DESTROY:guard.release(hwnd);u.PostQuitMessage(0);return 0
            except Exception:pass
            return u.DefWindowProcW(hwnd,msg,wparam,lparam)
        self._proc=WNDPROC(proc)  # keep the callback alive
        hinst=k.GetModuleHandleW(None);name=C.APP_ID+'SessionEnd'+str(next(_names))
        cls=WNDCLASSW(lpfnWndProc=self._proc,hInstance=hinst,lpszClassName=name)
        if not u.RegisterClassW(ctypes.byref(cls)):
            self.error='DESKTOP_SESSION_CLASS_FAILED: '+str(ctypes.get_last_error());self._ready.set();return
        hwnd=u.CreateWindowExW(0,name,C.APP_ID,0,0,0,0,0,None,None,hinst,None)  # never shown
        if not hwnd:
            self.error='DESKTOP_SESSION_WINDOW_FAILED: '+str(ctypes.get_last_error());u.UnregisterClassW(name,hinst);self._ready.set();return
        self.hwnd=hwnd;self._ready.set()
        msg=W.MSG()
        while u.GetMessageW(ctypes.byref(msg),None,0,0)>0:
            u.TranslateMessage(ctypes.byref(msg));u.DispatchMessageW(ctypes.byref(msg))
        u.UnregisterClassW(name,hinst);self.hwnd=None
    def _send(self,msg):
        import ctypes
        from ctypes import wintypes as W
        u=ctypes.WinDLL('user32',use_last_error=True);u.PostMessageW.argtypes=(W.HWND,W.UINT,W.WPARAM,W.LPARAM);u.PostMessageW.restype=W.BOOL
        return bool(self.hwnd) and bool(u.PostMessageW(self.hwnd,msg,0,0))
    def update(self,status):
        """Call on every poll: after STOPPED (or the engine is gone) the block reason is destroyed; STOPPED also
        clears the session-end mode (guard.observe)."""
        try:self.guard.observe(status)
        except Exception:pass
        if self.guard.needs_release(status):return self._send(WM_RELEASE)
        return False
    def close(self,timeout=5):
        if self.hwnd:self._send(WM_CLOSE)
        if self._thread is not None:self._thread.join(timeout)
