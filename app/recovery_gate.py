"""Session/epoch admission barrier; never changes clocks, prices or balances."""
import asyncio,json,sqlite3,time,uuid,math
from pathlib import Path
from contextlib import contextmanager,closing
ACTIVE=None
LOOP_GAP_S=3.0 # existing runtime probe / monitor loop-gap diagnostic boundary

def primary_path(database):
    if database is None:database=__import__('config').DB
    p=Path(database).resolve();stem=p.stem
    if stem.endswith('_monitor'):stem=stem[:-8]
    for suffix in ('_amm_paper','_route_paper'):
        if stem.endswith(suffix):stem=stem[:-len(suffix)];break
    return p.with_name(stem+'.db')

def path(database):
    p=primary_path(database);return p.with_name(p.stem+'_recovery_v75.json')

def read(database):
    p=path(database)
    def failed(reason,exc=None,field=None):
        error=dict(reason=reason,path=str(p),observed_ts=time.time(),observed_mono=time.monotonic(),pid=__import__('os').getpid())
        if exc is not None:
            error.update(error_type=type(exc).__name__,errno=getattr(exc,'errno',None),winerror=getattr(exc,'winerror',None),strerror=getattr(exc,'strerror',None))
            # Do not log file contents or arbitrary exception payloads.
            if isinstance(exc,json.JSONDecodeError):error.update(line=exc.lineno,column=exc.colno)
        if field:error['field']=field
        return dict(state='RECOVERING',epoch=None,session=None,reason=reason,read_error_v77=error)
    try:
        s=json.loads(p.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:return failed('RECOVERY_STATE_MISSING',exc)
    except PermissionError as exc:return failed('RECOVERY_STATE_PERMISSION_DENIED',exc)
    except OSError as exc:return failed('RECOVERY_STATE_IO_ERROR',exc)
    except json.JSONDecodeError as exc:return failed('RECOVERY_STATE_INVALID_JSON',exc)
    except UnicodeError as exc:return failed('RECOVERY_STATE_INVALID_ENCODING',exc)
    if not isinstance(s,dict) or s.get('schema')!='RECOVERY_GATE_V75':return failed('RECOVERY_STATE_INVALID_SCHEMA',field='schema')
    for field in ('epoch','session'):
        if not isinstance(s.get(field),str) or not s[field]:return failed('RECOVERY_STATE_INVALID_FIELDS',field=field)
    if s.get('state') not in ('READY','RECOVERING','STOPPED'):return failed('RECOVERY_STATE_INVALID_FIELDS',field='state')
    pulse=s.get('pulse_mono')
    if type(pulse) not in (int,float) or not math.isfinite(pulse):return failed('RECOVERY_STATE_INVALID_FIELDS',field='pulse_mono')
    return s

def pair():
    import clock_lab
    w,m,e=clock_lab.pair();pair.uncertainty=e;return w,m

def request_path(database):
    p=primary_path(database);return p.with_name(p.stem+'_recovery_request_v75.json')


class Coordinator:
    def __init__(self,database,session,clock=pair):
        self.database=database;self.session=session;self.clock=clock;self.previous=clock();self.previous_error=getattr(clock,'uncertainty',0);self.generation=0;self.check_token=None
        self.data=dict(schema='RECOVERY_GATE_V75',session=session,epoch=uuid.uuid4().hex,state='READY',reason='STARTUP_TIME_CHECK',generation=0,events=[],write_error=None)
        self.connected_epoch=None;self.tick_mono=None;self.time_pass=True;self.next_check=0.;self.save()
    def save(self):
        try:
            self.data.update(write_error=None,pulse_mono=self.clock()[1],time_pass=self.time_pass,connection_epoch=self.connected_epoch,market_mono=self.tick_mono)
            p=path(self.database);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data,allow_nan=False),encoding='utf-8');tmp.replace(p);self.data['write_error']=None
        except (OSError,ValueError) as e:self.data['write_error']=type(e).__name__
    def event(self,kind,**fields):
        self.data['events']=(self.data['events']+[dict(kind=kind,ts=self.clock()[0],**fields)])[-32:];self.save()
    def observe(self):
        w,m=self.clock();pw,pm=self.previous;self.previous=(w,m)
        import clock_lab
        gap=m-pm;step=(w-pw)-gap
        error=getattr(self.clock,'uncertainty',0);limit=clock_lab.JUMP_MS+1000*(error+self.previous_error);self.previous_error=error
        if gap>LOOP_GAP_S or abs(step)*1000>limit or gap<0:
            self.generation+=1;self.data.update(state='RECOVERING',epoch=uuid.uuid4().hex,generation=self.generation,reason='LOOP_GAP' if gap>LOOP_GAP_S else 'CLOCK_DISCONTINUITY',barrier_ts=w,barrier_mono=m)
            self.connected_epoch=None;self.tick_mono=None;self.time_pass=False;self.next_check=0.;self.data.pop('time_check',None)
            self.event('RECOVERY_BEGIN',gap_s=gap,wall_step_ms=step*1000,epoch=self.data['epoch'])
        return self.data
    def status(self):
        self.observe();return self.data
    def connection(self):
        self.observe();self.connected_epoch=self.data['epoch'];self.event('NEW_CONNECTION',epoch=self.connected_epoch)
    def market(self,e):
        self.observe()
        lag=e.get('provider_lag_ms');mono=e.get('received_mono')
        if self.connected_epoch==self.data['epoch'] and e.get('source')=='pumpapi_free' and e.get('pool')=='pump' and e.get('quote_mint')=='So11111111111111111111111111111111111111112' and e.get('price_basis')=='provider_price' and bool(e.get('signature')) and e.get('clock_status')=='OK' and type(lag) in (int,float) and 0<=lag<=1000 and type(mono) in (int,float) and 0<=self.clock()[1]-mono<=1:
            self.tick_mono=mono;self.release()
    def release(self):
        self.observe()
        if self.data['state']=='RECOVERING' and self.time_pass and self.connected_epoch==self.data['epoch'] and self.tick_mono is not None and 0<=self.clock()[1]-self.tick_mono<=1:
            self.data.update(state='READY',reason='FRESH_CONNECTION_MARKET_AND_TIME');self.event('RECOVERY_READY',epoch=self.data['epoch'])
    def remote_request(self):
        try:r=json.loads(request_path(self.database).read_text())
        except (OSError,ValueError):return
        if r.get('epoch')==self.data['epoch']:
            # A client paused while primary was running; request the same barrier.
            self.generation+=1;w,m=self.clock()
            self.data.update(state='RECOVERING',epoch=uuid.uuid4().hex,generation=self.generation,reason='CLIENT_PAUSE',barrier_ts=w,barrier_mono=m)
            self.connected_epoch=None;self.tick_mono=None;self.time_pass=False;self.next_check=0.;self.data.pop('time_check',None)
            self.event('RECOVERY_BEGIN',source='CLIENT_PAUSE',epoch=self.data['epoch'])
    def time_result(self,epoch,r):
        self.observe()
        if epoch!=self.data['epoch']:return False
        created=r.get('created_ts')
        self.time_pass=r.get('reference',{}).get('status')=='PASS' and type(created) in (int,float) and math.isfinite(created) and created>=self.data.get('barrier_ts',0)
        self.data['time_check']=dict(id=r.get('id'),status=r.get('reference',{}).get('status'),epoch=epoch)
        self.event('TIME_RECHECK',status=self.data['time_check']['status'],epoch=epoch);self.release();return self.time_pass
    async def run(self):
        import time_check,clock_lab
        probe=None;probe_epoch=None;published=0.
        try:
            while True:
                self.observe();self.remote_request()
                if self.clock()[1]-published>=1:self.save();published=self.clock()[1]
                if probe is not None and probe.done():
                    try:
                        r=probe.result();await asyncio.to_thread(time_check.save,r);self.time_result(probe_epoch,r)
                    except Exception as e:self.event('TIME_RECHECK_ERROR',error_type=type(e).__name__)
                    probe=None
                if self.data['state']=='RECOVERING' and not self.time_pass and probe is None and self.clock()[1]>=self.next_check:
                    probe_epoch=self.data['epoch'];self.next_check=self.clock()[1]+clock_lab.INTERVAL
                    probe=asyncio.create_task(time_check.collect())
                await asyncio.sleep(.1)
        finally:
            if probe is not None:
                probe.cancel();await asyncio.gather(probe,return_exceptions=True)
    def close(self):
        self.data.update(state='STOPPED',reason='PRIMARY_STOP');self.event('RECOVERY_STOP')

class Client:
    def __init__(self,database,clock=pair):
        self.database=database;self.clock=clock;self.previous=clock();self.previous_error=getattr(clock,'uncertainty',0)
        s=read(database);self.epoch=s.get('epoch');self.await_epoch=None;self.last_valid=None
        if not s.get('read_error_v77'):self.remember(s)
    def remember(self,s):
        self.last_valid={k:s.get(k) for k in ('session','epoch','generation','state','pulse_mono')}
    def status(self):
        import clock_lab
        w,m=self.clock();pw,pm=self.previous;self.previous=(w,m)
        error=getattr(self.clock,'uncertainty',0);limit=clock_lab.JUMP_MS+1000*(error+self.previous_error);self.previous_error=error
        if m-pm>LOOP_GAP_S or m<pm or abs((w-pw)-(m-pm))*1000>limit:self.await_epoch=self.epoch
        s=read(self.database)
        if s.get('read_error_v77'):
            # Last-known identifiers are diagnostic only, never an authorization.
            return dict(s,last_valid_state_v77=dict(self.last_valid) if self.last_valid else None,
                        waiting_for_epoch_v77=self.await_epoch)
        self.remember(s)
        if self.await_epoch is not None:
            if s.get('epoch')!=self.await_epoch and s.get('state')=='READY':self.await_epoch=None
            else:
                if s.get('epoch')==self.await_epoch:
                    try:
                        p=request_path(self.database);tmp=p.with_suffix('.'+str(__import__('os').getpid())+'.tmp')
                        tmp.write_text(json.dumps(dict(epoch=self.await_epoch)),encoding='utf-8');tmp.replace(p)
                    except OSError:pass
                return dict(s,state='RECOVERING',reason='LOCAL_PAUSE_WAITING_PRIMARY_EPOCH')
        self.epoch=s.get('epoch')
        pulse=s.get('pulse_mono')
        age=m-pulse
        s=dict(s,primary_state_v77=s.get('state'),pulse_mono_v77=pulse,observed_mono_v77=m)
        if age<0:return dict(s,state='RECOVERING',reason='PRIMARY_GATE_PULSE_IN_FUTURE',pulse_age_s_v77=age)
        if age>LOOP_GAP_S:return dict(s,state='RECOVERING',reason='PRIMARY_GATE_PULSE_STALE',pulse_age_s_v77=age)
        return dict(s,pulse_age_s_v77=age)

def status():return ACTIVE.status() if ACTIVE is not None else None
def admission(data=None):
    """One observation for both the decision and its diagnostic explanation."""
    s=status()
    if s is None:return dict(allowed=True,reason='GATE_NOT_ACTIVE')
    d=dict(allowed=False,state=s.get('state'),session=s.get('session'),epoch=s.get('epoch'),generation=s.get('generation'),
           gate_reason=s.get('reason'),write_error=s.get('write_error'),job_epoch=data.get('recovery_epoch_v75') if data is not None else None,
           job_session=data.get('recovery_session_v77') if data is not None else None,
           read_error_v77=s.get('read_error_v77'),last_valid_state_v77=s.get('last_valid_state_v77'),pulse_age_s_v77=s.get('pulse_age_s_v77'),
           primary_state_v77=s.get('primary_state_v77'),pulse_mono_v77=s.get('pulse_mono_v77'),observed_mono_v77=s.get('observed_mono_v77'))
    if s.get('write_error'):d['reason']='STATE_WRITE_FAILED'
    elif s.get('state')!='READY':d['reason']=s.get('reason') or 'GATE_NOT_READY'
    elif data is not None and data.get('recovery_epoch_v75')!=s.get('epoch'):d['reason']='EPOCH_MISMATCH'
    elif data is not None and data.get('recovery_session_v77')!=s.get('session'):d['reason']='SESSION_MISMATCH'
    elif data is not None and data.get('recovery_authorized_v75') is not True:d['reason']='JOB_NOT_AUTHORIZED'
    else:d.update(allowed=True,reason='READY')
    return d
def allowed(data=None):return admission(data)['allowed']
def stamp(data):
    s=status()
    if s is not None:data.update(recovery_epoch_v75=s.get('epoch'),recovery_session_v77=s.get('session'),recovery_authorized_v75=s.get('state')=='READY' and not s.get('write_error'))
    return data

def connection():
    if isinstance(ACTIVE,Coordinator):ACTIVE.connection()
def market(e):
    if isinstance(ACTIVE,Coordinator):ACTIVE.market(e)

def required(database):
    if path(database).exists():return True
    try:
        with closing(sqlite3.connect(primary_path(database).as_uri()+'?mode=ro',uri=True)) as c:
            r=c.execute("SELECT v FROM meta WHERE k='recovery_required_v75'").fetchone();return bool(r and r[0]=='1')
    except sqlite3.Error:return False

@contextmanager
def reader(database):
    global ACTIVE
    previous=ACTIVE
    # The monitor can start before primary writes its DB marker/state file.
    # Installing the client must not depend on evidence that appears later.
    ACTIVE=Client(database)
    try:yield
    finally:ACTIVE=previous
