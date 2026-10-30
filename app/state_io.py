"""Small control-state publication, never financial SQLite writes.

Content changes remain mandatory. Only transient rename errors retry, for three
short backoffs (35ms total); persistent errors propagate with exact evidence.
No success or process cleanup is inferred from a failed write.
"""
import errno,json,os,tempfile,threading,time
from pathlib import Path
DELAYS=(.005,.010,.020)
_LOCK=threading.Lock()

def log_path(path):return Path(str(path)+'.io_v87.jsonl')

def _pause(seconds):time.sleep(seconds)

def _retryable(exc):
    code=getattr(exc,'winerror',None)
    return code in (5,32,33) or (os.name=='nt' and code is None and exc.errno in (errno.EACCES,errno.EPERM))

def _journal(path,row):
    """Bounded error-only journal; logging failure never masks the real error."""
    import child_diagnostics
    try:
        with _LOCK:
            target=log_path(path)
            if target.exists() and target.stat().st_size>1048576:
                target.replace(Path(str(target)+'.1'))
            with target.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    except OSError as exc:
        row['journal_error']=child_diagnostics.exception(exc)

def atomic_json(path,data):
    path=Path(path);temp=None;stage='SERIALIZE';start=time.monotonic();failures=[];attempt=0
    try:
        body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
        stage='MKDIR';path.parent.mkdir(parents=True,exist_ok=True)
        stage='CREATE_TEMP'
        fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent);temp=Path(name)
        stage='WRITE_TEMP'
        with os.fdopen(fd,'wb') as stream:stream.write(body)
        stage='REPLACE'
        for attempt in range(len(DELAYS)+1):
            try:temp.replace(path);break
            except OSError as exc:
                failures.append(dict(attempt=attempt+1,error_type=type(exc).__name__,errno=exc.errno,winerror=getattr(exc,'winerror',None)))
                if attempt>=len(DELAYS) or not _retryable(exc):raise
                _pause(DELAYS[attempt])
        if failures:
            _journal(path,dict(schema='STATE_IO_V87',status='RECOVERED',path=str(path),stage=stage,ts=time.time(),pid=os.getpid(),attempts=attempt+1,failures=failures,elapsed_ms=(time.monotonic()-start)*1000))
        return dict(attempts=attempt+1,bytes=len(body),recovered=bool(failures))
    except BaseException as exc:
        if isinstance(exc,(OSError,ValueError,TypeError)):
            row=dict(schema='STATE_IO_V87',status='FAILED',path=str(path),stage=stage,ts=time.time(),pid=os.getpid(),attempts=attempt+1,failures=failures,
                     error_type=type(exc).__name__,errno=getattr(exc,'errno',None),winerror=getattr(exc,'winerror',None),elapsed_ms=(time.monotonic()-start)*1000)
            _journal(path,row);exc.state_write_v87=row
        raise
    finally:
        if temp is not None:
            try:temp.unlink(missing_ok=True)
            except OSError:pass # The FAILED record still makes publication failure explicit.
