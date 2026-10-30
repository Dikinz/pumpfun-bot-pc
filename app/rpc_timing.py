"""Bounded, best-effort diagnostics. Never logs URLs, params or error messages.

Network behavior, strategy gates and timeouts remain owned by existing callers.
OPEN_HEADERS combines DNS/TCP/TLS/server wait: urllib cannot separate them.
"""
import json,time,uuid,sqlite3,math
from pathlib import Path
from contextlib import contextmanager
ACTIVE=None
SESSION=uuid.uuid4().hex
SCHEMA='''CREATE TABLE IF NOT EXISTS preentry_timing_v56(
 job_id TEXT PRIMARY KEY,session TEXT,enqueued_mono REAL,started_mono REAL,
 queue_ms REAL,run_ms REAL,depth_start INTEGER,diagnostics_json TEXT);'''

class Collector:
    def __init__(self,out):
        self.path=Path(out)/'rpc_timing.json';self.started=time.monotonic();self.stage_at=self.started
        self.data=dict(schema='RPC_TIMING_V56',stages=[],requests=[],active_stage='CHILD_START',write_errors=0,checkpoint_ms=0.)
    def flush(self):
        t=time.monotonic()
        try:
            tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.data,allow_nan=False),encoding='utf-8');tmp.replace(self.path)
        except Exception:self.data['write_errors']+=1
        self.data['checkpoint_ms']+=(time.monotonic()-t)*1000
    def stage(self,name):
        now=time.monotonic();self.data['stages'].append(dict(stage=self.data['active_stage'],elapsed_ms=(now-self.stage_at)*1000))
        self.data['stages']=self.data['stages'][-64:];self.data['active_stage']=name;self.stage_at=now;self.flush()
    def snapshot(self):
        return dict(self.data,child_elapsed_ms=(time.monotonic()-self.started)*1000)

def start(out):
    global ACTIVE
    ACTIVE=Collector(out);ACTIVE.flush()
def stage(name):
    if ACTIVE:ACTIVE.stage(name)
@contextmanager
def stage_scope(name):
    old=ACTIVE.data['active_stage'] if ACTIVE else None
    stage(name)
    try:yield
    finally:
        if old is not None:stage(old)

def snapshot():return ACTIVE.snapshot() if ACTIVE else None

class Span:
    def __init__(self,method,count=None):
        self.collector=ACTIVE
        self.row=dict(method=method,account_count=count,stage=ACTIVE.data['active_stage'] if ACTIVE else None,status='STARTED',phases_ms={})
    @contextmanager
    def phase(self,name):
        if self.collector:self.row['active_phase']=name;self.collector.flush()
        t=time.monotonic()
        try:yield
        finally:self.row['phases_ms'][name]=(time.monotonic()-t)*1000
    def __enter__(self):
        if self.collector:
            self.collector.data['requests'].append(self.row);self.collector.data['requests']=self.collector.data['requests'][-64:];self.collector.flush()
        self.t=time.monotonic();return self
    def __exit__(self,typ,exc,tb):
        self.row.update(elapsed_ms=(time.monotonic()-self.t)*1000,status=('RPC_ERROR' if self.row.get('rpc_rejected') else 'OK') if exc is None else 'ERROR')
        if exc is not None:
            import connection_audit
            self.row['error']=connection_audit.diagnostic(exc)
            code=getattr(exc,'http_status',None) or getattr(exc,'code',None)
            self.row['http_status']=code if type(code)==int else None
        else:self.row['active_phase']=None
        if self.collector:self.collector.flush()
        return False

def request(method,count=None):return Span(method,count)

def attach(report,out):
    if isinstance(report.get('rpc_timing'),dict):return report
    try:
        p=Path(out)/'rpc_timing.json'
        if p.stat().st_size<=131072:report['rpc_timing']=json.loads(p.read_text())
    except Exception:pass
    return report

def queue_add(c,ident):
    try:c.execute('INSERT OR IGNORE INTO preentry_timing_v56(job_id,session,enqueued_mono) VALUES(?,?,?)',(ident,SESSION,time.monotonic()))
    except sqlite3.Error:pass

def queue_start(c,j,now):
    try:
        row=c.execute('SELECT * FROM preentry_timing_v56 WHERE job_id=?',(j['job_id'],)).fetchone()
        mono=time.monotonic();depth=c.execute("SELECT COUNT(*) FROM preentry_v0729 WHERE status='PENDING'").fetchone()[0]
        wait=(mono-row['enqueued_mono'])*1000 if row and row['session']==SESSION else None
        c.execute('UPDATE preentry_timing_v56 SET started_mono=?,queue_ms=?,depth_start=? WHERE job_id=?',(mono,wait,depth,j['job_id']))
    except sqlite3.Error:pass

def queue_finish(c,j,report):
    try:
        row=c.execute('SELECT * FROM preentry_timing_v56 WHERE job_id=?',(j['job_id'],)).fetchone()
        if not row:return
        mono=time.monotonic();same=row['session']==SESSION
        wait=row['queue_ms'] if row['queue_ms'] is not None else (mono-row['enqueued_mono'])*1000 if same else None
        runtime=(mono-row['started_mono'])*1000 if same and row['started_mono'] is not None else None
        c.execute('UPDATE preentry_timing_v56 SET queue_ms=?,run_ms=?,diagnostics_json=? WHERE job_id=?',(wait,runtime,json.dumps(report.get('rpc_timing') or {}),j['job_id']))
    except sqlite3.Error:pass

def distribution(values):
    a=sorted(float(v) for v in values if v is not None and math.isfinite(float(v)) and float(v)>=0)
    return dict(n=len(a),p50_ms=a[max(0,math.ceil(len(a)*.5)-1)] if a else None,p95_ms=a[max(0,math.ceil(len(a)*.95)-1)] if a else None,max_ms=max(a) if a else None)

def rpc_summary(reports):
    groups={};missing=0;stages={};control_values={k:[] for k in ('metadata_hold_ms','sqlite_commit_ms')};control_counts={k:0 for k in ('connections_opened','transactions','outcomes_drained')};contention={}
    cache=dict(jobs=0,hits=0,downloads=0,bytecode_bytes_reused=0,modes={})
    for report in reports:
        d=report.get('rpc_timing') or {}
        observed=set()
        chains=[(report.get('network_error') or {}).get('chain') or []]+[(row.get('error') or {}).get('chain') or [] for row in d.get('requests',[])]
        for chain in chains:
            for item in chain:
                c=item.get('control_v95') or {}
                if c.get('stage'):observed.add(c['stage'])
        for stage in observed:contention[stage]=contention.get(stage,0)+1
        if not d:missing+=1
        pc=d.get('program_cache')
        if pc:
            cache['jobs']+=1
            for name in ('hits','downloads','bytecode_bytes_reused'):cache[name]+=pc.get(name,0)
            mode=pc.get('mode','UNKNOWN');cache['modes'][mode]=cache['modes'].get(mode,0)+1
        for interval in d.get('stages',[]):stages.setdefault(interval['stage'],[]).append(interval.get('elapsed_ms'))
        for row in d.get('requests',[]):
            control=(row.get('rpc_admission_v89') or {}).get('control_v95') or {}
            for field in control_values:
                if type(control.get(field)) in (int,float):control_values[field].append(control[field])
            for field in control_counts:
                if type(control.get(field)) in (int,float) and math.isfinite(control[field]) and control[field]>=0:control_counts[field]+=control[field]
            key=(row.get('stage'),row.get('method'));g=groups.setdefault(key,dict(stage=key[0],method=key[1],requests=0,errors=0,incomplete=0,http_statuses={},phases={},durations=[]))
            g['requests']+=1;g['errors']+=row.get('status') in ('ERROR','RPC_ERROR');g['incomplete']+=row.get('status')=='STARTED'
            g['durations'].append(row.get('elapsed_ms'))
            if type(row.get('rpc_error_code'))==int:
                k='RPC_'+str(row['rpc_error_code']);g['http_statuses'][k]=g['http_statuses'].get(k,0)+1
            for k,v in row.get('phases_ms',{}).items():g['phases'].setdefault(k,[]).append(v)
            if row.get('http_status') is not None:
                k=str(row['http_status']);g['http_statuses'][k]=g['http_statuses'].get(k,0)+1
    for g in groups.values():g['elapsed']=distribution(g.pop('durations'));g['phases']={k:distribution(v) for k,v in g['phases'].items()}
    return dict(control_v95=dict(timings={k:distribution(v) for k,v in control_values.items()},counts=control_counts,checks_by_contention_stage=contention,note='Local coordination only; nested in worker time, not network latency. Contention counts deduplicated by stage within a check.'),program_cache=cache,groups=list(groups.values()),stage_intervals=[dict(stage=k,elapsed=distribution(v)) for k,v in stages.items()],uninstrumented_jobs=missing,note='Current run only. OPEN_HEADERS combines connection and server wait. Missing data is unknown. Durations are monotonic; instrumentation adds checkpoint IO.')

def run_start(c):
    try:
        row=c.execute('SELECT started_ts FROM runs_v45 ORDER BY started_ts DESC LIMIT 1').fetchone();return row[0] if row else None
    except sqlite3.Error:return None

def preentry_summary(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preentry_timing_v56'").fetchone():return dict(available=False,note='Timing instrumentation absent; no historical timing invented.')
    start=run_start(c)
    try:
        rows=[dict(r) for r in c.execute('SELECT p.*,t.queue_ms,t.run_ms,t.depth_start FROM preentry_v0729 p LEFT JOIN preentry_timing_v56 t ON p.job_id=t.job_id WHERE p.created_ts>=?',(start,))] if start is not None else []
    except sqlite3.OperationalError:return dict(available=False,note='Timing instrumentation absent; no historical timing invented.')
    reports=[json.loads(r['result_json'] or '{}') for r in rows];counts={}
    for r in rows:counts[r['status']]=counts.get(r['status'],0)+1
    return dict(available=True,run_started_ts=start,jobs=len(rows),statuses=counts,queue=distribution(r['queue_ms'] for r in rows),runtime=distribution(r['run_ms'] for r in rows),rpc=rpc_summary(reports),
        recent=[dict(job_id=r['job_id'],status=r['status'],queue_ms=r['queue_ms'],runtime_ms=r['run_ms'],depth_start=r['depth_start'],active_stage=(reports[i].get('rpc_timing') or {}).get('active_stage'),legacy_queue_wall_ms=(r['started_ts']-r['created_ts'])*1000 if r['started_ts'] is not None else None) for i,r in list(enumerate(rows))[-50:]],
        note='Queue/run measured within one process session. Legacy wall estimate is separate and may reflect clock changes. No extra retries or changed deadlines.')

def monitor_summary(c,start):
    if start is None:return dict(available=False,note='Primary run boundary unknown.')
    rows=[dict(r) for r in c.execute('SELECT status,result_json FROM position_checks_v0730 WHERE started_ts>=?',(start,))]
    return dict(available=True,run_started_ts=start,jobs=len(rows),rpc=rpc_summary([json.loads(r['result_json'] or '{}') for r in rows]))
