import ctypes as C,errno,os
from pathlib import Path
import pytest
import monitor_file_reader as reader,monitor_state as state

class Kernel:
    def __init__(self,data=b'',open_error=0,read_error=0,close_error=0):
        self.data=data;self.open_error=open_error;self.read_error=read_error;self.close_error=close_error
        self.last=0;self.closed=0;self.opens=[]
    def CreateFileW(self,*args):
        self.opens.append(args);self.last=self.open_error
        return C.c_void_p(-1).value if self.open_error else 11
    def ReadFile(self,handle,buffer,size,count,overlapped):
        assert handle==11
        self.last=self.read_error
        if self.read_error:return 0
        chunk=self.data[:size];self.data=self.data[size:]
        C.memmove(buffer,chunk,len(chunk));C.cast(count,C.POINTER(C.c_uint32))[0]=len(chunk)
        return 1
    def CloseHandle(self,handle):
        assert handle==11
        self.closed+=1;self.last=self.close_error
        return not self.close_error
    def backend(self):return self,lambda:self.last,error_factory

def error_factory(code):
    exc=FileNotFoundError(errno.ENOENT,'native missing') if code in (2,3) else PermissionError(errno.EACCES,'native denied') if code in (5,32) else OSError(errno.EIO,'native I/O')
    exc.winerror=code
    return exc

@pytest.mark.parametrize('code',[2,3,5,32])
def test_native_reader_model_open_error_is_captured_before_other_calls(code,monkeypatch,tmp_path):
    kernel=Kernel(open_error=code)
    monkeypatch.setattr(state,'_read_bytes',lambda p:reader.read_windows(p,backend=kernel.backend()))
    result=state.read(tmp_path/'state.json');details=result['state_read_error_v86']
    assert details['winerror']==code and details['native_api_v862']=='CreateFileW'
    assert details['native_error_source_v862']=='WIN32_LAST_ERROR_CAPTURED_AT_CALL'
    assert kernel.closed==0
    assert details['exception']['chain'][0]['winerror']==code
    assert result['status']=='STATE_READ_ERROR' and 'monotonic_ts' not in result

@pytest.mark.parametrize('length',[0,1,65536,65537,150000])
def test_native_reader_model_exact_bytes_and_shared_open(length):
    payload=(b'\x00\xff\r\n'+bytes(range(256)))*600
    payload=payload[:length];kernel=Kernel(data=payload)
    assert reader.read_windows('snapshot.json',backend=kernel.backend())==payload
    assert kernel.closed==1 and kernel.opens[0][2]==7 and kernel.opens[0][4]==3

@pytest.mark.parametrize('close_error',[0,6])
def test_native_reader_model_read_error_survives_close(close_error):
    kernel=Kernel(read_error=1117,close_error=close_error)
    with pytest.raises(OSError) as caught:reader.read_windows('state.json',backend=kernel.backend())
    assert caught.value.winerror==1117 and caught.value.native_api_v862=='ReadFile'
    assert kernel.closed==1
    assert getattr(caught.value,'close_winerror_v862',None)==(close_error or None)


def test_native_reader_model_successful_read_with_failed_close_is_not_success():
    kernel=Kernel(data=b'{}',close_error=6)
    with pytest.raises(OSError) as caught:reader.read_windows('state.json',backend=kernel.backend())
    assert caught.value.winerror==6 and caught.value.native_api_v862=='CloseHandle' and kernel.closed==1


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows native read required')
@pytest.mark.parametrize('length',[0,1,65537,150000])
def test_native_reader_windows_exact_bytes(tmp_path,length):
    path=tmp_path/'знімок.json';payload=(bytes(range(256))*600)[:length];path.write_bytes(payload)
    assert reader.read_windows(path)==payload
    # Successful close means no exclusive reader is left behind.
    moved=path.with_suffix('.moved');path.replace(moved);moved.unlink()


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows shared handle required')
def test_native_reader_windows_shared_read_closes_before_replace(tmp_path):
    path=tmp_path/'state.json';path.write_bytes(b'{"status":"RUNNING"}')
    k,last_error,error_factory=reader.bindings()
    handle=k.CreateFileW(str(path),0x80000000,7,None,3,0x80,None)
    code=last_error() if handle in (None,C.c_void_p(-1).value) else 0
    assert handle not in (None,C.c_void_p(-1).value),str(error_factory(code))
    try:
        assert reader.read_windows(path)==b'{"status":"RUNNING"}'
    finally:assert k.CloseHandle(handle)
    # The test's external handle is now closed. An exclusive open must succeed:
    # a leaked GENERIC_READ handle from read_windows would make it fail.
    exclusive=k.CreateFileW(str(path),0x80000000,0,None,3,0x80,None)
    code=last_error() if exclusive in (None,C.c_void_p(-1).value) else 0
    assert exclusive not in (None,C.c_void_p(-1).value),str(error_factory(code))
    assert k.CloseHandle(exclusive)
    # No exception is accepted here, and neither reading nor replacement retries.
    other=path.with_suffix('.tmp');other.write_bytes(b'{"status":"STOPPED"}');other.replace(path)
    assert not other.exists() and reader.read_windows(path)==b'{"status":"STOPPED"}'
