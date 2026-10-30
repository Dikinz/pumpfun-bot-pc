"""Cross-process admission for PAPER RPC reads in one installation.

OS-held byte locks bound concurrent requests and disappear on process death.
A separate SQLite control DB shares origin rate/cooldown and diagnostic counters.
It never opens a trading database. No endpoint URLs, keys, request bodies or
wallet state are persisted. No retries or changes to trading freshness limits.
"""
import errno,hashlib,json,math,os,sqlite3,time,urllib.error,uuid,traceback,mmap,struct,threading
from contextlib import closing,contextmanager
from pathlib import Path
from urllib.parse import urlsplit

class Deferred(TimeoutError):
    def __init__(self,reason,retry_after_s,wait_ms=0):
        self.rpc_budget_reason=reason;self.retry_after_s=max(0.,float(retry_after_s));self.wait_ms=wait_ms
        super().__init__('LOCAL_RPC_'+reason)

class Unavailable(RuntimeError):pass

class MetadataBusy(sqlite3.OperationalError):pass


def failure_details(exc):
    # Never retain arbitrary exception text: it can contain URLs/credentials.
    chain=[]
    seen=set()
    while exc is not None and id(exc) not in seen and len(chain)<8:
        seen.add(id(exc));text=str(exc).lower()
        reason=('METADATA_LOCK_BUSY' if isinstance(exc,MetadataBusy) else
                'SQLITE_BUSY_OR_LOCKED' if isinstance(exc,sqlite3.Error) and ('locked' in text or 'busy' in text) else
                'SQLITE_DISK_FULL' if 'full' in text else
                'SQLITE_READONLY' if 'readonly' in text else
                'SQLITE_CORRUPT' if 'malformed' in text or 'not a database' in text else
                'RPC_RATE_STATE_INVALID' if isinstance(exc,Unavailable) and str(exc)=='RPC_RATE_STATE_INVALID' else
                'RPC_OUTCOME_INVALID' if isinstance(exc,Unavailable) and str(exc)=='RPC_OUTCOME_INVALID' else
                'RPC_OUTCOME_BACKLOG_EXCEEDED' if isinstance(exc,Unavailable) and str(exc)=='RPC_OUTCOME_BACKLOG_EXCEEDED' else
                'RPC_LIMITS_DISAGREE' if isinstance(exc,Unavailable) and str(exc)=='RPC_LIMITS_DISAGREE_BETWEEN_PROCESSES' else
                'UNCLASSIFIED')
        safe_messages=('database is locked','database table is locked','disk i/o error',
            'database or disk is full','attempt to write a readonly database',
            'file is not a database','database disk image is malformed',
            'unable to open database file','cannot commit - no transaction is active',
            'cannot start a transaction within a transaction')
        message=next((m for m in safe_messages if text==m),None)
        chain.append(dict(error_type=type(exc).__name__,reason=reason,safe_message=message,
            frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(exc.__traceback__)[-8:]],
            errno=getattr(exc,'errno',None),winerror=getattr(exc,'winerror',None),
            sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),sqlite_errorname=getattr(exc,'sqlite_errorname',None)))
        exc=exc.__cause__ or exc.__context__
    return chain



def retry_after(headers):
    value=headers.get('Retry-After') if headers else None
    if not value:return None
    try:
        if value.isdigit():return float(value)
        from email.utils import parsedate_to_datetime
        return max(0.,parsedate_to_datetime(value).timestamp()-time.time())
    except (ValueError,TypeError,OverflowError):return None


def endpoint_key(url):
    p=urlsplit(url)
    if p.scheme not in ('https','http') or not p.hostname:raise Unavailable('RPC_ORIGIN_INVALID')
    origin=p.scheme+'://'+p.hostname.lower()+':'+str(p.port or (443 if p.scheme=='https' else 80))
    return hashlib.sha256(origin.encode()).hexdigest()[:24]


def lock_slot(path):
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        if os.fstat(fd).st_size==0:os.write(fd,b'0')
        os.lseek(fd,0,os.SEEK_SET)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return fd
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):return None
        raise


def unlock_slot(fd):
    # Closing also releases the kernel lock, including on process termination.
    if fd is not None:os.close(fd)


SCHEMA='''CREATE TABLE IF NOT EXISTS outcome_receipts(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY,rate REAL,slots INTEGER);
CREATE TABLE IF NOT EXISTS endpoints(
 key TEXT PRIMARY KEY,next_mono REAL NOT NULL DEFAULT 0,cooldown_mono REAL NOT NULL DEFAULT 0,
 updated_mono REAL NOT NULL DEFAULT 0,started INTEGER NOT NULL DEFAULT 0,
 server_429 INTEGER NOT NULL DEFAULT 0,other_errors INTEGER NOT NULL DEFAULT 0,
 deferred INTEGER NOT NULL DEFAULT 0,total_wait_ms REAL NOT NULL DEFAULT 0,
 max_wait_ms REAL NOT NULL DEFAULT 0,streak_429 INTEGER NOT NULL DEFAULT 0);'''

class Budget:
    RECORD_WAIT_SECONDS=.25 # Control-state contention only; no HTTP retries or worker deadline increase.
    def __init__(self,root,rate=6.,slots=4,wait=.75):
        if not math.isfinite(rate) or not 0<rate<=1000 or type(slots)!=int or not 1<=slots<=8 or not math.isfinite(wait) or not 0<=wait<=3:
            raise Unavailable('RPC_LIMITS_INVALID')
        self.root=Path(root);self.rate=rate;self.slots=slots;self.wait=wait
        self.path=self.root/'control.db';self.failed=self.root/'control_failed.json';self._local=threading.local()
    def connect(self):
        self.root.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(self.path,timeout=.025,isolation_level=None);c.row_factory=sqlite3.Row
        try:
            c.execute('PRAGMA busy_timeout=25')
            # Warm path is read-only setup: no DDL, INSERT OR IGNORE or
            # repeated synchronous commits while holding metadata.lock.
            tables={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required={'settings','endpoints','outcome_receipts'}
            if not tables:
                c.execute('PRAGMA journal_mode=WAL');c.executescript(SCHEMA)
                c.execute('INSERT INTO settings VALUES(1,?,?)',(self.rate,self.slots))
            elif not required.issubset(tables):raise Unavailable('RPC_CONTROL_SCHEMA_INCOMPLETE')
            r=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
            if r is None:raise Unavailable('RPC_CONTROL_SETTINGS_MISSING')
            if r['rate']!=self.rate or r['slots']!=self.slots:raise Unavailable('RPC_LIMITS_DISAGREE_BETWEEN_PROCESSES')
            return c
        except BaseException:c.close();raise
    def metrics(self):
        if not hasattr(self._local,'metrics'):
            self._local.metrics=dict(metadata_hold_ms=0.,sqlite_commit_ms=0.,connections_opened=0,transactions=0,outcomes_drained=0)
        return self._local.metrics
    @contextmanager
    def session(self):
        # One connection per Budget/thread; never a shared cross-thread connection.
        # File identity prevents a cached handle from authorizing against a replaced DB.
        self._local.stage='SQLITE_CONNECT'
        c=getattr(self._local,'connection',None)
        key=(os.getpid(),self.rate,self.slots)
        if c is None or getattr(self._local,'connection_key',None)!=key:
            if c is not None:c.close()
            c=self.connect();st=self.path.stat()
            self._local.connection=c;self._local.connection_key=key
            self._local.identity=(st.st_dev,st.st_ino);self.metrics()['connections_opened']+=1
        else:
            try:st=self.path.stat()
            except FileNotFoundError:raise Unavailable('RPC_CONTROL_DB_REPLACED') from None
            if (st.st_dev,st.st_ino)!=self._local.identity:raise Unavailable('RPC_CONTROL_DB_REPLACED')
        try:yield c
        finally:
            if c.in_transaction:
                try:c.rollback()
                except sqlite3.Error as exc:
                    self.close();raise Unavailable('RPC_CONTROL_ROLLBACK_UNCONFIRMED') from exc
    def close(self):
        c=getattr(self._local,'connection',None)
        if c is not None:
            self._local.connection=None;c.close()
    def commit(self,c):
        self._local.stage='SQLITE_COMMIT';start=time.monotonic()
        c.execute('COMMIT')
        stats=self.metrics();stats['transactions']+=1;stats['sqlite_commit_ms']+=(time.monotonic()-start)*1000
    @contextmanager
    def control_lock(self):
        self.root.mkdir(parents=True,exist_ok=True)
        self._local.stage='METADATA_LOCK'
        fd=lock_slot(self.root/'metadata.lock')
        if fd is None:raise MetadataBusy('RPC_CONTROL_METADATA_BUSY: database is locked')
        start=time.monotonic()
        try:yield
        finally:
            self.metrics()['metadata_hold_ms']+=(time.monotonic()-start)*1000
            unlock_slot(fd)
    @staticmethod
    def busy(exc):
        code=getattr(exc,'sqlite_errorcode',None)
        if isinstance(code,int):return code & 255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED)
        return 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
    def block(self,exc,stage='RECORD_HTTP_OUTCOME'):
        self.root.mkdir(parents=True,exist_ok=True)
        # Permanent fail-closed marker: inspect the report and use a clean
        # installation for retest; do not silently discard lost cooldown state.
        with self.failed.open('w',encoding='utf-8') as f:
            json.dump(dict(status='CONTROL_STATE_UNCONFIRMED',stage=stage,error_type=type(exc).__name__,errno=getattr(exc,'errno',None),winerror=getattr(exc,'winerror',None),sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),ts=time.time(),failure_chain=failure_details(exc)),f)
    @contextmanager
    def rate_state(self,key):
        # Map before doing SQLite work. The final shared timestamp assignment
        # has no SQLite commit/fsync after it; kernel-shared bytes are visible
        # to other processes even if this process dies before its HTTP read.
        fd=os.open(self.root/('rate_'+key+'.bin'),os.O_CREAT|os.O_RDWR,0o600)
        try:
            if os.fstat(fd).st_size==0:os.ftruncate(fd,8)
            if os.fstat(fd).st_size!=8:raise Unavailable('RPC_RATE_STATE_INVALID')
            with mmap.mmap(fd,8) as state:yield state
        finally:os.close(fd)
    def reserve(self,key,priority=False):
        with self.control_lock():
            self._drain_locked()
            try:exit_waiting=__import__('rpc_priority').pending(self.root)
            except (ValueError,OSError) as exc:raise Unavailable('RPC_EXIT_PRIORITY_UNCONFIRMED') from exc
            if exit_waiting and not priority:return ('EXIT_PRIORITY',.025)
            with self.rate_state(key) as state:
                last=struct.unpack('d',state[:])[0];now=time.monotonic()
                if not math.isfinite(last) or last<0:raise Unavailable('RPC_RATE_STATE_INVALID')
                if last>now+1:last=0. # Different monotonic epoch after host reboot.
                remaining=last+1/self.rate-now if last else 0.
                if remaining>0:return ('RATE',remaining)
                waiting=self._reserve_once(key)
                if waiting is None:state[:]=struct.pack('d',time.monotonic())
                return waiting
    def _reserve_once(self,key):
        with self.session() as c:
            self._local.stage='SQLITE_BEGIN';c.execute('BEGIN IMMEDIATE');c.execute('INSERT OR IGNORE INTO endpoints(key) VALUES(?)',(key,))
            r=c.execute('SELECT * FROM endpoints WHERE key=?',(key,)).fetchone()
            now=time.monotonic()
            if now+1<r['updated_mono']:
                c.execute('UPDATE endpoints SET next_mono=0,cooldown_mono=0 WHERE key=?',(key,))
                r=c.execute('SELECT * FROM endpoints WHERE key=?',(key,)).fetchone()
            due=max(r['next_mono'],r['cooldown_mono'])
            if due>now:
                self.commit(c);return ('COOLDOWN' if r['cooldown_mono']>now else 'RATE',due-now)
            c.execute('UPDATE endpoints SET next_mono=?,updated_mono=?,started=started+1 WHERE key=?',(now+1/self.rate,now,key))
            self.commit(c);return None
    def _pending(self):
        return sorted(p for p in (self.root/'outcomes').glob('*') if p.suffix in ('.json','.tmp'))
    def _read_outcomes(self):
        self._local.stage='OUTCOME_READ'
        pending=self._pending()
        if len(pending)>32:raise Unavailable('RPC_OUTCOME_BACKLOG_EXCEEDED')
        events=[]
        known=set()
        if pending:
            with self.session() as c:known={r[0] for r in c.execute('SELECT id FROM outcome_receipts LIMIT 33')}
            if len(known)>32:raise Unavailable('RPC_OUTCOME_RECEIPT_BACKLOG')
        for path in pending:
            if path.suffix=='.json' and path.stem in known:continue # Committed receipt: no reopen/delete sharing race with its recorder.
            if path.suffix=='.tmp':
                self._local.stage='OUTCOME_PUBLICATION'
                raise MetadataBusy('RPC_OUTCOME_PUBLICATION_BUSY: database is locked')
            try:
                if path.stat().st_size>8192:raise ValueError('oversize')
                event=json.loads(path.read_text(encoding='utf-8'))
                if event['schema']!='RPC_OUTCOME_V892' or event['id']!=path.stem:raise ValueError('schema')
                if len(event['key'])!=24 or any(c not in '0123456789abcdef' for c in event['key']):raise ValueError('key')
                if event['kind'] not in ('OK','ERROR','429'):raise ValueError('kind')
                for field in ('wait_ms','observed_mono','observed_wall'):
                    if type(event[field]) not in (int,float) or not math.isfinite(event[field]) or event[field]<0:raise ValueError(field)
                delay=event['retry_after_s']
                if delay is not None and (type(delay) not in (int,float) or not math.isfinite(delay) or delay<0):raise ValueError('delay')
            except FileNotFoundError:continue # A previous recorder removed an already-committed receipt after releasing metadata.lock.
            except (ValueError,KeyError,TypeError) as failure:raise Unavailable('RPC_OUTCOME_INVALID') from failure
            events.append(event)
        return pending,events
    def _unlink_outcomes(self,paths):
        for path in paths:
            try:path.unlink()
            except FileNotFoundError:pass # Another admission may already have replayed the receipt and removed it.
    def _drain_locked(self,unlink=True):
        pending,events=self._read_outcomes()
        if not pending:return []
        # One atomic commit for the whole validated journal, not two commits per
        # HTTP outcome. Receipts remain until the next batch can prove the file absent.
        with self.session() as c:
            self._local.stage='SQLITE_BEGIN';c.execute('BEGIN IMMEDIATE')
            live={path.stem for path in pending}
            for row in c.execute('SELECT id FROM outcome_receipts').fetchall():
                if row[0] not in live:c.execute('DELETE FROM outcome_receipts WHERE id=?',(row[0],))
            self._local.stage='OUTCOME_APPLY'
            for event in events:
                error=None
                if event['kind']!='OK':
                    error=RuntimeError('RECORDED_HTTP_OUTCOME');error.http_status=429 if event['kind']=='429' else None
                    error.retry_after_s=event['retry_after_s']
                self._apply_record(c,event['key'],error,event['wait_ms'],event=event)
            self.commit(c)
        self.metrics()['outcomes_drained']+=len(events)
        if unlink:self._unlink_outcomes(pending)
        return pending
    def record(self,key,exc,wait_ms,deferred=False):
        event=None
        if not deferred:
            directory=self.root/'outcomes';directory.mkdir(parents=True,exist_ok=True)
            code=getattr(exc,'code',None) or getattr(exc,'http_status',None)
            delay=retry_after(getattr(exc,'headers',None))
            if delay is None:delay=getattr(exc,'retry_after_s',None)
            if delay is not None and (not isinstance(delay,(int,float)) or not math.isfinite(delay) or delay<0):delay=30.
            event=dict(schema='RPC_OUTCOME_V892',id=uuid.uuid4().hex,key=key,
                kind='429' if code==429 else 'ERROR' if exc is not None else 'OK',
                retry_after_s=delay,wait_ms=wait_ms,observed_mono=time.monotonic(),observed_wall=time.time())
            path=directory/(event['id']+'.json');temporary=path.with_suffix('.tmp')
            with temporary.open('x',encoding='utf-8') as f:
                json.dump(event,f);f.flush();os.fsync(f.fileno())
            os.replace(temporary,path)
        deadline=time.monotonic()+self.RECORD_WAIT_SECONDS
        while True:
            try:
                with self.control_lock():
                    paths=self._drain_locked(unlink=False)
                    if deferred:self._record_once(key,exc,wait_ms,True)
                self._unlink_outcomes(paths)
                return
            except sqlite3.OperationalError as failure:
                remaining=deadline-time.monotonic()
                if not self.busy(failure):raise
                if remaining<=0:
                    # Actual HTTP outcomes are durable. Future admissions must
                    # drain them under the same metadata lock before starting.
                    if event is not None:return
                    raise
                time.sleep(min(.005,remaining))
    def _record_once(self,key,exc,wait_ms,deferred=False,event=None):
        with self.session() as c:
            self._local.stage='SQLITE_BEGIN';c.execute('BEGIN IMMEDIATE')
            self._apply_record(c,key,exc,wait_ms,deferred,event)
            self.commit(c)
    def _apply_record(self,c,key,exc,wait_ms,deferred=False,event=None):
        code=getattr(exc,'code',None) or getattr(exc,'http_status',None)
        if event is not None:
            if c.execute('INSERT OR IGNORE INTO outcome_receipts VALUES(?)',(event['id'],)).rowcount==0:
                return
        c.execute('INSERT OR IGNORE INTO endpoints(key) VALUES(?)',(key,))
        now=time.monotonic()
        c.execute('UPDATE endpoints SET total_wait_ms=total_wait_ms+?,max_wait_ms=MAX(max_wait_ms,?),deferred=deferred+?,updated_mono=? WHERE key=?',(wait_ms,wait_ms,int(deferred),now,key))
        if not deferred and code==429:
            r=c.execute('SELECT streak_429 FROM endpoints WHERE key=?',(key,)).fetchone();streak=r[0]+1
            delay=retry_after(getattr(exc,'headers',None))
            if delay is None:delay=getattr(exc,'retry_after_s',None)
            if delay is None:delay=min(30.,2**min(streak-1,5))
            if not isinstance(delay,(int,float)) or not math.isfinite(delay) or delay<0:delay=30.
            c.execute('UPDATE endpoints SET cooldown_mono=MAX(cooldown_mono,?),server_429=server_429+1,streak_429=? WHERE key=?',(max(0., (event['observed_mono'] if event and now+1>=event['observed_mono'] else now)+delay),streak,key))
        elif not deferred and exc is not None:c.execute('UPDATE endpoints SET other_errors=other_errors+1 WHERE key=?',(key,))
        elif not deferred:c.execute('UPDATE endpoints SET streak_429=0 WHERE key=?',(key,))
    def request(self,url,priority=False):return Permit(self,endpoint_key(url),priority)
    def snapshot(self):
        result=dict(schema='RPC_BUDGET_V89',scope='ONE_INSTALLATION_PAPER_RPC_READERS',status='BLOCKED' if self.failed.exists() else 'PENDING' if self._pending() else 'NOT_STARTED',rate_per_origin=self.rate,max_inflight=self.slots,endpoints=[])
        if self.failed.exists():
            try:result['failure']=json.loads(self.failed.read_text(encoding='utf-8'))
            except (OSError,ValueError):result['failure']={'status':'UNREADABLE'}
        try:result['exit_priority_pending_v94']=__import__('rpc_priority').pending(self.root)
        except (ValueError,OSError) as exc:result['exit_priority_error_v94']=type(exc).__name__
        result['pending_outcomes']=len(self._pending())
        if not self.path.exists():return result
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=.025)) as c:
                c.row_factory=sqlite3.Row;rows=list(c.execute('SELECT * FROM endpoints'))
            now=time.monotonic()
            for r in rows:
                result['endpoints'].append(dict(endpoint_hash=r['key'],started=r['started'],server_429=r['server_429'],other_errors=r['other_errors'],local_deferred=r['deferred'],total_wait_ms=r['total_wait_ms'],max_wait_ms=r['max_wait_ms'],cooldown_remaining_s=max(0,r['cooldown_mono']-now)))
            active=0
            for i in range(self.slots):
                fd=lock_slot(self.root/('slot_'+str(i)+'.lock'))
                if fd is None:active+=1
                else:unlock_slot(fd)
            result.update(status='BLOCKED' if self.failed.exists() else 'PENDING' if self._pending() else 'READY',inflight=active,pending_outcomes=len(self._pending()))
            if self.failed.exists():result['failure']=json.loads(self.failed.read_text(encoding='utf-8'))
        except (OSError,sqlite3.Error,ValueError) as exc:result.update(status='UNAVAILABLE',error_type=type(exc).__name__)
        return result

class Permit:
    def __init__(self,budget,key,priority=False):self.budget=budget;self.key=key;self.fd=None;self.wait_ms=0.;self.priority=priority
    def __enter__(self):
        start=time.monotonic();deadline=start+self.budget.wait;reason='CAPACITY';delay=.01;contention=None
        self.budget._local.metrics=dict(metadata_hold_ms=0.,sqlite_commit_ms=0.,connections_opened=0,transactions=0,outcomes_drained=0)
        while True:
            if self.budget.failed.exists():raise Unavailable('RPC_CONTROL_BLOCKED')
            try:
                self.budget.root.mkdir(parents=True,exist_ok=True)
                for i in range(self.budget.slots):
                    fd=lock_slot(self.budget.root/('slot_'+str(i)+'.lock'))
                    if fd is not None:self.fd=fd;break
                if self.fd is not None:
                    try:waiting=self.budget.reserve(self.key,priority=True) if self.priority else self.budget.reserve(self.key)
                    except BaseException:unlock_slot(self.fd);self.fd=None;raise
                    if waiting is None:
                        self.wait_ms=(time.monotonic()-start)*1000;return self
                    reason,delay=waiting;unlock_slot(self.fd);self.fd=None
                else:reason,delay='CAPACITY',.01
            except sqlite3.OperationalError as exc:
                if not self.budget.busy(exc):raise Unavailable('RPC_CONTROL_DB_ERROR') from exc
                reason,delay='STATE_LOCKED',.025
                contention=dict(stage=getattr(self.budget._local,'stage','UNKNOWN'),error_type=type(exc).__name__,sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),sqlite_errorname=getattr(exc,'sqlite_errorname',None))
            except Unavailable as exc:
                self.budget.block(exc,stage='DRAIN_BEFORE_ADMISSION');raise
            except sqlite3.Error as exc:raise Unavailable('RPC_CONTROL_DB_ERROR') from exc
            except OSError as exc:raise Unavailable('RPC_CONTROL_IO_ERROR') from exc
            remaining=deadline-time.monotonic()
            if remaining<=0 or delay>remaining:
                self.wait_ms=(time.monotonic()-start)*1000
                exc=Deferred(reason,delay,self.wait_ms)
                if contention:exc.control_v95=contention
                try:self.budget.record(self.key,exc,self.wait_ms,deferred=True)
                except (OSError,sqlite3.Error,Unavailable):pass # Admission was denied; no HTTP request was made.
                raise exc
            time.sleep(min(.025,delay,remaining))
    def __exit__(self,typ,exc,tb):
        try:
            try:self.budget.record(self.key,exc,self.wait_ms)
            except Exception as failure:
                self.budget.block(failure)
                if exc is None:raise Unavailable('RPC_CONTROL_RESULT_UNCONFIRMED') from failure
                setattr(exc,'rpc_control_persist_error',type(failure).__name__)
        finally:unlock_slot(self.fd);self.fd=None
        return False
    def telemetry(self):return dict(scope='GLOBAL_PAPER_RPC_ADMISSION',wait_ms=self.wait_ms,endpoint_hash=self.key,control_v95=self.budget.metrics())


_DEFAULT=None
_DEFAULT_KEY=None
def budget():
    global _DEFAULT,_DEFAULT_KEY
    import config
    key=(os.getpid(),str(Path(config.RPC_CONTROL_DIR).resolve()),config.RPC_MAX_REQUESTS_PER_SECOND,config.RPC_MAX_INFLIGHT,config.RPC_ADMISSION_WAIT_SECONDS)
    if _DEFAULT_KEY!=key:
        if _DEFAULT is not None:_DEFAULT.close()
        _DEFAULT=Budget(Path(key[1]),key[2],key[3],key[4]);_DEFAULT_KEY=key
    return _DEFAULT
def guard(url):return budget().request(url,priority=__import__('rpc_priority').IS_EXIT.get())
def snapshot():return budget().snapshot()
