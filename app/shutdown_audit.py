"""Independent bounded integrity probes and observed-process shutdown evidence."""
import json,os,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
from contextlib import closing

def save(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8');tmp.replace(path)

def probe(primary,phase):
    from sqlite_diagnostics import error
    primary=Path(primary).resolve();rows=[]
    for p in sorted(primary.parent.glob('*.db')):
        if not (p.name.startswith(primary.stem) or p.name=='program_cache_v57.db'):continue
        row=dict(database=p.name,started_ts=time.time(),status='INDETERMINATE')
        try:
            with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=.25)) as c:
                c.execute('PRAGMA query_only=ON');deadline=time.monotonic()+1
                c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
                c.execute('BEGIN')
                findings=[x[0] for x in c.execute('PRAGMA integrity_check(20)')]
                row.update(status='OK' if findings==['ok'] else 'CORRUPT',findings=findings)
        except sqlite3.Error as exc:
            row.update(status='CORRUPT' if getattr(exc,'sqlite_errorcode',0)&255 in (11,26) else 'INDETERMINATE',error=error(exc))
        row['finished_ts']=time.time();rows.append(row)
    return dict(schema='INTEGRITY_PHASE_V68',phase=phase,ts=time.time(),probe_pid=os.getpid(),databases=rows,
      scope='Separate read-only SQLite snapshots, not one transaction across databases; no repair, checkpoint or settings change')

def collect(primary,phase):
    primary=Path(primary).resolve();out=primary.with_name(primary.stem+'_integrity_'+phase+'_v68.json')
    proc=None
    try:
        flags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0
        proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--probe',str(primary),str(out),phase],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
        proc.wait(timeout=12)
        if proc.returncode==0:return json.loads(out.read_text(encoding='utf-8'))
        result=dict(phase=phase,status='PROBE_FAILED',exit_code=proc.returncode,databases=[])
    except subprocess.TimeoutExpired:
        from branch_transport import terminate_tree
        terminate_tree(proc);result=dict(phase=phase,status='PROBE_TIMEOUT',databases=[])
    except Exception as exc:result=dict(phase=phase,status='PROBE_UNAVAILABLE',error_type=type(exc).__name__,databases=[])
    save(out,result);return result

def closed(database,role,session=None):
    """Close only this thread's owned DB connection and write independent evidence."""
    import database as db
    c=getattr(db._LOCAL,'conn',None);data=dict(database=Path(database).name,pid=os.getpid(),role=role,session=session,ts=time.time(),status='NOT_OPEN')
    if c is not None:
        try:
            data['had_active_transaction']=c.in_transaction;c.close();db._LOCAL.conn=None;data['status']='CLOSED'
        except sqlite3.Error as exc:
            from sqlite_diagnostics import error
            data.update(status='CLOSE_FAILED',error=error(exc))
    try:save(Path(database).with_name(Path(database).stem+'_'+role+'_connection_close_v68.json'),data)
    except OSError as exc:data['ack_write_error_type']=type(exc).__name__
    return data

class Audit:
    def __init__(self,primary):
        self.primary=Path(primary).resolve();self.session=uuid.uuid4().hex;self.started=time.time();self.processes={};self.errors=[];self.before=None
    def capture(self,children):
        import psutil
        for role,child in children.items():
            if child.poll() is not None:continue
            try:
                root=psutil.Process(child.pid)
                for p in [root]+root.children(recursive=True):
                    created=p.create_time();self.processes[(p.pid,created)]=dict(pid=p.pid,created_ts=created,role=role if p.pid==root.pid else role+'_descendant')
            except psutil.NoSuchProcess:pass
            except psutil.Error as exc:self.errors.append(dict(role=role,error_type=type(exc).__name__))
        self.errors=self.errors[-20:]
    def begin_stop(self,children):
        self.capture(children);exit_codes={role:p.poll() for role,p in children.items()}
        self.before=collect(self.primary,'before')
        self.before['role_exit_codes_before_request']=exit_codes
        save(self.primary.with_name(self.primary.stem+'_integrity_before_v68.json'),self.before)
    def finish(self,children,stops):
        import psutil
        self.capture(children);rows=[]
        for item in self.processes.values():
            row=dict(item)
            try:
                p=psutil.Process(item['pid'])
                row['status']='PID_REUSED_ORIGINAL_EXITED' if abs(p.create_time()-item['created_ts'])>.01 else 'EXITED_ZOMBIE' if p.status()==psutil.STATUS_ZOMBIE else 'STILL_RUNNING'
            except psutil.NoSuchProcess:row['status']='EXITED'
            except psutil.Error:row['status']='UNVERIFIED'
            rows.append(row)
        observed_exited=bool(rows) and all(r['status'] in ('EXITED','EXITED_ZOMBIE','PID_REUSED_ORIGINAL_EXITED') for r in rows) and not self.errors
        connections=[];counters=[]
        for pattern,target in [('*_connection_close_v68.json',connections),('*_counter_writer_stop_v68.json',counters)]:
            for f in self.primary.parent.glob(pattern):
                if not f.name.startswith(self.primary.stem):continue
                try:
                    d=json.loads(f.read_text(encoding='utf-8'))
                    if d.get('ts',0)>=self.started:target.append(d)
                except (OSError,ValueError):pass
        # A launcher started successfully must provide its own connection close,
        # plus closures/counter acks for every actually observed runtime worker.
        closed_pids={r['pid'] for r in connections if r['status'] in ('CLOSED','NOT_OPEN')}
        stopped_pids={r['pid'] for r in counters if r.get('confirmed')}
        import paper_branch,route_branch
        expected_workers=set();runtime_roles={}
        for label,target in [('primary',self.primary),('amm',paper_branch.path(self.primary)),('route',route_branch.path(self.primary))]:
            f=target.with_name(target.stem+'_runtime_v60.json')
            try:
                d=json.loads(f.read_text());pid=d.get('pid')
                if d.get('started_ts',0)>=self.started and pid:expected_workers.add(pid);runtime_roles[pid]=label
            except (OSError,ValueError):pass
        expected_connections=set(expected_workers)
        for f in self.primary.parent.glob(self.primary.stem+'*_monitor_state.json'):
            try:
                d=json.loads(f.read_text())
                if d.get('started_ts',0)>=self.started and d.get('pid'):
                    expected_connections.add(d['pid']);runtime_roles[d['pid']]=f.name.removesuffix('_state.json')
            except (OSError,ValueError):pass
        for row in rows:row['runtime_role']=runtime_roles.get(row['pid'])
        acks=bool(expected_workers) and expected_connections<=closed_pids and expected_workers<=stopped_pids and os.getpid() in closed_pids
        after=collect(self.primary,'after')
        confirmed=observed_exited and acks and expected_connections<={r['pid'] for r in rows}
        b={r['database']:r for r in (self.before or {}).get('databases',[])};a={r['database']:r for r in after.get('databases',[])}
        comparison=[dict(database=n,before=b.get(n,{}).get('status','NOT_MEASURED'),after=a.get(n,{}).get('status','NOT_MEASURED')) for n in sorted(set(a)|set(b))]
        integrity_ok=self.primary.name in a and self.primary.name in b and bool(comparison) and all(r['before']=='OK' and r['after']=='OK' for r in comparison)
        result=dict(runtime_roles=runtime_roles,integrity_phases_ok=integrity_ok,expected_connection_pids=sorted(expected_connections),schema='SHUTDOWN_AUDIT_V68',session=self.session,started_ts=self.started,ts=time.time(),processes=rows,observation_errors=self.errors,
          stop_results=stops,connections=connections,counter_writers=counters,expected_runtime_pids=sorted(expected_workers),observed_processes_exited=observed_exited,
          writer_acks_complete=acks,all_observed_writers_finished=confirmed,after_is_after_confirmed_writers=confirmed,
          before=self.before,after=after,comparison=comparison,note='Observed installation process tree only; unrelated external writers cannot be excluded. Forced termination is separate from graceful acknowledgement. Integrity is not a financial reconciliation.')
        save(self.primary.with_name(self.primary.stem+'_shutdown_v68.json'),result);return result

if __name__=='__main__':
    if len(sys.argv)!=5 or sys.argv[1]!='--probe':raise SystemExit(2)
    save(sys.argv[3],probe(sys.argv[2],sys.argv[4]))
