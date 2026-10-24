"""Read-only clock reference and counterfactual event eligibility. No order API."""
import asyncio,socket,struct,time,math,json,uuid,hashlib
from collections import OrderedDict,deque
HOST='time.cloudflare.com'
INTERVAL=120
TOLERANCE_MS=100.0
JUMP_MS=20.0
EPOCH=2208988800
STATES=OrderedDict()
REFERENCE_UNCERTAINTY_LIMIT_MS=50.0
import candidate_lab
SCHEMA=candidate_lab.SCHEMA+'''
CREATE TABLE IF NOT EXISTS clock_config_v0740(id INTEGER PRIMARY KEY CHECK(id=1),created_ts REAL,payload TEXT,sha256 TEXT);
CREATE TABLE IF NOT EXISTS clock_reference_v0740(id INTEGER PRIMARY KEY,ts REAL,status TEXT,details_json TEXT);
CREATE TABLE IF NOT EXISTS local_clock_v0740(id INTEGER PRIMARY KEY,ts REAL,session TEXT,elapsed_s REAL,interval_s REAL,step_ms REAL,cumulative_ms REAL,pair_uncertainty_ms REAL,jump INTEGER);
CREATE TABLE IF NOT EXISTS clock_shadow_v0740(variant TEXT,reason TEXT,n INTEGER,PRIMARY KEY(variant,reason));
'''

def freeze(c):
    data=json.dumps(dict(host=HOST,interval_s=INTERVAL,shadow_tolerance_ms=TOLERANCE_MS,jump_threshold_ms=JUMP_MS,probe_jump_limit_ms=5,active_tolerance_ms=0,orders_enabled=False,reference_uncertainty_limit_ms=REFERENCE_UNCERTAINTY_LIMIT_MS,candidate_parameters=candidate_lab.parameters()),sort_keys=True)
    old=c.execute('SELECT payload FROM clock_config_v0740 WHERE id=1').fetchone()
    if old and old[0]!=data:raise RuntimeError('Clock lab parameters changed; use a new database')
    c.execute('INSERT OR IGNORE INTO clock_config_v0740 VALUES(1,?,?,?)',(time.time(),data,hashlib.sha256(data.encode()).hexdigest()))

def pair():
    a=time.monotonic();w=time.time();b=time.monotonic()
    return w,(a+b)/2,(b-a)/2

def stamp(t):
    n=t+EPOCH;sec=int(n)
    return struct.pack('!II',sec%(2**32),int((n-sec)*2**32))

def decode(b,near):
    sec,frac=struct.unpack('!II',b);base=sec+frac/2**32-EPOCH
    return base+round((near-base)/2**32)*2**32

def parse(packet,origin,t1,t4,m1,m4,pair_error=0):
    if len(packet)<48:raise ValueError('SHORT_PACKET')
    li=packet[0]>>6;version=(packet[0]>>3)&7;mode=packet[0]&7
    if mode!=4 or version not in (3,4):raise ValueError('INVALID_MODE_VERSION')
    if packet[24:32]!=origin:raise ValueError('ORIGIN_MISMATCH')
    if packet[1]==0:return dict(status='SERVER_REFUSAL',retry_after_s=900,stop_requests=packet[12:16]!=b'RATE')
    if li==3 or not 1<=packet[1]<=15:raise ValueError('SERVER_UNSYNCHRONIZED')
    if packet[32:40]==b'\0'*8 or packet[40:48]==b'\0'*8:raise ValueError('MISSING_SERVER_TIMESTAMP')
    t2=decode(packet[32:40],t1);t3=decode(packet[40:48],t1)
    elapsed=m4-m1;step=(t4-t1)-elapsed
    if elapsed<0 or t3<t2:raise ValueError('INVALID_TIMELINE')
    if abs(step)>0.005:return dict(status='LOCAL_CLOCK_CHANGED',wall_step_ms=step*1000)
    delay=elapsed-(t3-t2)
    if delay < -0.001:raise ValueError('NEGATIVE_NETWORK_DELAY')
    root_delay=struct.unpack('!i',packet[4:8])[0]/65536
    root_disp=struct.unpack('!I',packet[8:12])[0]/65536
    precision=struct.unpack('!b',packet[3:4])[0]
    if not -40<=precision<=0:raise ValueError('INVALID_PRECISION')
    uncertainty=max(0,delay)/2+abs(root_delay)/2+root_disp+2**precision+pair_error+abs(step)
    offset=((t2-t1)+(t3-t4))/2
    return dict(status='MEASURED' if uncertainty*1000<=REFERENCE_UNCERTAINTY_LIMIT_MS else 'TOO_IMPRECISE',offset_ms=offset*1000,uncertainty_ms=uncertainty*1000,
      offset_low_ms=(offset-uncertainty)*1000,offset_high_ms=(offset+uncertainty)*1000,rtt_ms=elapsed*1000,
      network_delay_ms=delay*1000,root_delay_ms=root_delay*1000,root_dispersion_ms=root_disp*1000,
      wall_step_ms=step*1000,stratum=packet[1],t1=t1,t2=t2,t3=t3,t4=t4)

async def probe():
    loop=asyncio.get_running_loop();sock=None
    try:
        infos=await asyncio.wait_for(loop.getaddrinfo(HOST,123,type=socket.SOCK_DGRAM),5)
        family,kind,proto,_,addr=infos[0]
        sock=socket.socket(family,kind,proto);sock.setblocking(False)
        await asyncio.wait_for(loop.sock_connect(sock,addr),3)
        t1,m1,err1=pair();origin=stamp(t1);request=bytes([0x23])+bytes(39)+origin
        await asyncio.wait_for(loop.sock_sendall(sock,request),3)
        packet=await asyncio.wait_for(loop.sock_recv(sock,1024),3)
        t4,m4,err4=pair()
        result=parse(packet,origin,t1,t4,m1,m4,err1+err4)
    except asyncio.CancelledError:raise
    except Exception as e:result=dict(status='UNAVAILABLE',error_type=type(e).__name__,reason=str(e) if isinstance(e,ValueError) else None)
    finally:
        if sock:sock.close()
    result.update(host=HOST,authenticated=False,clock_modified=False)
    return result

async def references():
    import database as db
    freeze(db.con())
    while True:
        r=await probe();db.con().execute('INSERT INTO clock_reference_v0740(ts,status,details_json) VALUES(?,?,?)',(time.time(),r['status'],json.dumps(r)))
        if r.get('stop_requests'):return
        await asyncio.sleep(r.get('retry_after_s',INTERVAL))

def drift(first,previous,current):
    w,m,e=current;pw,pm,pe=previous;fw,fm,fe=first
    step=((w-pw)-(m-pm))*1000
    return dict(elapsed_s=m-fm,interval_s=m-pm,step_ms=step,cumulative_ms=((w-fw)-(m-fm))*1000,
      pair_uncertainty_ms=(e+pe)*1000,jump=abs(step)>JUMP_MS+(e+pe)*1000)

async def local_watch():
    import database as db
    freeze(db.con());session=uuid.uuid4().hex;first=previous=pair()
    while True:
        await asyncio.sleep(1);current=pair();d=drift(first,previous,current)
        db.con().execute('INSERT INTO local_clock_v0740(ts,session,elapsed_s,interval_s,step_ms,cumulative_ms,pair_uncertainty_ms,jump) VALUES(?,?,?,?,?,?,?,?)',
          (current[0],session,d['elapsed_s'],d['interval_s'],d['step_ms'],d['cumulative_ms'],d['pair_uncertainty_ms'],int(d['jump'])))
        previous=current

def count(c,variant,reason):
    if __import__('counter_writer').buffered(c,variant,reason,'clock_shadow_v0740'):return
    c.execute('INSERT INTO clock_shadow_v0740 VALUES(?,?,1) ON CONFLICT(variant,reason) DO UPDATE SET n=n+1',(variant,reason))

def observe(c,mint,price,e,mono=None,context=None):
    import execution_model as ex
    mono=time.monotonic() if mono is None else mono
    for variant,tolerance in [('STRICT_0',0),('SHADOW_100',TOLERANCE_MS)]:
        candidate=dict(e or {});lag=candidate.get('provider_lag_ms')
        if isinstance(lag,(int,float)) and math.isfinite(lag) and -tolerance<=lag<0 and candidate.get('clock_status')=='FUTURE':
            candidate['clock_status']='OK';candidate['provider_lag_ms']=0
        if not isinstance(price,(int,float)) or not math.isfinite(price) or price<=0 or not ex.fresh(candidate,mono):
            count(c,variant,'REJECTED_METADATA');continue
        key=(variant,mint)
        if key not in STATES:
            if len(STATES)>=8192:
                evicted,_=STATES.popitem(last=False);count(c,evicted[0],'STATE_EVICTED')
            STATES[key]=dict(last=None,signatures=deque(maxlen=128))
        STATES.move_to_end(key);s=STATES[key];pt=candidate['provider_ts'];sig=candidate['signature']
        if s['last'] is not None and pt<s['last']:count(c,variant,'OUT_OF_ORDER');continue
        if sig in s['signatures']:count(c,variant,'DUPLICATE');continue
        s['last']=pt;s['signatures'].append(sig);count(c,variant,'ACCEPTED')
        if context is not None:candidate_lab.observe(c,variant,mint,price,candidate,s,dict(context,original_lag_ms=(e or {}).get("provider_lag_ms")))

def summary(c,limit=100):
    config=c.execute('SELECT * FROM clock_config_v0740 WHERE id=1').fetchone()
    refs=[]
    for r in c.execute('SELECT * FROM clock_reference_v0740 ORDER BY id DESC LIMIT ?',(limit,)):
        x=dict(r);x['details']=json.loads(x.pop('details_json'));refs.append(x)
    counts={v:{} for v in ['STRICT_0','SHADOW_100']}
    for r in c.execute('SELECT * FROM clock_shadow_v0740'):counts[r['variant']][r['reason']]=r['n']
    last=c.execute('SELECT * FROM local_clock_v0740 ORDER BY id DESC LIMIT 1').fetchone()
    jumps=[dict(r) for r in c.execute('SELECT * FROM local_clock_v0740 WHERE jump=1 ORDER BY id DESC LIMIT ?',(limit,))]
    return dict(candidates=candidate_lab.summary(c,limit),reference_quality_counts={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM clock_reference_v0740 GROUP BY status')},config=dict(config) if config else None,references=refs,shadow=counts,last_local=dict(last) if last else None,jumps=jumps,
      jump_count=c.execute('SELECT COUNT(*) FROM local_clock_v0740 WHERE jump=1').fetchone()[0],
      note='Positive reference offset means reference ahead of local clock. RTT/root dispersion uncertainty is an estimate, not a guaranteed error bound; unauthenticated SNTP. One source cannot prove provider clock error. No time correction. Shadow event counts are separate from feature-qualified candidates (not preflight-confirmed purchases or PNL); separate order/dedup history, bounded cache. Wall-vs-monotonic discontinuity can include suspend or timing effects.')
