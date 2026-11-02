"""One window per installation: named mutex Local\\PumpfunPaperDesktop_<identity>. A second instance sets
the event <name>_show and exits 0; the first shows its window on that event (spec 2.6, spike check 4).
The first instance also holds the fixed-name mutex C.APP_MUTEX ("PumpfunPaperDesktopApp", same for every
installation) for the Inno Setup AppMutex check; it is only a marker, never waited on or owned.
kernel32 is injectable so the logic is tested without Windows."""
import threading
from . import constants as C
ERROR_ALREADY_EXISTS=183
WAIT_OBJECT_0=0;WAIT_TIMEOUT=0x102
EVENT_MODIFY_STATE=0x0002;SYNCHRONIZE=0x00100000

def names(identity):
    base='Local\\'+C.APP_ID+'_'+identity
    return base,base+'_show'

def kernel32():
    import ctypes
    from ctypes import wintypes as W
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateMutexW.argtypes=(ctypes.c_void_p,W.BOOL,W.LPCWSTR);k.CreateMutexW.restype=W.HANDLE
    k.CreateEventW.argtypes=(ctypes.c_void_p,W.BOOL,W.BOOL,W.LPCWSTR);k.CreateEventW.restype=W.HANDLE
    k.OpenEventW.argtypes=(W.DWORD,W.BOOL,W.LPCWSTR);k.OpenEventW.restype=W.HANDLE
    k.SetEvent.argtypes=(W.HANDLE,);k.SetEvent.restype=W.BOOL
    k.WaitForSingleObject.argtypes=(W.HANDLE,W.DWORD);k.WaitForSingleObject.restype=W.DWORD
    k.CloseHandle.argtypes=(W.HANDLE,);k.CloseHandle.restype=W.BOOL
    return k,ctypes.get_last_error

class SingleInstance:
    def __init__(self,identity,api=None,app_mutex_name=C.APP_MUTEX):
        self.mutex_name,self.event_name=names(identity);self.app_mutex_name=app_mutex_name
        self.k,self.last_error=api or kernel32()
        self.mutex=None;self.event=None;self.app_mutex=None;self.primary=None;self._stop=threading.Event();self._thread=None

    def acquire(self):
        """True: this is the first instance (it owns the mutex and the show event).
        False: another instance runs; it has been asked to show its window."""
        handle=self.k.CreateMutexW(None,False,self.mutex_name);error=self.last_error()
        if not handle:raise OSError('DESKTOP_MUTEX_FAILED: '+str(error))
        if error==ERROR_ALREADY_EXISTS:
            self.k.CloseHandle(handle);self.primary=False;self.signal_first();return False
        self.mutex=handle
        # Auto-reset event: one SetEvent wakes the listener once.
        self.event=self.k.CreateEventW(None,False,False,self.event_name)
        if not self.event:
            error=self.last_error();self.close();raise OSError('DESKTOP_EVENT_FAILED: '+str(error))
        # Installer marker: ERROR_ALREADY_EXISTS (a window of another installation) is fine; a failure only
        # costs the installer's running-app warning, never the window.
        self.app_mutex=self.k.CreateMutexW(None,False,self.app_mutex_name) or None
        self.primary=True;return True

    def signal_first(self):
        event=self.k.OpenEventW(EVENT_MODIFY_STATE|SYNCHRONIZE,False,self.event_name)
        if not event:return False  # first instance still starting: it will show its own window anyway
        try:return bool(self.k.SetEvent(event))
        finally:self.k.CloseHandle(event)

    def wait_show(self,timeout_ms=500):
        """One wait on the show event: True when a second instance asked to show the window."""
        if not self.event:return False
        return self.k.WaitForSingleObject(self.event,timeout_ms)==WAIT_OBJECT_0

    def listen(self,callback,timeout_ms=500):
        """Background thread calling callback() on each show request until close()."""
        def loop():
            while not self._stop.is_set():
                if self.wait_show(timeout_ms) and not self._stop.is_set():
                    try:callback()
                    except Exception:pass
        self._thread=threading.Thread(target=loop,name='desktop-single-instance',daemon=True);self._thread.start();return self._thread

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():self._thread.join(2)
        for name in ('app_mutex','event','mutex'):
            handle=getattr(self,name)
            if handle:self.k.CloseHandle(handle);setattr(self,name,None)  # the mutex is never owned, only named
