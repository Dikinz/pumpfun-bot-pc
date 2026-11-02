"""WP2 single instance (spec 2.6): named mutex Local\\PumpfunPaperDesktop_<identity>, the second instance
sets <name>_show and leaves; the first shows its window. Fake kernel32 for the logic, real Windows below."""
import os,subprocess,sys,threading,time,uuid
import pytest
from fakes import forbid_live,ROOT
from desktop import single_instance as si

class FakeKernel:
    """Named kernel objects shared by every 'process' that uses this registry."""
    def __init__(self):self.objects={};self.handles={};self.next=100;self.error=0;self.closed=[]
    def _open(self,name,kind,create):
        obj=self.objects.get(name)
        if obj is None:
            if not create:self.error=2;return 0
            obj=self.objects[name]=dict(kind=kind,refs=0,signaled=False);self.error=0
        else:self.error=183 if create else 0
        obj['refs']+=1;self.next+=1;self.handles[self.next]=name;return self.next
    def CreateMutexW(self,attrs,owner,name):return self._open(name,'mutex',True)
    def CreateEventW(self,attrs,manual,initial,name):return self._open(name,'event',True)
    def OpenEventW(self,access,inherit,name):return self._open(name,'event',False)
    def SetEvent(self,h):self.objects[self.handles[h]]['signaled']=True;return 1
    def WaitForSingleObject(self,h,ms):
        obj=self.objects[self.handles[h]]
        if obj['signaled']:obj['signaled']=False;return si.WAIT_OBJECT_0
        time.sleep(min(ms,20)/1000);return si.WAIT_TIMEOUT
    def CloseHandle(self,h):
        name=self.handles.pop(h);self.closed.append(name);obj=self.objects[name];obj['refs']-=1
        if obj['refs']==0:del self.objects[name]
        return 1
    def api(self):return self,lambda:self.error

def test_names_follow_the_spec():
    assert si.names('abc')==('Local\\PumpfunPaperDesktop_abc','Local\\PumpfunPaperDesktop_abc_show')

def test_second_instance_signals_first_and_exits():
    k=FakeKernel();first=si.SingleInstance('id1',api=k.api())
    assert first.acquire() is True and first.primary
    assert first.wait_show(1) is False
    second=si.SingleInstance('id1',api=k.api())
    assert second.acquire() is False and second.primary is False and second.mutex is None
    assert first.wait_show(1) is True and first.wait_show(1) is False  # auto-reset: one request, one show
    other=si.SingleInstance('id2',api=k.api());assert other.acquire() is True  # another installation is independent
    first.close();assert 'Local\\PumpfunPaperDesktop_id1' not in k.objects
    third=si.SingleInstance('id1',api=k.api());assert third.acquire() is True
    third.close();other.close();assert not k.objects

def test_listen_calls_back_until_close():
    k=FakeKernel();first=si.SingleInstance('id',api=k.api());first.acquire()
    shown=threading.Event();first.listen(shown.set,timeout_ms=10)
    si.SingleInstance('id',api=k.api()).acquire()
    assert shown.wait(3)
    first.close();assert not first._thread.is_alive() and not k.objects

def test_second_instance_before_event_exists_does_not_fail():
    k=FakeKernel();k.CreateMutexW(None,False,'Local\\PumpfunPaperDesktop_x')  # first instance still starting
    assert si.SingleInstance('x',api=k.api()).acquire() is False

def test_installer_app_mutex_held_by_the_first_instance_only():
    '''Inno Setup AppMutex: the fixed name PumpfunPaperDesktopApp exists exactly while a window process runs.'''
    k=FakeKernel();first=si.SingleInstance('idA',api=k.api())
    assert first.app_mutex_name=='PumpfunPaperDesktopApp'==si.C.APP_MUTEX
    assert 'PumpfunPaperDesktopApp' not in k.objects
    assert first.acquire() and first.app_mutex and k.objects['PumpfunPaperDesktopApp']['refs']==1
    second=si.SingleInstance('idA',api=k.api());assert second.acquire() is False and second.app_mutex is None
    assert k.objects['PumpfunPaperDesktopApp']['refs']==1  # the second instance leaves at once and holds nothing
    other=si.SingleInstance('idB',api=k.api());assert other.acquire() and k.objects['PumpfunPaperDesktopApp']['refs']==2  # another folder
    first.close();assert k.objects['PumpfunPaperDesktopApp']['refs']==1 and first.app_mutex is None
    other.close();assert 'PumpfunPaperDesktopApp' not in k.objects and not k.objects
    assert k.closed.count('PumpfunPaperDesktopApp')==2

def test_installer_app_mutex_failure_does_not_stop_the_window():
    k=FakeKernel();real=k.CreateMutexW
    k.CreateMutexW=lambda attrs,owner,name:0 if name=='PumpfunPaperDesktopApp' else real(attrs,owner,name)
    first=si.SingleInstance('idC',api=k.api());assert first.acquire() is True and first.app_mutex is None
    first.close();assert not k.objects

def test_mutex_failure_raises():
    k=FakeKernel();k.CreateMutexW=lambda *a:0;k.error=5
    with pytest.raises(OSError,match='DESKTOP_MUTEX_FAILED'):si.SingleInstance('x',api=k.api()).acquire()

SECOND=r'''
import sys;sys.path.insert(0,sys.argv[1])
from desktop import single_instance as si
s=si.SingleInstance(sys.argv[2]);first=s.acquire();s.close()
raise SystemExit(0 if first is False else 7)
'''
@pytest.mark.skipif(os.name!='nt',reason='Windows kernel objects')
def test_real_windows_app_mutex_created_and_released():
    '''Same calls as for PumpfunPaperDesktopApp, under a unique name so a parallel run cannot collide.'''
    import ctypes
    from ctypes import wintypes as W
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenMutexW.argtypes=(W.DWORD,W.BOOL,W.LPCWSTR);k.OpenMutexW.restype=W.HANDLE;k.CloseHandle.argtypes=(W.HANDLE,)
    name='PumpfunPaperDesktopAppTest'+uuid.uuid4().hex[:12];SYNCHRONIZE=0x00100000
    def exists():
        h=k.OpenMutexW(SYNCHRONIZE,False,name)
        if h:k.CloseHandle(h)
        return bool(h)
    first=si.SingleInstance('test'+uuid.uuid4().hex[:12],app_mutex_name=name)
    assert not exists()
    try:assert first.acquire() is True and first.app_mutex and exists()
    finally:first.close()
    assert first.app_mutex is None and not exists()

@pytest.mark.skipif(os.name!='nt',reason='Windows kernel objects')
def test_real_windows_mutex_across_processes():
    ident='test'+uuid.uuid4().hex[:12]
    first=si.SingleInstance(ident)
    try:
        assert first.acquire() is True
        shown=threading.Event();first.listen(shown.set,timeout_ms=50)
        r=subprocess.run([sys.executable,'-I','-c',SECOND,str(ROOT),ident],capture_output=True,text=True,errors='replace',timeout=60)
        assert r.returncode==0,r.stdout+r.stderr
        assert shown.wait(5)
    finally:first.close()
    again=si.SingleInstance(ident)
    try:assert again.acquire() is True
    finally:again.close()
