"""Bounded runtime diagnostics and independent STOP, no strategy or RPC policy."""
import asyncio,json,os,re,sys,threading,time,queue
from pathlib import Path
from contextlib import contextmanager
ACTIVE=None

class RequestedStop(Exception):
    """Session-bound launcher request, handled as normal primary shutdown."""

def request_primary_stop(primary):
    import sqlite3
    from contextlib import closing
    # Bind STOP to a durable run, never a folder-wide flag reused by next run.
    try:
        with closing(sqlite3.connect(Path(primary).resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)) as c:
            row=c.execute("SELECT id FROM runs_v45 WHERE status='RUNNING' ORDER BY rowid DESC LIMIT 1").fetchone()
    except sqlite3.Error:return None # Signal path still quiesces primary; never stop branches early.
    if row:request_stop(primary,row[0]);return row[0]


def control_path(primary,session):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',session):raise ValueError('INVALID_CONTROL_SESSION')
    p=Path(primary).resolve();return p.with_name(p.stem+'_stop_'+session+'.json')

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+str(os.getpid())+'.'+str(threading.get_ident())+'.tmp')
    try:tmp.write_text(json.dumps(value,allow_nan=False),encoding='utf-8');tmp.replace(path)
    finally:tmp.unlink(missing_ok=True)

def request_stop(primary,session):atomic_json(control_path(primary,session),dict(control='STOP',session=session))

def stop_requested(primary,session):
    try:
        d=json.loads(control_path(primary,session).read_text());return d.get('control')=='STOP' and d.get('session')==session
    except (OSError,ValueError,TypeError,AttributeError):return False

def status_path(primary):
    p=Path(primary).resolve();return p.with_name(p.stem+'_runtime_v60.json')

async def close_market_socket(ws,stopping=False):
    """One bounded close; stopping never runs the reconnect cooldown."""
    if ws is None:return
    try:await asyncio.wait_for(ws.close(),timeout=1.5 if stopping else 4)
    except asyncio.CancelledError:
        transport=getattr(ws,'transport',None)
        if transport is not None:transport.abort()
        raise
    except Exception:
        transport=getattr(ws,'transport',None)
        if transport is not None:transport.abort()

async def cancel_tasks(named,primary,timeout=5):
    import system_health
    for _,task in named:task.cancel()
    _,pending=await asyncio.wait([task for _,task in named],timeout=timeout)
    if pending:
        frames=[]
        for name,task in named:
            if task in pending:
                frames.append(dict(task=name,frames=[dict(file=Path(f.f_code.co_filename).name,
                    function=f.f_code.co_name,line=f.f_lineno) for f in task.get_stack(limit=12)]))
        system_health.event(primary,'TASK_SHUTDOWN_TIMEOUT',pending=frames)
        for task in pending:task.cancel()
        _,pending=await asyncio.wait(pending,timeout=2)
    return not pending

class InputState:
    def __init__(self,clock=time.time):
        self.seq=0;self.applied=0;self.first=None;self.stale=0;self.clock=clock;self.max_age_ms=0.;self.depth_max=0
    def observe(self,item,depth=0):
        now=self.clock();n=item.get('seq');stamp=item.get('ts');expected=self.seq+1;
        if type(n)!=int or n<1:raise ValueError('INVALID_INPUT_SEQUENCE')
        self.depth_max=max(self.depth_max,depth)
        reason=None;age=now-stamp if type(stamp) in (int,float) else float('inf')
        if type(n)!=int or n!=self.seq+1:reason='SEQUENCE_GAP'
        elif not 0<=age<=1:reason='STALE_INPUT'
        if type(n)==int:self.seq=n
        if not 0<=age<=1:self.stale+=1
        if 0<=age<1e12:self.max_age_ms=max(self.max_age_ms,age*1000)
        if reason and self.first is None:
            self.first=dict(reason=reason,ts=now,expected_seq=expected,received_seq=n,
                age_ms=age*1000 if abs(age)<1e12 else None,depth=depth,mint=str(item.get('mint',''))[:64],emitted_ts=stamp if type(stamp) in (int,float) and abs(stamp)<1e12 else None)
        if 0<=age<=1:self.applied=n;return True
        return False
    def snapshot(self):return dict(received_seq=self.seq,applied_seq=self.applied,first_gap=self.first,stale_inputs=self.stale,max_input_age_ms=self.max_age_ms,queue_depth_max=self.depth_max)

class ByteInbox(queue.Queue):
    def __init__(self,maxsize=2000,max_bytes=4*1024*1024):
        super().__init__(maxsize);self.bytes=0;self.max_bytes=max_bytes
    def _put(self,item):
        super()._put(item);self.bytes+=len(item) if item is not None else 0
    def _get(self):
        item=super()._get();self.bytes-=len(item) if item is not None else 0;return item
    def put(self,item,block=True,timeout=None):
        size=len(item) if item is not None else 0
        if size>self.max_bytes:raise queue.Full
        end=time.monotonic()+timeout if timeout is not None else None
        with self.not_full:
            while (self.maxsize and self._qsize()>=self.maxsize) or self.bytes+size>self.max_bytes:
                if not block:raise queue.Full
                remain=None if end is None else end-time.monotonic()
                if remain is not None and remain<=0:raise queue.Full
                self.not_full.wait(remain)
            self._put(item);self.unfinished_tasks+=1;self.not_empty.notify()

class Probe:
    def __init__(self,primary,extra=None,control=None):
        self.started_ts=time.time();self.primary=primary;self.extra=extra or (lambda:{});self.control=control
        self.main_thread=threading.get_ident();self.pulse=time.monotonic();self.stage='IDLE';self.closed=threading.Event();self.stop=threading.Event()
        self.samples={};self.lock=threading.Lock();self.last_stall=0.;self.stalls=0;self.first_stack=None
    def record(self,name,seconds):
        with self.lock:
            a=self.samples.setdefault(name,dict(calls=0,total_ms=0.,max_ms=0.))
            a['calls']+=1;a['total_ms']+=seconds*1000;a['max_ms']=max(a['max_ms'],seconds*1000)
    def inspect(self):
        now=time.monotonic();lag=now-self.pulse
        if lag>3 and (not self.last_stall or now-self.last_stall>=30):
            self.last_stall=now;self.stalls+=1
            frame=sys._current_frames().get(self.main_thread);stack=[]
            while frame and len(stack)<16:
                stack.append(dict(file=Path(frame.f_code.co_filename).name,function=frame.f_code.co_name,line=frame.f_lineno));frame=frame.f_back
            if self.first_stack is None:self.first_stack=dict(age_s=lag,stage=self.stage,frames=stack,ts=time.time())
        with self.lock:samples={k:dict(v) for k,v in self.samples.items()}
        return dict(schema='RUNTIME_V60',pid=os.getpid(),started_ts=self.started_ts,session=self.control[1] if self.control else None,ts=time.time(),state='PROBE_STOPPED' if self.closed.is_set() else 'RUNNING',
            loop_pulse_age_s=lag,active_stage=self.stage,stalls=self.stalls,first_stall=self.first_stack,samples=samples,**self.extra())
    def watch(self):
        while not self.closed.wait(.5):
            try:
                if self.control and stop_requested(*self.control):self.stop.set()
                atomic_json(status_path(self.primary),self.inspect())
            except (OSError,ValueError,RuntimeError):pass
    async def run(self):
        while True:self.pulse=time.monotonic();await asyncio.sleep(.1)
    def start(self):
        global ACTIVE
        ACTIVE=self;self.thread=threading.Thread(target=self.watch,daemon=True,name='runtime-watch');self.thread.start();self.task=asyncio.create_task(self.run());return self
    async def close(self):
        global ACTIVE
        self.closed.set();self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        await asyncio.to_thread(self.thread.join,1)
        try:atomic_json(status_path(self.primary),self.inspect())
        except OSError:pass
        if ACTIVE is self:ACTIVE=None

@contextmanager
def scope(name):
    p=ACTIVE
    if p is None:yield;return
    old=p.stage;p.stage=name;start=time.monotonic()
    try:yield
    finally:p.record(name,time.monotonic()-start);p.stage=old

def measured(name):
    def decorate(fn):
        import functools
        @functools.wraps(fn)
        def call(*args,**kw):
            with scope(name):return fn(*args,**kw)
        return call
    return decorate

class MarketTelemetry:
    """Only counters/timestamps. Trading ledger, evidence and prices never buffered."""
    def __init__(self):self.count=0;self.raw_ts=0.;self.market_ts=None;self.last=0.
    def received(self,now):self.count+=1;self.raw_ts=now
    def market(self,now):self.market_ts=now
    def flush(self,db,force=False):
        mono=time.monotonic()
        if not force and mono-self.last<.25:return False
        if not self.count and self.market_ts is None:return False
        # Clear only after commit: a failed flush never invents delivered counters.
        with db.atomic():
            if self.count:db.inc('pumpapi_messages',self.count)
            values=dict(pumpapi_last_message_ts=self.raw_ts,trade_last_raw_ts=self.raw_ts)
            if self.market_ts is not None:values.update(trade_last_market_ts=self.market_ts,trade_stream_state='RECEIVING',trade_watchdog_state='RECEIVING')
            db.setmany(values)
        self.count=0;self.market_ts=None;self.last=mono;return True

def read(primary):
    try:return json.loads(status_path(primary).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}

def watchdog_read(primary):
    try:
        def reject_constant(value):raise ValueError('NON_FINITE_JSON')
        value=json.loads(status_path(primary).read_text(encoding='utf-8'),parse_constant=reject_constant)
        return value if isinstance(value,dict) else dict(read_error='NOT_AN_OBJECT')
    except (OSError,ValueError) as exc:
        return dict(read_error=type(exc).__name__,errno=getattr(exc,'errno',None))

class ChildWatchdog:
    """Require a second observation; retain the actual trigger, not a later snapshot."""
    def __init__(self,session,pid):
        self.session=session;self.pid=pid;self.pending=None;self.events=[];self.confirmed=None
    def observe(self,snapshot,wall,mono):
        import math
        snapshot=json.loads(json.dumps(snapshot))
        valid=lambda x:type(x) in (int,float) and math.isfinite(x)
        age=wall-snapshot['ts'] if valid(snapshot.get('ts')) else None
        pulse=snapshot.get('loop_pulse_age_s')
        reason=('UNREADABLE_SNAPSHOT' if not snapshot or snapshot.get('read_error') else
          'SESSION_MISMATCH' if snapshot.get('session')!=self.session else
          'PID_MISMATCH' if snapshot.get('pid')!=self.pid else
          'INVALID_SNAPSHOT_TIME' if age is None or age < -1 or not valid(pulse) or pulse<0 else
          'STALE_SNAPSHOT' if age>15 else 'EVENT_LOOP_STALLED' if pulse>15 else
          'UNEXPECTED_PROBE_STATE' if snapshot.get('state')!='RUNNING' else None)
        sample=dict(observed_ts=wall,snapshot_age_s=age,expected_session=self.session,expected_pid=self.pid,
                    reason=reason,snapshot=snapshot)
        if reason is None:
            if self.pending:self.events.append(dict(state='RECOVERED',trigger=self.pending['sample'],repeat=sample))
            self.pending=None;self.events=self.events[-32:];return False
        if self.pending is None or self.pending['sample']['reason']!=reason:
            self.pending=dict(mono=mono,sample=sample);self.events.append(dict(state='SUSPECT',trigger=sample))
        else:
            # Missing/foreign/stale files do not prove an event-loop stall. Give the
            # independent probe 15 seconds to publish a current-session snapshot.
            delay=2 if reason=='EVENT_LOOP_STALLED' else 15
            if mono-self.pending['mono']>=delay:
                self.confirmed=dict(state='CONFIRMED',trigger=self.pending['sample'],repeat=sample)
                self.events.append(self.confirmed);self.events=self.events[-32:];return True
        self.events=self.events[-32:];return False
    def snapshot(self):
        return dict(schema='CHILD_WATCHDOG_V61',session=self.session,pid=self.pid,
                    state='CONFIRMED' if self.confirmed else 'SUSPECT' if self.pending else 'HEALTHY',events=self.events)

def export(primary,folder):
    import paper_branch,route_branch,shutil
    for p in (primary,paper_branch.path(primary),route_branch.path(primary)):
        src=status_path(p)
        if src.exists():shutil.copy2(src,Path(folder)/src.name)
        for suffix in ('_transport_status_v60.json','_watchdog_v61.json'):
            f=Path(p).resolve();src=f.with_name(f.stem+suffix)
            if src.exists():shutil.copy2(src,Path(folder)/src.name)

def request_branch_stops(primary):
    import paper_branch,route_branch
    for engine in (paper_branch,route_branch):
        state=engine.read_lifecycle(primary)
        if state.get('session') and state.get('status')=='RUNNING':request_stop(engine.path(primary),state['session'])

def shutdown_status(primary,bot_result):
    import sqlite3,paper_branch,route_branch
    from contextlib import closing
    children={}
    for engine in (paper_branch,route_branch):
        ack=engine.read_lifecycle(primary);transport=engine.read_lifecycle(primary,True)
        children[engine.__name__]=dict(ack=ack,transport=transport,confirmed=bool(ack.get('status')=='STOPPED' and transport.get('status')=='STOPPED' and ack.get('session')==transport.get('session') and transport.get('exit_code')==0 and not transport.get('forced') and not ack.get('monitor_forced')))
    state=None
    try:
        with closing(sqlite3.connect(Path(primary).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as c:
            row=c.execute("SELECT v FROM meta WHERE k='runtime_state_v41'").fetchone();state=row[0] if row else None
    except sqlite3.Error:pass
    confirmed=bool(bot_result and not bot_result.get('forced') and bot_result.get('exit_code')==0 and state=='STOPPED' and all(x['confirmed'] for x in children.values()))
    return dict(confirmed=confirmed,primary_state=state,bot=bot_result,branches=children)
