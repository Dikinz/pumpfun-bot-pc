"""v0.7.102.0 AMM observer: what happens to a pump.fun token after it leaves the curve.

Observation only. No entries, no orders, no PAPER ledger, no signing, no transactions.
Own database next to PRIMARY (<primary>_amm_observer.db); the PRIMARY database is never written.

Three parts:
- Tap: called on the PumpApi hot path for every message, before the curve-pool filter.
  O(1) work: counts messages per (pool, action), keeps one sample per pair per hour and,
  for a mint that traded on the curve within CURVE_WINDOW_S and now trades in another
  pool, hands the trade to a bounded queue. A full queue drops and counts; it never blocks.
- Recorder thread: owns the database, writes counts, samples, migrations and trades,
  and schedules the follow-up jobs.
- Prober thread: runs the jobs under its own pacing inside the shared RPC budget
  (rpc_budget via local_simulation.read_accounts) and the Jupiter quote API:
  CONFIRM (canonical PumpSwap pool, flags, reserves, fee settings: 1 RPC request),
  PROBE (pool reserves later: 1 RPC request), QUOTE (Jupiter buy then sell of the bought amount).

Any failure stays inside the observer: it is recorded and the observer stops; PRIMARY goes on.
Detection limits: a migration counts only if the curve trades were seen in this session, so
tokens that migrated before the start or within CURVE_WINDOW_S of it can be missed.
"""
import collections,json,math,queue,sqlite3,struct,threading,time
from contextlib import closing
from pathlib import Path
import config

SCHEMA_VERSION='AMM_OBSERVER_V1'
MODES=('off','counts','observe')
CURVE_POOL='pump'
CURVE_WINDOW_S=600.        # curve trade this recent + first trade in another pool = migration
TRACK_S=1800.              # trades are recorded for 30 minutes after the first AMM sighting
MAX_TRACKED=300
MAX_CURVE_MINTS=50000
MAX_TICKS_PER_MINT=5000
QUEUE_MAX=20000
COUNT_FLUSH_S=60.
SAMPLE_EVERY_S=3600.
MAX_LABEL_PAIRS=500        # distinct (pool, action) pairs kept in memory before an early flush
PROBE_OFFSETS=(60,300,900,1800)
QUOTE_OFFSETS=(60,300,900)
JOB_EXPIRE_S=180.
CONFIRM_EXPIRE_S=300.
MAX_JOB_ATTEMPTS=5
QUOTE_SIZE_USDC=3.0        # same PAPER position size as PRIMARY (PAPER dollars read as USDC)
JUPITER_MIN_INTERVAL_S=12. # one quote job = 2 requests; shares the key with exit_quotes.worker
JUPITER_BACKOFF_S=120.
TOKEN_2022='TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
PROBE_WALLET='11111111111111111111111111111111'  # address math only; no account is read or used

TAP=None
ACTIVE=None

SCHEMA='''
CREATE TABLE IF NOT EXISTS meta_v1(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS stream_counts_v1(start_ts REAL,end_ts REAL,pool TEXT,action TEXT,n INTEGER);
CREATE TABLE IF NOT EXISTS stream_samples_v1(pool TEXT,action TEXT,ts REAL,sample_json TEXT);
CREATE TABLE IF NOT EXISTS migrations_v1(mint TEXT PRIMARY KEY,first_seen_ts REAL,first_pool TEXT,first_action TEXT,
 source TEXT,last_curve_ts REAL,status TEXT,confirmed_ts REAL,pool TEXT,base_vault TEXT,quote_vault TEXT,
 base_program TEXT,ticks INTEGER DEFAULT 0,ticks_dropped INTEGER DEFAULT 0,details_json TEXT);
CREATE TABLE IF NOT EXISTS ticks_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT,ts REAL,provider_ts REAL,pool TEXT,
 side TEXT,price REAL,quote_amount REAL,token_amount REAL,signer TEXT,signature TEXT,pool_fee_rate REAL);
CREATE INDEX IF NOT EXISTS ticks_mint_ts ON ticks_v1(mint,ts);
CREATE TABLE IF NOT EXISTS jobs_v1(id INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT,kind TEXT,offset_s REAL,due_ts REAL,
 expire_ts REAL,status TEXT,attempts INTEGER DEFAULT 0,started_ts REAL,finished_ts REAL,result_json TEXT);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs_v1(status,due_ts);
'''

def path(primary):
    p=Path(primary).resolve();return p.with_name(p.stem+'_amm_observer.db')

def connect(primary):
    c=sqlite3.connect(path(primary),timeout=5);c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL');c.executescript(SCHEMA)
    c.execute("INSERT OR IGNORE INTO meta_v1 VALUES('schema',?)",(SCHEMA_VERSION,))
    c.execute("INSERT OR IGNORE INTO meta_v1 VALUES('created_ts',?)",(str(time.time()),));c.commit()
    return c

def number(v):
    try:
        n=float(v);return n if math.isfinite(n) and n>0 else None
    except (TypeError,ValueError):return None

def sample_of(x):
    """Public market message, trimmed: scalar fields only, bounded size."""
    out={}
    for k,v in list(x.items())[:40]:
        if isinstance(v,(int,float,bool)) or v is None:out[str(k)[:40]]=v
        elif isinstance(v,str):out[str(k)[:40]]=v[:200]
        else:out[str(k)[:40]]='<'+type(v).__name__+'>'
    return out

def tick_of(x):
    price=number(x.get('price'));qa=number(x.get('quoteAmount'));ta=number(x.get('tokenAmount'))
    if price is None and qa and ta:price=qa/ta
    stamp=number(x.get('timestamp'));stamp=stamp/1000 if stamp and 1e12<=stamp<=1e14 else None
    return dict(price=price,quote_amount=qa,token_amount=ta,provider_ts=stamp,
        signer=str(x.get('txSigner') or x.get('trader') or '')[:64],signature=str(x.get('signature') or '')[:140],
        pool_fee_rate=number(x.get('poolFeeRate')))

class Tap:
    """Hot path. Every mutation happens on the caller's thread; other threads only read stats."""
    def __init__(self,out,mode='observe'):
        self.out=out;self.mode=mode;self.curve=collections.OrderedDict();self.tracked={}
        self.counts=collections.Counter();self.count_start=None;self.sampled={}
        self.stats=dict(messages=0,queued=0,dropped=0,errors=0,migrations=0,untracked_amm=0,tracked_full=0,ticks=0)
    def message(self,x,now):
        try:self._message(x,now)
        except Exception:self.stats['errors']+=1
    def _put(self,item):
        try:self.out.put_nowait(item);self.stats['queued']+=1;return True
        except queue.Full:self.stats['dropped']+=1;return False
    def _message(self,x,now):
        self.stats['messages']+=1
        pool=str(x.get('pool') or '')[:40].lower();action=str(x.get('action') or x.get('txType') or '')[:24].lower()
        key=(pool,action)
        if self.count_start is None:self.count_start=now
        elif now-self.count_start>=COUNT_FLUSH_S or len(self.counts)>=MAX_LABEL_PAIRS:
            self._put(('COUNTS',self.count_start,now,dict(self.counts)));self.counts=collections.Counter();self.count_start=now
        self.counts[key]+=1
        if now-self.sampled.get(key,-1e18)>=SAMPLE_EVERY_S:
            if len(self.sampled)>=MAX_LABEL_PAIRS:self.sampled.clear()
            self.sampled[key]=now;self._put(('SAMPLE',pool,action,now,sample_of(x)))
        if self.mode!='observe':return
        mint=str(x.get('mint') or '')
        if not mint or len(mint)>64:return
        if pool==CURVE_POOL:
            if mint in self.tracked:return
            self.curve[mint]=now;self.curve.move_to_end(mint)
            while self.curve:
                oldest,seen=next(iter(self.curve.items()))
                if now-seen>CURVE_WINDOW_S or len(self.curve)>MAX_CURVE_MINTS:self.curve.popitem(last=False)
                else:break
            return
        if not pool:return
        first=self.tracked.get(mint)
        if first is None:
            last_curve=self.curve.get(mint)
            if last_curve is None or now-last_curve>CURVE_WINDOW_S:self.stats['untracked_amm']+=1;return
            if len(self.tracked)>=MAX_TRACKED:
                for m in [m for m,t in self.tracked.items() if now-t>TRACK_S]:self.tracked.pop(m,None)
                if len(self.tracked)>=MAX_TRACKED:self.stats['tracked_full']+=1;return
            self.tracked[mint]=first=now;self.curve.pop(mint,None);self.stats['migrations']+=1
            self._put(('MIGRATION',mint,now,pool,action,last_curve))
        elif now-first>TRACK_S:return
        if self._put(('TICK',mint,now,pool,action,tick_of(x))):self.stats['ticks']+=1

def tap(x,now):
    """PumpApi hot path entry point: one global read when the observer is off."""
    t=TAP
    if t is not None:t.message(x,now)

class Recorder(threading.Thread):
    def __init__(self,owner):
        super().__init__(name='amm_observer_recorder',daemon=True);self.owner=owner
        self.stats=dict(batches=0,rows=0,errors=0);self.ticks={};self.dropped_ticks=collections.Counter()
    def run(self):
        o=self.owner
        try:
            with closing(connect(o.primary)) as c:
                last_status=0.
                while True:
                    stopping=o.stop_event.is_set()
                    try:items=[o.queue.get(timeout=.25)]
                    except queue.Empty:items=[]
                    while items and len(items)<500:
                        try:items.append(o.queue.get_nowait())
                        except queue.Empty:break
                    if items:
                        with c:
                            for item in items:self.apply(c,item)
                        self.stats['batches']+=1;self.stats['rows']+=len(items)
                    if time.monotonic()-last_status>=5 or (stopping and not items):
                        with c:o.write_status(c)
                        last_status=time.monotonic()
                    if stopping and not items:break
        except Exception as exc:o.fail('recorder',exc)
    def apply(self,c,item):
        kind=item[0]
        if kind=='COUNTS':
            _,start,end,counts=item
            c.executemany('INSERT INTO stream_counts_v1 VALUES(?,?,?,?,?)',[(start,end,p,a,n) for (p,a),n in counts.items()])
        elif kind=='SAMPLE':
            _,pool,action,ts,sample=item
            c.execute('INSERT INTO stream_samples_v1 VALUES(?,?,?,?)',(pool,action,ts,json.dumps(sample,ensure_ascii=False)))
        elif kind=='MIGRATION':
            _,mint,ts,pool,action,last_curve=item
            new=c.execute("INSERT OR IGNORE INTO migrations_v1(mint,first_seen_ts,first_pool,first_action,source,last_curve_ts,status) VALUES(?,?,?,?,'FIRST_AMM_TRADE',?,'OBSERVED')",(mint,ts,pool,action,last_curve)).rowcount
            if new:
                self.owner.schedule(c,mint,'CONFIRM',ts,0,CONFIRM_EXPIRE_S)
                if self.owner.jupiter:
                    for off in QUOTE_OFFSETS:self.owner.schedule(c,mint,'QUOTE',ts,off,JOB_EXPIRE_S)
        elif kind=='TICK':
            _,mint,ts,pool,action,t=item
            n=self.ticks.get(mint)
            if n is None:
                row=c.execute('SELECT ticks FROM migrations_v1 WHERE mint=?',(mint,)).fetchone();n=row[0] if row else 0
            if n>=MAX_TICKS_PER_MINT:
                c.execute('UPDATE migrations_v1 SET ticks_dropped=ticks_dropped+1 WHERE mint=?',(mint,));return
            c.execute('INSERT INTO ticks_v1(mint,ts,provider_ts,pool,side,price,quote_amount,token_amount,signer,signature,pool_fee_rate) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (mint,ts,t['provider_ts'],pool,action,t['price'],t['quote_amount'],t['token_amount'],t['signer'],t['signature'],t['pool_fee_rate']))
            c.execute('UPDATE migrations_v1 SET ticks=ticks+1 WHERE mint=?',(mint,))
            self.ticks[mint]=n+1
            if len(self.ticks)>2*MAX_TRACKED:self.ticks.clear()

class Prober(threading.Thread):
    def __init__(self,owner,clock=time.time):
        super().__init__(name='amm_observer_prober',daemon=True);self.owner=owner;self.clock=clock
        self.next_rpc=0.;self.next_jupiter=0.;self.stats=collections.Counter()
    def run(self):
        o=self.owner
        try:
            with closing(connect(o.primary)) as c:
                while not o.stop_event.wait(.25):self.step(c)
        except Exception as exc:o.fail('prober',exc)
    def step(self,c):
        now=self.clock()
        with c:
            for row in c.execute("SELECT id FROM jobs_v1 WHERE status='PENDING' AND expire_ts<?",(now,)).fetchall():
                c.execute("UPDATE jobs_v1 SET status='EXPIRED',finished_ts=? WHERE id=?",(now,row[0]));self.stats['EXPIRED']+=1
        rows=c.execute("SELECT * FROM jobs_v1 WHERE status='PENDING' AND due_ts<=? ORDER BY due_ts LIMIT 20",(now,)).fetchall()
        for job in rows:
            jupiter=job['kind']=='QUOTE'
            if now<(self.next_jupiter if jupiter else self.next_rpc):continue
            if jupiter:self.next_jupiter=now+JUPITER_MIN_INTERVAL_S
            else:self.next_rpc=now+1/self.owner.rpc_rate
            self.execute(c,dict(job));return True
        return False
    def execute(self,c,job):
        import rpc_budget,exit_quotes,local_simulation as lab
        started=self.clock();retry=None
        try:
            mig=dict(c.execute('SELECT * FROM migrations_v1 WHERE mint=?',(job['mint'],)).fetchone() or {})
            if job['kind']=='CONFIRM':result=confirm(job['mint'])
            elif job['kind']=='PROBE':result=probe(job['mint'],mig)
            elif job['kind']=='QUOTE':result=quote(job['mint'])
            else:result=dict(status='UNKNOWN_JOB')
        except rpc_budget.Deferred as exc:
            result=dict(status='RPC_DEFERRED',reason=exc.rpc_budget_reason);retry=max(1.,exc.retry_after_s)
        except lab.LabError as exc:result=dict(status=str(exc)[:80])
        except Exception as exc:result=dict(status='INTERNAL_ERROR',error_type=type(exc).__name__)
        if result.get('status') in ('RATE_LIMITED','AUTH_ERROR'):
            self.next_jupiter=self.clock()+JUPITER_BACKOFF_S;retry=JUPITER_BACKOFF_S
        if result.get('status')=='RPC_DEFERRED' and retry is None:retry=5.
        finished=self.clock();result.update(elapsed_ms=round((finished-started)*1000,1))
        self.stats[job['kind']+':'+str(result.get('status'))]+=1
        with c:
            if retry is not None and job['attempts']+1<MAX_JOB_ATTEMPTS and finished+retry<=job['expire_ts']:
                c.execute("UPDATE jobs_v1 SET attempts=attempts+1,due_ts=?,result_json=? WHERE id=?",(finished+retry,json.dumps(result),job['id']));return result
            c.execute("UPDATE jobs_v1 SET status=?,attempts=attempts+1,started_ts=?,finished_ts=?,result_json=? WHERE id=?",
                ('DONE' if retry is None else 'GAVE_UP',started,finished,json.dumps(result,ensure_ascii=False,default=str),job['id']))
            if job['kind']=='CONFIRM':
                c.execute('UPDATE migrations_v1 SET status=?,confirmed_ts=?,pool=?,base_vault=?,quote_vault=?,base_program=?,details_json=? WHERE mint=?',
                    (result.get('status'),finished,result.get('pool'),result.get('base_vault'),result.get('quote_vault'),result.get('base_program'),
                     json.dumps(result,ensure_ascii=False,default=str),job['mint']))
                if result.get('status')=='CONFIRMED':
                    first=c.execute('SELECT first_seen_ts FROM migrations_v1 WHERE mint=?',(job['mint'],)).fetchone()[0]
                    for off in PROBE_OFFSETS:self.owner.schedule(c,job['mint'],'PROBE',first,off,JOB_EXPIRE_S)
        return result

def decode_fee_config(account):
    """FeeConfig of the pump fee program for PumpSwap: flat fees and market-cap tiers (bps)."""
    import local_simulation as lab,amm_simulation as amm
    if not account or account.get('owner')!=lab.FEE:return dict(status='FEE_CONFIG_MISSING')
    raw=lab.raw(account);disc=bytes(next(x['discriminator'] for x in amm.IDL['accounts'] if x['name']=='FeeConfig'))
    if raw[:8]!=disc:return dict(status='FEE_CONFIG_DISCRIMINATOR_MISMATCH')
    at=8+1+32
    def fees():
        nonlocal at
        lp,protocol,creator=struct.unpack_from('<QQQ',raw,at);at+=24
        return dict(lp_fee_bps=lp,protocol_fee_bps=protocol,creator_fee_bps=creator)
    def tiers():
        nonlocal at
        n=struct.unpack_from('<I',raw,at)[0];at+=4
        if n>64:raise ValueError('FEE_TIER_COUNT')
        out=[]
        for _ in range(n):
            threshold=int.from_bytes(raw[at:at+16],'little');at+=16;out.append(dict(market_cap_lamports_threshold=str(threshold),**fees()))
        return out
    try:
        flat=fees();tier=tiers();stable=tiers();exotic=fees()
    except (struct.error,ValueError) as exc:return dict(status='FEE_CONFIG_UNDECODED',error=str(exc)[:60])
    return dict(status='DECODED',flat_fees=flat,fee_tiers=tier,stable_fee_tiers=stable,exotic_flat_fees=exotic)

def tier_for(tiers,market_cap_lamports):
    """Estimate only: the highest tier whose threshold is not above the current market cap."""
    best=None
    for t in tiers or []:
        if int(t['market_cap_lamports_threshold'])<=market_cap_lamports:best=t
    return best

def confirm(mint,read=None):
    import local_simulation as lab,amm_simulation as amm
    read=read or lab.read_accounts
    pool,_,_=amm.canonical(mint);glob=lab.pda(amm.AMM,b'global_config');fee=lab.pda(lab.FEE,b'fee_config',bytes(lab.key(amm.AMM)))
    vaults=[lab.ata(pool,mint,amm.TOKEN),lab.ata(pool,mint,TOKEN_2022),lab.ata(pool,amm.SOL,amm.TOKEN)]
    snap=read(list(dict.fromkeys([pool,mint,amm.SOL,glob,fee]+vaults)))
    a=snap['accounts'];base=dict(pool=pool,slot=snap['slot'],received_ts=snap['received_ts'])
    try:names,p,g=amm.names_for(mint,PROBE_WALLET,a)
    except amm.AmmError as exc:
        details={k:v for k,v in (exc.details or {}).items() if k in ('expected_pool','is_mayhem_mode','is_cashback_coin','is_holder_reward','creator_fee_bps','can_edit_creator_fee','supported','extensions')}
        return dict(base,status=str(exc),details=details)
    try:
        base_raw=amm.token_info(a.get(names['pool_base_token_account']),mint,pool,names['base_token_program'])
        quote_raw=amm.token_info(a.get(names['pool_quote_token_account']),amm.SOL,pool,amm.TOKEN)
    except amm.AmmError as exc:return dict(base,status=str(exc))
    m=lab.raw(a[mint]);supply=struct.unpack_from('<Q',m,36)[0];decimals=m[44]
    effective=quote_raw+p['virtual_quote_reserves']
    price=effective/base_raw if base_raw else None
    cap=int(price*supply) if price else None
    fee_config=decode_fee_config(a.get(fee))
    return dict(base,status='CONFIRMED',base_vault=names['pool_base_token_account'],quote_vault=names['pool_quote_token_account'],
        base_program=names['base_token_program'],decimals=decimals,supply_raw=str(supply),
        reserves=dict(base_raw=str(base_raw),quote_raw=str(quote_raw),virtual_quote_raw=str(p['virtual_quote_reserves']),effective_quote_raw=str(effective)),
        price_lamports_per_raw=price,market_cap_lamports=cap,coin_creator=p['coin_creator'],
        global_fees=dict(lp_fee_bps=g['lp_fee_basis_points'],protocol_fee_bps=g['protocol_fee_basis_points'],
            coin_creator_fee_bps=g['coin_creator_fee_basis_points'],buyback_bps=g['buyback_basis_points']),
        fee_config=fee_config,fee_tier_estimate=tier_for(fee_config.get('fee_tiers'),cap) if cap is not None else None,
        note='Read-only snapshot. The tier is an estimate from the market cap at this slot, not the fee charged on a trade.')

def probe(mint,mig,read=None):
    import local_simulation as lab,amm_simulation as amm
    read=read or lab.read_accounts
    if not mig or mig.get('status')!='CONFIRMED':return dict(status='NOT_CONFIRMED')
    pool,bv,qv=mig['pool'],mig['base_vault'],mig['quote_vault']
    snap=read([pool,bv,qv]);a=snap['accounts']
    try:
        p=amm.decode(a.get(pool),'Pool')
        base_raw=amm.token_info(a.get(bv),mint,pool,mig['base_program']);quote_raw=amm.token_info(a.get(qv),amm.SOL,pool,amm.TOKEN)
    except amm.AmmError as exc:return dict(status=str(exc),slot=snap['slot'],received_ts=snap['received_ts'])
    effective=quote_raw+p['virtual_quote_reserves']
    return dict(status='RESERVES',slot=snap['slot'],received_ts=snap['received_ts'],base_raw=str(base_raw),quote_raw=str(quote_raw),
        virtual_quote_raw=str(p['virtual_quote_reserves']),effective_quote_raw=str(effective),lp_supply=str(p['lp_supply']),
        price_lamports_per_raw=effective/base_raw if base_raw else None)

def route_of(data):
    return [dict(label=(r.get('swapInfo') or {}).get('label'),amm_key=(r.get('swapInfo') or {}).get('ammKey'),percent=r.get('percent')) for r in data.get('routePlan') or []]

def quote(mint,now=None):
    """Jupiter quote only (no taker, no transaction): buy QUOTE_SIZE_USDC of SOL worth, then sell what it would buy."""
    import exit_quotes as q
    from cost_evidence import SOL
    now=time.time() if now is None else now
    if not config.JUPITER_API_KEY:return dict(status='NOT_CONFIGURED')
    fx=q.FX if q.FX and 0<=now-q.FX['ts']<=120 else None
    if not fx:return dict(status='MISSING_FX')
    lamports=int(QUOTE_SIZE_USDC/float(fx['rate'])*1e9)
    slippage=int(config.QUOTE_SLIPPAGE_BPS)
    try:
        buy=q.get_quote(SOL,mint,lamports,slippage);tokens,_=q.validate(buy,SOL,mint,lamports)
        sell=q.get_quote(mint,SOL,tokens,slippage);back,_=q.validate(sell,mint,SOL,tokens)
    except q.ProbeError as exc:return dict(status=exc.status,stage=exc.stage,http_status=exc.http_status,response=exc.payload)
    return dict(status='QUOTED',fx_rate=fx['rate'],in_lamports=lamports,tokens_raw=str(tokens),back_lamports=back,
        round_trip_bps=round((back/lamports-1)*1e4,1),buy_route=route_of(buy),sell_route=route_of(sell),
        buy=q.sanitize(buy),sell=q.sanitize(sell),
        note='Provider quotes, not fills. Round trip includes pool fees, Jupiter platform fee and price impact; no network costs.')

class Observer:
    def __init__(self,primary,mode,rpc_rate,jupiter):
        self.primary=primary;self.mode=mode;self.rpc_rate=rpc_rate;self.jupiter=jupiter
        self.queue=queue.Queue(maxsize=QUEUE_MAX);self.stop_event=threading.Event();self.failure=None
        self.started_ts=time.time();self.tap=Tap(self.queue,mode)
        with closing(connect(primary)) as c:
            with c:
                c.execute("INSERT OR REPLACE INTO meta_v1 VALUES('session_started_ts',?)",(str(self.started_ts),))
                c.execute("INSERT OR REPLACE INTO meta_v1 VALUES('mode',?)",(mode,))
        self.recorder=Recorder(self);self.prober=Prober(self) if mode=='observe' else None
    def start(self):
        self.recorder.start()
        if self.prober:self.prober.start()
        return self
    def schedule(self,c,mint,kind,origin,offset,expire):
        due=origin+offset
        c.execute("INSERT INTO jobs_v1(mint,kind,offset_s,due_ts,expire_ts,status) VALUES(?,?,?,?,?,'PENDING')",(mint,kind,offset,due,due+expire))
    def fail(self,part,exc):
        global TAP
        TAP=None;self.failure=dict(part=part,error_type=type(exc).__name__,ts=time.time());self.stop_event.set()
        try:
            import system_health;system_health.event(self.primary,'AMM_OBSERVER_FAILED',**self.failure)
        except Exception:pass
        try:
            with closing(connect(self.primary)) as c,c:self.write_status(c)
        except Exception:pass
    def status(self):
        return dict(schema=SCHEMA_VERSION,mode=self.mode,state='FAILED' if self.failure else 'STOPPING' if self.stop_event.is_set() else 'RUNNING',
            started_ts=self.started_ts,heartbeat_ts=time.time(),rpc_rate=self.rpc_rate,jupiter=self.jupiter,failure=self.failure,
            queue_depth=self.queue.qsize(),tap=dict(self.tap.stats),recorder=dict(self.recorder.stats),
            prober=dict(self.prober.stats) if self.prober else None,tracked=len(self.tap.tracked),curve_mints=len(self.tap.curve))
    def write_status(self,c):
        c.execute("INSERT OR REPLACE INTO meta_v1 VALUES('status',?)",(json.dumps(self.status()),))
    def stop(self,timeout=10.):
        global TAP
        if TAP is self.tap:TAP=None
        self.stop_event.set();deadline=time.monotonic()+timeout
        for t in (self.recorder,self.prober):
            if t is not None and t.is_alive():t.join(max(0.,deadline-time.monotonic()))
        alive=[t.name for t in (self.recorder,self.prober) if t is not None and t.is_alive()]
        final=dict(self.status(),state='FAILED' if self.failure else 'STOPPED',threads_alive=alive)
        try:
            with closing(connect(self.primary)) as c,c:c.execute("INSERT OR REPLACE INTO meta_v1 VALUES('status',?)",(json.dumps(final),))
        except Exception:pass
        return final

def settings():
    mode=str(getattr(config,'AMM_OBSERVER_MODE','observe')).strip().lower()
    rate=getattr(config,'AMM_OBSERVER_RPC_RATE',.2)
    problems=[]
    if mode not in MODES:problems.append('AMM_OBSERVER_MODE')
    if not (isinstance(rate,(int,float)) and math.isfinite(rate) and 0<rate<=1 and rate<=config.RPC_MAX_REQUESTS_PER_SECOND):problems.append('AMM_OBSERVER_RPC_RATE')
    return mode,rate,bool(getattr(config,'AMM_OBSERVER_JUPITER',True)),problems

def start(primary):
    """Engine start. Never raises: a broken observer is recorded and stays off."""
    global TAP,ACTIVE
    try:
        mode,rate,jupiter,problems=settings()
        if problems:
            import system_health;system_health.event(primary,'AMM_OBSERVER_CONFIG_REJECTED',fields=problems);return None
        if mode=='off':return None
        ACTIVE=Observer(primary,mode,rate,jupiter).start();TAP=ACTIVE.tap;return ACTIVE
    except Exception as exc:
        TAP=None;ACTIVE=None
        try:
            import system_health;system_health.event(primary,'AMM_OBSERVER_START_FAILED',error_type=type(exc).__name__)
        except Exception:pass
        return None

def stop():
    """Engine stop. Never raises."""
    global ACTIVE,TAP
    TAP=None;o=ACTIVE;ACTIVE=None
    if o is None:return None
    try:return o.stop()
    except Exception as exc:return dict(state='STOP_ERROR',error_type=type(exc).__name__)

def summary(primary,limit=50):
    """Small read-only report for the dashboard and 05_RESULTS."""
    target=path(primary)
    if not target.exists():return dict(schema=SCHEMA_VERSION,state='NOT_STARTED',note='No observer database yet.')
    try:
        with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True,timeout=.5)) as c:
            c.row_factory=sqlite3.Row
            meta=dict(c.execute('SELECT k,v FROM meta_v1'))
            status=json.loads(meta.get('status') or '{}')
            hour=time.time()-3600
            labels=[dict(r) for r in c.execute('SELECT pool,action,SUM(n) n FROM stream_counts_v1 WHERE end_ts>=? GROUP BY pool,action ORDER BY n DESC LIMIT 20',(hour,))]
            labels_all=[dict(r) for r in c.execute('SELECT pool,action,SUM(n) n FROM stream_counts_v1 GROUP BY pool,action ORDER BY n DESC LIMIT 20')]
            migrations=dict(c.execute('SELECT status,COUNT(*) FROM migrations_v1 GROUP BY status'))
            first_pools=dict(c.execute('SELECT first_pool,COUNT(*) FROM migrations_v1 GROUP BY first_pool'))
            span=c.execute('SELECT MIN(first_seen_ts),MAX(first_seen_ts),COUNT(*) FROM migrations_v1').fetchone()
            jobs=[dict(r) for r in c.execute('SELECT kind,status,COUNT(*) n FROM jobs_v1 GROUP BY kind,status')]
            ticks=c.execute('SELECT COUNT(*) FROM ticks_v1').fetchone()[0]
            trips=[]
            for (r,) in c.execute("SELECT result_json FROM jobs_v1 WHERE kind='QUOTE' AND status='DONE'"):
                d=json.loads(r or '{}')
                if d.get('status')=='QUOTED':trips.append(d['round_trip_bps'])
            fees={}
            for (r,) in c.execute("SELECT details_json FROM migrations_v1 WHERE status='CONFIRMED' ORDER BY confirmed_ts DESC LIMIT 200"):
                d=json.loads(r or '{}');t=d.get('fee_tier_estimate') or {}
                k='%s/%s/%s'%(t.get('lp_fee_bps'),t.get('protocol_fee_bps'),t.get('creator_fee_bps'));fees[k]=fees.get(k,0)+1
            recent=[dict(r) for r in c.execute('SELECT mint,first_seen_ts,first_pool,status,ticks,ticks_dropped FROM migrations_v1 ORDER BY first_seen_ts DESC LIMIT ?',(limit,))]
    except (sqlite3.Error,ValueError) as exc:return dict(schema=SCHEMA_VERSION,state='REPORT_ERROR',error_type=type(exc).__name__)
    hours=max((span[1]-span[0])/3600,1/60) if span[2] else None
    trips.sort()
    return dict(schema=SCHEMA_VERSION,state=status.get('state','UNKNOWN'),mode=status.get('mode') or meta.get('mode'),
        heartbeat_age_s=round(time.time()-status['heartbeat_ts'],1) if status.get('heartbeat_ts') else None,failure=status.get('failure'),
        tap=status.get('tap'),queue_depth=status.get('queue_depth'),pool_labels_last_hour=labels,pool_labels_total=labels_all,
        migrations=dict(total=span[2],by_status=migrations,first_pool=first_pools,per_hour=round(span[2]/hours,2) if hours else None),
        ticks=ticks,jobs=jobs,quotes=dict(n=len(trips),round_trip_bps_median=trips[len(trips)//2] if trips else None,
            round_trip_bps_p10=trips[len(trips)//10] if trips else None,round_trip_bps_p90=trips[9*len(trips)//10] if trips else None),
        fee_tiers_bps_lp_protocol_creator=fees,recent=recent,
        note='Observation only: no entries, orders or PAPER balance. Quotes are provider quotes, not fills; reserves are read-only snapshots.')

def export(primary,folder):
    """05_RESULTS: small summary JSON plus a consistent copy of the observer database in a ZIP."""
    import tempfile,zipfile
    folder=Path(folder);report=summary(primary)
    (folder/'amm_observer_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    target=path(primary)
    if not target.exists():return report
    with tempfile.TemporaryDirectory() as td:
        copy=Path(td)/target.name
        with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as a,closing(sqlite3.connect(copy)) as b:a.backup(b)
        with zipfile.ZipFile(folder/'amm_observer.zip','w',zipfile.ZIP_DEFLATED) as z:
            z.write(copy,target.name);z.writestr('summary.json',json.dumps(report,ensure_ascii=False))
    return report
