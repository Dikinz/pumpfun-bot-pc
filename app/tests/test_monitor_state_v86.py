"""Read failures never become fictitious heartbeat gaps. Temporary PAPER only."""
import asyncio,errno,json,os,sqlite3,threading,time,zipfile
from pathlib import Path
from contextlib import closing
import pytest
import config,main,monitor_service as ms,monitor_state as state,branch_isolation as bi,system_health as sh,paper_capital as cap
from test_primary_integrity import fresh
from test_p0_isolation_v79 import tree_snapshot


def record(value):
    if os.getenv('MONITOR_STATE_EVIDENCE'):
        with open(os.environ['MONITOR_STATE_EVIDENCE'],'a') as f:f.write(json.dumps(value)+'\n')

@pytest.fixture(autouse=True)
def audit(fresh,tmp_path,monkeypatch,request):
    monkeypatch.setattr(__import__('recovery_gate'),'ACTIVE',None)
    __import__('wallet_costs').ensure(fresh);assert main.buy('read-state-position',1)[0]
    cap.record(fresh,main.P['read-state-position']['trade_id'],'BUY',3,{'pool_fee_rate':.0125})
    before=tree_snapshot();money=cap.summary(fresh)
    def integrity():
        result={}
        for p in tmp_path.rglob('*.db'):
            with closing(sqlite3.connect(p)) as c:result[p.name]=c.execute('PRAGMA integrity_check').fetchone()[0]
        assert all(x=='ok' for x in result.values());return result
    initial=integrity();yield;final=integrity()
    after=tree_snapshot();assert all(before.get(pid)==birth for pid,birth in after.items())
    current=cap.summary(fresh)
    for key in ('net_cash','reserved','available','open_principal','total_cost','closed_net'):assert current[key]==money[key]
    assert current['open_principal']==3 and not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()
    record(dict(kind='AUDIT',test=request.node.name,before=before,after=after,integrity_before=initial,integrity_after=final,open_principal=3))

def snapshot(pulse=100.,pid=101,birth=1000.,session='session-a',status='RUNNING'):
    return dict(status=status,pid=pid,monotonic_ts=pulse,heartbeat_ts=1700000000.+pulse,
                monitor_identity_v86=dict(pid=pid,created_ts=birth,session=session))

def fail_read(monkeypatch,exc):
    path=ms.paths()['state'];original=state._read_bytes
    def read(p):
        if p==path:raise exc
        return original(p)
    monkeypatch.setattr(state,'_read_bytes',read)

@pytest.mark.parametrize('exc,reason',[(FileNotFoundError(errno.ENOENT,'missing'),'MONITOR_STATE_MISSING'),(PermissionError(errno.EACCES,'sharing denied'),'MONITOR_STATE_PERMISSION_DENIED'),(OSError(errno.EIO,'disk I/O'),'MONITOR_STATE_IO_ERROR')])
def test_monitor_state_os_error_is_exact(monkeypatch,exc,reason):
    if isinstance(exc,PermissionError):exc.winerror=5
    fail_read(monkeypatch,exc);result=ms.read_state();error=result['state_read_error_v86']
    assert result['status']=='STATE_READ_ERROR' and 'monotonic_ts' not in result
    assert error['reason']==reason and error['errno']==exc.errno and error['error_type']==type(exc).__name__
    assert error['path']==str(ms.paths()['state']) and error['exception']['chain'][0]['error_type']==type(exc).__name__
    if isinstance(exc,PermissionError):assert error['winerror']==5
    record(dict(kind='OS_ERROR',result=result))

@pytest.mark.parametrize('payload,reason',[(b'{partial','MONITOR_STATE_INVALID_JSON'),(b'[]','MONITOR_STATE_INVALID_SCHEMA'),(b'\xff','MONITOR_STATE_INVALID_ENCODING'),(b'{"status":"RUNNING","monotonic_ts":NaN}','MONITOR_STATE_INVALID_MONOTONIC_TS'),(b'{"status":"RUNNING","monotonic_ts":true}','MONITOR_STATE_INVALID_MONOTONIC_TS'),(b'{"status":"RUNNING","monotonic_ts":"100"}','MONITOR_STATE_INVALID_MONOTONIC_TS'),(b'{"status":"RUNNING","heartbeat_ts":Infinity}','MONITOR_STATE_INVALID_HEARTBEAT_TS'),(b'{"status":"RUNNING","monotonic_ts":'+b'9'*500+b'}','MONITOR_STATE_INVALID_MONOTONIC_TS'),(b'['*2000+b']'*2000,('MONITOR_STATE_INVALID_JSON','MONITOR_STATE_INVALID_SCHEMA'))],ids=['bad-json','array','encoding','nan-pulse','bool-pulse','string-pulse','infinite-wall','huge-integer','deep-array'])
def test_monitor_state_invalid_payload_is_not_heartbeat(payload,reason):
    ms.paths()['state'].write_bytes(payload);result=ms.read_state();details=result['state_read_error_v86']
    assert details['reason'] in (reason if isinstance(reason,tuple) else (reason,))
    assert details['bytes']==len(payload) and len(details['sha256'])==64
    assert result['status']=='STATE_READ_ERROR' and 'monotonic_ts' not in result
    if details['error_type']=='JSONDecodeError':assert details['lineno']==1 and details['pos']>=0


def test_monitor_state_long_uptime_read_failure_keeps_real_age(monkeypatch):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0])
    now[0]=3384.;ms.publish(snapshot(3384.));assert watch.sample()['status']=='RUNNING'
    now[0]=3385.;error=PermissionError(errno.EACCES,'sharing denied');error.winerror=32
    with monkeypatch.context() as patch:
        fail_read(patch,error);result=watch.sample()
    assert result['status']=='MONITOR_STATE_READ_ERROR' and result['age_s']==1 and result['age_s']!=3285
    assert result['age_basis_v86']=='LAST_VALID_SNAPSHOT' and result['snapshot_is_current_v86'] is False
    assert result['last_valid_snapshot_v86']['monitor_identity_v86']['session']=='session-a'
    assert result['state_read_error_v86']['winerror']==32
    alerts=[x for x in sh.history(config.DB) if x.get('kind')=='ALERT_OPEN']
    assert alerts[-1]['code']=='MONITOR_STATE_READ_ERROR' and not any(x.get('code')=='MONITOR_NOT_RUNNING' for x in alerts)
    now[0]=3386.;ms.publish(snapshot(3386.));recovered=watch.sample();assert recovered['status']=='RUNNING'
    assert recovered['last_read_incident_v86']['resolved'] is True
    assert recovered['last_read_incident_v86']['first_error']['winerror']==32
    saved=json.loads(ms.paths()['supervision'].read_text());assert saved['last_read_incident_v86']['trigger_last_valid']['monotonic_ts']==3384.
    record(dict(kind='CAPTURED_3285S_REGRESSION',failed=result,recovered=recovered))


def test_monitor_state_repeated_errors_do_not_refresh_last_pulse(monkeypatch):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());watch.sample()
    fail_read(monkeypatch,PermissionError(errno.EACCES,'denied'))
    for tick in (101.,110.,115.9):
        now[0]=tick;result=watch.sample();assert result['status']=='MONITOR_STATE_READ_ERROR' and result['age_s']==tick-100.
    now[0]=116.;result=watch.sample();assert result['status']=='MONITOR_NOT_RUNNING' and result['age_s']==16
    assert result['reason']=='LAST_VALID_HEARTBEAT_EXPIRED_DURING_READ_ERROR'
    assert result['read_error_streak_v86']==4 and result['last_valid_snapshot_v86']['monotonic_ts']==100.
    assert watch.alert_codes=={'MONITOR_STATE_READ_ERROR','MONITOR_NOT_RUNNING'}
    assert not any(x.get('kind')=='ALERT_CLEARED' and x.get('code')=='MONITOR_STATE_READ_ERROR' for x in sh.history(config.DB))
    record(dict(kind='REAL_STALE_WITH_UNREADABLE_FILE',result=result))


def test_monitor_state_without_any_heartbeat_has_unknown_age(monkeypatch):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);fail_read(monkeypatch,FileNotFoundError(errno.ENOENT,'missing'))
    now[0]=4000.;result=watch.sample()
    assert result['status']=='MONITOR_STATE_READ_ERROR' and result['age_s'] is None
    assert result['age_basis_v86']=='UNKNOWN' and result['last_valid_snapshot_v86'] is None

@pytest.mark.parametrize('replacement',[dict(pid=102),dict(birth=1001.),dict(session='session-b')])
def test_monitor_state_identity_change_does_not_borrow_old_pulse(monkeypatch,replacement):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());watch.sample()
    new=snapshot(**replacement);new.pop('monotonic_ts');now[0]=101.;ms.publish(new)
    result=watch.sample();assert result['status']=='MONITOR_HEARTBEAT_UNKNOWN' and result['last_valid_snapshot_v86'] is None
    fail_read(monkeypatch,PermissionError(errno.EACCES,'denied'));now[0]=200.
    result=watch.sample();assert result['status']=='MONITOR_STATE_READ_ERROR' and result['age_s'] is None


def test_monitor_state_cache_is_observation_only(monkeypatch):
    observer=state.Observer();ms.publish(snapshot());observer.observe(ms.read_state(),100.)
    fail_read(monkeypatch,PermissionError(errno.EACCES,'denied'))
    raw=ms.read_state();observed=observer.observe(raw,101.);service=ms.service_status()
    assert observed['status']!='RUNNING' and observed['observation_only_v86'] is True
    assert observed['last_valid_snapshot_v86']['observed_only_v86'] is True
    assert 'monotonic_ts' not in raw and service['status']=='STATE_READ_ERROR' and service['stale'] is True


def test_monitor_state_regressed_pulse_keeps_last_valid(monkeypatch):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());watch.sample()
    now[0]=101.;ms.publish(snapshot(90.));result=watch.sample()
    assert result['status']=='MONITOR_STATE_READ_ERROR' and result['age_s']==1
    assert result['state_read_error_v86']['reason']=='MONITOR_STATE_HEARTBEAT_REGRESSED'
    assert result['last_valid_snapshot_v86']['monotonic_ts']==100.


def test_monitor_state_recorder_does_not_relabel_read_error(monkeypatch):
    now=[100.];monkeypatch.setattr(sh.time,'monotonic',lambda:now[0]);recorder=sh.Recorder(config.DB)
    now[0]=200.;ms.publish(snapshot(200.));assert recorder.sample({},force=True)['monitor_status']=='RUNNING'
    now[0]=201.;fail_read(monkeypatch,PermissionError(errno.EACCES,'denied'))
    result=recorder.sample({},force=True)
    assert result['monitor_status']=='MONITOR_STATE_READ_ERROR'
    assert 'MONITOR_STATE_READ_ERROR' in result['alerts'] and 'MONITOR_NOT_RUNNING' not in result['alerts']
    detail=result['monitor_supervision_v86'];assert detail['age_s']==1 and detail['last_valid_snapshot_v86']['monotonic_ts']==200.
    alert=next(x for x in sh.history(config.DB) if x.get('kind')=='ALERT_OPEN' and x.get('code')=='MONITOR_STATE_READ_ERROR')
    assert alert['supervision']['state_read_error_v86']['errno']==errno.EACCES


def test_monitor_state_current_heartbeat_expires_at_same_threshold():
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());assert watch.sample()['status']=='RUNNING'
    now[0]=115.99;assert watch.sample()['status']=='RUNNING'
    now[0]=116.;result=watch.sample();assert result['status']=='MONITOR_NOT_RUNNING' and result['age_s']==16 and result['age_basis_v86']=='CURRENT_SNAPSHOT'


def test_monitor_state_known_failure_is_not_hidden_by_read_error(monkeypatch):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());watch.sample()
    ms.publish(snapshot(status='FAILED'));assert watch.sample()['status']=='MONITOR_NOT_RUNNING'
    fail_read(monkeypatch,PermissionError(errno.EACCES,'denied'));now[0]=101.;result=watch.sample()
    assert result['status']=='MONITOR_NOT_RUNNING' and result['reason']=='LAST_KNOWN_FAILED' and result['state_read_error_v86']


def test_monitor_state_export_preserves_read_incident(tmp_path,monkeypatch):
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());watch.sample()
    with monkeypatch.context() as patch:
        fail_read(patch,PermissionError(errno.EACCES,'denied'));now[0]=101.;watch.sample()
    now[0]=102.;ms.publish(snapshot(102.));watch.sample()
    out=ms.export(config.DB,tmp_path/'export')
    with zipfile.ZipFile(out) as z:
        name=ms.paths()['supervision'].name;assert name in z.namelist()
        incident=json.loads(z.read(name))['last_read_incident_v86']
        assert incident['resolved'] and incident['first_error']['errno']==errno.EACCES
    record(dict(kind='EXPORTED_READ_INCIDENT',incident=incident))


def test_monitor_state_producer_publishes_process_session_identity(monkeypatch):
    stop=threading.Event();__import__('recovery_gate').Coordinator(config.DB,'producer-session')
    async def worker(job,c):
        __import__('position_monitor').finish(c,job,dict(status='REQUEST_ERROR'));stop.set()
    monkeypatch.setattr(__import__('position_monitor'),'run_job',worker)
    asyncio.run(ms.run(config.DB,0,stop_event=stop));current=ms.read_state();ident=current['monitor_identity_v86']
    assert ident['pid']==__import__('late_spawn').own_identity()[0] and ident['created_ts']>0 and ident['session']=='producer-session'


def test_monitor_state_persisted_producer_error_is_not_current_read_failure(monkeypatch):
    observer=state.Observer();ms.publish(snapshot());observer.observe(ms.read_state(),100.)
    with monkeypatch.context() as patch:
        fail_read(patch,PermissionError(errno.EACCES,'prior producer read failed'));old=ms.read_state()
    failed=dict(snapshot(status='FAILED'),state_read_error_v86=old['state_read_error_v86'])
    ms.publish(failed);current=ms.read_state();assert 'state_read_error_v86' not in current
    assert current['producer_state_read_error_v86']['errno']==errno.EACCES
    result=observer.observe(current,101.);assert result['status']=='MONITOR_NOT_RUNNING' and result['reason']=='MONITOR_STATE_FAILED'


def test_monitor_state_invalid_new_identity_clears_old_cached_heartbeat():
    observer=state.Observer();ms.publish(snapshot());observer.observe(ms.read_state(),100.)
    invalid=snapshot(pid=202,birth=2000.,session='session-b');invalid['monotonic_ts']=float('nan')
    ms.paths()['state'].write_text(json.dumps(invalid));result=observer.observe(ms.read_state(),200.)
    assert result['status']=='MONITOR_STATE_READ_ERROR' and result['age_s'] is None and result['last_valid_snapshot_v86'] is None


@pytest.mark.skipif(os.name!='nt',reason='Actual Win32 sharing-violation read requires Windows')
def test_monitor_state_native_windows_sharing_violation_preserves_heartbeat():
    import ctypes as C
    now=[100.];watch=bi.MonitorWatch(config.DB,lambda:now[0]);ms.publish(snapshot());watch.sample()
    k=C.WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.argtypes=[C.c_wchar_p,C.c_uint32,C.c_uint32,C.c_void_p,C.c_uint32,C.c_uint32,C.c_void_p];k.CreateFileW.restype=C.c_void_p
    k.CloseHandle.argtypes=[C.c_void_p];k.CloseHandle.restype=C.c_int
    handle=k.CreateFileW(str(ms.paths()['state']),0x80000000,0,None,3,0,None)
    assert handle not in (None,C.c_void_p(-1).value)
    try:
        now[0]=101.;result=watch.sample()
        record(dict(kind='NATIVE_WINDOWS_SHARING_ATTEMPT',failed=result))
        assert result['status']=='MONITOR_STATE_READ_ERROR' and result['age_s']==1
        assert result['state_read_error_v86']['winerror']==32
        assert result['state_read_error_v86']['native_api_v862']=='CreateFileW'
        assert result['state_read_error_v86']['native_error_source_v862']=='WIN32_LAST_ERROR_CAPTURED_AT_CALL'
        assert result['last_valid_snapshot_v86']['monotonic_ts']==100.
    finally:assert k.CloseHandle(handle)
    now[0]=102.;ms.publish(snapshot(102.));recovered=watch.sample();assert recovered['status']=='RUNNING'
    record(dict(kind='NATIVE_WINDOWS_SHARING_VIOLATION',failed=result,recovered=recovered))
