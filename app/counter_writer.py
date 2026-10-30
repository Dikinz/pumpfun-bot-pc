"""Bounded best-effort candidate counter writer; never used for financial state."""
import json,os,sqlite3,threading,time,uuid,math
from pathlib import Path
ACTIVE=None
META_KEYS=frozenset(('events','valid_price_events','invalid_price_events'))
TABLES=('candidate_counts_v0742','clock_shadow_v0740','meta','clock_minutes_v0739','candidate_seen_v0742','candidate_first_v0742')
ROW_TABLES={'candidate_seen_v0742':3,'candidate_first_v0742':6}
MAX_ROW_BYTES=8192
SQL={name:'INSERT INTO '+name+' VALUES(?,?,?) ON CONFLICT(variant,reason) DO UPDATE SET n=n+excluded.n' for name in TABLES}

SQL['meta']="INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=CAST(COALESCE(meta.v,'0') AS REAL)+excluded.v"
SQL['clock_minutes_v0739']='''INSERT INTO clock_minutes_v0739 VALUES(?,?,?,?,?,?,?) ON CONFLICT(minute,bucket) DO UPDATE SET
 n=n+excluded.n,min_ms=MIN(min_ms,excluded.min_ms),max_ms=MAX(max_ms,excluded.max_ms),sum_ms=sum_ms+excluded.sum_ms,last_json=excluded.last_json'''

SQL['candidate_seen_v0742']='INSERT OR IGNORE INTO candidate_seen_v0742 VALUES(?,?,?)'
SQL['candidate_first_v0742']='INSERT OR IGNORE INTO candidate_first_v0742 VALUES(?,?,?,?,?,?)'

def count(value):return value['n'] if isinstance(value,dict) else value
def total(values):return sum(count(v) for v in values)

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
            if (table=='meta' and (variant not in META_KEYS or reason!='')) or type(amount) not in (int,float) or not math.isfinite(amount) or table not in TABLES or table=='clock_minutes_v0739' or table in ROW_TABLES or self.stopping or not isinstance(variant,str) or not isinstance(reason,str) or len(variant)>64 or len(reason)>128:
                self.state['dropped']+=1;return
            if key not in self.pending and len(self.pending)>=self.limit:
                self.state['dropped']+=1;return
            self.pending[key]=self.pending.get(key,0)+amount;self.state['accepted']+=amount
            if self.first_pending is None:self.first_pending=time.monotonic()
    def add_row(self,table,params):
        # First-pass evidence only. No financial state or order operation is accepted.
        if table not in ROW_TABLES or len(params)!=ROW_TABLES[table]:raise ValueError('INVALID_OBSERVATION_ROW')
        params=tuple(params);size=sum(len(x.encode('utf-8')) if isinstance(x,str) else 8 for x in params)
        key=(table,params[0],params[1] if table=='candidate_seen_v0742' else (params[1],params[2]))
        with self.lock:
            if key in self.pending or key in self.inflight:return False
            if self.stopping or self.state['status'].startswith('FAILED') or len(self.pending)>=self.limit or size>MAX_ROW_BYTES:
                self.state['dropped']+=1;return False
            self.pending[key]=dict(n=1,params=params,bytes=size);self.state['accepted']+=1
            if self.first_pending is None:self.first_pending=time.monotonic()
            return True
    def seen_pending(self,variant,mint):
        key=('candidate_seen_v0742',variant,mint)
        with self.lock:return key in self.pending or key in self.inflight
    def add_clock(self,minute,bucket,value,sample):
        key=('clock_minutes_v0739',minute,bucket)
        with self.lock:
            if self.stopping or (key not in self.pending and len(self.pending)>=self.limit):
                self.state['dropped']+=1;return
            row=self.pending.get(key)
            if row is None:self.pending[key]=dict(n=1,min=value,max=value,sum=value,last=sample)
            else:
                row['n']+=1
                row['min']=min(row['min'],value) if row['min'] is not None and value is not None else None
                row['max']=max(row['max'],value) if row['max'] is not None and value is not None else None
                row['sum']=row['sum']+value if row['sum'] is not None and value is not None else None
                row['last']=sample
            self.state['accepted']+=1
            if self.first_pending is None:self.first_pending=time.monotonic()
    def snapshot(self):
        with self.lock:
            sql=self.state.get('active_sql')
            sql=dict(sql,age_s=max(0,time.monotonic()-sql['started_mono'])) if sql else None
            stalled=bool(sql and sql['age_s']>3)
            frames=[]
            if stalled:
                frame=__import__('sys')._current_frames().get(self.thread.ident)
                while frame and len(frames)<12:
                    frames.append(dict(file=Path(frame.f_code.co_filename).name,function=frame.f_code.co_name,line=frame.f_lineno));frame=frame.f_back
            values=list(self.pending.values())+list(self.inflight.values())
            return dict(self.state,active_sql=sql,writer_stalled_v94=stalled,writer_frames_v94=frames,
                pending_keys=len(self.pending),inflight_keys=len(self.inflight),unflushed=total(values),
                observation_bytes_v94=sum(v.get('bytes',0) for v in values if isinstance(v,dict)),
                observation_byte_bound_v94=2*self.limit*MAX_ROW_BYTES,
                thread_native_id_v94=self.thread.native_id,diagnostic_gap_v94=bool(self.state['dropped'] or self.state['status'].endswith('UNFLUSHED') or stalled),
                buffer_key_limit=self.limit,max_total_keys=2*self.limit,thread_alive=self.thread.is_alive())
    def publish(self):
        try:
            p=self.database.with_name(self.database.stem+'_counter_writer_v67.json');tmp=p.with_suffix('.'+str(os.getpid())+'.tmp')
            tmp.write_text(json.dumps(self.snapshot(),ensure_ascii=False,allow_nan=False),encoding='utf-8');tmp.replace(p)
        except OSError:pass
    def phase(self,name,batch,publish=True):
        # Publish BEFORE a potentially blocking SQLite call, outside market thread.
        self.state['active_sql']=dict(phase=name,started_ts=time.time(),started_mono=time.monotonic(),busy_timeout_ms=250,
            statements=[dict(table=table,sql=SQL[table],keys=len([k for k in batch if k[0]==table])) for table in TABLES if any(k[0]==table for k in batch)])
        if publish:self.publish()
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
                timing=dict(queue_ms=max(0,(time.monotonic()-first)*1000),keys=len(batch),increments=total(batch.values()))
                try:
                    phase='BEGIN_IMMEDIATE';self.phase(phase,batch);t=time.monotonic();c.execute('BEGIN IMMEDIATE');timing['begin_ms']=(time.monotonic()-t)*1000
                    held=time.monotonic()
                    # No JSON/file replacement while this connection owns SQLite's writer lock.
                    # The independent runtime probe samples active_sql from memory.
                    phase='WRITE_BATCH';self.phase(phase,batch,publish=False);t=time.monotonic()
                    for table in TABLES:
                        values=[(a,b,n) for (name,a,b),n in batch.items() if name==table]
                        if table in ROW_TABLES:values=[n['params'] for a,b,n in values]
                        elif table=='clock_minutes_v0739':values=[(a,b,n['n'],n['min'],n['max'],n['sum'],n['last']) for a,b,n in values]
                        elif table=='meta':values=[(a,n) for a,b,n in values]
                        if values:
                            self.state['active_sql'].update(current_table_v94=table,statement_v94=SQL[table],in_transaction_v94=c.in_transaction)
                            c.executemany(SQL[table],values)
                    timing['write_ms']=(time.monotonic()-t)*1000
                    phase='COMMIT';self.phase(phase,batch,publish=False);t=time.monotonic();c.commit();timing['commit_ms']=(time.monotonic()-t)*1000
                    timing['transaction_ms']=(time.monotonic()-held)*1000
                    with self.lock:
                        self.state['active_sql']=None;self.inflight={};self.state['committed']+=total(batch.values());self.state['batches']+=1
                        for k in ('queue_ms','begin_ms','write_ms','commit_ms'):self.state['max_'+k]=max(self.state['max_'+k],timing[k])
                        self.state['max_transaction_ms']=max(self.state.get('max_transaction_ms',0.),timing['transaction_ms'])
                        self.state['recent']=(self.state['recent']+[timing])[-20:]
                    self.publish()
                except sqlite3.Error as exc:
                    import sqlite_diagnostics
                    failed_ms=(time.monotonic()-t)*1000
                    failed_sql=self.state.get('active_sql')
                    self.phase('ROLLBACK',batch,publish=False)
                    try:c.rollback()
                    except BaseException as rollback_error:
                        self.state['status']='FAILED_UNFLUSHED';self.state['last_error']=dict(sqlite_diagnostics.error(exc),rollback_error=sqlite_diagnostics.error(rollback_error),statements=failed_sql);break
                    detail=dict(sqlite_diagnostics.error(exc),statements=failed_sql,phase=phase,phase_ms=failed_ms,queue_ms=timing['queue_ms'])
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
            if c is not None:
                self.phase('CLOSE',{},publish=False);c.close();self.state['active_sql']=None
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
