"""Read-only evidence captured when the launcher stops a stalled primary.

run_20261101 fix: the user's PAPER run ended with the primary main thread blocked
53 s inside a SQLite COMMIT (busy_timeout 250 ms, ~0 % CPU). The root cause is
not proven (WAL checkpoint/fsync, antivirus or disk I/O are candidates). This
module records what is observable from outside the primary BEFORE the forced
stop: database/WAL/SHM sizes and times, process/thread CPU and I/O counters,
and the primary's own runtime probe (all-thread stacks at the first stall).
It opens no database, sends no signal and changes no trading or timing rule.
"""
import json,os,time
from pathlib import Path

SCHEMA='PRIMARY_STALL_EVIDENCE_V1012'


def path(primary):
    p=Path(primary).resolve();return p.with_name(p.stem+'_stall_evidence_v1012.json')


def file_stats(primary):
    p=Path(primary).resolve();out=[]
    for f in (p,p.with_name(p.name+'-wal'),p.with_name(p.name+'-shm')):
        try:
            st=f.stat();out.append(dict(name=f.name,bytes=st.st_size,mtime=st.st_mtime))
        except OSError as exc:out.append(dict(name=f.name,error_type=type(exc).__name__))
    return out


def _proc(p):
    import psutil
    row=dict(pid=p.pid)
    for key,call in (('name',p.name),('status',p.status),('num_threads',p.num_threads)):
        try:row[key]=call()
        except (psutil.Error,OSError) as exc:row[key+'_error']=type(exc).__name__
    try:t=p.cpu_times();row['cpu_user_s']=t.user;row['cpu_system_s']=t.system
    except (psutil.Error,OSError) as exc:row['cpu_times_error']=type(exc).__name__
    try:row['rss_bytes']=p.memory_info().rss
    except (psutil.Error,OSError) as exc:row['memory_error']=type(exc).__name__
    try:
        io=p.io_counters();row['io']=dict(read_count=io.read_count,write_count=io.write_count,read_bytes=io.read_bytes,write_bytes=io.write_bytes)
    except (psutil.Error,OSError,AttributeError) as exc:row['io_error']=type(exc).__name__
    try:row['threads']=[dict(id=t.id,user_s=t.user_time,system_s=t.system_time) for t in p.threads()[:64]]
    except (psutil.Error,OSError) as exc:row['threads_error']=type(exc).__name__
    return row


def processes(child):
    if child is None:return []
    import psutil
    try:root=psutil.Process(child.pid);family=[root]+root.children(recursive=True)
    except psutil.Error as exc:return [dict(pid=getattr(child,'pid',None),error_type=type(exc).__name__)]
    return [_proc(p) for p in family[:32]]


def capture(primary,children=None,sampled=None,write=True):
    """Best effort; every section fails independently and never raises."""
    data=dict(schema=SCHEMA,ts=time.time(),launcher_pid=os.getpid(),root_cause_proven=False,
              note='Evidence only. Root cause of the primary SQLite stall is not proven; no thresholds changed.')
    try:data['files']=file_stats(primary)
    except Exception as exc:data['files_error']=type(exc).__name__
    try:data['bot_processes']=processes((children or {}).get('bot'))
    except Exception as exc:data['bot_processes_error']=type(exc).__name__
    try:
        runtime=__import__('runtime_control').read(primary)
        data['primary_runtime_v60']={k:runtime.get(k) for k in ('pid','ts','state','loop_pulse_age_s','active_stage','sql_v91','stalls','first_stall','latest_stall_v1012')}
    except Exception as exc:data['primary_runtime_error']=type(exc).__name__
    if isinstance(sampled,dict):
        data['recorder']={k:sampled.get(k) for k in ('primary_heartbeat_age_s','alerts','event_loop_lag_ms','total_cpu_pct','disk_free_bytes') if k in sampled}
    if write:
        try:
            p=path(primary);tmp=p.with_name(p.name+'.tmp')
            tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8');tmp.replace(p)
            data['written']=p.name
        except OSError as exc:data['write_error']=type(exc).__name__
    return data
