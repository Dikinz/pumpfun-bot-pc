"""Optional supervision publication, separate from fresh monitor observation.

Only this diagnostic caller degrades on a storage error. Common STOP/recovery
writes retain their strict exception contract. One incident is retained, never
an unbounded history/cache or a substitute heartbeat.
"""
import copy,json,time,traceback
from pathlib import Path

ALERT='MONITOR_SUPERVISION_PUBLICATION_FAILED'

def error(exc):
    chain=[];seen=set();current=exc
    while isinstance(current,BaseException) and id(current) not in seen and len(chain)<8:
        seen.add(id(current))
        item=dict(error_type=type(current).__name__,errno=getattr(current,'errno',None),winerror=getattr(current,'winerror',None),
            sqlite_errorcode=getattr(current,'sqlite_errorcode',None),sqlite_errorname=getattr(current,'sqlite_errorname',None),
            frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(current.__traceback__)[-12:]])
        write=getattr(current,'state_write_v87',None)
        if isinstance(write,dict):
            item['state_write_v87']={k:write.get(k) for k in ('status','stage','attempts','elapsed_ms','error_type','errno','winerror')}
            item['state_write_v87']['target_name']=Path(write.get('path') or '').name
            item['state_write_v87']['failures']=[{k:f.get(k) for k in ('attempt','error_type','errno','winerror')} for f in write.get('failures',[])[:4] if isinstance(f,dict)]
        cleanup=getattr(current,'state_temp_cleanup_v99',None)
        if isinstance(cleanup,dict):item['temp_cleanup_v99']={k:cleanup.get(k) for k in ('error_type','errno','winerror')}
        if getattr(current,'publication_operation_v99',None):item['publication_operation_v99']=current.publication_operation_v99
        chain.append(item);current=current.__cause__ or current.__context__
    return dict(chain=chain,note='Codes, operation and stack locations only; no exception messages, contents or credentials.')

class Publisher:
    def __init__(self):
        self.sequence=0;self.failures=0;self.incident=None;self.pending_temp=None
    def publish(self,target,observation):
        import runtime_control
        self.sequence+=1;now=time.time();incident=copy.deepcopy(self.incident)
        if incident is not None and not incident.get('resolved'):
            incident.update(resolved=True,resolved_ts=now)
        candidate=dict(schema='MONITOR_PUBLICATION_V99',status='PUBLISHED',sequence=self.sequence,
            attempt_ts=now,failed_attempts_total=self.failures,last_incident=incident,
            target_name=Path(target).name,authority='DIAGNOSTIC_ONLY_NOT_HEARTBEAT_OR_STOP')
        # PUBLISHED is visible in the target only if atomic replacement succeeds.
        payload=dict(observation,publication_v99=candidate)
        try:
            if self.pending_temp is not None:
                try:self.pending_temp.unlink(missing_ok=True)
                except OSError as exc:
                    exc.publication_operation_v99='RETRY_TEMP_CLEANUP';raise
                self.pending_temp=None
            info=runtime_control.atomic_json(target,payload)
        except OSError as exc:
            cleanup=getattr(exc,'state_temp_cleanup_v99',None)
            if isinstance(cleanup,dict):self.pending_temp=Path(cleanup['path'])
            detail=error(exc);self.failures+=1
            if self.incident is None or self.incident.get('resolved'):
                self.incident=dict(first_error=detail,first_error_ts=now,failures=0,resolved=False)
            self.incident.update(last_error=detail,last_error_ts=now,failures=self.incident['failures']+1,resolved=False)
            return dict(candidate,status='FAILED',failed_attempts_total=self.failures,last_incident=copy.deepcopy(self.incident),error=detail,temp_cleanup_pending=self.pending_temp is not None)
        if isinstance(info,dict) and isinstance(info.get('temp_cleanup_v99'),dict):self.pending_temp=Path(info['temp_cleanup_v99']['path'])
        self.incident=incident
        return candidate

def from_meta(meta):
    try:
        value=json.loads(meta.get('monitor_publication_v99','{}'))
        return value if isinstance(value,dict) else dict(status='UNREADABLE')
    except (ValueError,TypeError):return dict(status='UNREADABLE')

def export(primary,folder):
    import shutil,state_io
    primary=Path(primary).resolve();folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    target=primary.with_name(primary.stem+'_monitor_supervision_v79.json')
    report=dict(schema='MONITOR_PUBLICATION_EXPORT_V99',files=[],errors=[])
    log=state_io.log_path(target)
    for path in (target,log,Path(str(log)+'.1')):
        try:
            if path.stat().st_size>2*1024*1024:raise ValueError('PUBLICATION_DIAGNOSTIC_OVERSIZE')
            shutil.copy2(path,folder/path.name);report['files'].append(path.name)
        except FileNotFoundError:continue
        except (OSError,ValueError) as exc:report['errors'].append(dict(name=path.name,exception=error(exc)))
    report['status']='PARTIAL' if report['errors'] else 'CAPTURED'
    (folder/'monitor_publication_export_v99.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report
