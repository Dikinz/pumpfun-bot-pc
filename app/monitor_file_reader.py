"""Read one monitor snapshot with native Windows error provenance.

Path.read_bytes may expose only CRT errno=EACCES on Windows. A native open/read
captures the failing Win32 call's last error before formatting or closing.
"""
import ctypes as C
from functools import lru_cache

@lru_cache(maxsize=1)
def bindings():
    k=C.WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.argtypes=[C.c_wchar_p,C.c_uint32,C.c_uint32,C.c_void_p,C.c_uint32,C.c_uint32,C.c_void_p];k.CreateFileW.restype=C.c_void_p
    k.ReadFile.argtypes=[C.c_void_p,C.c_void_p,C.c_uint32,C.POINTER(C.c_uint32),C.c_void_p];k.ReadFile.restype=C.c_int
    k.CloseHandle.argtypes=[C.c_void_p];k.CloseHandle.restype=C.c_int
    return k,C.get_last_error,C.WinError


def read_windows(path,*,backend=None):
    k,last_error,error_factory=bindings() if backend is None else backend
    def failure(code,api):
        exc=error_factory(code)
        exc.filename=str(path)
        exc.native_api_v862=api
        exc.native_error_source_v862='WIN32_LAST_ERROR_CAPTURED_AT_CALL'
        return exc
    # Share reads, writes and deletes so this reader does not obstruct atomic
    # replacement of the snapshot. An exclusive existing handle still denies us.
    handle=k.CreateFileW(str(path),0x80000000,0x7,None,3,0x80,None)
    code=last_error() if handle in (None,C.c_void_p(-1).value) else 0
    if handle in (None,C.c_void_p(-1).value):raise failure(code,'CreateFileW')
    primary=None
    try:
        chunks=[];buffer=C.create_string_buffer(65536)
        while True:
            count=C.c_uint32()
            ok=k.ReadFile(handle,buffer,len(buffer),C.byref(count),None)
            code=last_error() if not ok else 0
            if not ok:raise failure(code,'ReadFile')
            if count.value==0:break
            chunks.append(buffer.raw[:count.value])
        return b''.join(chunks)
    except BaseException as exc:
        primary=exc;raise
    finally:
        ok=k.CloseHandle(handle)
        code=last_error() if not ok else 0
        if not ok:
            if primary is None:raise failure(code,'CloseHandle')
            primary.close_winerror_v862=code
