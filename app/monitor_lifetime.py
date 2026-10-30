"""Closable raw-pipe lifetime observer; never holds CPython buffered stdin locks."""
import os,sys,threading,time

class StdinLifetime:
    def __init__(self,fd=None):
        self.fd=sys.stdin.fileno() if fd is None else fd
        self.stop=threading.Event();self.closing=threading.Event();self.done=threading.Event()
        self.reason=None;self.error=None;self.thread=threading.Thread(target=self._run,name='monitor-stdin-v81',daemon=True)
        self.thread.start()
    def _run(self):
        try:
            peek=None
            if os.name=='nt':
                import ctypes as c,msvcrt
                kernel=c.WinDLL('kernel32',use_last_error=True)
                peek=kernel.PeekNamedPipe;peek.argtypes=[c.c_void_p,c.c_void_p,c.c_uint32,c.c_void_p,c.POINTER(c.c_uint32),c.c_void_p];peek.restype=c.c_int
                handle=msvcrt.get_osfhandle(self.fd)
            else:import select
            while not self.closing.is_set():
                if peek is not None:
                    available=c.c_uint32()
                    if not peek(handle,None,0,None,c.byref(available),None):
                        code=c.get_last_error()
                        if code in (109,232):self.reason='PIPE_EOF';self.stop.set();return
                        raise c.WinError(code)
                    ready=bool(available.value)
                else:ready=bool(select.select([self.fd],[],[],.05)[0])
                if ready:
                    data=os.read(self.fd,1)
                    self.reason='PIPE_DATA' if data else 'PIPE_EOF';self.stop.set();return
                self.closing.wait(.05)
            self.reason='OBSERVER_CLOSED'
        except BaseException as exc:
            self.reason='PIPE_ERROR';self.error=__import__('child_diagnostics').exception(exc);self.stop.set()
        finally:self.done.set()
    def close(self):
        self.closing.set();start=time.monotonic();self.thread.join(1)
        return dict(schema='MONITOR_STDIN_V81',pid=os.getpid(),confirmed=self.done.is_set() and not self.thread.is_alive(),thread_alive=self.thread.is_alive(),reason=self.reason,error=self.error,join_ms=(time.monotonic()-start)*1000,buffered_stdin_used=False)
