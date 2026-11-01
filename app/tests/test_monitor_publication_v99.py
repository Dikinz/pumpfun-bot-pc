"""Publication failures are visible; heartbeat and mandatory STOP stay strict."""
import errno,json,os,time
from pathlib import Path
import pytest
import branch_isolation as bi,config,monitor_service as ms,runtime_control as control,state_io
import monitor_publication_v99 as publication
from test_primary_integrity import fresh


def snapshot(pulse=100.):
    return dict(status='RUNNING',pid=101,monotonic_ts=pulse,heartbeat_ts=1700000000.+pulse,
        monitor_identity_v86=dict(pid=101,created_ts=1000.,session='test-v99'))


def denied(code):
    exc=PermissionError(errno.EACCES,'PRIVATE https://SECRET.invalid/?token=SECRET');exc.winerror=code
    return exc


def hold_replace(monkeypatch,target,code=5,count=None):
    original=Path.replace;calls=[]
    def replace(path,destination):
        if Path(destination)==target:
            calls.append(path)
            if count is None or len(calls)<=count:raise denied(code)
        return original(path,destination)
    monkeypatch.setattr(Path,'replace',replace);monkeypatch.setattr(state_io,'_pause',lambda s:None)
    return calls


@pytest.mark.parametrize('code',[5,32,33])
def test_pub99_transient_replace_is_retried_with_exact_codes(tmp_path,monkeypatch,code):
    target=tmp_path/'status.json';calls=hold_replace(monkeypatch,target,code,2)
    r=publication.Publisher().publish(target,snapshot())
    assert r['status']=='PUBLISHED' and len(calls)==3
    log=json.loads(state_io.log_path(target).read_text().splitlines()[-1])
    assert log['status']=='RECOVERED' and [x['winerror'] for x in log['failures']]==[code,code]
    assert json.loads(target.read_text())['monotonic_ts']==100.


@pytest.mark.parametrize('code',[5,32,33])
def test_pub99_monitor_continues_and_recovers_latest_observation(fresh,monkeypatch,code):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());watch.sample()
    target=Path(config.DB).with_name(Path(config.DB).stem+'_monitor_supervision_v79.json');old=target.read_bytes()
    before=list(fresh.execute('SELECT * FROM trades'))
    with monkeypatch.context() as patch:
        calls=hold_replace(patch,target,code)
        for i in range(3):
            now[0]+=1;ms.publish(snapshot(now[0]));r=watch.sample()
            assert r['status']=='RUNNING' and r['publication_v99']['status']=='FAILED'
            assert r['age_s']==0 and publication.ALERT in watch.alert_codes
        assert len(calls)==12 and target.read_bytes()==old
        detail=r['publication_v99']['error']['chain'][0]
        assert detail['winerror']==code and detail['state_write_v87']['stage']=='REPLACE'
        assert 'SECRET' not in json.dumps(r)
    now[0]+=1;ms.publish(snapshot(now[0]));r=watch.sample();saved=json.loads(target.read_text())
    assert r['publication_v99']['status']=='PUBLISHED' and saved['age_s']==0
    assert saved['publication_v99']['last_incident']['resolved'] is True
    assert saved['publication_v99']['last_incident']['first_error']['chain'][0]['winerror']==code
    assert publication.ALERT not in watch.alert_codes and list(fresh.execute('SELECT * FROM trades'))==before


@pytest.mark.parametrize('code',[5,32,33])
def test_pub99_stop_remains_strict_after_exhausted_retries(tmp_path,monkeypatch,code):
    primary=tmp_path/'paper.db';target=control.control_path(primary,'session');calls=hold_replace(monkeypatch,target,code)
    with pytest.raises(PermissionError) as err:control.request_stop(primary,'session')
    assert err.value.winerror==code and len(calls)==4 and not target.exists()
    assert not control.stop_requested(primary,'session')
    assert err.value.state_write_v87['stage']=='REPLACE'


def test_pub99_stop_create_temp_failure_is_not_acknowledged(tmp_path,monkeypatch):
    def fail(*a,**kw):raise OSError(errno.ENOSPC,'disk full')
    monkeypatch.setattr(state_io.tempfile,'mkstemp',fail)
    with pytest.raises(OSError) as err:control.request_stop(tmp_path/'p.db','session')
    assert err.value.errno==errno.ENOSPC and err.value.state_write_v87['stage']=='CREATE_TEMP'


@pytest.mark.parametrize('value',[{'bad':object()},{'bad':float('nan')}])
def test_pub99_serialization_errors_are_not_suppressed(tmp_path,value):
    with pytest.raises((TypeError,ValueError)):publication.Publisher().publish(tmp_path/'s.json',value)


def test_pub99_denied_temp_cleanup_keeps_one_pending_file_and_original_error(tmp_path,monkeypatch):
    target=tmp_path/'s.json';publisher=publication.Publisher();unlink=Path.unlink
    with monkeypatch.context() as patch:
        calls=hold_replace(patch,target,5)
        def fail(path,*a,**kw):
            if path.suffix=='.tmp':raise denied(33)
            return unlink(path,*a,**kw)
        patch.setattr(Path,'unlink',fail)
        r=publisher.publish(target,snapshot())
        assert r['error']['chain'][0]['winerror']==5
        assert r['error']['chain'][0]['temp_cleanup_v99']['winerror']==33
        for i in range(20):r=publisher.publish(target,snapshot(101.+i))
        assert len(calls)==4 and len(list(tmp_path.glob('*.tmp')))==1
        assert r['temp_cleanup_pending'] and r['error']['chain'][0]['publication_operation_v99']=='RETRY_TEMP_CLEANUP'
        assert 'SECRET' not in json.dumps(r)
    r=publisher.publish(target,snapshot(200.))
    assert r['status']=='PUBLISHED' and json.loads(target.read_text())['monotonic_ts']==200.
    assert not list(tmp_path.glob('*.tmp'))


def test_pub99_failed_publication_never_refreshes_unreadable_heartbeat(fresh,monkeypatch):
    import monitor_state
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());watch.sample()
    target=Path(config.DB).with_name(Path(config.DB).stem+'_monitor_supervision_v79.json')
    hold_replace(monkeypatch,target)
    original=monitor_state._read_bytes
    def fail(path):
        if path==ms.paths()['state']:raise denied(32)
        return original(path)
    monkeypatch.setattr(monitor_state,'_read_bytes',fail)
    for tick in (101.,110.,115.9):
        now[0]=tick;r=watch.sample();assert r['status']=='MONITOR_STATE_READ_ERROR' and r['age_s']==tick-100.
    now[0]=116.;r=watch.sample()
    assert r['status']=='MONITOR_NOT_RUNNING' and r['age_s']==16
    assert {'MONITOR_NOT_RUNNING','MONITOR_STATE_READ_ERROR',publication.ALERT}==watch.alert_codes


def test_pub99_bounded_incident_recovers_without_unbounded_history(tmp_path,monkeypatch):
    target=tmp_path/'s.json';p=publication.Publisher()
    with monkeypatch.context() as patch:
        hold_replace(patch,target)
        for i in range(80):r=p.publish(target,snapshot(float(i)))
        assert r['last_incident']['failures']==80 and len(json.dumps(r))<5000
    assert p.publish(target,snapshot(200.))['last_incident']['resolved'] is True


def test_pub99_error_preserves_chain_without_credentials():
    try:
        try:raise denied(5)
        except OSError as exc:raise RuntimeError('SECRET') from exc
    except Exception as exc:detail=publication.error(exc)
    assert [r['error_type'] for r in detail['chain']]==['RuntimeError','PermissionError']
    assert detail['chain'][1]['winerror']==5 and detail['chain'][0]['frames']
    assert 'SECRET' not in json.dumps(detail)


def test_pub99_export_keeps_original_evidence_and_marks_copy_failure(tmp_path,monkeypatch):
    primary=tmp_path/'p.db';target=tmp_path/'p_monitor_supervision_v79.json'
    target.write_text('{"old":true}');state_io.log_path(target).write_text('{"winerror":5}\n')
    original=target.read_bytes();r=publication.export(primary,tmp_path/'out')
    assert r['status']=='CAPTURED' and (tmp_path/'out'/target.name).read_bytes()==original
    assert target.read_bytes()==original and state_io.log_path(target).name in r['files']
    import shutil
    monkeypatch.setattr(shutil,'copy2',lambda *a,**k:(_ for _ in ()).throw(denied(5)))
    assert publication.export(primary,tmp_path/'failed')['status']=='PARTIAL'


def test_pub99_metadata_parser_exposes_invalid_status():
    assert publication.from_meta({})=={}
    for raw in ('[1]','{broken',None):assert publication.from_meta({'monitor_publication_v99':raw})['status']=='UNREADABLE'


def test_pub99_native_sharing_denial_or_portable_model_recovers(tmp_path,monkeypatch):
    target=tmp_path/'s.json';target.write_text('{"old":true}');p=publication.Publisher()
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        handle=kernel.CreateFileW(str(target),0x80000000,3,None,3,0,None)
        assert handle not in (None,ctypes.c_void_p(-1).value)
        try:r=p.publish(target,snapshot())
        finally:assert kernel.CloseHandle(handle)
        assert r['status']=='FAILED' and r['error']['chain'][0]['winerror'] in (5,32,33)
    else:
        with monkeypatch.context() as patch:
            hold_replace(patch,target,32);r=p.publish(target,snapshot())
            assert r['status']=='FAILED'
    assert p.publish(target,snapshot(200.))['status']=='PUBLISHED'
    assert json.loads(target.read_text())['monotonic_ts']==200.


def test_pub99_health_and_dashboard_diagnostics_expose_publication_failure(fresh,monkeypatch):
    import database as db,system_health as health,strategy_diagnostics as diagnostics
    now=time.monotonic();ms.publish(snapshot(now))
    target=Path(config.DB).with_name(Path(config.DB).stem+'_monitor_supervision_v79.json')
    hold_replace(monkeypatch,target,5)
    r=bi.MonitorWatch(config.DB).sample();db.setm('monitor_publication_v99',json.dumps(r['publication_v99']))
    sampled=health.Recorder(config.DB).sample({},force=True)
    assert publication.ALERT in sampled['alerts'] and sampled['monitor_status']=='RUNNING'
    report=diagnostics.summary(fresh,{},config.DB)
    assert report['monitor_publication_v99']['status']=='FAILED'
    assert any(i['code']==publication.ALERT for i in report['issues'])


def test_pub99_unhandled_monitor_failure_keeps_original_os_cause(fresh):
    import system_health as health
    try:
        try:raise denied(5)
        except OSError as exc:raise RuntimeError('PRIVATE monitor wrapper') from exc
    except Exception as exc:health.failure(config.DB,'monitor',exc)
    row=next(r for r in reversed(health.history(config.DB)) if r.get('kind')=='UNHANDLED_ERROR')
    assert row['exception_v99']['chain'][1]['winerror']==5
    assert 'PRIVATE' not in json.dumps(row)
