"""Independent, bounded SQLite error journal; no SQL parameters or row contents."""
import json,os,sqlite3,time
from pathlib import Path

def error(exc):
    d=dict(error_type=type(exc).__name__,sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),sqlite_errorname=getattr(exc,'sqlite_errorname',None),errno=getattr(exc,'errno',None),winerror=getattr(exc,'winerror',None))
    if isinstance(exc,sqlite3.Error):d['message']=str(exc)[:1000]
    return d

def record(database,operation,exc=None,**details):
    try:
        p=Path(database).with_name(Path(database).name+'.sqlite_diagnostics.jsonl')
        if p.exists() and p.stat().st_size>1024*1024:p.replace(p.with_suffix(p.suffix+'.1'))
        row=dict(ts=time.time(),pid=os.getpid(),database=Path(database).name,operation=operation,sqlite_version=sqlite3.sqlite_version,**details)
        if exc is not None:row.update(error(exc))
        with p.open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    except (OSError,ValueError):pass

class ObservedConnection(sqlite3.Connection):
    diagnostic_path=None
    def observed(self,operation,fn,*args):
        try:return fn(*args)
        except sqlite3.Error as exc:
            if self.diagnostic_path:record(self.diagnostic_path,operation,exc,in_transaction=self.in_transaction)
            raise
    def execute(self,sql,parameters=(),/):return self.observed('SQL_'+(sql.lstrip().split(None,1)[0].upper() if sql.strip() else 'EMPTY'),super().execute,sql,parameters)
    def executemany(self,sql,parameters,/):return self.observed('EXECUTEMANY',super().executemany,sql,parameters)
    def executescript(self,sql,/):return self.observed('EXECUTESCRIPT',super().executescript,sql)
    def commit(self):return self.observed('COMMIT',super().commit)
    def rollback(self):return self.observed('ROLLBACK',super().rollback)
    def __exit__(self,*exc):return self.observed('TRANSACTION_EXIT',super().__exit__,*exc)
