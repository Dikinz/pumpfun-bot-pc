"""Observational PumpApi receive diagnostics; no queue consumption or trade decisions."""
import json, math, time, uuid
from collections import deque
KEY='stream_diagnostics_v52_2'
NOTE=('Buffer samples are optional library internals: frames for asyncio, messages for legacy. '
      'Not kernel/TCP occupancy or message residence time. recv wait is not network latency. '
      'Provider lag uses unverified timestamps; network and upstream delay cannot be separated here. '
      'No extra socket, RPC, dropped messages or changes to trading filters. Samples every 5 s, last 120 retained.')

def buffer_state(ws):
    try:
        assembler=getattr(ws,'recv_messages',None)
        if assembler is not None and hasattr(assembler,'frames'):
            return dict(unit='frames',depth=len(assembler.frames),paused=getattr(assembler,'paused',None))
        messages=getattr(ws,'messages',None)
        if messages is not None:return dict(unit='messages',depth=len(messages),paused=None)
    except Exception:pass
    return dict(unit='unavailable',depth=None,paused=None)

class Probe:
    def __init__(self,write,clock=time.monotonic,wall=time.time):
        self.write=write;self.clock=clock;self.wall=wall;self.session=uuid.uuid4().hex
        self.started=wall();self.last_flush=clock();self.samples=deque(maxlen=120)
        self.total=0;self.connections=0;self.errors=0;self.write_errors=0;self.reset()
        self.flush(force=True)
    def reset(self):
        self.window=dict(messages=0,handler_sum_ms=0.,handler_max_ms=0.,recv_sum_ms=0.,recv_max_ms=0.,
            buffer_samples=0,buffer_nonempty=0,buffer_max=None,buffer_unit='unavailable',paused_samples=0,
            provider_samples=0,provider_over_1000=0,provider_future=0,provider_lag_sum_ms=0.,provider_lag_max_ms=None)
    def connected(self):self.connections+=1
    def error(self):self.errors+=1
    def received(self,ws,recv_started):
        now=self.clock();w=self.window;wait=max(0.,(now-recv_started)*1000)
        w['messages']+=1;self.total+=1;w['recv_sum_ms']+=wait;w['recv_max_ms']=max(w['recv_max_ms'],wait)
        self.observe_buffer(ws)
        return now
    def observe_buffer(self,ws):
        b=buffer_state(ws);w=self.window
        if b['depth'] is not None:
            w['buffer_samples']+=1;w['buffer_nonempty']+=b['depth']>0
            w['buffer_max']=max(w['buffer_max'] or 0,b['depth']);w['buffer_unit']=b['unit']
            w['paused_samples']+=b['paused'] is True
    def provider(self,x,received_wall):
        if str(x.get('action') or x.get('txType') or '').lower() not in ('buy','sell'):return
        stamp=x.get('timestamp')
        try:stamp=float(stamp)
        except (TypeError,ValueError,OverflowError):return
        if not math.isfinite(stamp) or not 1e12<=stamp<=1e14:return
        lag=received_wall*1000-stamp;w=self.window;w['provider_samples']+=1
        w['provider_over_1000']+=lag>1000;w['provider_future']+=lag<0
        w['provider_lag_sum_ms']+=lag
        w['provider_lag_max_ms']=max(w['provider_lag_max_ms'] if w['provider_lag_max_ms'] is not None else lag,lag)
    def finished(self,ws,started):
        elapsed=max(0.,(self.clock()-started)*1000);w=self.window
        w['handler_sum_ms']+=elapsed;w['handler_max_ms']=max(w['handler_max_ms'],elapsed)
        self.observe_buffer(ws);self.flush()
    def flush(self,force=False):
        now=self.clock()
        if not force and now-self.last_flush<5:return
        w=dict(self.window);n=w['messages'];pn=w['provider_samples']
        w.update(ts=self.wall(),interval_s=now-self.last_flush,
            handler_mean_ms=w['handler_sum_ms']/n if n else None,
            recv_mean_ms=w['recv_sum_ms']/n if n else None,
            provider_lag_mean_ms=w['provider_lag_sum_ms']/pn if pn else None)
        self.samples.append(w);self.last_flush=now;self.reset()
        payload=json.dumps(dict(schema='STREAM_DIAGNOSTICS_V1',session=self.session,started_ts=self.started,
            updated_ts=self.wall(),messages=self.total,connections=self.connections,receive_errors=self.errors,write_errors=self.write_errors,
            windows=list(self.samples),note=NOTE),allow_nan=False)
        try:self.write(KEY,payload)
        except Exception:self.write_errors+=1

def summary(c):
    row=c.execute('SELECT v FROM meta WHERE k=?',(KEY,)).fetchone()
    if not row:return dict(status='NO_DATA',note=NOTE)
    try:
        data=json.loads(row[0]);data['age_s']=max(0.,time.time()-data['updated_ts']);data['status']='RECORDED'
        return data
    except (ValueError,TypeError,KeyError):return dict(status='INVALID_DATA',note=NOTE)
