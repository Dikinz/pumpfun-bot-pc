"""Bounded local fanout with explicit, acknowledged child shutdown."""
import json,queue,threading,subprocess,sys,time,uuid,os
from pathlib import Path
import runtime_control
ACTIVE=None
LAST=None

def bind_worker(child,ready,session):
    """Windows venv launchers can be a parent of the actual Python worker."""
    if not isinstance(ready,dict) or ready.get('session')!=session:
        raise RuntimeError('BRANCH_READY_SESSION_MISMATCH')
    pid=ready.get('pid')
    if type(pid)!=int or pid<=0:raise RuntimeError('BRANCH_READY_INVALID_PID')
    if pid!=child.pid:
        import psutil
        try:related=child.pid in [p.pid for p in psutil.Process(pid).parents()]
        except psutil.Error:related=False
        if not related:raise RuntimeError('BRANCH_READY_PID_NOT_DESCENDANT')
    return pid

def process_options(platform=None):
    return {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if (platform or os.name)=='nt' else {'start_new_session':True}

def terminate_tree(child):
    import psutil
    try:descendants=psutil.Process(child.pid).children(recursive=True)
    except psutil.Error:descendants=[]
    if __import__('late_spawn').wait_before_force(child,descendants):
        try:
            child.wait(timeout=3)
            _,remaining=psutil.wait_procs(descendants,timeout=0)
            if not remaining:return False
        except subprocess.TimeoutExpired:pass
    for p in reversed(descendants):
        try:p.terminate()
        except psutil.Error:pass
    if child.poll() is None:child.terminate()
    _,alive=psutil.wait_procs(descendants,timeout=2)
    for p in alive:
        try:p.kill()
        except psutil.Error:pass
    try:child.wait(timeout=3)
    except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)

class Publisher:
    def __init__(self,primary,module="paper_branch"):
        self.created_mono=time.monotonic();self.engine=__import__(module);self.module=module;self.first_gap=None;self.pending_bytes=0;self.peak_bytes=0;self.byte_lock=threading.Lock();self.last_status=0.;self.max_send_ms=0.
        global LAST
        self.queue=queue.Queue(maxsize=2000);self.seq=0;self.dropped=0;self.error=False;self.primary=primary;self.protected={}
        self.session=uuid.uuid4().hex;self.stopping=threading.Event();self.close_lock=threading.Lock();self.closed=None;self.thread=None
        ready=Path(primary).resolve().with_name('branch_ready_'+self.session+'.tmp')
        self.child=subprocess.Popen([sys.executable,module+'.py','--worker',str(Path(primary).resolve()),str(ready),self.session],stdin=subprocess.PIPE,text=True,bufsize=1,cwd=Path(__file__).parent,**process_options())
        LAST=self
        deadline=time.monotonic()+20
        while not ready.exists():
            if self.child.poll() is not None:raise RuntimeError('BRANCH_START_FAILED')
            if time.monotonic()>deadline:
                terminate_tree(self.child);raise RuntimeError('BRANCH_START_TIMEOUT')
            time.sleep(.05)
        try:
            self.worker_pid=bind_worker(self.child,json.loads(ready.read_text(encoding='utf-8')),self.session)
        finally:ready.unlink(missing_ok=True)
        self.thread=threading.Thread(target=self.send,daemon=True);self.thread.start()
        self.watchdog=runtime_control.ChildWatchdog(self.session,self.worker_pid)
    def refresh_positions(self):
        import sqlite3,paper_branch
        from contextlib import closing
        try:
            with closing(sqlite3.connect(getattr(self,'engine',paper_branch).path(self.primary).as_uri()+'?mode=ro',uri=True,timeout=.1)) as c:
                self.protected=dict(c.execute('SELECT mint,opened_ts FROM positions'))
        except sqlite3.Error:pass
    def begin_stop(self):
        if not self.stopping.is_set():
            self.stop_started_ts=time.time();self.stop_started_mono=time.monotonic()
            self.stopping.set()
            self.persist_status(force=True)
        return getattr(self,'stop_started_ts',None)
    def valid_stop_ack(self,ack):
        since=getattr(self,'stop_started_ts',float('inf'))
        if hasattr(self,'primary') and hasattr(self,'module'):
            request=__import__('branch_isolation').controlled_request(self.primary,self.module,self.session,getattr(self,'worker_pid',None))
            if request:since=min(since,request['ts'])
        return (ack.get('session')==self.session and ack.get('status')=='STOPPED'
            and ack.get('pid')==getattr(self,'worker_pid',None)
            and type(ack.get('ts')) in (int,float) and ack['ts']>=since
            and not ack.get('monitor_forced'))
    def snapshot(self):
        return dict(failure=getattr(self,'failure',None),delivery_uncertain=getattr(self,'delivery_uncertain',0),unsent=self.queue.qsize()+getattr(self,'unsent_batch',0),stop_requested=self.stopping.is_set(),stop_started_ts=getattr(self,'stop_started_ts',None),stop_pipe_error=getattr(self,'stop_pipe_error',None),sender_finished=not self.thread.is_alive() if getattr(self,'thread',None) else None,module=getattr(self,'module','paper_branch'),session=self.session,launcher_pid=getattr(getattr(self,'child',None),'pid',None),worker_pid=getattr(self,'worker_pid',None),queue_depth=self.queue.qsize(),queue_capacity=self.queue.maxsize,pending_bytes=getattr(self,'pending_bytes',0),peak_bytes=getattr(self,'peak_bytes',0),dropped=self.dropped,first_gap=getattr(self,'first_gap',None),max_send_ms=getattr(self,'max_send_ms',0.),send_blocked_age_s=max(0,time.monotonic()-self.send_started) if getattr(self,'send_started',0) else 0,unsent_on_stop=self.queue.qsize()+getattr(self,'unsent_batch',0) if self.stopping.is_set() else 0,error=self.error)
    def record_gap(self,reason,**details):
        if getattr(self,'first_gap',None) is None:self.first_gap=dict(reason=reason,ts=time.time(),seq=self.seq,depth=self.queue.qsize(),**details)
    def persist_status(self,force=False):
        if not hasattr(self,'engine') or not hasattr(self,'primary'):return
        mono=time.monotonic()
        if force or mono-getattr(self,'last_status',0)>=.5:
            p=self.engine.path(self.primary);target=p.with_name(p.stem+'_transport_status_v60.json')
            try:runtime_control.atomic_json(target,self.snapshot())
            except OSError:pass
            self.last_status=mono
    def emit(self,m,side,p,trader,source,evidence,born,_payload=None):
        if getattr(self,'stopping',None) is not None and self.stopping.is_set():
            if self.error or getattr(self,'failure',None):self.dropped+=1
            return
        self.seq+=1
        item=dict(_payload) if _payload is not None else dict(mint=m,side=side,price=p,trader=trader,source=source,evidence=evidence,born=born,ts=time.time())
        item['seq']=self.seq
        wire=json.dumps(item,allow_nan=False)+'\n';size=len(wire.encode())
        if size>65536:
            self.record_gap('FRAME_TOO_LARGE',frame_bytes=size);self.error=True;return
        lock=getattr(self,'byte_lock',None)
        if lock is not None:
            with lock:
                if self.pending_bytes+size>4*1024*1024:
                    self.dropped+=1;self.record_gap('QUEUE_BYTE_LIMIT');return
                self.pending_bytes+=size;self.peak_bytes=max(self.peak_bytes,self.pending_bytes)
        item['_wire']=wire
        try:self.queue.put_nowait(item)
        except queue.Full:
            self.dropped+=1;self.record_gap('QUEUE_COUNT_LIMIT')
            if lock is not None:
                with lock:self.pending_bytes-=size
    def send(self):
        try:
            while not self.stopping.is_set():
                try:x=self.queue.get(timeout=.05)
                except queue.Empty:
                    self.persist_status();continue
                batch=[x]
                for _ in range(31):
                    try:batch.append(self.queue.get_nowait())
                    except queue.Empty:break
                wires=[x.get('_wire') or json.dumps(x,allow_nan=False)+'\n' for x in batch]
                with self.byte_lock:self.pending_bytes-=sum(len(w.encode()) for w in wires)
                if self.stopping.is_set():
                    self.unsent_batch=len(batch);break
                t=time.monotonic();self.send_started=t;self.child.stdin.write(''.join(wires));self.child.stdin.flush();self.send_started=0
                self.max_send_ms=max(self.max_send_ms,(time.monotonic()-t)*1000)
                self.persist_status()
        except (OSError,ValueError) as exc:
            if self.stopping.is_set():self.stop_pipe_error=dict(error_type=type(exc).__name__,ts=time.time(),errno=getattr(exc,'errno',None))
            else:
                self.error=True;self.delivery_uncertain=len(batch) if 'batch' in locals() else 0
                self.record_gap('PIPE_WRITE_ERROR',delivery_uncertain=self.delivery_uncertain,errno=getattr(exc,'errno',None))
                self.begin_stop()
        finally:
            self.persist_status(force=True)
            # Keep stdin open during quiescence: EOF must not outrun control STOP.
            if not self.stopping.is_set():
                try:self.child.stdin.close()
                except (OSError,ValueError):pass
    def close(self,timeout=18):
        import paper_branch,system_health
        with self.close_lock:
            if self.closed is not None:return self.closed
            # Stop sending BEFORE independent child control; never write STOP to a closed pipe.
            self.begin_stop()
            engine=getattr(self,'engine',paper_branch)
            runtime_control.request_stop(engine.path(self.primary),self.session)
            if self.thread is None:
                try:self.child.stdin.close()
                except (OSError,ValueError,AttributeError):pass
            forced=False
            try:self.child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                forced=terminate_tree(self.child) is not False
            if self.thread is not None:self.thread.join(timeout=1)
            if self.thread is None or not self.thread.is_alive():
                try:self.child.stdin.close()
                except (OSError,ValueError,AttributeError):pass
            engine=getattr(self,'engine',paper_branch)
            ack=engine.read_lifecycle(self.primary)
            valid=ack.get('session')==self.session and ack.get('status') in ('STOPPED','FAILED')
            status='FAILED' if getattr(self,'failure',None) else 'STOPPED' if self.valid_stop_ack(ack) and self.child.returncode==0 and not forced and not self.error and not (self.thread and self.thread.is_alive()) else 'FAILED' if valid and ack['status']=='FAILED' else 'FORCED_STOP' if forced else 'UNCONFIRMED_EXIT'
            self.closed=dict(session=self.session,status=status,exit_code=self.child.returncode,forced=forced,ts=time.time(),stop_started_ts=getattr(self,'stop_started_ts',None),sender_finished=not self.thread.is_alive() if self.thread else True,stop_pipe_error=getattr(self,'stop_pipe_error',None),unsent_on_stop=(self.queue.qsize() if hasattr(self,'queue') else 0)+getattr(self,'unsent_batch',0))
            self.closed['failure']=getattr(self,'failure',None)
            self.closed['cleanup_confirmed']=bool(self.valid_stop_ack(ack) and self.child.returncode==0 and not forced and not (self.thread and self.thread.is_alive()))
            engine.write_transport(self.primary,self.closed)
            if getattr(self,'failure',None):__import__('branch_isolation').finish(self.primary,self.session,self.closed)
            system_health.event(self.primary,'BRANCH_STOP_ACK' if status=='STOPPED' else 'BRANCH_STOP_UNCONFIRMED',session=self.session,status=status,exit_code=self.child.returncode)
            return self.closed

def close_last():
    if LAST:return LAST.close()

def emit(*args):
    if ACTIVE:ACTIVE.emit(*args)

def watches(m):return ACTIVE is not None and m in ACTIVE.protected

def born(m):return ACTIVE.protected.get(m,time.time()) if ACTIVE else time.time()

class Fanout:
    """Two durable scenarios share one normalized market feed, separate monitors."""
    def __init__(self,primary):
        global LAST
        self.publishers=[];self.protected={};LAST=self
        try:
            for module in __import__('branch_mode_v100').modules(primary):
                p=Publisher.__new__(Publisher)
                try:Publisher.__init__(p,primary,module)
                except BaseException:
                    if hasattr(p,'child'):self.publishers.append(p)
                    raise
                self.publishers.append(p);LAST=self
        except BaseException:
            self.close();LAST=self;raise
    def emit(self,*args):
        payload=dict(zip(('mint','side','price','trader','source','evidence','born'),args));payload['ts']=time.time()
        for p in self.publishers:p.emit(*args,_payload=payload)
    def snapshot(self):return dict(branches=[p.snapshot() for p in self.publishers])
    def refresh_positions(self):
        self.protected={}
        for p in self.publishers:
            p.refresh_positions()
            for mint,opened in p.protected.items():self.protected[mint]=min(opened,self.protected.get(mint,opened))
    @property
    def pending(self):return sum(p.queue.qsize() for p in self.publishers)
    @property
    def dropped(self):return sum(p.dropped for p in self.publishers)
    @property
    def failed(self):
        for p in self.publishers:
            if p.stopping.is_set() and not p.error and p.child.poll()==0 and p.valid_stop_ack(p.engine.read_lifecycle(p.primary)):continue
            if p.error or p.child.poll() is not None:
                self.failure_reason=p.module+':'+('PIPE_WRITE_ERROR' if p.error else 'CHILD_EXITED')
                return True
            r=runtime_control.watchdog_read(p.engine.path(p.primary))
            if time.monotonic()-p.created_mono>30:
                if not hasattr(p,'watchdog'):p.watchdog=runtime_control.ChildWatchdog(p.session,getattr(p,'worker_pid',p.child.pid))
                failed=p.watchdog.observe(r,time.time(),time.monotonic())
                target=p.engine.path(p.primary);target=target.with_name(target.stem+'_watchdog_v61.json')
                try:runtime_control.atomic_json(target,p.watchdog.snapshot())
                except OSError:pass
                if failed:
                    self.failure_reason=p.module+':'+p.watchdog.confirmed['repeat']['reason']
                    p.record_gap('CHILD_WATCHDOG_CONFIRMED',watchdog=p.watchdog.confirmed);return True
        return False
    def isolate_failures(self):
        """Off-loop inspection. Failed scenarios stay disabled until next session."""
        import branch_isolation
        found=[]
        for p in self.publishers:
            if getattr(p,'failure',None):continue
            single=Fanout.__new__(Fanout);single.publishers=[p]
            if not single.failed:continue
            p.failure=dict(status='FAILED',reason=single.failure_reason,ts=time.time(),session=p.session,module=p.module,stopped_open_positions=list(getattr(p,'protected',{})))
            p.begin_stop();branch_isolation.record(p.primary,p.failure);p.persist_status(force=True);found.append(p)
        return found
    def begin_stop(self):
        # Mark all branches before any branch receives STOP.
        return [p.begin_stop() for p in self.publishers]
    def close(self):
        self.begin_stop()
        from concurrent.futures import ThreadPoolExecutor
        if not self.publishers:return []
        with ThreadPoolExecutor(max_workers=len(self.publishers)) as pool:results=list(pool.map(lambda p:p.close(),self.publishers))
        failed=[p for p in self.publishers if getattr(p,'failure',None)]
        if failed:__import__('branch_isolation').end_session(failed[0].primary,[p.session for p in failed])
        return results
