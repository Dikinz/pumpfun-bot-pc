"""WP2 session end (spec 2.6): block reason and stop file only for this window's own running campaign;
the reason is removed after STOPPED; EXTERNAL_SAME never blocks Windows. Fake user32 for the logic,
plus a real hidden window that receives WM_QUERYENDSESSION sent only to itself."""
import os,threading,time
import pytest
from fakes import forbid_live,TOKEN,make_layout,write_state,Health,Clock,controller,C
from desktop import session_end as se
from desktop.controller import Status

class FakeApi:
    def __init__(self):self.calls=[]
    def block(self,hwnd,text):self.calls.append(('block',hwnd,text));return True
    def unblock(self,hwnd):self.calls.append(('unblock',hwnd));return True
    def priority(self,level=C.SHUTDOWN_PRIORITY):self.calls.append(('priority',level));return True

def setup(tmp_path,**fields):
    lay=make_layout(tmp_path)
    if fields:write_state(lay,**fields)
    ctl=controller(lay,health=Health().same(lay));api=FakeApi()
    return lay,ctl,api,se.SessionEndGuard(ctl.stop_for_session_end,api)

def stops(lay):return sorted(p.name for p in lay.control.glob('stop_*')) if lay.control.exists() else []

def test_own_running_blocks_and_starts_graceful_stop(tmp_path):
    lay,ctl,api,guard=setup(tmp_path,phase='RUNNING')
    assert guard.on_query(55)==0 and guard.blocking
    assert api.calls==[('block',55,'Идёт корректная остановка PAPER-бота')] and stops(lay)==['stop_'+TOKEN+'.request']
    assert guard.on_query(55)==0 and len(api.calls)==1 and len(stops(lay))==1  # repeated query: no second reason/file
    write_state(lay,phase='STOP_REQUESTED');s=ctl.refresh()
    assert not guard.needs_release(s) and guard.on_query(55)==0
    write_state(lay,phase='STOPPED',exit_code=0);s=ctl.refresh()
    assert guard.needs_release(s);guard.release(55)
    assert api.calls[-1]==('unblock',55) and not guard.blocking and guard.on_query(55)==1

@pytest.mark.parametrize('phase',['PREFLIGHT','STARTING'])
def test_logoff_during_start_is_held_like_running(tmp_path,phase):
    '''MINOR 4: the stop file is written at once (the host honours it once its stop_condition runs) and the
    session is held until the run has ended.'''
    lay,ctl,api,guard=setup(tmp_path,phase=phase)
    assert guard.on_query(7,se.ENDSESSION_LOGOFF)==0 and guard.blocking and guard.mode()==se.MODE_LOGOFF
    assert api.calls==[('block',7,C.TEXT['shutdown_block'])] and stops(lay)==['stop_'+TOKEN+'.request']
    assert guard.on_query(7)==0 and len(stops(lay))==1 and len(api.calls)==1
    assert not guard.needs_release(ctl.refresh())
    write_state(lay,phase='STOP_REQUESTED');assert not guard.needs_release(ctl.refresh())
    write_state(lay,phase='STOPPED',exit_code=0);assert guard.needs_release(ctl.refresh())

@pytest.mark.parametrize('phase',['RUNNING','STARTING','STOP_REQUESTED',None])
def test_restart_manager_closeapp_never_stops_or_holds(tmp_path,phase):
    '''MINOR 5: ENDSESSION_CLOSEAPP (Restart Manager, e.g. the installer) only closes the window process.'''
    lay,ctl,api,guard=setup(tmp_path,**(dict(phase=phase) if phase else {}))
    assert guard.on_query(3,se.ENDSESSION_CLOSEAPP)==1 and guard.on_query(3,se.ENDSESSION_CLOSEAPP|se.ENDSESSION_CRITICAL)==1
    assert api.calls==[] and stops(lay)==[] and not guard.blocking and guard.mode()==se.MODE_CLOSEAPP and guard.session_ending
    assert guard.on_end(3,False,se.ENDSESSION_CLOSEAPP)==0 and guard.mode() is None and not guard.session_ending

def test_session_mode_follows_query_and_cancel(tmp_path):
    lay,ctl,api,guard=setup(tmp_path)
    assert guard.mode() is None and not guard.session_ending
    assert guard.on_query(1,se.ENDSESSION_LOGOFF)==1 and guard.mode()==se.MODE_LOGOFF  # nothing to hold, but known
    assert guard.on_query(1,se.ENDSESSION_CLOSEAPP)==1 and guard.mode()==se.MODE_LOGOFF  # a log-off is not downgraded
    guard.on_end(1,True,0);assert guard.mode()==se.MODE_LOGOFF  # the session really ends
    guard.on_end(1,False,0);assert guard.mode() is None  # cancelled: the window behaves normally again
    assert guard.on_query(1,0)==1 and guard.mode()==se.MODE_LOGOFF  # shutdown (lParam 0) counts as log-off
    seen=[];threads=[threading.Thread(target=lambda:seen.append(guard.mode())) for _ in range(8)]
    for th in threads:th.start()
    for th in threads:th.join()
    assert seen==[se.MODE_LOGOFF]*8

@pytest.mark.parametrize('fields,health',[(None,'same'),(None,None),(dict(phase='STOPPED',exit_code=0),None),(dict(phase='FAILED',exit_code=1),None)])
def test_everything_else_lets_windows_continue(tmp_path,fields,health):
    lay=make_layout(tmp_path)
    if fields:write_state(lay,**fields)
    ctl=controller(lay,health=Health().same(lay) if health else Health());api=FakeApi();guard=se.SessionEndGuard(ctl.stop_for_session_end,api)
    assert guard.on_query(1)==1 and api.calls==[] and stops(lay)==[]

def test_engine_lost_and_errors_never_block(tmp_path):
    lay,ctl,api,guard=setup(tmp_path,phase='RUNNING')
    ctl.client._alive=lambda st:False  # the dashboard still answers: ORPHAN_RUNNING, which the window cannot stop
    assert ctl.refresh().kind=='ORPHAN_RUNNING' and guard.on_query(1)==1 and stops(lay)==[]
    ctl.client._health_get=Health();ctl.refresh();assert ctl.refresh().kind=='ENGINE_LOST'
    assert guard.on_query(1)==1 and stops(lay)==[]
    def boom():raise OSError('x')
    assert se.SessionEndGuard(boom,api).on_query(1)==1 and api.calls==[]

def test_release_after_engine_lost(tmp_path):
    lay,ctl,api,guard=setup(tmp_path,phase='RUNNING')
    guard.on_query(9);assert guard.needs_release(Status('ENGINE_LOST',{}))
    assert not guard.needs_release(None) and not guard.needs_release(Status('OWN_RUNNING',{}))

@pytest.mark.skipif(os.name!='nt',reason='Win32 window')
def test_real_hidden_window_receives_query_end_session():
    import ctypes
    from ctypes import wintypes as W
    decision=dict(hold=True);api=FakeApi()
    guard=se.SessionEndGuard(lambda:decision['hold'],api);win=se.HiddenWindow(guard,set_priority=False)
    hwnd=win.start()
    try:
        u=ctypes.WinDLL('user32',use_last_error=True)
        u.SendMessageW.argtypes=(W.HWND,W.UINT,W.WPARAM,W.LPARAM);u.SendMessageW.restype=ctypes.c_ssize_t
        u.IsWindowVisible.argtypes=(W.HWND,);u.IsWindow.argtypes=(W.HWND,)
        assert u.IsWindow(hwnd) and not u.IsWindowVisible(hwnd)
        assert u.SendMessageW(hwnd,se.WM_QUERYENDSESSION,0,se.ENDSESSION_LOGOFF)==0
        assert api.calls[0][0]=='block' and api.calls[0][1]==hwnd and guard.blocking
        assert win.update(Status('OWN_RUNNING',{})) is False
        assert win.update(Status('STOPPED',{})) is True
        deadline=time.monotonic()+5
        while guard.blocking and time.monotonic()<deadline:time.sleep(.02)
        assert not guard.blocking and api.calls[-1]==('unblock',hwnd)
        decision['hold']=False
        assert u.SendMessageW(hwnd,se.WM_QUERYENDSESSION,0,se.ENDSESSION_LOGOFF)==1 and guard.mode()==se.MODE_LOGOFF
        assert u.SendMessageW(hwnd,se.WM_ENDSESSION,0,se.ENDSESSION_LOGOFF)==0 and guard.mode() is None  # wParam FALSE: cancelled
        decision['hold']=True;calls=len(api.calls)
        assert u.SendMessageW(hwnd,se.WM_QUERYENDSESSION,0,se.ENDSESSION_CLOSEAPP)==1  # Restart Manager: lParam reaches the guard
        assert guard.mode()==se.MODE_CLOSEAPP and len(api.calls)==calls
    finally:win.close()
    assert not win._thread.is_alive() and win.hwnd is None

@pytest.mark.skipif(os.name!='nt',reason='Win32 API')
def test_real_user32_block_reason_on_own_window():
    """ShutdownBlockReasonCreate/Destroy succeed on the window thread (as Windows requires)."""
    api=se.User32Api();results=[]
    class Recording:
        def block(self,hwnd,text):results.append(api.block(hwnd,text));return results[-1]
        def unblock(self,hwnd):results.append(api.unblock(hwnd));return results[-1]
        def priority(self,level=C.SHUTDOWN_PRIORITY):return True
    import ctypes
    from ctypes import wintypes as W
    guard=se.SessionEndGuard(lambda:True,Recording());win=se.HiddenWindow(guard,set_priority=False);hwnd=win.start()
    try:
        u=ctypes.WinDLL('user32',use_last_error=True);u.SendMessageW.argtypes=(W.HWND,W.UINT,W.WPARAM,W.LPARAM);u.SendMessageW.restype=ctypes.c_ssize_t
        assert u.SendMessageW(hwnd,se.WM_QUERYENDSESSION,0,0)==0
        win.update(Status('STOPPED',{}))
        deadline=time.monotonic()+5
        while guard.blocking and time.monotonic()<deadline:time.sleep(.02)
    finally:win.close()
    assert results==[True,True]

# ---- MINOR 2 round 2: the session-end mode is not sticky ----
def test_query_without_endsession_expires_on_the_monotonic_clock(tmp_path):
    lay=make_layout(tmp_path);ctl=controller(lay);clock=Clock();guard=se.SessionEndGuard(ctl.stop_for_session_end,FakeApi(),clock=clock)
    assert guard.ttl==se.MODE_TTL_S==60.
    assert guard.on_query(1,se.ENDSESSION_LOGOFF)==1 and guard.mode()==se.MODE_LOGOFF  # third-party broadcast, idle window
    clock.advance(59.9);assert guard.mode()==se.MODE_LOGOFF and guard.session_ending
    clock.advance(.1);assert guard.mode() is None and not guard.session_ending
    assert guard.on_query(1,se.ENDSESSION_CLOSEAPP)==1 and guard.mode()==se.MODE_CLOSEAPP
    clock.advance(60);assert guard.mode() is None
    guard.on_query(1,0);clock.advance(30);guard.on_query(1,0);clock.advance(59);assert guard.mode()==se.MODE_LOGOFF  # renewed
    clock.advance(1);assert guard.mode() is None

def test_endsession_true_keeps_the_mode(tmp_path):
    lay=make_layout(tmp_path);ctl=controller(lay);clock=Clock();guard=se.SessionEndGuard(ctl.stop_for_session_end,FakeApi(),clock=clock)
    guard.on_query(1,se.ENDSESSION_LOGOFF);guard.on_end(1,True,se.ENDSESSION_LOGOFF)
    clock.advance(3600);assert guard.mode()==se.MODE_LOGOFF  # the session really ends
    guard.observe(Status('STOPPED',{}));assert guard.mode()==se.MODE_LOGOFF
    guard.on_end(1,False,0);assert guard.mode() is None

def test_held_query_is_cleared_when_stopped_and_expires_too(tmp_path):
    lay,ctl,api,guard=setup(tmp_path,phase='RUNNING');clock=Clock();guard.clock=clock
    assert guard.on_query(5,se.ENDSESSION_LOGOFF)==0 and guard.mode()==se.MODE_LOGOFF and stops(lay)==['stop_'+TOKEN+'.request']
    guard.observe(ctl.refresh());assert guard.mode()==se.MODE_LOGOFF  # still running: held
    write_state(lay,phase='STOP_REQUESTED');guard.observe(ctl.refresh());assert guard.mode()==se.MODE_LOGOFF
    write_state(lay,phase='STOPPED',exit_code=0);guard.observe(ctl.refresh())
    assert guard.mode() is None and guard.blocking  # mode cleared; the block reason goes with HiddenWindow.update/release
    assert len(stops(lay))==1
    lay2,ctl2,api2,guard2=setup(tmp_path/'b',phase='RUNNING');clock2=Clock();guard2.clock=clock2
    assert guard2.on_query(5,se.ENDSESSION_LOGOFF)==0
    clock2.advance(60);assert guard2.mode() is None and len(stops(lay2))==1  # no WM_ENDSESSION: expires as well

def test_stopped_does_not_clear_an_idle_logoff(tmp_path):
    '''The window was idle (STOPPED) when the log-off came: its own 'closing' must still see the log-off, so a
    STOPPED poll does not clear a query that held nothing (only the TTL or WM_ENDSESSION(FALSE) does).'''
    lay=make_layout(tmp_path);ctl=controller(lay);clock=Clock();guard=se.SessionEndGuard(ctl.stop_for_session_end,FakeApi(),clock=clock)
    assert guard.on_query(1,se.ENDSESSION_LOGOFF)==1
    s=ctl.refresh();assert s.kind=='STOPPED';guard.observe(s);guard.observe(None);assert guard.mode()==se.MODE_LOGOFF

def test_hidden_window_update_observes_the_status(tmp_path):
    lay,ctl,api,guard=setup(tmp_path,phase='RUNNING')
    assert guard.on_query(5)==0;write_state(lay,phase='STOPPED',exit_code=0)
    hidden=se.HiddenWindow(guard,set_priority=False);sent=[];hidden._send=lambda msg:sent.append(msg) or True
    assert hidden.update(ctl.refresh()) is True and sent==[se.WM_RELEASE] and guard.mode() is None
