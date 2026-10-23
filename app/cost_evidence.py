"""Observe the existing feed only. No quotes, signing, RPC or order submission.
Provider fields are not verified on-chain. Fee-only sums are first-order PAPER
sensitivity calculations, not execution prices or compounded wallet balances.
"""
import time, math, json, sqlite3, argparse
from pathlib import Path
HORIZONS=(250,500,1000)
MAX_LATE_MS=250
MAX_AGE_MS=1000
SOL='So11111111111111111111111111111111111111112'
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
WATCH=set()
ORIGINS={}
SCHEMA='''
CREATE TABLE IF NOT EXISTS cost_decisions_v0719(
 trade_id TEXT NOT NULL,side TEXT NOT NULL,mint TEXT NOT NULL,decision_ts REAL NOT NULL,
 price REAL,size REAL,reason TEXT,source TEXT,price_basis TEXT,pool TEXT,quote_mint TEXT,
 signature TEXT,provider_ts REAL,received_ts REAL,provider_lag_ms REAL,price_age_ms REAL,
 pool_fee_rate REAL,priority_fee_raw REAL,clock_status TEXT,metadata_status TEXT,
 PRIMARY KEY(trade_id,side));
CREATE TABLE IF NOT EXISTS cost_probes_v0719(
 trade_id TEXT NOT NULL,side TEXT NOT NULL,mint TEXT NOT NULL,horizon_ms INTEGER NOT NULL,
 status TEXT NOT NULL,observed_ts REAL,elapsed_ms REAL,lateness_ms REAL,price REAL,
 adverse_pct REAL,signature TEXT,PRIMARY KEY(trade_id,side,horizon_ms));
CREATE INDEX IF NOT EXISTS cost_probe_pending ON cost_probes_v0719(mint,status);
CREATE INDEX IF NOT EXISTS cost_probe_status ON cost_probes_v0719(status);
'''

def finite(v,lo=0,hi=float('inf')):
    try:
        n=float(v)
        return n if math.isfinite(n) and lo<=n<=hi else None
    except (TypeError,ValueError):return None

def metadata(raw,source,price_basis,received_ts=None,received_mono=None):
    raw=raw or {};wall=time.time() if received_ts is None else received_ts
    mono=time.monotonic() if received_mono is None else received_mono
    # PumpApi documents timestamp in milliseconds. Other adapters do not claim this contract.
    stamp=finite(raw.get('timestamp'),1e12,1e14) if source=='pumpapi_free' else None
    stamp=stamp/1000 if stamp else None
    lag=(wall-stamp)*1000 if stamp is not None else None
    clock='MISSING' if lag is None else ('FUTURE' if lag < 0 else 'OK')
    return {'source':source,'price_basis':price_basis,'pool':str(raw.get('pool') or '')[:80],
      'quote_mint':str(raw.get('quoteMint') or '')[:100],'signature':str(raw.get('signature') or '')[:140],
      'timestamp_raw_ms':finite(raw.get('timestamp')) if source=='pumpapi_free' else None,
      'timestamp_input_state':('MISSING' if raw.get('timestamp') is None else 'INVALID' if stamp is None else 'VALID'),
      'provider_ts':stamp,'received_ts':wall,'received_mono':mono,'provider_lag_ms':lag,
      'pool_fee_rate':finite(raw.get('poolFeeRate'),0,.20) if source=='pumpapi_free' else None,
      'priority_fee_raw':finite(raw.get('priorityFee')) if source=='pumpapi_free' else None,'clock_status':clock}

def record(c,trade_id,side,mint,price,size,reason,evidence=None,wall=None,mono=None):
    wall=time.time() if wall is None else wall;mono=time.monotonic() if mono is None else mono
    e=evidence or {};age=(mono-e['received_mono'])*1000 if 'received_mono' in e else None
    status='MISSING' if not e else ('STALE' if age is None or age<0 or age>MAX_AGE_MS else 'PRESENT')
    c.execute('INSERT INTO cost_decisions_v0719 VALUES('+','.join('?'*20)+')',
      (trade_id,side,mint,wall,price,size,reason,e.get('source','unknown'),e.get('price_basis','unknown'),e.get('pool'),e.get('quote_mint'),e.get('signature'),e.get('provider_ts'),e.get('received_ts'),e.get('provider_lag_ms'),age,e.get('pool_fee_rate'),e.get('priority_fee_raw'),e.get('clock_status','MISSING'),status))
    c.executemany('INSERT INTO cost_probes_v0719(trade_id,side,mint,horizon_ms,status) VALUES(?,?,?,?,?)',[(trade_id,side,mint,h,'PENDING') for h in HORIZONS])
    WATCH.add(mint);ORIGINS[(trade_id,side)]=mono

def on_tick(c,mint,price,evidence,wall=None,mono=None):
    if mint not in WATCH:return
    wall=time.time() if wall is None else wall;mono=time.monotonic() if mono is None else mono
    rows=c.execute('''SELECT p.*,d.price initial,d.signature initial_signature,d.provider_ts initial_ts,d.source initial_source,d.quote_mint initial_quote,d.price_basis initial_basis FROM cost_probes_v0719 p
      JOIN cost_decisions_v0719 d USING(trade_id,side) WHERE p.mint=? AND p.status='PENDING' ''',(mint,)).fetchall()
    for r in rows:
        start=ORIGINS.get((r['trade_id'],r['side']))
        if start is None:continue
        elapsed=(mono-start)*1000;late=elapsed-r['horizon_ms']
        if late<0:continue
        if late>MAX_LATE_MS:
            status='NO_FRESH_TICK';adverse=None;sample=None
        elif evidence.get('signature') and evidence.get('signature')==r['initial_signature']:
            continue # A repeat of the trigger transaction is not a new observation.
        else:
            sample=price;status='OBSERVED'
            if evidence.get('source')!=r['initial_source'] or evidence.get('quote_mint')!=r['initial_quote']:status='SOURCE_CHANGED'
            elif evidence.get('price_basis')!=r['initial_basis']:status='BASIS_CHANGED'
            elif evidence.get('clock_status')=='FUTURE':status='CLOCK_INVALID'
            elif evidence.get('clock_status')!='OK':status='TIME_UNKNOWN'
            elif evidence.get('provider_ts') is not None and r['initial_ts'] is not None and evidence['provider_ts']<r['initial_ts']:status='SOURCE_TIME_REVERSED'
            elif evidence.get('provider_lag_ms') is not None and evidence['provider_lag_ms']>MAX_AGE_MS:status='PROVIDER_STALE'
            adverse=(price/r['initial']-1)*100*(1 if r['side']=='BUY' else -1) if status=='OBSERVED' and price>0 and r['initial']>0 else None
        c.execute('''UPDATE cost_probes_v0719 SET status=?,observed_ts=?,elapsed_ms=?,lateness_ms=?,price=?,adverse_pct=?,signature=? WHERE trade_id=? AND side=? AND horizon_ms=?''',
          (status,wall,elapsed,late,sample,adverse,evidence.get('signature'),r['trade_id'],r['side'],r['horizon_ms']))

def poll(c,mono=None):
    if not WATCH:return
    mono=time.monotonic() if mono is None else mono
    for row in c.execute("SELECT * FROM cost_probes_v0719 WHERE status='PENDING'").fetchall():
        start=ORIGINS.get((row['trade_id'],row['side']))
        if start is not None and (mono-start)*1000>row['horizon_ms']+MAX_LATE_MS:
            c.execute("UPDATE cost_probes_v0719 SET status='NO_FRESH_TICK' WHERE trade_id=? AND side=? AND horizon_ms=?",(row['trade_id'],row['side'],row['horizon_ms']))
    pending={(r['trade_id'],r['side']):r['mint'] for r in c.execute("SELECT trade_id,side,mint FROM cost_probes_v0719 WHERE status='PENDING'")}
    WATCH.intersection_update(pending.values())
    for k in list(ORIGINS):
        if k not in pending:ORIGINS.pop(k,None)

def restart(c):
    c.execute("UPDATE cost_probes_v0719 SET status='INTERRUPTED' WHERE status='PENDING'")
    WATCH.clear();ORIGINS.clear()

def quantile(values,p):
    a=sorted(v for v in values if v is not None and math.isfinite(v))
    if not a:return None
    return a[int((len(a)-1)*p)]

def summarize(c):
    cols={r[1] for r in c.execute('PRAGMA table_info(cost_decisions_v0719)')}
    decisions=[dict(r) for r in c.execute('SELECT * FROM cost_decisions_v0719')] if cols else []
    probes=[dict(r) for r in c.execute('SELECT * FROM cost_probes_v0719')] if cols else []
    pairs=[dict(r) for r in c.execute('''SELECT b.trade_id,b.size,b.price entry,s.price exit,s.pnl FROM trades b JOIN trades s ON b.trade_id=s.trade_id AND s.side='SELL' WHERE b.side='BUY' ORDER BY s.ts''')]
    gross=sum(r['pnl'] for r in pairs);turnover=sum(r['size']*(1+r['exit']/r['entry']) for r in pairs)
    routes=[]
    for name,route in [('Direct protocol scenario',0),('PumpApi scenario',.0025),('PumpPortal Local scenario',.005),('PumpPortal Lightning scenario',.01)]:
        rate=.0125+route;cost=turnover*rate
        routes.append({'route':name,'pool_fee_rate':.0125,'route_fee_rate':route,'closed':len(pairs),'fee_only':cost,'gross':gross,'after_fee_only':gross-cost,'remaining_per_trade':(gross-cost)/len(pairs) if pairs else None})
    by={(r['trade_id'],r['side']):r for r in decisions};matched=[]
    for r in pairs:
        b=by.get((r['trade_id'],'BUY'));s=by.get((r['trade_id'],'SELL'))
        def usable(x):return x and x['pool']=='pump' and x['quote_mint'] in (SOL,USDC) and x['pool_fee_rate'] is not None and x['metadata_status']=='PRESENT' and x['clock_status']=='OK' and 0<=x['provider_lag_ms']<=MAX_AGE_MS
        if usable(b) and usable(s) and b['quote_mint']==s['quote_mint']:
            cost=r['size']*b['pool_fee_rate']+r['size']*r['exit']/r['entry']*s['pool_fee_rate']
            matched.append((r['pnl'],cost))
    probe_summary=[]
    for side in ('BUY','SELL'):
        for h in HORIZONS:
            rr=[r for r in probes if r['side']==side and r['horizon_ms']==h];valid=[r['adverse_pct'] for r in rr if r['status']=='OBSERVED' and r['adverse_pct'] is not None]
            states={}
            for r in rr:states[r['status']]=states.get(r['status'],0)+1
            probe_summary.append({'side':side,'horizon_ms':h,'total':len(rr),'observed':len(valid),'coverage_pct':100*len(valid)/len(rr) if rr else None,'adverse_mean_pct':sum(valid)/len(valid) if valid else None,'adverse_p90_pct':quantile(valid,.9),'states':states})
    errors=[]
    if cols:
        trade_keys={(r['trade_id'],r['side']) for r in c.execute('SELECT trade_id,side FROM trades')}
        for k in trade_keys-set(by):errors.append('Missing decision: '+str(k))
        for k in set(by)-trade_keys:errors.append('Orphan decision: '+str(k))
        horizons={}
        for r in probes:horizons.setdefault((r['trade_id'],r['side']),[]).append(r['horizon_ms'])
        for k in by:
            if sorted(horizons.get(k,[]))!=list(HORIZONS):errors.append('Probe coverage mismatch: '+str(k))
        for r in probes:
            if (r['trade_id'],r['side']) not in by:errors.append('Orphan probe')
    lags=[r['provider_lag_ms'] for r in decisions if r['clock_status']=='OK']
    ages=[r['price_age_ms'] for r in decisions]
    return {'schema':'cost_evidence_v0719','status':'ERROR' if errors else ('OK' if decisions else 'NO_EVIDENCE'),'errors':errors[:50],'error_count':len(errors),'closed':len(pairs),'gross':gross,'turnover':turnover,'break_even_oneway_rate':gross/turnover if turnover else None,'routes':routes,
      'decisions':len(decisions),'metadata_present':sum(r['metadata_status']=='PRESENT' for r in decisions),'stale_decisions':sum(r['metadata_status']=='STALE' for r in decisions),'fee_field_present':sum(r['pool_fee_rate'] is not None for r in decisions),'source_time_missing':sum(r['clock_status']=='MISSING' for r in decisions),'future_timestamps':sum(r['clock_status']=='FUTURE' for r in decisions),'provider_lag_p50_ms':quantile(lags,.5),'provider_lag_p95_ms':quantile(lags,.95),'local_price_age_p95_ms':quantile(ages,.95),'provider_priority_raw_p50':quantile([r['priority_fee_raw'] for r in decisions],.5),
      'observed_fee_subset':{'closed':len(matched),'gross':sum(x[0] for x in matched) if matched else None,'pool_fee_only':sum(x[1] for x in matched) if matched else None,'after_pool_fee_only':sum(x[0]-x[1] for x in matched) if matched else None},'probes':probe_summary,
      'limitations':'Fee scenarios assume ordinary pump bonding curve 1.25% each side; approximate PAPER turnover, no network/tip/rent/price impact. Provider-reported fees and timestamps are unverified. Probes measure subsequent feed price movement, NOT executable quotes or fill slippage. Legacy LOW/BASE/STRESS remain unchanged for comparison.'}

def check(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN');return summarize(c)

if __name__=='__main__':
    import config
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=config.DB);ap.add_argument('--out',default='cost_evidence_results.json');args=ap.parse_args()
    r=check(args.db);Path(args.out).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False,indent=2));raise SystemExit(1 if r['error_count'] else 0)
