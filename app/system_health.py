"""Bounded local flight recorder and launcher-side process sampling. No RPC/orders."""
import json, os, time, traceback, uuid, sqlite3, shutil
from pathlib import Path

MAX_LOG_BYTES = 2 * 1024 * 1024
# run_20261101 fix: 2 backups (3 x 2 MiB) held ~64 min at the observed ~1.63 KB/s, less than
# a 120-min PAPER run; 8 backups (9 x 2 MiB ~ 18.9 MB) cover ~3.2 h. Diagnostics only.
# v0.7.101.4: the 02.11 night run wrote ~1.9 KB/s, so 9 files held only the last 2 h 40 min.
# 35 backups (36 x 2 MiB ~ 75 MB) cover ~10 h.
BACKUPS = 35

def paths(primary):
    p = Path(primary).resolve()
    return dict(state=p.with_name(p.stem+'_system_state.json'),
                journal=p.with_name(p.stem+'_system_events.jsonl'))

def identity(primary):
    import hashlib
    return hashlib.sha256(os.path.normcase(str(Path(primary).resolve())).encode()).hexdigest()[:20]

def read_state(primary):
    try:
        d = json.loads(paths(primary)['state'].read_text(encoding='utf-8'))
        if not isinstance(d,dict):return {}
        stamp=d.get('heartbeat_ts')
        if not isinstance(stamp,(int,float)) or not __import__('math').isfinite(stamp):return {}
        if not isinstance(d.get('alerts',[]),list) or any(not isinstance(x,str) for x in d.get('alerts',[])):return {}
        return d
    except (OSError, ValueError):
        return {}

def write_state(primary, data):
    p=paths(primary)['state'];p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        tmp.write_text(json.dumps(data,allow_nan=False),encoding='utf-8');tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)

def event(primary, kind, **details):
    # Only callers' controlled codes/numbers and frame locations; never exception
    # messages, locals, source lines, URLs, environment, command lines or keys.
    p=paths(primary)['journal']
    try:
        p.parent.mkdir(parents=True,exist_ok=True)
        if p.exists() and p.stat().st_size >= MAX_LOG_BYTES:
            p.with_name(p.name+'.'+str(BACKUPS)).unlink(missing_ok=True)
            for i in range(BACKUPS-1,0,-1):
                f=p.with_name(p.name+'.'+str(i))
                if f.exists():f.replace(p.with_name(p.name+'.'+str(i+1)))
            p.replace(p.with_name(p.name+'.1'))
        record=dict(ts=time.time(),pid=os.getpid(),kind=kind,**details)
        with p.open('a',encoding='utf-8') as f:f.write(json.dumps(record,allow_nan=False)+'\n')
        return True
    except (OSError,ValueError):
        return False

def failure(primary, component, exc):
    frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name)
            for f in traceback.extract_tb(exc.__traceback__)[-12:]]
    return event(primary,'UNHANDLED_ERROR',component=component,
                 error_type=type(exc).__name__,frames=frames,
                 exception_v99=__import__('monitor_publication_v99').error(exc))

async def supervise(name, coroutine, primary, allow_return=False):
    import asyncio
    try:
        await coroutine
    except (asyncio.CancelledError,__import__('runtime_control').RequestedStop):
        raise
    except BaseException as exc:
        failure(primary,name,exc);raise
    else:
        event(primary,'TASK_RETURNED',component=name)
        if allow_return:return
        raise RuntimeError('A required background task returned: '+name)

class Recorder:
    def __init__(self, primary):
        self.primary=primary;self.session=uuid.uuid4().hex;self.origin=time.monotonic()
        self.procs={};self.previous=set();self.last_sample=0.;self.last={}
        self.loop_lag_window=__import__('collections').deque(maxlen=120)
        self.monitor_observer=__import__('monitor_state').Observer(16.)
        event(primary,'LAUNCHER_START',session=self.session)
    def sample(self, processes, force=False):
        import psutil
        mono=time.monotonic()
        if not force and mono-self.last_sample<5:return self.last
        self.last_sample=mono;now=time.time();rows=[];alerts=[];rss=0.;cpu=0.
        for role, child in processes.items():
            if child is None:continue
            code=child.poll()
            if code is not None:
                alerts.append(role.upper()+'_EXITED')
                rows.append(dict(role=role,pid=child.pid,exit_code=code));continue
            try:
                p=self.procs.get(child.pid)
                if p is None:p=psutil.Process(child.pid);self.procs[child.pid]=p;p.cpu_percent(None)
                family=[p]+p.children(recursive=True)
                for member in family:
                    cached=self.procs.get(member.pid)
                    if cached is None:
                        cached=member;self.procs[member.pid]=cached;cached.cpu_percent(None)
                    mem=cached.memory_info().rss/1048576;usage=cached.cpu_percent(None)
                    handles=cached.num_handles() if hasattr(cached,'num_handles') else cached.num_fds() if hasattr(cached,'num_fds') else None
                    rows.append(dict(role=role if member.pid==p.pid else role+'_child',pid=member.pid,rss_mb=round(mem,2),cpu_pct=usage,handles=handles,handles_kind='HANDLES' if hasattr(cached,'num_handles') else 'FD'))
                    rss+=mem;cpu+=usage
            except psutil.Error:
                if not any(row['pid']==child.pid for row in rows):rows.append(dict(role=role,pid=child.pid,sample_status='UNAVAILABLE'))
                alerts.append(role.upper()+'_SAMPLE_UNAVAILABLE')
        active_pids={r['pid'] for r in rows};self.procs={k:v for k,v in self.procs.items() if k in active_pids}
        data=dict(session=self.session,status='RUNNING',heartbeat_ts=now,elapsed_s=mono-self.origin,
                  database_identity=identity(self.primary),processes=rows,total_rss_mb=round(rss,2),total_cpu_pct=cpu)
        try:
            primary=Path(self.primary).resolve()
            with __import__('contextlib').closing(sqlite3.connect(primary.as_uri()+'?mode=ro',uri=True,timeout=.1)) as c:
                meta=dict(c.execute('SELECT k,v FROM meta'))
                opened=c.execute('SELECT COUNT(*) FROM positions').fetchone()[0]
            age=now-float(meta.get('heartbeat_ts',0));data['primary_heartbeat_age_s']=age
            publication=__import__('monitor_publication_v99').from_meta(meta)
            data['monitor_publication_v99']=publication
            if publication.get('status') in ('FAILED','UNREADABLE'):alerts.append(__import__('monitor_publication_v99').ALERT)
            data['open_positions']=opened
            data['event_loop_lag_ms']=float(meta.get('event_loop_lag_ms',0))
            if mono-self.origin>30 and (age>15 or age < -2):alerts.append('PRIMARY_HEARTBEAT_INVALID')
            if __import__('math').isfinite(data['event_loop_lag_ms']):self.loop_lag_window.append(data['event_loop_lag_ms'])
            window=sorted(self.loop_lag_window)
            data['loop_lag_samples_v87']=dict(n=len(window),p50_ms=window[len(window)//2] if window else None,p95_ms=window[min(len(window)-1,int(len(window)*.95))] if window else None,scope='LAST_120_FIVE_SECOND_SAMPLES_NOT_EVERY_TICK')
            if data['event_loop_lag_ms']>500:alerts.append('EVENT_LOOP_LAG')
            data['database_bytes']=primary.stat().st_size
            wal=Path(str(primary)+'-wal');data['wal_bytes']=wal.stat().st_size if wal.exists() else 0
            data['disk_free_bytes']=shutil.disk_usage(primary.parent).free
            if data['disk_free_bytes']<512*1048576:alerts.append('LOW_DISK_SPACE')
            import monitor_service
            raw_monitor=monitor_service.read_state(self.primary)
            monitor=self.monitor_observer.observe(raw_monitor,time.monotonic())
            data['monitor_status']=monitor['status'];data['monitor_supervision_v86']=monitor
            if mono-self.origin>15:
                if monitor['status']!='RUNNING':alerts.append(monitor['status'])
                if monitor['state_read_error_v86']:alerts.append('MONITOR_STATE_READ_ERROR')
            if raw_monitor.get('overdue'):alerts.append('MONITOR_OVERDUE')
        except (OSError,sqlite3.Error,ValueError):alerts.append('SYSTEM_SAMPLE_UNAVAILABLE')
        if rss>1000:alerts.append('PROCESS_TREE_MEMORY_HIGH')
        data['resource_sample_complete']=not any('SAMPLE_UNAVAILABLE' in x for x in alerts)
        data['alerts']=sorted(set(alerts));current=set(alerts)
        for code in current-self.previous:
            details=dict(supervision=data.get('monitor_supervision_v86')) if code.startswith('MONITOR_') and code not in ('MONITOR_EXITED','MONITOR_SAMPLE_UNAVAILABLE') else {}
            event(self.primary,'ALERT_OPEN',code=code,session=self.session,**details)
        for code in self.previous-current:
            details=dict(supervision=data.get('monitor_supervision_v86')) if code.startswith('MONITOR_') and code not in ('MONITOR_EXITED','MONITOR_SAMPLE_UNAVAILABLE') else {}
            event(self.primary,'ALERT_CLEARED',code=code,session=self.session,**details)
        self.previous=current
        if not event(self.primary,'SAMPLE',**data):data['alerts'].append('JOURNAL_WRITE_FAILED')
        try:write_state(self.primary,data)
        except OSError:print('WARNING: diagnostic state could not be saved.',flush=True)
        self.last=data;return data
    def stop(self, code, reason):
        data=dict(self.last,session=self.session,status='STOPPED' if code==0 else 'FAILED',
                  heartbeat_ts=time.time(),elapsed_s=time.monotonic()-self.origin,exit_code=code,stop_reason=reason)
        event(self.primary,'LAUNCHER_STOP',session=self.session,exit_code=code,reason=reason)
        try:write_state(self.primary,data)
        except OSError:print('WARNING: final diagnostic state could not be saved.',flush=True)

def history(primary, limit=80):
    from collections import deque
    rows=deque(maxlen=limit)
    # Skip bulk resource samples in the on-screen alert history.
    p=paths(primary)['journal']
    for f in [p.with_name(p.name+'.'+str(i)) for i in range(BACKUPS,0,-1)]+[p]:
        if not f.exists():continue
        with f.open(encoding='utf-8',errors='replace') as stream:
            for line in stream:
                try:
                    row=json.loads(line)
                    if row.get('kind')!='SAMPLE':rows.append(row)
                except ValueError:pass
    return list(rows)

def export(primary, folder):
    import zipfile
    p=paths(primary)
    with zipfile.ZipFile(Path(folder)/'system_flight_recorder.zip','w',zipfile.ZIP_DEFLATED) as z:
        for f in [p['state'],p['journal']]+[p['journal'].with_name(p['journal'].name+'.'+str(i)) for i in range(1,BACKUPS+1)]:
            if f.is_file():z.write(f,f.name)


def apply_lifecycle(snapshot, primary):
    """Accept a successful user stop only when it covers this runtime heartbeat."""
    state=read_state(primary);runtime=snapshot.get('runtime',{})
    started=runtime.get('started_ts');updated=runtime.get('updated_ts');stopped=state.get('heartbeat_ts')
    matches=(state.get('database_identity')==identity(primary)
        and state.get('status')=='STOPPED' and state.get('stop_reason')=='USER_STOP'
        and state.get('exit_code')==0 and state.get('shutdown_confirmed') is not False and runtime.get('state')!='FAILED'
        and all(isinstance(x,(int,float)) and __import__('math').isfinite(x) for x in (started,updated,stopped))
        and 0 < started <= stopped <= time.time()+2
        and (updated<=stopped or (runtime.get('state')=='STOPPED' and updated-stopped<=10)))
    if not matches:return False
    runtime.update(state='STOPPED',label='Остановлен пользователем',frozen=True,stop_reason='USER_STOP')
    diag=snapshot.get('diagnostics',{})
    old_blocks=diag.get('entry_blocks',[])
    diag['entry_blocks']=['Бот остановлен пользователем']+[b for b in old_blocks if b not in ('Бот остановлен','Бот остановлен пользователем','Нет свежего heartbeat: состояние бота неизвестно')]
    diag['entry_status']=' / '.join(diag['entry_blocks'])
    diag.update(frozen=True,runtime_state='STOPPED',stop_reason='USER_STOP')
    snapshot.get('market_feed',{}).update(state='STOPPED')
    run=snapshot.get('runs',{}).get('current')
    if run and isinstance(run.get('started_ts'),(int,float)) and abs(run['started_ts']-started)<1:run['status']='STOPPED'
    return True
