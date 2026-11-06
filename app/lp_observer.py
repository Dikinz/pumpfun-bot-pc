"""v0.7.103.0 LP recorder for the virtual concentrated-liquidity maker test (research/lp_virtual_20261105/PREREG.md).

Observation only: no positions, orders, PAPER balance, signing or transactions. Own database next to PRIMARY
(<primary>_lp_observer.db); the PRIMARY database is never written. Failures stay inside the recorder.

- Tap (PumpApi hot path, O(1)): for venue labels orca-whirlpool / raydium-clmm / meteora-dlmm only.
  Before the pool set is frozen it counts swaps per pool; afterwards it forwards every message of the selected
  pools (swaps, add/remove liquidity) to a bounded queue. A full queue drops and counts; it never blocks.
- Recorder thread: owns the database, stores candidate counts, freezes the selection once (first SELECT_S
  seconds of the first run, persisted), writes the tape.
- Prober thread, paced inside the shared RPC budget: raw pool accounts every SNAPSHOT_S, DLMM bin arrays around
  the active bin every BIN_ARRAY_S (after calibrating the active-bin field against the feed price), on-chain
  signatures per pool every SIGNATURES_S for the feed-completeness audit. Raw account bytes are stored zlib-compressed
  and decoded offline, where decoding is validated against the feed.
"""
import collections,json,math,queue,sqlite3,struct,threading,time,zlib
from contextlib import closing
from pathlib import Path
import config

SCHEMA_VERSION='LP_RECORDER_V1'
MODES=('off','record')
VENUES=('orca-whirlpool','raydium-clmm','meteora-dlmm')
SOL='So11111111111111111111111111111111111111112'
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
QUOTES=(SOL,USDC)
STABLES={USDC,'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB','2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo',
         'USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA','USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB'}
DLMM_PROGRAM='LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'
SELECT_S=7200.
PER_VENUE=4
MAX_SWAPS_PER_HOUR=6000.
MAX_CANDIDATES=20000
QUEUE_MAX=50000
COUNT_FLUSH_S=60.
SNAPSHOT_S=30.
BIN_ARRAY_S=300.
SIGNATURES_S=120.
BINS_PER_ARRAY=70
DLMM_ACTIVE_ID_OFFSET=76   # expected offset of active_id (i32) in LbPair; verified against the feed before use

TAP=None
ACTIVE=None

SCHEMA='''
CREATE TABLE IF NOT EXISTS meta_v1(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS candidates_v1(pool_id TEXT PRIMARY KEY,venue TEXT,mint TEXT,quote_mint TEXT,swaps INTEGER,
 first_ts REAL,last_ts REAL,sample_json TEXT);
CREATE TABLE IF NOT EXISTS pools_v1(pool_id TEXT PRIMARY KEY,venue TEXT,mint TEXT,quote_mint TEXT,rank INTEGER,
 swaps_per_hour REAL,selected_ts REAL,sample_json TEXT,dlmm_offset INTEGER,dlmm_orientation TEXT,calibration_json TEXT);
CREATE TABLE IF NOT EXISTS tape_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,pool_id TEXT,ts REAL,block INTEGER,provider_ts REAL,
 action TEXT,token_amount REAL,quote_amount REAL,tokens_in_pool REAL,quote_in_pool REAL,price REAL,pool_fee_rate REAL,
 priority_fee REAL,sig16 TEXT);
CREATE INDEX IF NOT EXISTS tape_pool_ts ON tape_v1(pool_id,ts);
CREATE TABLE IF NOT EXISTS snapshots_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,pool_id TEXT,kind TEXT,address TEXT,owner TEXT,
 ts REAL,slot INTEGER,data_zlib BLOB);
CREATE INDEX IF NOT EXISTS snapshots_pool_ts ON snapshots_v1(pool_id,kind,ts);
CREATE TABLE IF NOT EXISTS audit_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,pool_id TEXT,ts REAL,returned INTEGER,truncated INTEGER,
 checked INTEGER,missing INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS verify_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,pool_id TEXT,signature TEXT,ts REAL,status TEXT,details_json TEXT);
'''

def path(primary):
    p=Path(primary).resolve();return p.with_name(p.stem+'_lp_observer.db')

def connect(primary):
    c=sqlite3.connect(path(primary),timeout=5);c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL');c.executescript(SCHEMA)
    c.execute("INSERT OR IGNORE INTO meta_v1 VALUES('schema',?)",(SCHEMA_VERSION,))
    c.execute("INSERT OR IGNORE INTO meta_v1 VALUES('created_ts',?)",(str(time.time()),));c.commit()
    return c

def number(v):
    try:
        n=float(v);return n if math.isfinite(n) else None
    except (TypeError,ValueError):return None

def meta_of(x):
    keep=('pool','poolId','mint','quoteMint','decimals','quoteDecimals','binStep','tickSpacing','poolFeeRate','symbol','quoteSymbol','tokenProgram')
    return {k:x.get(k) for k in keep if isinstance(x.get(k),(str,int,float)) or x.get(k) is None}

def row_of(x):
    stamp=number(x.get('timestamp'));stamp=stamp/1000 if stamp and 1e12<=stamp<=1e14 else None
    block=x.get('block');block=block if type(block)==int else None
    return dict(block=block,provider_ts=stamp,action=str(x.get('action') or '')[:24].lower(),token_amount=number(x.get('tokenAmount')),
        quote_amount=number(x.get('quoteAmount')),tokens_in_pool=number(x.get('tokensInPool')),quote_in_pool=number(x.get('quoteInPool')),
        price=number(x.get('price')),pool_fee_rate=number(x.get('poolFeeRate')),priority_fee=number(x.get('priorityFee')),
        sig16=str(x.get('signature') or '')[:16])

class Tap:
    """Hot path. Mutations happen on the caller's thread; other threads read stats and replace `selected`."""
    def __init__(self,out):
        self.out=out;self.selected=None;self.counts=collections.Counter();self.meta={};self.count_start=None
        self.stats=dict(messages=0,venue_messages=0,queued=0,dropped=0,errors=0,tape=0,candidates_full=0)
    def message(self,x,now):
        try:self._message(x,now)
        except Exception:self.stats['errors']+=1
    def _put(self,item):
        try:self.out.put_nowait(item);self.stats['queued']+=1;return True
        except queue.Full:self.stats['dropped']+=1;return False
    def _message(self,x,now):
        self.stats['messages']+=1
        venue=x.get('pool')
        if venue not in VENUES:return
        self.stats['venue_messages']+=1
        pool=x.get('poolId')
        if not isinstance(pool,str) or not 30<=len(pool)<=48:return
        selected=self.selected
        if selected is not None:
            if pool in selected and self._put(('TAPE',pool,now,row_of(x))):self.stats['tape']+=1
            return
        action=str(x.get('action') or '').lower()
        if self.count_start is None:self.count_start=now
        elif now-self.count_start>=COUNT_FLUSH_S:
            self._put(('COUNTS',self.count_start,now,dict(self.counts),self.meta));self.counts=collections.Counter();self.meta={};self.count_start=now
        if action not in ('buy','sell'):return
        key=(venue,pool)
        if key not in self.counts and len(self.counts)>=MAX_CANDIDATES:self.stats['candidates_full']+=1;return
        self.counts[key]+=1
        if key not in self.meta:self.meta[key]=meta_of(x)

def tap(x,now):
    t=TAP
    if t is not None:t.message(x,now)

def choose(rows,hours):
    """rows: candidate dicts (venue, pool_id, mint, quote_mint, swaps). Pre-registered selection rule."""
    chosen=[]
    for venue in VENUES:
        eligible=[r for r in rows if r['venue']==venue and r['quote_mint'] in QUOTES and not (r['mint'] in STABLES and r['quote_mint'] in STABLES)
                  and r['swaps']/hours<=MAX_SWAPS_PER_HOUR]
        eligible.sort(key=lambda r:(-r['swaps'],r['pool_id']))
        for rank,r in enumerate(eligible[:PER_VENUE],1):chosen.append(dict(r,rank=rank,swaps_per_hour=r['swaps']/hours))
    return chosen

class Recorder(threading.Thread):
    def __init__(self,owner,clock=time.time):
        super().__init__(name='lp_observer_recorder',daemon=True);self.owner=owner;self.clock=clock
        self.stats=dict(batches=0,rows=0,tape_rows=0)
    def run(self):
        o=self.owner
        try:
            with closing(connect(o.primary)) as c:
                last_status=0.
                while True:
                    stopping=o.stop_event.is_set()
                    try:items=[o.queue.get(timeout=.25)]
                    except queue.Empty:items=[]
                    while items and len(items)<1000:
                        try:items.append(o.queue.get_nowait())
                        except queue.Empty:break
                    with c:
                        for item in items:self.apply(c,item)
                        self.maybe_freeze(c)
                        if time.monotonic()-last_status>=5 or (stopping and not items):o.write_status(c);last_status=time.monotonic()
                    if items:self.stats['batches']+=1;self.stats['rows']+=len(items)
                    if stopping and not items:break
        except Exception as exc:o.fail('recorder',exc)
    def apply(self,c,item):
        if item[0]=='COUNTS':
            _,start,end,counts,meta=item
            for (venue,pool),n in counts.items():
                m=meta.get((venue,pool),{})
                c.execute('''INSERT INTO candidates_v1(pool_id,venue,mint,quote_mint,swaps,first_ts,last_ts,sample_json) VALUES(?,?,?,?,?,?,?,?)
                  ON CONFLICT(pool_id) DO UPDATE SET swaps=swaps+excluded.swaps,last_ts=excluded.last_ts''',
                  (pool,venue,m.get('mint'),m.get('quoteMint'),n,start,end,json.dumps(m)))
        elif item[0]=='TAPE':
            _,pool,ts,r=item
            c.execute('INSERT INTO tape_v1(pool_id,ts,block,provider_ts,action,token_amount,quote_amount,tokens_in_pool,quote_in_pool,price,pool_fee_rate,priority_fee,sig16) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (pool,ts,r['block'],r['provider_ts'],r['action'],r['token_amount'],r['quote_amount'],r['tokens_in_pool'],r['quote_in_pool'],r['price'],r['pool_fee_rate'],r['priority_fee'],r['sig16']))
            self.stats['tape_rows']+=1
    def maybe_freeze(self,c):
        o=self.owner
        if o.tap.selected is not None:return
        if self.clock()-o.started_ts<SELECT_S:return
        rows=[dict(r) for r in c.execute('SELECT * FROM candidates_v1')]
        chosen=choose(rows,SELECT_S/3600)
        now=self.clock()
        for r in chosen:
            c.execute('INSERT OR IGNORE INTO pools_v1(pool_id,venue,mint,quote_mint,rank,swaps_per_hour,selected_ts,sample_json) VALUES(?,?,?,?,?,?,?,?)',
                (r['pool_id'],r['venue'],r['mint'],r['quote_mint'],r['rank'],r['swaps_per_hour'],now,r['sample_json']))
        c.execute("INSERT OR REPLACE INTO meta_v1 VALUES('selection_frozen_ts',?)",(str(now),))
        o.tap.selected=frozenset(r['pool_id'] for r in chosen)

def dlmm_price(active_id,bin_step,dec_x,dec_y):
    return (1+bin_step/10000)**active_id*10**(dec_x-dec_y)

def calibrate_dlmm(raw,bin_step,dec_base,dec_quote,feed_price):
    """Find the active_id field: an i32 whose bin price matches the feed price within 1.5 bins in either orientation.
    The expected offset is tried first; otherwise the match must be unique."""
    if not (bin_step and feed_price and feed_price>0):return None
    tol=1.5*bin_step/10000
    def fits(off):
        if off+4>len(raw):return None
        v=struct.unpack_from('<i',raw,off)[0]
        if not -443636<=v<=443636:return None
        try:
            for orient,(dx,dy),target in (('BASE_IS_X',(dec_base,dec_quote),feed_price),('BASE_IS_Y',(dec_quote,dec_base),1/feed_price)):
                p=dlmm_price(v,bin_step,dx,dy)
                if p>0 and abs(p/target-1)<=tol:return dict(offset=off,active_id=v,orientation=orient)
        except OverflowError:return None
        return None
    hit=fits(DLMM_ACTIVE_ID_OFFSET)
    if hit:return dict(hit,method='EXPECTED_OFFSET')
    hits=[h for h in (fits(o) for o in range(8,min(len(raw)-3,400))) if h]
    return dict(hits[0],method='UNIQUE_SCAN') if len(hits)==1 else None

def bin_array_address(pool,index):
    import local_simulation as lab
    return lab.pda(DLMM_PROGRAM,b'bin_array',bytes(lab.key(pool)),struct.pack('<q',index))

class Prober(threading.Thread):
    def __init__(self,owner,clock=time.time,read=None,rpc=None):
        super().__init__(name='lp_observer_prober',daemon=True);self.owner=owner;self.clock=clock;self.read=read;self.rpc=rpc
        self.next_rpc=0.;self.last={'snap':0.,'bins':0.};self.last_sig={};self.next_sig={};self.pending={};self.stats=collections.Counter()
    def run(self):
        o=self.owner
        try:
            with closing(connect(o.primary)) as c:
                while not o.stop_event.wait(.5):self.step(c)
        except Exception as exc:o.fail('prober',exc)
    def pools(self,c):
        return [dict(r) for r in c.execute('SELECT * FROM pools_v1 ORDER BY venue,rank')]
    def step(self,c):
        now=self.clock()
        if now<self.next_rpc:return False
        pools=self.pools(c)
        if not pools:return False
        for job in ('snap','bins','sigs'):
            if self.due(job,now,pools):
                self.next_rpc=now+1/self.owner.rpc_rate
                try:getattr(self,'do_'+job)(c,pools,now)
                except Exception as exc:
                    import rpc_budget
                    self.stats[job+':'+('RPC_DEFERRED' if isinstance(exc,rpc_budget.Deferred) else type(exc).__name__)]+=1
                return True
        return False
    def due(self,job,now,pools):
        if job=='snap':return now-self.last['snap']>=SNAPSHOT_S
        if job=='bins':return now-self.last['bins']>=BIN_ARRAY_S and any(p['venue']=='meteora-dlmm' and p['dlmm_offset'] is not None for p in pools)
        return any(now>=self.next_sig.get(p['pool_id'],0.) for p in pools)
    def read_accounts(self,addresses):
        if self.read:return self.read(addresses)
        import local_simulation as lab
        return lab.read_accounts(addresses)
    def store(self,c,pool,kind,address,account,snap,now):
        import base64
        raw=base64.b64decode(account['data'][0]) if account else b''
        c.execute('INSERT INTO snapshots_v1(pool_id,kind,address,owner,ts,slot,data_zlib) VALUES(?,?,?,?,?,?,?)',
            (pool,kind,address,account.get('owner') if account else None,snap.get('received_ts',now),snap.get('slot'),zlib.compress(raw,6)))
        return raw
    def do_snap(self,c,pools,now):
        self.last['snap']=now
        snap=self.read_accounts([p['pool_id'] for p in pools])
        with c:
            for p in pools:
                acc=snap['accounts'].get(p['pool_id'])
                raw=self.store(c,p['pool_id'],'POOL',p['pool_id'],acc,snap,now)
                self.stats['snap:'+('OK' if acc else 'MISSING')]+=1
                if p['venue']=='meteora-dlmm' and p['dlmm_offset'] is None and raw:self.try_calibrate(c,p,raw,now)
    def try_calibrate(self,c,p,raw,now):
        sample=json.loads(p['sample_json'] or '{}')
        row=c.execute("SELECT price FROM tape_v1 WHERE pool_id=? AND action IN ('buy','sell') AND price>0 ORDER BY ts DESC LIMIT 1",(p['pool_id'],)).fetchone()
        dec_quote=sample.get('quoteDecimals') or (9 if p['quote_mint']==SOL else 6 if p['quote_mint']==USDC else None)
        hit=calibrate_dlmm(raw,sample.get('binStep'),sample.get('decimals'),dec_quote,row[0] if row else None) if dec_quote is not None and sample.get('decimals') is not None else None
        status=dict(ts=now,result=hit,feed_price=row[0] if row else None,bin_step=sample.get('binStep'))
        c.execute('UPDATE pools_v1 SET dlmm_offset=?,dlmm_orientation=?,calibration_json=? WHERE pool_id=?',
            (hit['offset'] if hit else None,hit['orientation'] if hit else None,json.dumps(status),p['pool_id']))
        self.stats['calibration:'+('OK' if hit else 'NO_MATCH')]+=1
    def do_bins(self,c,pools,now):
        self.last['bins']=now;wanted=[]
        for p in pools:
            if p['venue']!='meteora-dlmm' or p['dlmm_offset'] is None:continue
            row=c.execute("SELECT data_zlib FROM snapshots_v1 WHERE pool_id=? AND kind='POOL' ORDER BY id DESC LIMIT 1",(p['pool_id'],)).fetchone()
            if not row:continue
            raw=zlib.decompress(row[0])
            if p['dlmm_offset']+4>len(raw):continue
            active=struct.unpack_from('<i',raw,p['dlmm_offset'])[0];index=active//BINS_PER_ARRAY
            for i in (index-1,index,index+1):wanted.append((p['pool_id'],bin_array_address(p['pool_id'],i)))
        if not wanted:return
        snap=self.read_accounts([a for _,a in wanted])
        with c:
            for pool,address in wanted:
                acc=snap['accounts'].get(address);self.store(c,pool,'BIN_ARRAY',address,acc,snap,now)
                self.stats['bins:'+('OK' if acc else 'MISSING')]+=1
    def call(self,body,stage):
        if self.rpc:return self.rpc(body)
        import exit_quotes as q
        return q.request_json(config.QUOTE_RPC_URL,stage,body)
    def do_sigs(self,c,pools,now):
        """Completeness audit. Signatures fetched in the previous cycle (now >= SIGNATURES_S old) are checked against
        the tape; one missing successful transaction is fetched to see whether it changed the pool's token balances
        (a real miss) or only read the pool (not a feed gap)."""
        import random
        p=min(pools,key=lambda p:self.next_sig.get(p['pool_id'],0.));pool=p['pool_id']
        self.next_sig[pool]=now+SIGNATURES_S
        params={'limit':1000,'commitment':'confirmed'}
        if self.last_sig.get(pool):params['until']=self.last_sig[pool]
        data=self.call({'jsonrpc':'2.0','id':1,'method':'getSignaturesForAddress','params':[pool,params]},'LP_SIGNATURES')
        result=data.get('result') if isinstance(data,dict) else None
        if not isinstance(result,list):
            with c:c.execute('INSERT INTO audit_v1(pool_id,ts,returned,truncated,status) VALUES(?,?,?,?,?)',(pool,now,0,0,'RPC_ERROR'))
            self.stats['sigs:RPC_ERROR']+=1;return
        rows=[r for r in result if isinstance(r,dict) and isinstance(r.get('signature'),str)]
        prev,prev_truncated=self.pending.get(pool,([],1))
        ok=[s for s,failed in prev if not failed];missing=[]
        for i in range(0,len(ok),500):
            chunk=ok[i:i+500];have={r[0] for r in c.execute('SELECT sig16 FROM tape_v1 WHERE pool_id=? AND sig16 IN (%s)'%','.join('?'*len(chunk)),[pool]+[s[:16] for s in chunk])}
            missing+=[s for s in chunk if s[:16] not in have]
        with c:c.execute('INSERT INTO audit_v1(pool_id,ts,returned,truncated,checked,missing,status) VALUES(?,?,?,?,?,?,?)',
            (pool,now,len(rows),prev_truncated,len(ok),len(missing),'OK' if prev else 'BASELINE'))
        self.pending[pool]=([(r['signature'],r.get('err') is not None) for r in rows],int(len(rows)>=1000 or not self.last_sig.get(pool)))
        if rows:self.last_sig[pool]=rows[0]['signature']
        self.stats['sigs:OK']+=1
        if missing:self.verify(c,pool,random.choice(missing),now)
    def verify(self,c,pool,signature,now):
        data=self.call({'jsonrpc':'2.0','id':1,'method':'getTransaction','params':[signature,{'encoding':'json','maxSupportedTransactionVersion':0,'commitment':'confirmed'}]},'LP_VERIFY')
        meta=((data or {}).get('result') or {}).get('meta') if isinstance(data,dict) else None
        if not isinstance(meta,dict):status='UNAVAILABLE';details={}
        else:
            def balances(key):return {(b.get('accountIndex'),b.get('mint')):(b.get('uiTokenAmount') or {}).get('amount') for b in meta.get(key) or [] if b.get('owner')==pool}
            pre,post=balances('preTokenBalances'),balances('postTokenBalances')
            changed=sorted(str(k) for k in set(pre)|set(post) if pre.get(k)!=post.get(k))
            status='STATE_CHANGING' if changed else 'READ_ONLY';details=dict(changed=changed,pool_owned_balances=len(set(pre)|set(post)))
        with c:c.execute('INSERT INTO verify_v1(pool_id,signature,ts,status,details_json) VALUES(?,?,?,?,?)',(pool,signature,now,status,json.dumps(details)))
        self.stats['verify:'+status]+=1

class Observer:
    def __init__(self,primary,rpc_rate,clock=time.time):
        self.primary=primary;self.rpc_rate=rpc_rate;self.clock=clock
        self.queue=queue.Queue(maxsize=QUEUE_MAX);self.stop_event=threading.Event();self.failure=None
        self.tap=Tap(self.queue)
        with closing(connect(primary)) as c:
            with c:
                created=float(dict(c.execute('SELECT k,v FROM meta_v1'))['created_ts'])
                selected=[r[0] for r in c.execute('SELECT pool_id FROM pools_v1')]
                c.execute("INSERT OR REPLACE INTO meta_v1 VALUES('session_started_ts',?)",(str(clock()),))
        self.started_ts=created  # the 2-hour selection window counts from the database creation, across restarts
        if selected:self.tap.selected=frozenset(selected)
        self.recorder=Recorder(self,clock);self.prober=Prober(self,clock)
    def start(self):
        self.recorder.start();self.prober.start();return self
    def fail(self,part,exc):
        global TAP
        TAP=None;self.failure=dict(part=part,error_type=type(exc).__name__,ts=time.time());self.stop_event.set()
        try:
            import system_health;system_health.event(self.primary,'LP_OBSERVER_FAILED',**self.failure)
        except Exception:pass
    def status(self):
        return dict(schema=SCHEMA_VERSION,state='FAILED' if self.failure else 'STOPPING' if self.stop_event.is_set() else 'RUNNING',
            heartbeat_ts=time.time(),rpc_rate=self.rpc_rate,failure=self.failure,queue_depth=self.queue.qsize(),
            selection='FROZEN' if self.tap.selected is not None else 'COLLECTING',selected=len(self.tap.selected or ()),
            tap=dict(self.tap.stats),recorder=dict(self.recorder.stats),prober=dict(self.prober.stats))
    def write_status(self,c):
        c.execute("INSERT OR REPLACE INTO meta_v1 VALUES('status',?)",(json.dumps(self.status()),))
    def stop(self,timeout=10.):
        global TAP
        if TAP is self.tap:TAP=None
        self.stop_event.set();deadline=time.monotonic()+timeout
        for t in (self.recorder,self.prober):
            if t.is_alive():t.join(max(0.,deadline-time.monotonic()))
        alive=[t.name for t in (self.recorder,self.prober) if t.is_alive()]
        final=dict(self.status(),state='FAILED' if self.failure else 'STOPPED',threads_alive=alive)
        try:
            with closing(connect(self.primary)) as c,c:c.execute("INSERT OR REPLACE INTO meta_v1 VALUES('status',?)",(json.dumps(final),))
        except Exception:pass
        return final

def settings():
    mode=str(getattr(config,'LP_OBSERVER_MODE','record')).strip().lower();rate=getattr(config,'LP_OBSERVER_RPC_RATE',.3)
    problems=[]
    if mode not in MODES:problems.append('LP_OBSERVER_MODE')
    if not (isinstance(rate,(int,float)) and math.isfinite(rate) and 0<rate<=1 and rate<=config.RPC_MAX_REQUESTS_PER_SECOND):problems.append('LP_OBSERVER_RPC_RATE')
    return mode,rate,problems

def start(primary):
    """Engine start. Never raises."""
    global TAP,ACTIVE
    try:
        mode,rate,problems=settings()
        if problems:
            import system_health;system_health.event(primary,'LP_OBSERVER_CONFIG_REJECTED',fields=problems);return None
        if mode=='off':return None
        ACTIVE=Observer(primary,rate).start();TAP=ACTIVE.tap;return ACTIVE
    except Exception as exc:
        TAP=None;ACTIVE=None
        try:
            import system_health;system_health.event(primary,'LP_OBSERVER_START_FAILED',error_type=type(exc).__name__)
        except Exception:pass
        return None

def stop():
    """Engine stop. Never raises."""
    global ACTIVE,TAP
    TAP=None;o=ACTIVE;ACTIVE=None
    if o is None:return None
    try:return o.stop()
    except Exception as exc:return dict(state='STOP_ERROR',error_type=type(exc).__name__)

def summary(primary):
    """Small read-only report for the dashboard and 05_RESULTS."""
    target=path(primary)
    if not target.exists():return dict(schema=SCHEMA_VERSION,state='NOT_STARTED')
    try:
        with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True,timeout=.5)) as c:
            c.row_factory=sqlite3.Row
            meta=dict(c.execute('SELECT k,v FROM meta_v1'));status=json.loads(meta.get('status') or '{}')
            pools=[]
            for p in c.execute('SELECT * FROM pools_v1 ORDER BY venue,rank'):
                p=dict(p);s=json.loads(p.pop('sample_json') or '{}');p.pop('calibration_json',None)
                q=c.execute("SELECT COUNT(*),MIN(ts),MAX(ts) FROM tape_v1 WHERE pool_id=?",(p['pool_id'],)).fetchone()
                snaps=c.execute("SELECT kind,COUNT(*) FROM snapshots_v1 WHERE pool_id=? GROUP BY kind",(p['pool_id'],)).fetchall()
                a=c.execute("SELECT COALESCE(SUM(checked),0),COALESCE(SUM(missing),0) FROM audit_v1 WHERE pool_id=? AND status='OK' AND truncated=0",(p['pool_id'],)).fetchone()
                v=dict(c.execute("SELECT status,COUNT(*) FROM verify_v1 WHERE pool_id=? GROUP BY status",(p['pool_id'],)).fetchall())
                pools.append(dict(p,symbol=s.get('symbol'),quote_symbol=s.get('quoteSymbol'),fee_rate=s.get('poolFeeRate'),
                    tape_rows=q[0],tape_hours=round((q[2]-q[1])/3600,2) if q[0] else 0,snapshots=dict(snaps),
                    audit=dict(checked=a[0],missing=a[1],missing_share=round(a[1]/a[0],4) if a[0] else None,verified=v)))
            candidates=c.execute('SELECT COUNT(*),SUM(swaps) FROM candidates_v1').fetchone()
    except (sqlite3.Error,ValueError) as exc:return dict(schema=SCHEMA_VERSION,state='REPORT_ERROR',error_type=type(exc).__name__)
    created=float(meta.get('created_ts',0))
    return dict(schema=SCHEMA_VERSION,state=status.get('state','UNKNOWN'),selection=status.get('selection'),
        selection_frozen_ts=float(meta['selection_frozen_ts']) if meta.get('selection_frozen_ts') else None,
        selection_due_ts=created+SELECT_S,heartbeat_age_s=round(time.time()-status['heartbeat_ts'],1) if status.get('heartbeat_ts') else None,
        failure=status.get('failure'),tap=status.get('tap'),prober=status.get('prober'),
        candidates=dict(pools=candidates[0],swaps=candidates[1]),pools=pools,
        note='Observation only for the virtual maker test (research/lp_virtual_20261105/PREREG.md): no positions, orders or PAPER balance.')

def export(primary,folder):
    """05_RESULTS: summary only; the database itself (can be large) is kept by 08_PRESERVE_DATA."""
    report=summary(primary)
    (Path(folder)/'lp_observer_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
