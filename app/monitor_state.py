"""Monitor-state read evidence. Cached heartbeat is observation only, never a permit."""
import errno,hashlib,json,math,time,os
from pathlib import Path

def _read_bytes(path):
    if os.name=='nt':return __import__('monitor_file_reader').read_windows(path)
    return path.read_bytes()

def number(value):
    if type(value) not in (int,float):return False
    try:return math.isfinite(value)
    except OverflowError:return False

def identity(state):
    explicit=state.get('monitor_identity_v86') or {}
    legacy=state.get('late_creation_v85') or {}
    admission=state.get('recovery_admission_v77') or {}
    if not all(isinstance(x,dict) for x in (explicit,legacy,admission)):return None
    result=dict(pid=explicit.get('pid',legacy.get('monitor_pid',state.get('pid'))),
                created_ts=explicit.get('created_ts',legacy.get('monitor_created_ts',state.get('started_ts'))),
                session=explicit.get('session',admission.get('session')))
    if type(result['pid'])!=int or result['pid']<=0 or not number(result['created_ts']) or result['created_ts']<=0 or not isinstance(result['session'],str) or not result['session']:return None
    return result

def error(reason,path,exc=None,**fields):
    result=dict(reason=reason,path=str(path),observed_ts=time.time(),observed_mono=time.monotonic(),
                error_type=type(exc).__name__ if exc else 'InvalidMonitorState',errno=getattr(exc,'errno',None),
                winerror=getattr(exc,'winerror',None),strerror=getattr(exc,'strerror',None))
    for key in ('lineno','colno','pos','start','end','native_api_v862','native_error_source_v862','close_winerror_v862'):
        if exc is not None and hasattr(exc,key):result[key]=getattr(exc,key)
    result.update(fields);return result

def read(path):
    path=Path(path);raw=None;data=None
    try:
        raw=_read_bytes(path);data=json.loads(raw.decode('utf-8'))
        if not isinstance(data,dict) or not isinstance(data.get('status'),str) or not data['status']:
            raise ValueError('MONITOR_STATE_INVALID_SCHEMA')
        for key in ('monotonic_ts','heartbeat_ts'):
            if key in data and not number(data[key]):raise ValueError('MONITOR_STATE_INVALID_'+key.upper())
        if 'state_read_error_v86' in data:
            data=dict(data);data['producer_state_read_error_v86']=data.pop('state_read_error_v86')
        data.pop('observed_identity_v86',None)
        return dict(data,state_read_v86=dict(ok=True,path=str(path),observed_ts=time.time(),observed_mono=time.monotonic(),sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw)))
    except (OSError,UnicodeDecodeError,ValueError,RecursionError) as exc:
        if isinstance(exc,FileNotFoundError):reason='MONITOR_STATE_MISSING'
        elif isinstance(exc,PermissionError) or isinstance(exc,OSError) and exc.errno in (errno.EACCES,errno.EPERM):reason='MONITOR_STATE_PERMISSION_DENIED'
        elif isinstance(exc,OSError):reason='MONITOR_STATE_IO_ERROR'
        elif isinstance(exc,UnicodeDecodeError):reason='MONITOR_STATE_INVALID_ENCODING'
        elif isinstance(exc,(json.JSONDecodeError,RecursionError)):reason='MONITOR_STATE_INVALID_JSON'
        else:reason=str(exc)
        details=error(reason,path,exc)
        details['exception']=__import__('child_diagnostics').exception(exc)
        if details['strerror'] is not None:details['strerror']=__import__('child_diagnostics').clean(details['strerror'])
        if raw is not None:details.update(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
        return dict(status='STATE_READ_ERROR',state_read_error_v86=details,observed_identity_v86=identity(data) if isinstance(data,dict) else None)

class Observer:
    def __init__(self,limit=16.):
        self.limit=limit;self.last_valid=None;self.last_status=None;self.bound=None
        self.streak=0;self.first_error=None;self.last_incident=None
    def observe(self,state,now):
        state=dict(state);ident=identity(state);read_error=state.get('state_read_error_v86')
        observed_ident=state.get('observed_identity_v86')
        if read_error and observed_ident is not None and observed_ident!=self.bound:
            self.last_valid=None;self.last_status=None;self.bound=observed_ident
        if not read_error:
            if ident is None or ident!=self.bound:
                self.last_valid=None;self.last_status=None;self.bound=ident
            pulse=state.get('monotonic_ts')
            if self.last_valid is not None and number(pulse) and pulse<self.last_valid['monotonic_ts']:
                read_error=error('MONITOR_STATE_HEARTBEAT_REGRESSED',state.get('state_read_v86',{}).get('path',''),previous_pulse=self.last_valid['monotonic_ts'],observed_pulse=pulse)
                state=dict(status='STATE_READ_ERROR',state_read_error_v86=read_error,observed_identity_v86=ident)
        if read_error:
            self.streak+=1
            if self.first_error is None:
                self.first_error=dict(read_error)
                self.last_incident=dict(first_error=dict(read_error),last_error=dict(read_error),count=1,trigger_last_valid=self.last_valid,resolved=False)
            self.last_incident.update(last_error=dict(read_error),count=self.streak)
            cached=self.last_valid;pulse=cached['monotonic_ts'] if cached else None
            age=now-pulse if pulse is not None else None
            known_failure=self.last_status in ('FAILED','STORAGE_ERROR','STOPPED')
            if known_failure:status='MONITOR_NOT_RUNNING';reason='LAST_KNOWN_'+self.last_status
            elif age is not None and age>=self.limit:status='MONITOR_NOT_RUNNING';reason='LAST_VALID_HEARTBEAT_EXPIRED_DURING_READ_ERROR'
            elif age is not None and age<0:status='MONITOR_HEARTBEAT_INVALID';reason='LAST_VALID_PULSE_IN_FUTURE'
            else:status='MONITOR_STATE_READ_ERROR';reason=read_error['reason']
            basis='LAST_VALID_SNAPSHOT' if cached else 'UNKNOWN'
        else:
            pulse=state.get('monotonic_ts');age=now-pulse if number(pulse) else None
            self.last_status=state.get('status')
            if age is not None and age>=0 and ident is not None:
                self.last_valid={k:state[k] for k in ('status','monotonic_ts','heartbeat_ts','started_ts') if k in state}
                self.last_valid.update(monitor_identity_v86=ident,observed_only_v86=True)
            if self.streak:
                self.last_incident.update(resolved=True,resolved_mono=now,resolved_identity=ident)
                self.streak=0;self.first_error=None
            if self.last_status in ('FAILED','STORAGE_ERROR','STOPPED'):status='MONITOR_NOT_RUNNING';reason='MONITOR_STATE_'+self.last_status
            elif age is None:status='MONITOR_HEARTBEAT_UNKNOWN';reason='MONOTONIC_HEARTBEAT_NOT_OBSERVED'
            elif age<0:status='MONITOR_HEARTBEAT_INVALID';reason='CURRENT_PULSE_IN_FUTURE'
            elif age>=self.limit:status='MONITOR_NOT_RUNNING';reason='CURRENT_HEARTBEAT_EXPIRED'
            elif self.last_status in ('RUNNING','CLEANUP_PENDING','STOP_WAITING_CHILD_CLEANUP'):status='RUNNING';reason='CURRENT_HEARTBEAT_FRESH'
            else:status='MONITOR_HEARTBEAT_UNKNOWN';reason='MONITOR_STATE_UNKNOWN'
            basis='CURRENT_SNAPSHOT' if age is not None else 'UNKNOWN'
        return dict(status=status,reason=reason,age_s=age,age_basis_v86=basis,threshold_s=self.limit,observed_mono=now,
                    snapshot=state,snapshot_is_current_v86=not bool(read_error),identity_verified_current_v86=not bool(read_error) and ident is not None,last_valid_snapshot_v86=self.last_valid,
                    state_read_error_v86=read_error,first_read_error_v86=self.first_error,read_error_streak_v86=self.streak,
                    last_read_incident_v86=self.last_incident,observation_only_v86=True)
