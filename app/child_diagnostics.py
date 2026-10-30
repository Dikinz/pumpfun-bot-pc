"""Bounded failure evidence; no locals, command arguments or environment capture."""
import json,os,re,time,traceback,threading
from pathlib import Path

def clean(value,limit=2000):
    s=re.sub(r'https?://[^\s\"\']+','[URL REDACTED]',str(value))
    s=re.sub(r'(?im)(authorization\s*[:=]\s*)[^\r\n]+',r'\1[REDACTED]',s)
    s=re.sub(r'(?i)(authorization|api[_-]?key|private[_-]?key|secret|token|password)([\s:=]+)[^\s,;\"\']+',r'\1\2[REDACTED]',s)
    return s[:limit]

def exception(exc):
    chain=[];seen=set()
    while exc is not None and id(exc) not in seen and len(chain)<5:
        seen.add(id(exc))
        chain.append(dict(error_type=type(exc).__name__,message=clean(exc),errno=getattr(exc,'errno',None),winerror=getattr(exc,'winerror',None),sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),state_write_v87=getattr(exc,'state_write_v87',None),
            frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(exc.__traceback__)[-20:]]))
        exc=exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    return dict(chain=chain)

class Trace:
    def __init__(self,out,job):
        self.lock=threading.RLock()
        self.out=Path(out);self.data=dict(schema='CHILD_DIAGNOSTICS_V73',check_id=job.get('id'),trade_id=job.get('trade_id'),monitor_pid=os.getpid(),monitor_parent_pid=os.getppid(),child_pid=None,child_created_ts=None,phase=None,events=[],write_errors=[])
    def spawn_context(self,args,cwd):
        # Runs in the existing owner thread; no command line or env snapshot.
        import sys,hashlib,struct
        context=dict(schema='SPAWN_CONTEXT_V89',pid=os.getpid(),native_thread_id=threading.get_native_id(),python_version='.'.join(map(str,sys.version_info[:3])),pointer_bits=struct.calcsize('P')*8,venv_runtime=sys.prefix!=sys.base_prefix,executable_name=os.path.basename(str(args[0])) if args else None,cwd_identity=hashlib.sha256(os.path.abspath(str(cwd)).encode()).hexdigest()[:16],acl_verified=False,security_product_cause='UNKNOWN')
        try:
            stat=os.stat(str(args[0])) if args else None
            context.update(executable_bytes=stat.st_size if stat else None,executable_exists=True if stat else None)
        except OSError as exc:context.update(executable_exists=None,stat_error_type=type(exc).__name__,stat_winerror=getattr(exc,'winerror',None))
        with self.lock:self.data['spawn_context_v89']=context;self.save()
    def event(self,phase,**fields):
        with self.lock:self._event(phase,**fields)
    def _event(self,phase,**fields):
        self.data['phase']=phase;self.data['ts']=time.time()
        if phase.startswith('CLEANUP_'):self.data['cleanup_phase']=phase;self.data['cleanup_phase_ts']=self.data['ts']
        self.data['events']=(self.data['events']+[dict(phase=phase,ts=self.data['ts'],mono=time.monotonic(),**fields)])[-48:]
        self.save()
    def error(self,key,exc):
        with self.lock:
            self.data[key]=dict(exception(exc),at_phase=self.data.get('phase'),phase_elapsed_ms=max(0,(time.monotonic()-self.data['events'][-1]['mono'])*1000) if self.data['events'] else None)
            self.save()
    def identify(self,pid,created_ts):
        with self.lock:self.data.update(child_pid=pid,child_created_ts=created_ts);self.save()
    def native_enter(self,api,expected_probe=False):
        with self.lock:
            seq=self.data.get('native_sequence_v82',0)+1;self.data['native_sequence_v82']=seq
            row=dict(call_id=seq,api=api,state='ENTERED',entered_mono=time.monotonic(),entered_ts=time.time(),thread_id=threading.get_native_id(),expected_probe=expected_probe)
            self.data['native_calls_v82']=(self.data.get('native_calls_v82',[])+[row])[-32:]
            self._event('NATIVE_CALL_ENTER_V82',api=api,call_id=seq)
            return seq
    def native_started(self,seq):
        with self.lock:
            row=next(r for r in self.data['native_calls_v82'] if r['call_id']==seq)
            row['api_started_mono']=time.monotonic()
            return row['api_started_mono']
    def native_return(self,seq,api,ok,winerror,returned_mono,returned_ts,identity=None):
        with self.lock:
            row=next(r for r in self.data['native_calls_v82'] if r['call_id']==seq)
            if identity is not None:self.data.update(child_pid=identity[0],child_created_ts=identity[1])
            row.update(state='RETURNED',returned_mono=returned_mono,returned_ts=returned_ts,elapsed_ms=max(0,(returned_mono-row.get('api_started_mono',row['entered_mono']))*1000),entry_record_ms=max(0,(row.get('api_started_mono',row['entered_mono'])-row['entered_mono'])*1000),ok=ok,winerror=winerror)
            if api=='CreateProcessW':
                self.data['native_create_v89']=dict(duration_ms=row['elapsed_ms'],created=bool(ok),winerror=winerror,slow_ge_1000_ms=row['elapsed_ms']>=1000,root_cause='UNKNOWN',ownership_policy='RETAIN_UNTIL_CONFIRMED')
            self._event('NATIVE_CALL_RETURN_V82',api=api,call_id=seq,ok=ok,winerror=winerror,elapsed_ms=row['elapsed_ms'])
    def snapshot(self):
        with self.lock:return json.loads(json.dumps(self.data))
    def save(self):
        with self.lock:self._save()
    def _save(self):
        try:
            p=self.out/'child_lifecycle_v73.json';tmp=p.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data,allow_nan=False),encoding='utf-8');tmp.replace(p)
        except (OSError,ValueError) as e:self.data['write_errors']=(self.data['write_errors']+[dict(error_type=type(e).__name__,errno=getattr(e,'errno',None))])[-8:]

def identity(proc):
    import psutil
    try:return dict(pid=proc.pid,created_ts=proc.create_time())
    except psutil.Error as e:return dict(pid=proc.pid,identity_error=type(e).__name__)
