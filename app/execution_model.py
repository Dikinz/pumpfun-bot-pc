"""Durable delayed PAPER orders. Feed-tick proxy only, never exchange fills."""
import time,json,uuid,sqlite3,argparse,math
from pathlib import Path
from collections import deque
import cost_evidence as ce
import time_order_audit as ta
import paper_capital,entry_activity,exit_quotes
DELAY=.250
BUY_TIMEOUT=2.0
MAX_PRICE_AGE=1.0
ORIGINS={}
SCHEMA='''
CREATE TABLE IF NOT EXISTS paper_orders_v0720(
 order_id TEXT PRIMARY KEY,mint TEXT NOT NULL,side TEXT NOT NULL,status TEXT NOT NULL,
 signal_ts REAL NOT NULL,signal_price REAL,signal_signature TEXT,signal_provider_ts REAL,
 size REAL,score INTEGER,snapshot TEXT,reason TEXT,trade_id TEXT,
 fill_ts REAL,fill_price REAL,fill_signature TEXT,fill_provider_ts REAL,
 wait_ms REAL,price_age_ms REAL,provider_lag_ms REAL,note TEXT,reserved_total REAL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS paper_one_pending ON paper_orders_v0720(mint,side) WHERE status='PENDING';
CREATE INDEX IF NOT EXISTS paper_status ON paper_orders_v0720(status,mint);
'''

SCHEMA+=ta.SCHEMA

def fresh(e,mono=None):
    mono=time.monotonic() if mono is None else mono
    if not e:return False
    vals=[e.get('received_mono'),e.get('provider_lag_ms'),e.get('provider_ts')]
    if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in vals):return False
    return (e.get('source')=='pumpapi_free' and e.get('pool')=='pump' and e.get('quote_mint')==ce.SOL
      and e.get('price_basis')=='provider_price' and bool(e.get('signature')) and e.get('clock_status')=='OK'
      and 0<=mono-e['received_mono']<=MAX_PRICE_AGE and 0<=e['provider_lag_ms']<=MAX_PRICE_AGE*1000)

def pending(c,mint,side):
    return c.execute("SELECT * FROM paper_orders_v0720 WHERE mint=? AND side=? AND status='PENDING'",(mint,side)).fetchone()

def reserve(c,exclude=None):
    r=c.execute("SELECT COUNT(*),COALESCE(SUM(reserved_total),0) FROM paper_orders_v0720 WHERE side='BUY' AND status='PENDING' AND order_id<>?",(exclude or '',)).fetchone()
    return r[0],r[1]

def request(c,mint,side,price,size,reason,evidence,score=0,snapshot=None,trade_id=None):
    old=pending(c,mint,side)
    if old:return old['order_id']
    oid=uuid.uuid4().hex;wall=time.time();mono=time.monotonic();e=evidence or {}
    # Serialize the pre-signal features once. No future tick is used to relabel them.
    snap=dict(snapshot or {})
    snap['_recovery_v75']=__import__('recovery_gate').stamp({})
    data=json.dumps(snap,default=lambda x:list(x) if isinstance(x,(set,deque,tuple)) else str(x))
    c.execute('''INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,signal_signature,signal_provider_ts,size,score,snapshot,reason,trade_id,note) VALUES(?,?,?,'PENDING',?,?,?,?,?,?,?,?,?,?)''',
      (oid,mint,side,wall,price,e.get('signature'),e.get('provider_ts'),size,score,data,reason,trade_id,'tick proxy; no live order'))
    if side=='BUY':
        c.execute('UPDATE paper_orders_v0720 SET reserved_total=? WHERE order_id=?',(paper_capital.budget(c,size),oid))
        entry_activity.record(c,oid,snapshot,wall)
    if side=='SELL':exit_quotes.on_exit(c,oid,trade_id,mint,price,wall)
    ORIGINS[oid]=mono
    ta.add(c,oid,"lifecycle","CREATED",e,wall)
    return oid

def eligible(order,e,mono=None):
    import recovery_gate
    if not recovery_gate.allowed():return False
    if order['side']=='BUY' and not recovery_gate.allowed(json.loads(order['snapshot']).get('_recovery_v75',{})):return False
    if not recovery_gate.allowed(e or {}):return False
    mono=time.monotonic() if mono is None else mono
    start=ORIGINS.get(order['order_id'])
    if start is None or not fresh(e,mono):return False
    # The event must be received after the delay, not merely processed later.
    elapsed=e['received_mono']-start
    if elapsed<DELAY or (order['side']=='BUY' and elapsed>BUY_TIMEOUT):return False
    return e['signature']!=order['signal_signature'] and (order['signal_provider_ts'] is None or e['provider_ts']>=order['signal_provider_ts'])

def filled(c,oid,trade_id,price,e):
    row=c.execute('SELECT * FROM paper_orders_v0720 WHERE order_id=?',(oid,)).fetchone()
    if not row or row['status']!='PENDING':raise RuntimeError('Order is not pending')
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='branch_amm_settlements' AND type='table'").fetchone():
        amm=c.execute('SELECT check_id,available_ts,details FROM branch_amm_settlements WHERE trade_id=?',(trade_id,)).fetchone()
        if amm and row['side']=='SELL':
            c.execute("UPDATE paper_orders_v0720 SET status='FILLED',trade_id=?,fill_ts=?,fill_price=?,fill_signature=NULL,fill_provider_ts=NULL,wait_ms=?,note=? WHERE order_id=?",(trade_id,time.time(),price,(amm['available_ts']-row['signal_ts'])*1000,{'PUMP_CURVE':'LOCAL_CURVE_VERIFIED_NO_BROADCAST','DRAINED_CURVE_WRITE_OFF':'DRAINED_CURVE_WRITE_OFF_NO_SALE','PAPER_INVENTORY_CURVE_FLOOR':'PAPER_INVENTORY_CURVE_FLOOR_NO_SALE'}.get(json.loads(amm['details']).get('route'),'LOCAL_AMM_VERIFIED_NO_BROADCAST'),oid))
            return
    if not eligible(row,e):raise RuntimeError('Fill evidence is ineligible')
    c.execute("""UPDATE paper_orders_v0720 SET status='FILLED',trade_id=?,fill_ts=?,fill_price=?,fill_signature=?,fill_provider_ts=?,wait_ms=?,price_age_ms=?,provider_lag_ms=? WHERE order_id=?""",
       (trade_id,time.time(),price,e['signature'],e['provider_ts'],(e['received_mono']-ORIGINS[oid])*1000,(time.monotonic()-e['received_mono'])*1000,e['provider_lag_ms'],oid))

    ta.add(c,oid,'lifecycle','FILLED',e)
    if row['side']=='SELL':
        import exit_comparison
        exit_comparison.record_tick(c,oid)

def expire(c,mono=None):
    mono=time.monotonic() if mono is None else mono
    for r in c.execute("SELECT order_id FROM paper_orders_v0720 WHERE side='BUY' AND status='PENDING'").fetchall():
        start=ORIGINS.get(r[0])
        if start is not None and mono-start>BUY_TIMEOUT:
            ta.add(c,r[0],'lifecycle','EXPIRED')
            c.execute("UPDATE paper_orders_v0720 SET status='EXPIRED',note='No eligible tick within 2 seconds' WHERE order_id=?",(r[0],))
    active={r[0] for r in c.execute("SELECT order_id FROM paper_orders_v0720 WHERE status='PENDING'")}
    for oid in list(ORIGINS):
        if oid not in active:ORIGINS.pop(oid,None)

def cancel_unfilled_buys_on_stop(c,reason='RUN_SHUTDOWN'):
    """Atomic status/reservation/audit update; no trades, positions or cash writes.

    Caller must quiesce order producers first. SAVEPOINT also works inside the
    caller's final shutdown transaction; no implicit outer commit or RAM edits.
    """
    stamp=time.time();cancelled=[]
    c.execute('SAVEPOINT stop_buys_v71')
    try:
        rows=c.execute("SELECT order_id,reserved_total FROM paper_orders_v0720 WHERE side='BUY' AND status='PENDING'").fetchall()
        for row in rows:
            oid=row['order_id'];released=row['reserved_total']
            note=json.dumps(dict(code='CANCELLED_STOP',reason=reason,ts=stamp,released_reservation=released),allow_nan=False)
            changed=c.execute("UPDATE paper_orders_v0720 SET status='CANCELLED_STOP',reserved_total=0,note=? WHERE order_id=? AND side='BUY' AND status='PENDING'",(note,oid)).rowcount
            if changed:
                ta.add(c,oid,'lifecycle','CANCELLED_STOP',now=stamp)
                cancelled.append(oid)
        c.execute('RELEASE stop_buys_v71')
    except BaseException:
        c.execute('ROLLBACK TO stop_buys_v71');c.execute('RELEASE stop_buys_v71')
        raise
    return cancelled


def restart(c):
    for r in c.execute("SELECT order_id FROM paper_orders_v0720 WHERE side='BUY' AND status='PENDING'").fetchall():ta.add(c,r[0],'lifecycle','CANCELLED_RESTART')
    c.execute("UPDATE paper_orders_v0720 SET status='CANCELLED_RESTART',note='Unfilled entry cancelled; reservation released' WHERE side='BUY' AND status='PENDING'")
    ORIGINS.clear()
    for r in c.execute("SELECT order_id FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING'"):
        ORIGINS[r[0]]=time.monotonic() # new delay; keep exposure and original trigger

def summary(c):
    orders=[dict(r) for r in c.execute('SELECT * FROM paper_orders_v0720')]
    trades={(r['trade_id'],r['side']):dict(r) for r in c.execute('SELECT * FROM trades')}
    errors=[];counts={};seen=set()
    for o in orders:
        key=o['side']+'_'+o['status'];counts[key]=counts.get(key,0)+1
        if o['status']=='FILLED':
            k=(o['trade_id'],o['side']);t=trades.get(k)
            if k in seen:errors.append('Duplicate filled order for trade side')
            seen.add(k)
            if not t or t['mint']!=o['mint'] or not math.isclose(t['price'],o['fill_price'],rel_tol=1e-10):errors.append('Order/trade mismatch')
            if o['wait_ms'] is None or o['wait_ms']<DELAY*1000-1e-6 or (o['side']=='BUY' and o['wait_ms']>BUY_TIMEOUT*1000+1e-6):errors.append('Invalid fill timing')
            if not o['fill_signature'] or o['fill_signature']==o['signal_signature']:errors.append('Fill reused signal transaction')
            if o['price_age_ms'] is None or not 0<=o['price_age_ms']<=MAX_PRICE_AGE*1000:errors.append('Stale fill')
            if o['provider_lag_ms'] is None or not 0<=o['provider_lag_ms']<=MAX_PRICE_AGE*1000:errors.append('Stale provider fill')
        if o['status']=='PENDING' and o['side']=='SELL':
            if (o['trade_id'],'BUY') not in trades or (o['trade_id'],'SELL') in trades:errors.append('Pending exit without open trade')
    if set(trades)-seen:errors.append('Trade without filled order')
    pos=[dict(r) for r in c.execute('SELECT * FROM positions')]
    cash=float(c.execute("SELECT v FROM meta WHERE k='cash'").fetchone()[0])
    n,reserved=reserve(c)
    if reserved>max(0,paper_capital.cash(c))+1e-8:errors.append('Reservations exceed net paper cash')
    waiting=[]
    for o in orders:
        if o['status']=='PENDING':waiting.append({k:o[k] for k in ('order_id','trade_id','mint','side','signal_ts','signal_price','reason','size')})
    return {'status':'ERROR' if errors else ('OK' if orders else 'NO_DATA'),'errors':errors,'counts':counts,'closed':sum(k[1]=='SELL' for k in trades),'cash_before_costs':cash,'entry_reserved':reserved,'cash_available_before_costs':cash-reserved,'open_positions':len(pos),'open_principal':sum(x['size'] for x in pos),'pending':waiting,'positions':[{'mint':x['mint'],'trade_id':x['trade_id'],'size':x['size'],'last_price':x['current'],'last_price_age_s':max(0,time.time()-x['last_update_ts'])} for x in pos], 'limits':{'delay_ms':DELAY*1000,'entry_timeout_ms':BUY_TIMEOUT*1000,'max_age_ms':MAX_PRICE_AGE*1000,'quote':ce.SOL},'note':'Only delayed fresh-tick PAPER proxy, not an executable quote. Unresolved exits retain capital and slots. Cash and marks are before costs; last price does not prove liquidation value.'}

def check(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN');return summary(c)
if __name__=='__main__':
    import config
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=config.DB);ap.add_argument('--out',default='execution_model_results.json');a=ap.parse_args();r=check(a.db)
    Path(a.out).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False,indent=2));raise SystemExit(1 if r['errors'] else 0)
