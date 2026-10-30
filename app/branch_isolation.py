"""Experimental branch failures are durable exclusions, never primary failures."""
import json,time,threading
from pathlib import Path
import runtime_control,system_health,monitor_service
_LOCK=threading.RLock()
def path(primary):
    p=Path(primary).resolve();return p.with_name(p.stem+'_branch_outages_v79.json')
def read(primary):
    try:return json.loads(path(primary).read_text(encoding='utf-8'))
    except FileNotFoundError:return []
def record(primary,failure):
    with _LOCK:
        rows=read(primary)
        if not any(r['session']==failure['session'] for r in rows):
            rows.append(dict(failure));runtime_control.atomic_json(path(primary),rows)
            system_health.event(primary,'ALERT_OPEN',code='BRANCH_UNAVAILABLE',failure=failure)
def finish(primary,session,cleanup):
    with _LOCK:
        rows=read(primary)
        for r in rows:
            if r['session']==session:r['cleanup']=cleanup
        runtime_control.atomic_json(path(primary),rows)
def end_session(primary,sessions):
    with _LOCK:
        rows=read(primary)
        for r in rows:
            if r['session'] in sessions:r['primary_stop_ts']=time.time()
        if rows:runtime_control.atomic_json(path(primary),rows)
def exclusion(primary,module,opened,closed=None):
    for r in read(primary):
        end=r.get('primary_stop_ts',float('inf'))
        if r['module']==module and r['ts']<=(closed if closed is not None else time.time()) and end>=opened:
            return dict(reason='BRANCH_UNAVAILABLE',session=r['session'],failure=r['reason'])
    return None
class MonitorWatch:
    LIMIT=16.0
    def __init__(self,primary,clock=None):
        self.primary=primary;self.clock=clock or time.monotonic;self.origin=self.clock();self.alert=False;self.alert_code=None;self.alert_codes=set()
        self.observer=__import__('monitor_state').Observer(self.LIMIT)
    def sample(self):
        result=self.observer.observe(monitor_service.read_state(self.primary),self.clock())
        result.update(schema='MONITOR_SUPERVISION_V86',source='PRIMARY_MONITOR_WATCH')
        codes=set()
        if result['state_read_error_v86']:codes.add('MONITOR_STATE_READ_ERROR')
        if result['status']!='RUNNING':codes.add(result['status'])
        for code in self.alert_codes-codes:system_health.event(self.primary,'ALERT_CLEARED',code=code,supervision=result)
        for code in codes-self.alert_codes:system_health.event(self.primary,'ALERT_OPEN',code=code,supervision=result)
        self.alert_codes=codes;self.alert=bool(codes);self.alert_code=result['status'] if codes else None
        target=Path(self.primary).resolve();runtime_control.atomic_json(target.with_name(target.stem+'_monitor_supervision_v79.json'),result)
        return result

def fault_path(primary,module):
    p=Path(primary).resolve();return p.with_name(p.stem+'_'+module+'_fault_request_v79.json')
def controlled_request(primary,module,session,pid):
    try:r=json.loads(fault_path(primary,module).read_text())
    except (OSError,ValueError):return None
    return r if r.get('session')==session and r.get('worker_pid')==pid and r.get('module')==module and type(r.get('ts')) in (int,float) else None

def inject_stop(primary,module='paper_branch'):
    import config,paper_branch,route_branch
    if config.EXECUTION_MODE!='paper':raise RuntimeError('PAPER_ONLY')
    if module not in ('paper_branch','route_branch'):raise ValueError('UNKNOWN_BRANCH')
    state=runtime_control.read(primary)
    if state.get('state')!='RUNNING' or not 0<=time.time()-state.get('ts',0)<=3:raise RuntimeError('PRIMARY_SESSION_NOT_FRESH')
    selected=next((p for p in state.get('branches',[]) if p.get('module')==module),None)
    if not selected or selected.get('stop_requested') or selected.get('failure') or not selected.get('session'):raise RuntimeError('BRANCH_NOT_AVAILABLE')
    engine=paper_branch if module=='paper_branch' else route_branch
    request=dict(module=module,session=selected['session'],worker_pid=selected.get('worker_pid'),ts=time.time())
    runtime_control.atomic_json(fault_path(primary,module),request)
    system_health.event(primary,'CONTROLLED_BRANCH_STOP_V79',module=module,branch_session=selected['session'])
    runtime_control.request_stop(engine.path(primary),selected['session'])
    return dict(module=module,session=selected['session'],status='STOP_REQUESTED')
