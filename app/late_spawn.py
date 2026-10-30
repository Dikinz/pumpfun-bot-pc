"""Retain unreturned creation or delayed process/job cleanup, never a late result.

No attempt to interrupt a Windows kernel call. A pending owner prevents new
checks/entries and prevents normal STOP from killing the process that owns it.
An explicit external force-kill is outside this guarantee and is never certified.
"""
import json,os,time
from pathlib import Path
from collections import deque
ACTIVE=None
SUFFIX='_pending_cleanup_v85.json'

def path(primary):
    p=Path(primary).resolve();return p.with_name(p.stem+SUFFIX)

def write(p,data):
    return __import__('state_io').atomic_json(p,data)

def creation_pending(owner):
    # Read only the published in-memory fields; do not take the trace's disk-I/O lock.
    return (owner.pid is None and not owner.done.is_set() and any(
        r.get('api')=='CreateProcessW' and r.get('state')=='ENTERED'
        for r in list(owner.trace.data.get('native_calls_v82',[]))))

def own_identity():
    import psutil
    pid=os.getpid()
    if os.name!='nt':
        # Same procfs namespace convention used by the existing process-tree audit.
        status=Path('/proc/self/status')
        if status.exists():
            line=next((s for s in status.read_text().splitlines() if s.startswith('NSpid:')),None)
            if line:pid=int(line.split()[1])
    return pid,psutil.Process(pid).create_time()

class Registry:
    def __init__(self,primary,session=None):
        import psutil
        self.primary=primary;self.session=session;self.pid,self.created=own_identity()
        self.owners={};self.history=deque(maxlen=32);self.last_save=0;self._published_key=None;self._published_stamp=None;self.skipped_writes_v87=0;self.writes_v87=0
    def register(self,owner,out,job,retain_cleanup=False):
        key=job.get('id') or job.get('check_id')
        creation=creation_pending(owner)
        # STOP can race native setup before a PID/handle is published. Retain
        # that owner too; it may create a process after the caller's deadline.
        cleanup=retain_cleanup and not owner.done.is_set()
        if key in self.owners or not (creation or cleanup):return False
        owner.retain_delayed_cleanup=True
        call=next((r for r in reversed(list(owner.trace.data.get('native_calls_v82',[]))) if r.get('api')=='CreateProcessW'),{})
        boundary={k:v for k,v in dict(call).items() if k in ('api','state','call_id','entered_ts','entered_mono','api_started_mono')}
        row=dict(native_boundary=boundary,check_id=key,trade_id=job.get('trade_id'),state='PENDING',registered_ts=time.time(),
                 source='CREATEPROCESS_BOUNDARY_UNRETURNED_AT_STOP' if creation else 'PROCESS_HANDLE_CLEANUP_PENDING_V93',original_result_ineligible=True,child_pid=owner.pid,child_created_ts=owner.created_ts)
        self.owners[row['check_id']]=(owner,Path(out),row);self.save(force=True)
        owner.trace.event('LATE_CREATION_OWNERSHIP_RETAINED_V85' if creation else 'CLEANUP_OWNERSHIP_REGISTERED_V93',check_id=row['check_id'])
        return True
    def handled(self,job,exc):
        return str(exc)=='SIMULATION_CHILD_CLEANUP_UNCONFIRMED' and (
            job.get('id') in self.owners or any(r['check_id']==job.get('id') and r['state']=='CONFIRMED' for r in self.history))
    @property
    def pending(self):return bool(self.owners)
    def snapshot(self):
        return dict(schema='LATE_CREATION_OWNERSHIP_V85',monitor_pid=self.pid,monitor_created_ts=self.created,
                    session=self.session,ts=time.time(),pending=bool(self.owners),
                    owners=[dict(row,child_pid=owner.pid,child_created_ts=owner.created_ts,thread_alive=owner.thread.is_alive(),cleanup_pending_reason=getattr(owner,'cleanup_pending_reason',None))
                            for owner,out,row in self.owners.values()],history=list(self.history))
    def save(self,force=False):
        if not force and time.monotonic()-self.last_save<1:return
        data=self.snapshot();key=json.dumps({k:v for k,v in data.items() if k!='ts'},sort_keys=True)
        target=path(self.primary)
        try:
            st=target.stat();stamp=(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_ctime_ns,st.st_size)
        except FileNotFoundError:stamp=None
        if not force and key==self._published_key and stamp==self._published_stamp:
            self.skipped_writes_v87+=1;self.last_save=time.monotonic();return
        write(target,data)
        st=target.stat();self._published_stamp=(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_ctime_ns,st.st_size)
        self._published_key=key;self.last_save=time.monotonic();self.writes_v87+=1
    def poll(self):
        completed=[]
        for key,(owner,out,row) in list(self.owners.items()):
            if not owner.done.is_set() or owner.thread.is_alive():continue
            trace=owner.trace.snapshot();phases={e['phase'] for e in trace.get('events',[])}
            calls=[r for r in trace.get('native_calls_v82',[]) if r.get('api')=='CreateProcessW']
            failed_creation=(owner.pid is None and bool(calls) and calls[-1].get('state')=='RETURNED' and calls[-1].get('ok') is False)
            cancelled_before_call=owner.pid is None and 'SPAWN_CANCELLED_BEFORE_CALL_V78' in phases and getattr(owner,'error',None) is None
            confirmed=(owner.cleanup_error is None and ('CLEANUP_CONFIRMED' in phases or failed_creation or cancelled_before_call))
            final=dict(row,state='CONFIRMED' if confirmed else 'FAILED',resolved_ts=time.time(),child_pid=owner.pid,
                       child_created_ts=owner.created_ts,returncode=owner.returncode,
                       outcome='SPAWN_FAILED_NO_PROCESS' if failed_creation else 'SPAWN_CANCELLED_NO_PROCESS' if cancelled_before_call else ('DELAYED_CLEANUP_CONFIRMED' if row.get('source')=='PROCESS_HANDLE_CLEANUP_PENDING_V93' else 'LATE_PROCESS_CLEANED') if confirmed else 'CLEANUP_NOT_CONFIRMED',
                       original_result_ineligible=True,thread_alive=owner.thread.is_alive(),child_diagnostics_v73=trace)
            write(out/'late_cleanup_v85.json',final)
            # Keep bounded summaries; the per-check artifact and monitor journal retain full evidence.
            self.history.append({k:v for k,v in final.items() if k!='child_diagnostics_v73'})
            del self.owners[key];completed.append(final)
        self.save(force=bool(completed));return completed

def register(owner,out,job):
    return ACTIVE.register(owner,out,job) if ACTIVE is not None else False

def live_pending(directory,known=None,strict=False):
    """Only protect a recorded monitor with its exact current process identity."""
    import psutil
    found=[]
    for p in Path(directory).glob('*'+SUFFIX):
        try:data=json.loads(p.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            if strict:raise RuntimeError('LATE_CREATION_STATE_UNREADABLE: '+p.name)
            found.append(dict(state='UNREADABLE'));continue
        if not isinstance(data,dict):
            if strict:raise RuntimeError('LATE_CREATION_STATE_INVALID: '+p.name)
            found.append(dict(state='INVALID_SCHEMA'));continue
        if not data.get('pending'):continue
        pid=data.get('monitor_pid');birth=data.get('monitor_created_ts')
        if type(pid)!=int or not isinstance(birth,(int,float)):
            if strict:raise RuntimeError('LATE_CREATION_IDENTITY_UNKNOWN: '+p.name)
            found.append(dict(state='IDENTITY_UNKNOWN'));continue
        if known is not None and pid not in known:continue
        try:
            proc=psutil.Process(pid)
            if proc.is_running() and proc.status()!=psutil.STATUS_ZOMBIE and abs(proc.create_time()-birth)<.01:found.append(data)
        except psutil.NoSuchProcess:pass
        except psutil.AccessDenied:
            if strict:raise RuntimeError('LATE_CREATION_IDENTITY_UNREADABLE: '+p.name)
            found.append(data)
    return found

def blocks_entries(primary):return bool(live_pending(Path(primary).resolve().parent))

def wait_before_force(child,family=(),directory=None):
    """Called from existing shutdown threads. No new worker deadline or success claim."""
    import config,psutil
    known={child.pid}
    if child.pid==os.getpid():known.add(own_identity()[0])
    for p in family:known.add(p.pid)
    try:known.update(p.pid for p in psutil.Process(child.pid).children(recursive=True))
    except psutil.Error:pass
    directory=Path(directory or Path(config.DB).resolve().parent);waited=False;last_note=0
    while live_pending(directory,known=known,strict=True):
        waited=True
        if time.monotonic()-last_note>=5:
            print('STOP WAITING: retained simulation owner; process/job cleanup is not yet confirmed.',flush=True);last_note=time.monotonic()
        time.sleep(.1)
    return waited


def add_export(archive,checks,root):
    import hashlib
    records=[];errors=[]
    for check in checks:
        p=Path(root)/check['id']/'late_cleanup_v85.json'
        if not p.exists():continue
        try:
            content=p.read_bytes();data=json.loads(content)
            if data.get('check_id')!=check['id']:raise ValueError('CHECK_ID_MISMATCH')
            name=check['id']+'/late_cleanup_v85.json';archive.writestr(name,content)
            records.append(dict(check_id=check['id'],file=name,sha256=hashlib.sha256(content).hexdigest(),state=data.get('state'),outcome=data.get('outcome'),original_result_ineligible=data.get('original_result_ineligible')))
        except (OSError,ValueError) as exc:errors.append(dict(check_id=check['id'],error_type=type(exc).__name__,errno=getattr(exc,'errno',None)))
    manifest=dict(schema='LATE_CLEANUP_EXPORT_V85',status='PARTIAL' if errors else 'OK',files=records,errors=errors,
                  scope='Final late-cleanup artifacts when present; pending ownership remains in the monitor sidecar and journal.')
    archive.writestr('late_cleanup_manifest_v85.json',json.dumps(manifest,ensure_ascii=False));return manifest
