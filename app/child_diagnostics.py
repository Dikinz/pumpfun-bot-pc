"""Bounded failure evidence; no locals, command arguments or environment capture."""
import json,os,re,time,traceback
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
        chain.append(dict(error_type=type(exc).__name__,message=clean(exc),errno=getattr(exc,'errno',None),sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),
            frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(exc.__traceback__)[-20:]]))
        exc=exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    return dict(chain=chain)

class Trace:
    def __init__(self,out,job):
        self.out=Path(out);self.data=dict(schema='CHILD_DIAGNOSTICS_V73',check_id=job.get('id'),trade_id=job.get('trade_id'),monitor_pid=os.getpid(),monitor_parent_pid=os.getppid(),child_pid=None,child_created_ts=None,phase=None,events=[],write_errors=[])
    def event(self,phase,**fields):
        self.data['phase']=phase;self.data['ts']=time.time()
        if phase.startswith('CLEANUP_'):self.data['cleanup_phase']=phase;self.data['cleanup_phase_ts']=self.data['ts']
        self.data['events']=(self.data['events']+[dict(phase=phase,ts=self.data['ts'],mono=time.monotonic(),**fields)])[-48:]
        self.save()
    def error(self,key,exc):
        self.data[key]=dict(exception(exc),at_phase=self.data.get('phase'),phase_elapsed_ms=max(0,(time.monotonic()-self.data['events'][-1]['mono'])*1000) if self.data['events'] else None)
        self.save()
    def save(self):
        try:
            p=self.out/'child_lifecycle_v73.json';tmp=p.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data,allow_nan=False),encoding='utf-8');tmp.replace(p)
        except (OSError,ValueError) as e:self.data['write_errors']=(self.data['write_errors']+[dict(error_type=type(e).__name__,errno=getattr(e,'errno',None))])[-8:]

def identity(proc):
    import psutil
    try:return dict(pid=proc.pid,created_ts=proc.create_time())
    except psutil.Error as e:return dict(pid=proc.pid,identity_error=type(e).__name__)
