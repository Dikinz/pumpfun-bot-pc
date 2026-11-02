"""Separate scheduler process, private WAL database, read-only attached PAPER DB."""
import asyncio,json,os,time,sqlite3,sys,signal
from pathlib import Path
from contextlib import contextmanager
import config,position_monitor as pm,price_exit_trial as trial
from contextlib import closing
EVENT_SCHEMA='''CREATE TABLE IF NOT EXISTS monitor_events_v0731(id INTEGER PRIMARY KEY,ts REAL,kind TEXT,details_json TEXT);'''
PRIMARY_TABLES=('positions','trades','quote_inventory_v0723','wallet_state_v0730','capital_ledger_v0721','paper_orders_v0720','capital_settings_v0721','preentry_v0729','meta')
GRACE=2.0

def paths(primary=None):
    base=Path(primary or config.DB).resolve()
    return dict(db=base.with_name(base.stem+'_monitor.db'),state=base.with_name(base.stem+'_monitor_state.json'),
      journal=base.with_name(base.stem+'_monitor_activity.jsonl'),stdin_lifetime=base.with_name(base.stem+'_monitor_stdin_v81.json'),pending_cleanup=__import__('late_spawn').path(base),supervision=base.with_name(base.stem+'_monitor_supervision_v79.json'),lock=base.with_name(base.stem+'_monitor.lock'))

def connect(primary=None,readonly=False):
    primary=Path(primary or config.DB).resolve();path=paths(primary)['db'];path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path.as_uri()+('?mode=ro' if readonly else '?mode=rwc'),uri=True,timeout=1,isolation_level=None,factory=__import__('sqlite_diagnostics').ObservedConnection)
    c.diagnostic_path=path
    c.row_factory=sqlite3.Row;c.execute('PRAGMA busy_timeout=1000')
    if not readonly:
        c.execute('PRAGMA journal_mode=WAL');c.executescript(pm.SCHEMA+trial.SCHEMA+EVENT_SCHEMA)
    c.execute('ATTACH DATABASE ? AS paper',(primary.as_uri()+'?mode=ro',))
    for table in PRIMARY_TABLES:c.execute(f'CREATE TEMP VIEW {table} AS SELECT * FROM paper.{table}')
    if not readonly:
        import exit_rules_v33
        exit_rules_v33.freeze(c)
    return c

@contextmanager
def singleton(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    f=path.open('a+b');f.seek(0)
    if f.read(1)==b'':f.write(b'0');f.flush()
    f.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:f.close()

def read_state(primary=None):
    return __import__('monitor_state').read(paths(primary)['state'])

def publish(data,primary=None):
    registry=__import__('late_spawn').ACTIVE
    if registry is not None and Path(registry.primary).resolve()==Path(primary or config.DB).resolve():
        data=dict(data,monitor_identity_v86=dict(pid=registry.pid,created_ts=registry.created,session=__import__('recovery_gate').admission().get('session')))
    __import__('state_io').atomic_json(paths(primary)['state'],data)

def compact_heartbeat(state):
    late=state.get('late_creation_v85') or {}
    return dict({k:v for k,v in state.items() if k!='late_creation_v85'},compact_v1014=True,
        late_creation_v85=dict(pending=late.get('pending'),owners=len(late.get('owners') or []),history=len(late.get('history') or [])))

def journal(kind,details,primary=None,c=None):
    record=dict(ts=time.time(),kind=kind,**details);p=paths(primary)['journal'];p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
    if c is not None:
        try:c.execute('INSERT INTO monitor_events_v0731(ts,kind,details_json) VALUES(?,?,?)',(record['ts'],kind,json.dumps(details)))
        except sqlite3.Error:pass # The independent file remains available even if DB writes fail.

def journal_worker_exception(primary,c,lane,job,exc):
    from child_diagnostics import exception
    child=getattr(exc,'child_diagnostics_v73',{})
    if not child:
        try:child=json.loads((pm.ROOT/job['id']/'child_lifecycle_v73.json').read_text())
        except (OSError,ValueError):pass
    journal('WORKER_EXCEPTION',dict(check_id=job['id'],trade_id=job['trade_id'],lane=lane,
        error_type=type(exc).__name__,exception=exception(exc),child_diagnostics_v73=child),primary,c)


def service_status(primary=None,now=None):
    s=read_state(primary);now=time.time() if now is None else now
    s['heartbeat_age_s']=max(0,now-s['heartbeat_ts']) if s.get('heartbeat_ts') else None
    s['stale']=s['heartbeat_age_s'] is None or s['heartbeat_age_s']>=16
    if s.get('status')=='RUNNING' and s['stale']:s['status']='NO_HEARTBEAT'
    return s

def overdue(c,now):
    out=[]
    for p in c.execute('SELECT trade_id,mint,opened_ts FROM positions'):
        row=c.execute('SELECT started_ts,status FROM position_checks_v0730 WHERE trade_id=? ORDER BY started_ts DESC LIMIT 1',(p['trade_id'],)).fetchone()
        due=pm.next_due(c,p['trade_id'],p['opened_ts'])
        if now>due+GRACE:
            out.append(dict(trade_id=p['trade_id'],mint=p['mint'],due_ts=due,late_s=now-due,previous_started_ts=row['started_ts'] if row else None,
              reason='PREVIOUS_CHECK_RUNNING' if row and row['status']=='RUNNING' else 'SCHEDULER_DELAY'))
    return out

async def run(primary,parent_pid,parent_created=None,stop_event=None):
    primary=primary or config.DB
    import psutil
    pths=paths(primary)
    connection_opened=False
    import late_spawn
    previous_registry=late_spawn.ACTIVE
    registry=late_spawn.Registry(primary,__import__('recovery_gate').admission().get('session'))
    late_spawn.ACTIVE=registry
    def poll_late(c):
        registry.session=__import__('recovery_gate').admission().get('session')
        for record in registry.poll():
            journal('LATE_CHILD_CLEANUP_'+record['state'],record,primary,c)
            if record['state']!='CONFIRMED':raise RuntimeError('SIMULATION_LATE_CHILD_CLEANUP_FAILED')
    try:
        with __import__('recovery_gate').reader(primary),singleton(pths['lock']):
            with closing(connect(primary)) as c:
                connection_opened=True
                pm.recover(c);__import__('priority_exit').recover(c);start=time.time();last_mono=time.monotonic();last_wall=start;last_log=0;last_full_log=0;last_publish=0;warned=set();active={};last_alert_row=0;cleanup_failed=False;failure=None
                def parent_alive():
                    if stop_event is not None:return not stop_event.is_set()
                    if not parent_pid:return True
                    try:
                        p=psutil.Process(parent_pid)
                        return p.is_running() and p.status()!=psutil.STATUS_ZOMBIE and (parent_created is None or abs(p.create_time()-parent_created)<.01)
                    except psutil.Error:return False
                journal('START',dict(pid=os.getpid(),parent_pid=parent_pid,recovery_required_v77=True,
                    recovery_admission_v77=__import__('recovery_gate').admission()),primary,c)
                try:
                    while parent_alive():
                        poll_late(c)
                        recovery_admission=__import__('recovery_gate').admission()
                        now=time.time();mono=time.monotonic();gap=mono-last_mono;wall_gap=now-last_wall;last_mono=mono;last_wall=now
                        if gap>3:journal('MONITOR_LOOP_GAP',dict(monotonic_gap_s=gap,wall_gap_s=wall_gap),primary,c)
                        if abs(wall_gap-gap)>2:journal('CLOCK_CHANGE',dict(monotonic_gap_s=gap,wall_gap_s=wall_gap),primary,c)
                        for lane,(task,job) in list(active.items()):
                            if not task.done():continue
                            try:task.result()
                            except asyncio.CancelledError:pass
                            except Exception as e:
                                journal_worker_exception(primary,c,lane,job,e)
                                if str(e).startswith('SIMULATION_CHILD_CLEANUP_') or getattr(e,'child_diagnostics_v73',{}).get('cleanup_exception'):
                                    if registry.handled(job,e):journal('CHILD_CLEANUP_PENDING',dict(check_id=job['id'],original_status='CLEANUP_FAILED'),primary,c)
                                    else:cleanup_failed=True;raise
                                if not registry.handled(job,e):__import__('system_health').failure(primary,'monitor_worker',e)
                            row=c.execute('SELECT status,finished_ts FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
                            if row and row['status']=='RUNNING' and task.cancelled():
                                pm.finish(c,job,dict(status=job.get('cancel_status','INTERRUPTED_STOP')))
                                row=c.execute('SELECT status,finished_ts FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
                            journal('CHECK_FINISHED',dict(check_id=job['id'],trade_id=job['trade_id'],lane=lane,status=row['status'] if row else 'MISSING',finished_ts=row['finished_ts'] if row else None),primary,c)
                            del active[lane]
                        try:
                            for alert in c.execute('SELECT rowid,* FROM monitor_alerts_v0737 WHERE rowid>? ORDER BY rowid',(last_alert_row,)):
                                last_alert_row=alert['rowid']
                                journal('TECHNICAL_ERROR_WARNING',dict(trade_id=alert['trade_id'],streak=alert['streak'],check_id=alert['check_id']),primary,c)
                                print('WARNING: 3 consecutive technical errors. Check dashboard. Retry backoff active. Position '+alert['trade_id'],flush=True)
                            import priority_exit
                            priority_exit.observe(c,now)
                            priority_exit.preempt(c,active)
                            late=overdue(c,now)
                            warned.intersection_update((item['trade_id'],item['previous_started_ts']) for item in late)
                            for item in late:
                                key=(item['trade_id'],item['previous_started_ts'])
                                if key not in warned:journal('MISSED_CHECK',item,primary,c);warned.add(key)
                            hb=c.execute("SELECT v FROM meta WHERE k='heartbeat_ts'").fetchone()
                            age=max(0,now-float(hb[0])) if hb else None
                            state=dict(status='CLEANUP_PENDING' if registry.pending else 'RUNNING',late_creation_v85=registry.snapshot(),pid=os.getpid(),parent_pid=parent_pid,started_ts=start,heartbeat_ts=now,
                              recovery_required_v77=True,recovery_admission_v77=recovery_admission,
                              monotonic_ts=mono,loop_gap_s=gap,primary_heartbeat_age_s=age,primary_stale=age is None or age>15,
                              oldest_active_started_ts=min((job['scheduled_ts'] for task,job in active.values()),default=None),active_check=bool(active),active_check_ids=[job['id'] for task,job in active.values()],active_lanes=list(active),overdue=late)
                            if now-last_publish>=1 or now<last_publish:
                                publish(state,primary);last_publish=now
                            if now-last_log>=5:
                                # v0.7.101.4: the full state (with late-creation history) is journalled once a minute;
                                # the 5 s beats in between are compact. The state file still has everything every second.
                                full=now-last_full_log>=60 or now<last_full_log
                                journal('HEARTBEAT',state if full else compact_heartbeat(state),primary);last_log=now
                                if full:last_full_log=now
                            for lane in ('exit0','exit1','initial','initial2','regular','regular2'):
                                if registry.pending or lane in active or len(active)>=2:continue
                                c.execute('BEGIN')
                                try:
                                    job=priority_exit.dispatch(c,now) if lane.startswith('exit') else pm.next_job(c,now,lane=lane.rstrip('2'))
                                    c.commit()
                                except BaseException:c.rollback();raise
                                if job:
                                    journal('CHECK_STARTED',dict(check_id=job['id'],trade_id=job['trade_id'],mint=job['mint'],lane=lane,entry_to_start_s=now-job['opened_ts']),primary,c)
                                    active[lane]=(asyncio.create_task(pm.run_job(job,c)),job)
                        except (sqlite3.Error,OSError) as e:
                            journal('STORAGE_ERROR',dict(error_type=type(e).__name__,exception=__import__('child_diagnostics').exception(e),state_write_v87=getattr(e,'state_write_v87',None)),primary)
                            publish(dict(status='STORAGE_ERROR',pid=os.getpid(),heartbeat_ts=now,error_type=type(e).__name__),primary)
                        await asyncio.sleep(.1 if active or c.execute('SELECT 1 FROM positions LIMIT 1').fetchone() else .5)
                except BaseException as exc:
                    failure=__import__('child_diagnostics').exception(exc)
                    raise
                finally:
                    for task,job in active.values():task.cancel()
                    for lane,(task,job) in active.items():
                        try:await task
                        except asyncio.CancelledError:pass
                        except Exception as e:
                            journal_worker_exception(primary,c,lane,job,e)
                            if (str(e).startswith('SIMULATION_CHILD_CLEANUP_') or getattr(e,'child_diagnostics_v73',{}).get('cleanup_exception')) and not registry.handled(job,e):cleanup_failed=True
                        row=c.execute('SELECT status,finished_ts FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
                        journal('CHECK_FINISHED',dict(check_id=job['id'],trade_id=job['trade_id'],lane=lane,status=row['status'] if row else 'MISSING',finished_ts=row['finished_ts'] if row else None),primary,c)
                    while registry.pending:
                        poll_late(c)
                        if registry.pending:
                            publish(dict(status='STOP_WAITING_CHILD_CLEANUP',pid=os.getpid(),heartbeat_ts=time.time(),late_creation_v85=registry.snapshot()),primary)
                            await asyncio.sleep(.1)
                    pm.recover(c,status='CLEANUP_FAILED' if cleanup_failed else 'INTERRUPTED_STOP')
                    __import__('priority_exit').stop(c,time.time(),status='CLEANUP_FAILED' if cleanup_failed else 'INTERRUPTED_STOP')
                    publish(dict(status='FAILED' if cleanup_failed or failure else 'STOPPED',failure_v81=failure,pid=os.getpid(),heartbeat_ts=time.time(),started_ts=start,
                        recovery_required_v77=True,recovery_admission_v77=__import__('recovery_gate').admission()),primary)
                    journal('CLEANUP_FAILED' if cleanup_failed else 'STOP',dict(pid=os.getpid()),primary,c)

    finally:
        late_spawn.ACTIVE=previous_registry
        if connection_opened:
            __import__('shutdown_audit').save(pths['db'].with_name(pths['db'].stem+'_connection_close_v68.json'),dict(database=pths['db'].name,pid=os.getpid(),role='monitor',ts=time.time(),status='CLOSED'))

def report(primary=None,now=None):
    primary=primary or config.DB
    if not paths(primary)['db'].exists():
        return dict(history=dict(state="DATABASE_MISSING",by_trade={}),counts={},positions=[],recent=[],timing_counts={},trial={'decisions':{},'rows':[],'paired':0},service=service_status(primary,now),events=[])
    with closing(connect(primary,readonly=True)) as c:
        c.execute('BEGIN')
        state=service_status(primary,now)
        report_time=state.get('heartbeat_ts') if state.get('status')=='STOPPED' else now
        r=pm.summary(c,report_time);r['priority_exit_v65']=__import__('priority_exit').summary(c);r['trial']=trial.summary(c)
        import cohort_report
        r['cohort']=cohort_report.summary(c)
        r['health']=cohort_report.health(c,report_time)
        import connection_audit
        r['connection']=connection_audit.summary(c,report_time)
        import exit_rules_v33
        r['rules']=exit_rules_v33.summary(c)
        import entry_cost_delta
        r['rules']['entry_costs']=entry_cost_delta.summary(c,report_time)
        r['events']=[dict(x) for x in c.execute('SELECT ts,kind,details_json FROM monitor_events_v0731 ORDER BY id DESC LIMIT 50')]
        r['service']=state
        import monitor_history
        r['history']=monitor_history.summary(c,report_time)
        import signal_exit
        r['signal_exit']=signal_exit.summary(c)
        import amm_exit_scenario
        r['amm_signal_exit']=amm_exit_scenario.summary(c)
        import cost_price_audit
        r['cost_price_audit']=cost_price_audit.summary(c,r['signal_exit'])
        import rpc_timing
        with closing(sqlite3.connect(Path(primary).resolve().as_uri()+'?mode=ro',uri=True)) as source:
            boundary=rpc_timing.run_start(source)
        r['rpc_timing']=rpc_timing.monitor_summary(c,boundary)
        return r


def export(primary,folder):
    import zipfile
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    p=paths(primary);target=folder/('position_monitor_'+time.strftime('%Y%m%d_%H%M%S')+'.zip')
    copy_errors=[]
    with zipfile.ZipFile(target,'w',zipfile.ZIP_STORED) as z:
        z.writestr('summary.json',json.dumps(report(primary),ensure_ascii=False))
        if p['db'].exists():
            with closing(connect(primary,readonly=True)) as c:
                c.execute('BEGIN')
                checks=[dict(r) for r in c.execute('SELECT id FROM position_checks_v0730 ORDER BY started_ts')]
                for table in ('position_checks_v0730','monitor_events_v0731','monitor_timing_v0731','price_trial_decisions_v0731','price_trial_exits_v0731','rules_config_v0733','rules_decisions_v0733','rules_exits_v0733','delta_baselines_v0734','monitor_alerts_v0737','priority_exit_requests_v65','exit_retry_v69','exit_retry_attempts_v69','monitor_rpc_pause_v69'):
                    __import__('json_arrays').write(z,table+'.json',(dict(r) for r in c.execute('SELECT * FROM '+table)))
                copy_errors=__import__('monitor_artifacts').add(z,checks,pm.ROOT)
                __import__('late_spawn').add_export(z,checks,pm.ROOT)
            # SQLite backup, including WAL; no raw copy of a potentially uncheckpointed DB.
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                dest=Path(td)/p['db'].name
                with closing(sqlite3.connect(p['db'].as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(dest)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Monitor backup integrity failed')
                z.write(dest,dest.name)
        for key in ('state','pending_cleanup'):
            io_target=__import__('state_io').log_path(p[key])
            for f in (io_target,Path(str(io_target)+'.1')):
                if f.exists():z.write(f,f.name)
        for f in (pm.ROOT/'blobs').glob('*.zlib'):z.write(f,'blobs/'+f.name)
        for key in ('state','journal','stdin_lifetime','pending_cleanup','supervision'):
            if p[key].exists():z.write(p[key],p[key].name)
    __import__('monitor_artifacts').finalize(target,copy_errors)
    return target

async def run_with_stdin(primary,parent_pid,parent_created=None,fd=None):
    from monitor_lifetime import StdinLifetime
    observer=StdinLifetime(fd);failure=None
    try:
        await run(primary,parent_pid,parent_created,observer.stop)
    except BaseException as exc:
        failure=exc;raise
    finally:
        report=await asyncio.to_thread(observer.close)
        try:__import__('runtime_control').atomic_json(paths(primary)['stdin_lifetime'],report)
        except OSError:
            if failure is None:raise
        if not report['confirmed'] or report['error']:
            code='MONITOR_STDIN_SHUTDOWN_UNCONFIRMED' if not report['confirmed'] else 'MONITOR_STDIN_READ_FAILED'
            state=read_state(primary);state.update(status='FAILED',stdin_failure_v81=code,stdin_lifetime_v81=report,heartbeat_ts=time.time())
            try:publish(state,primary)
            except OSError:
                if failure is None:raise
            if failure is None:raise RuntimeError(code)

if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('--parent-pid',type=int,default=0);ap.add_argument('--parent-created',type=float);ap.add_argument('--stdin-lifetime',action='store_true');a=ap.parse_args()
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    asyncio.run(run_with_stdin(config.DB,a.parent_pid,a.parent_created) if a.stdin_lifetime else run(config.DB,a.parent_pid,a.parent_created))
