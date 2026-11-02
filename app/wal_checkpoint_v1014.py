"""Background WAL checkpoints for the runtime PAPER database (v0.7.101.4).

The 01.11 and 02.11 runs stopped on a SQLite write that blocked the market loop for
15-53 s. In WAL mode with synchronous=NORMAL the only fsync on the commit path is the
automatic checkpoint, which SQLite runs inside the committing connection. Here the
market connection does not checkpoint; a daemon thread with its own connection runs
PASSIVE checkpoints, which never wait for readers or writers. If that thread dies, or
stops after repeated checkpoint errors, the market connection gets its automatic
checkpoint back, so the WAL cannot grow unbounded.
"""
import sqlite3,threading,time

INTERVAL_S=1.0
SLOW_MS=1000.0
MAX_CONSECUTIVE_ERRORS=10
AUTOCHECKPOINT_PAGES=1000

class Checkpointer:
    def __init__(self,path,connection,interval=None):
        self.path=str(path);self.connection=connection;self.interval=INTERVAL_S if interval is None else interval
        self.lock=threading.Lock();self.stop=threading.Event()
        self.state=dict(schema='WAL_CHECKPOINT_V1014',status='STARTING',started_ts=time.time(),checkpoints=0,busy=0,slow=0,errors=0,
            last_ms=None,max_ms=0.0,last_ok_ts=None,in_checkpoint_since=None,wal_frames=None,checkpointed_frames=None,last_error=None,fallback_autocheckpoint=False)
        connection.execute('PRAGMA wal_autocheckpoint=0')
        self.thread=threading.Thread(target=self.run,name='wal-checkpoint-v1014',daemon=True)
        self.thread.start()
    def run(self):
        c=None
        try:
            c=sqlite3.connect(self.path,timeout=.25,isolation_level=None,check_same_thread=False)
            c.execute('PRAGMA busy_timeout=250')
            with self.lock:self.state['status']='RUNNING'
            failures=0
            while not self.stop.wait(self.interval):
                started=time.monotonic()
                with self.lock:self.state['in_checkpoint_since']=time.time()
                try:busy,frames,done=c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
                except sqlite3.Error as exc:
                    with self.lock:self.state.update(errors=self.state['errors']+1,last_error=type(exc).__name__+': '+str(exc)[:200],in_checkpoint_since=None)
                    failures+=1
                    # Exit so guard() hands the automatic checkpoint back to the market connection.
                    if failures>=MAX_CONSECUTIVE_ERRORS:raise
                    continue
                failures=0
                ms=(time.monotonic()-started)*1000
                with self.lock:
                    s=self.state;slow=ms>=SLOW_MS
                    s.update(checkpoints=s['checkpoints']+1,busy=s['busy']+int(bool(busy)),slow=s['slow']+int(slow),last_ms=ms,max_ms=max(s['max_ms'],ms),
                        wal_frames=frames,checkpointed_frames=done,last_ok_ts=time.time(),in_checkpoint_since=None)
                if slow:__import__('sqlite_diagnostics').record(self.path,'WAL_CHECKPOINT_SLOW',duration_ms=round(ms,1),wal_frames=frames,checkpointed_frames=done)
        except BaseException as exc:
            with self.lock:self.state.update(status='FAILED',last_error=type(exc).__name__+': '+str(exc)[:200])
        finally:
            if c is not None:c.close()
            with self.lock:
                if self.state['status']!='FAILED':self.state['status']='STOPPED'
    def guard(self):
        """Market thread only: restore the automatic checkpoint if the background thread died."""
        if self.thread.is_alive() or self.stop.is_set():return False
        with self.lock:
            if self.state['fallback_autocheckpoint']:return False
            self.state['fallback_autocheckpoint']=True
        self.connection.execute('PRAGMA wal_autocheckpoint=%d'%AUTOCHECKPOINT_PAGES)
        __import__('sqlite_diagnostics').record(self.path,'WAL_CHECKPOINT_FALLBACK',status=self.state['status'],last_error=self.state['last_error'])
        return True
    def snapshot(self):
        with self.lock:return dict(self.state,thread_alive=self.thread.is_alive())
    def close(self,timeout=2.0):
        """Owning (market) thread: stop the checkpoints and give the connection its automatic checkpoint back."""
        self.stop.set();self.thread.join(timeout)
        try:self.connection.execute('PRAGMA wal_autocheckpoint=%d'%AUTOCHECKPOINT_PAGES)
        except sqlite3.Error:pass # already closed: nothing left to checkpoint through it

ACTIVE=None

def start(path,connection):
    global ACTIVE
    if ACTIVE is not None and (ACTIVE.path!=str(path) or ACTIVE.connection is not connection):
        ACTIVE.close();ACTIVE=None
    if ACTIVE is None:ACTIVE=Checkpointer(path,connection)
    return ACTIVE

def guard():return ACTIVE.guard() if ACTIVE is not None else False

def close():
    global ACTIVE
    if ACTIVE is not None:ACTIVE.close();ACTIVE=None

def status():return ACTIVE.snapshot() if ACTIVE is not None else None
