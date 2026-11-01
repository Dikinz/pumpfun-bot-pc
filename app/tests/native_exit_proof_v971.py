"""Test-only process exit proof through a handle captured before cancellation."""
import ctypes as C,math,os
import windows_child as wc

class WindowsAPI:
    def __init__(self):
        if os.name!='nt':raise RuntimeError('NATIVE_EXIT_PROOF_REQUIRES_WINDOWS')
        self.k=C.WinDLL('kernel32',use_last_error=True)
        signatures={
            'OpenProcess':([wc.DWORD,wc.BOOL,wc.DWORD],wc.HANDLE),
            'GetProcessTimes':([wc.HANDLE]+[C.POINTER(wc.FILETIME)]*4,wc.BOOL),
            'WaitForSingleObject':([wc.HANDLE,wc.DWORD],wc.DWORD),
            'GetExitCodeProcess':([wc.HANDLE,C.POINTER(wc.DWORD)],wc.BOOL),
            'CloseHandle':([wc.HANDLE],wc.BOOL)}
        for name,(args,result) in signatures.items():
            fn=getattr(self.k,name);fn.argtypes=args;fn.restype=result
    def open(self,pid):
        handle=self.k.OpenProcess(0x100000|0x1000,False,pid) # SYNCHRONIZE | QUERY_LIMITED_INFORMATION, no terminate rights.
        if not handle:raise C.WinError(C.get_last_error())
        return handle
    def birth(self,handle):
        birth,end,kernel,user=wc.FILETIME(),wc.FILETIME(),wc.FILETIME(),wc.FILETIME()
        if not self.k.GetProcessTimes(handle,C.byref(birth),C.byref(end),C.byref(kernel),C.byref(user)):
            raise C.WinError(C.get_last_error())
        return ((birth.high<<32)|birth.low)/10_000_000-11644473600
    def wait(self,handle):
        status=self.k.WaitForSingleObject(handle,0)
        if status==0xffffffff:raise C.WinError(C.get_last_error())
        return status
    def exit_code(self,handle):
        code=wc.DWORD()
        if not self.k.GetExitCodeProcess(handle,C.byref(code)):raise C.WinError(C.get_last_error())
        return code.value
    def close(self,handle):
        if not self.k.CloseHandle(handle):raise C.WinError(C.get_last_error())

class ExitProbe:
    def __init__(self,pid,birth,api=None):
        if type(pid)!=int or pid<=0 or type(birth) not in (int,float) or not math.isfinite(birth):
            raise AssertionError('INVALID_PROCESS_IDENTITY')
        self.api=api if api is not None else WindowsAPI();self.pid=pid;self.expected_birth=birth;self.handle=None
        try:
            self.handle=self.api.open(pid);self.created_ts=self.api.birth(self.handle)
            if not math.isfinite(self.created_ts) or abs(self.created_ts-birth)>.01:
                raise AssertionError('PROCESS_IDENTITY_MISMATCH')
            if self.api.wait(self.handle)!=258:raise AssertionError('PROCESS_NOT_LIVE_BEFORE_CANCEL')
        except BaseException:
            self.close();raise
    def signaled(self):
        if self.handle is None:raise AssertionError('EXIT_PROOF_HANDLE_CLOSED')
        status=self.api.wait(self.handle)
        if status not in (0,258):raise AssertionError('INVALID_RETAINED_HANDLE_WAIT:'+str(status))
        return status==0
    def exited(self):
        if self.handle is None:raise AssertionError('EXIT_PROOF_HANDLE_CLOSED')
        status=self.api.wait(self.handle)
        if status!=0:raise AssertionError('RETAINED_PROCESS_HANDLE_NOT_SIGNALED:'+str(status))
        return dict(proof='PRE_CANCEL_RETAINED_WINDOWS_HANDLE_V971',pid=self.pid,created_ts=self.created_ts,
            wait_status=status,wait_state='SIGNALED',exit_code=self.api.exit_code(self.handle),
            handle_retained_before_cancel=True,pid_reopened_after_cancel=False)
    def close(self):
        if self.handle is not None:
            self.api.close(self.handle);self.handle=None
