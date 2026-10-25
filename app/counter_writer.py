"""Bounded best-effort candidate counter writer; never used for financial state."""
import json,os,sqlite3,threading,time,uuid,math
from pathlib import Path
ACTIVE=None
META_KEYS=frozenset(('events','valid_price_events','invalid_price_events'))
TABLES=('candidate_counts_v0742','clock_shadow_v0740','meta')
SQL={name:'INSERT INTO '+name+' VALUES(?,?,?) ON CONFLICT(variant,reason) DO UPDATE SET n=n+excluded.n' for name in TABLES}

SQL['meta']="INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=CAST(COALESCE(meta.v,'0') AS REAL)+excluded.v"

class Writer:
    def __init__(self,database,limit=256,interval=.5,connect=None,session=None):
        self.database=Path(database).resolve();self.limit=limit;self.interval=interval;self.connect=connect or sqlite3.connect
        self.lock=threading.Lock();self.pending={};self.inflight={};self.stopping=False;self.first_pending=None
        self.state=dict(schema='COUNTER_WRITER_V67',pid=os.getpid(),session=session or uuid.uuid4().hex,database=self.database.name,status='STARTING',accepted=0,committed=0,dropped=0,busy_retries=0,batches=0,last_error=None,recent=[],max_queue_ms=0.,max_begin_ms=0.,max_write_ms=0.,max_commit_ms=0.)
        self.thread=threading.Thread(target=self.run,name='candidate-counter-writer',daemon=True)
    def start(self):self.thread.start();return self
    def add(self,variant,reason,table='candidate_counts_v0742',amount=1):
        # Constant bounded RAM; no SQLite, file I/O, or queue wait in this callback.
        key=(table,variant,reason)
        with self.lock:
            if (table=='meta' and (variant not in META_KEYS or reason!='')) or type(amount) not in (int,float) or not math.isfinite(amount) or table not in TABLES or self.stopping or not isinstance(variant,str) or not isinstance(reason,str) or len(variant)>64 or len(reason)>128:
                self.state['dropped']+=1;return
            if key not in self.pending and len(self.pending)>=self.limit:
                self.state['dropped']+=1;return
            self.pending[key]=self.pending.get(key,0)+amount;self.state['accepted']+=amount
            if self.first_pending is None:self.first_pending=time.monotonic()
    def snapshot(self):
        with self.lock:
            return dict(self.state,pending_keys=len(self.pending),inflight_keys=len(self.inflight),unflushed=sum(self.pending.values())+sum(self.inflight.values()),buffer_key_limit=self.limit,max_total_keys=2*self.limit,thread_alive=self.thread.is_alive())
    def publish(self):
        try:
            p=self.database.with_name(self.database.stem+'_counter_writer_v67.json');tmp=p.with_suffix('.'+str(os.getpid())+'.tmp')
            tmp.write_text(json.dumps(self.snapshot(),ensure_ascii=False,allow_nan=False),encoding='utf-8');tmp.replace(p)
        except OSError:pass
    def phase(self,name,batch):
        # Publish BEFORE a potentially blocking SQLite call, outside market thread.
        self.state['active_sql']=dict(phase=name,started_ts=time.time(),busy_timeout_ms=250,
            statements=[dict(table=table,sql=SQL[table],keys=len([k for k in batch if k[0]==table])) for table in TABLES if any(k[0]==table for k in batch)])
        self.publish()
    def run(self):
        c=None;first=None
        try:
            c=self.connect(self.database,timeout=.25,isolation_level=None)
            c.execute('PRAGMA busy_timeout=250');c.execute('PRAGMA synchronous=NORMAL')
            self.state['status']='RUNNING'
            while True:
                with self.lock:
                    if not self.inflight and self.pending:
                        self.inflight=self.pending;self.pending={};first=self.first_pending;self.first_pending=None
                    batch=dict(self.inflight);stop=self.stopping
                if not batch:
                    if stop:break
                    self.publish();time.sleep(self.interval);continue
                timing=dict(queue_ms=max(0,(time.monotonic()-first)*1000),keys=len(batch),increments=sum(batch.values()))
                try:
                    phase='BEGIN_IMMEDIATE';self.phase(phase,batch);t=time.monotonic();c.execute('BEGIN IMMEDIATE');timing['begin_ms']=(time.monotonic()-t)*1000
                    phase='WRITE_BATCH';self.phase(phase,batch);t=time.monotonic()
                    for table in TABLES:
                        values=[(a,b,n) for (name,a,b),n in batch.items() if name==table]
                        if values:c.executemany(SQL[table],[(a,n) for a,b,n in values] if table=='meta' else values)
                    timing['write_ms']=(time.monotonic()-t)*1000
                    phase='COMMIT';self.phase(phase,batch);t=time.monotonic();c.commit();timing['commit_ms']=(time.monotonic()-t)*1000
                    with self.lock:
                        self.state['active_sql']=None;self.inflight={};self.state['committed']+=sum(batch.values());self.state['batches']+=1
                        for k in ('queue_ms','begin_ms','write_ms','commit_ms'):self.state['max_'+k]=max(self.state['max_'+k],timing[k])
                        self.state['recent']=(self.state['recent']+[timing])[-20:]
                    self.publish()
                except sqlite3.Error as exc:
                    import sqlite_diagnostics
                    failed_ms=(time.monotonic()-t)*1000
                    c.rollback();detail=dict(sqlite_diagnostics.error(exc),statements=self.state.get('active_sql'),phase=phase,phase_ms=failed_ms,queue_ms=timing['queue_ms'])
                    sqlite_diagnostics.record(self.database,'DIAGNOSTIC_COUNTER_BATCH',exc)
                    with self.lock:self.state['last_error']=detail
                    if getattr(exc,'sqlite_errorcode',0)&255 in (5,6):
                        with self.lock:self.state['busy_retries']+=1
                        self.publish()
                        if stop:break
                        time.sleep(self.interval);continue
                    self.state['status']='FAILED_UNFLUSHED';break
                # Rate limit batches, including under an uninterrupted market stream.
                if not stop:time.sleep(self.interval)
            if self.state['status']!='FAILED_UNFLUSHED':self.state['status']='STOPPED' if not self.snapshot()['unflushed'] else 'STOPPED_UNFLUSHED'
        except Exception as exc:
            import sqlite_diagnostics
            self.state['status']='FAILED_UNFLUSHED';self.state['last_error']=sqlite_diagnostics.error(exc)
            sqlite_diagnostics.record(self.database,'DIAGNOSTIC_COUNTER_WORKER',exc)
        finally:
            if c is not None:c.close()
            self.publish()
    async def close(self,timeout=2):
        import asyncio
        with self.lock:self.stopping=True
        await asyncio.to_thread(self.thread.join,timeout)
        if not self.thread.is_alive():await asyncio.to_thread(self.publish)
        return self.snapshot()

def start(database,session=None):
    global ACTIVE
    if ACTIVE is not None:raise RuntimeError('COUNTER_WRITER_ALREADY_ACTIVE')
    ACTIVE=Writer(database,session=session).start();return ACTIVE

def buffered(c,variant,reason,table='candidate_counts_v0742'):
    writer=ACTIVE
    if writer is None:return False
    writer.add(variant,reason,table);return True

async def stop(writer):
    global ACTIVE
    state=await writer.close()
    # A timed out worker stays attached: no replacement writer or synchronous fallback.
    from shutdown_audit import save
    path=writer.database.with_name(writer.database.stem+'_counter_writer_stop_v68.json')
    data=dict(state,ts=time.time(),confirmed=not state['thread_alive'] and state['status']=='STOPPED' and state['unflushed']==0)
    try:await __import__('asyncio').to_thread(save,path,data)
    except OSError as exc:state['stop_ack_error_type']=type(exc).__name__
    if not state['thread_alive']:ACTIVE=None
    return state
