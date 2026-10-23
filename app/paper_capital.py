"""Net PAPER ledger. Provider pool rate + explicit modeled service/fixed costs.
Fixed cost is a scenario budget, NOT measured network fees. No SOL/USD oracle.
"""
import json,sqlite3,math,time,argparse
from pathlib import Path
SCHEMA='''CREATE TABLE IF NOT EXISTS capital_settings_v0721(k TEXT PRIMARY KEY,v REAL);
CREATE TABLE IF NOT EXISTS capital_ledger_v0721(trade_id TEXT,side TEXT,ts REAL,notional REAL,pool_rate REAL,pool_fee REAL,route_fee REAL,fixed_cost REAL,total_cost REAL,cash_delta REAL,rate_source TEXT,PRIMARY KEY(trade_id,side));'''

def init(c,start):
    c.executemany('INSERT OR IGNORE INTO capital_settings_v0721 VALUES(?,?)',[('start',start),('route_rate',.0025),('fixed_per_fill',.005),('entry_pool_cap',.03)])

def settings(c):return {r[0]:float(r[1]) for r in c.execute('SELECT k,v FROM capital_settings_v0721')}
def cash(c):return settings(c)['start']+float(c.execute('SELECT COALESCE(SUM(cash_delta),0) FROM capital_ledger_v0721').fetchone()[0])
def budget(c,size):
    s=settings(c);return size*(1+s['entry_pool_cap']+s['route_rate'])+s['fixed_per_fill']
def quote(c,notional,e,side):
    s=settings(c);rate=(e or {}).get('pool_fee_rate');known=isinstance(rate,(int,float)) and math.isfinite(rate) and 0<=rate<=.20
    if side=='BUY' and (not known or rate>s['entry_pool_cap']):return None
    # Missing exit rate must never prevent risk reduction; keep an explicit conservative assumption.
    source='provider' if known else 'fallback_20pct';rate=float(rate) if known else .20
    pool=notional*rate;route=notional*s['route_rate'];fixed=s['fixed_per_fill']
    return {'pool_rate':rate,'pool_fee':pool,'route_fee':route,'fixed_cost':fixed,'total_cost':pool+route+fixed,'rate_source':source}
def record(c,tid,side,notional,e):
    if side=='SELL' and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='branch_amm_settlements'").fetchone():
        a=c.execute('SELECT gross,receipt,cost FROM branch_amm_settlements WHERE trade_id=?',(tid,)).fetchone()
        if a:
            if not math.isclose(notional,a['gross'],rel_tol=1e-9) or a['cost']<0:raise RuntimeError('AMM_LEDGER_MISMATCH')
            c.execute('INSERT INTO capital_ledger_v0721 VALUES(?,?,?,?,?,?,?,?,?,?,?)',(tid,side,time.time(),notional,0,0,0,a['cost'],a['cost'],a['receipt'],'AMM_VERIFIED_ISOLATED'))
            return
    q=quote(c,notional,e,side)
    if q is None:raise RuntimeError('Unbudgeted entry fee')
    delta=(-notional if side=='BUY' else notional)-q['total_cost']
    c.execute('INSERT INTO capital_ledger_v0721 VALUES(?,?,?,?,?,?,?,?,?,?,?)',(tid,side,time.time(),notional,q['pool_rate'],q['pool_fee'],q['route_fee'],q['fixed_cost'],q['total_cost'],delta,q['rate_source']))

def rebalance(c):
    pending=[dict(r) for r in c.execute("SELECT order_id,reserved_total FROM paper_orders_v0720 WHERE side='BUY' AND status='PENDING' ORDER BY signal_ts DESC")]
    total=sum(r['reserved_total'] for r in pending);available=max(0,cash(c))
    for r in pending:
        if total<=available+1e-9:break
        c.execute("UPDATE paper_orders_v0720 SET status='CANCELLED_CAPITAL',note='Released after capital changed' WHERE order_id=?",(r['order_id'],))
        total-=r['reserved_total']

def summary(c):
    s=settings(c);rows=[dict(r) for r in c.execute('SELECT * FROM capital_ledger_v0721')];trades={(r['trade_id'],r['side']):dict(r) for r in c.execute('SELECT * FROM trades')};errors=[]
    keys={(r['trade_id'],r['side']) for r in rows}
    if keys!=set(trades):errors.append('Ledger/trade coverage mismatch')
    for r in rows:
        t=trades.get((r['trade_id'],r['side']));b=trades.get((r['trade_id'],'BUY'))
        expected=b['size'] if b and r['side']=='BUY' else b['size']*t['price']/b['price'] if b and t else None
        close=lambda a,b: a is not None and b is not None and math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-9)
        if not close(expected,r['notional']):errors.append('Notional mismatch')
        if not close(r['total_cost'],r['pool_fee']+r['route_fee']+r['fixed_cost']):errors.append('Cost sum mismatch')
        special=r['rate_source'] in ('AMM_VERIFIED_ISOLATED','CURVE_VERIFIED_ISOLATED')
        if special:
            try:a=c.execute('SELECT gross,receipt,cost FROM branch_amm_settlements WHERE trade_id=?',(r['trade_id'],)).fetchone()
            except sqlite3.Error:a=None
            if not a or r['side']!='SELL' or not close(r['notional'],a['gross']) or not close(r['total_cost'],a['cost']) or not close(r['cash_delta'],a['receipt']) or not close(a['gross']-a['receipt'],a['cost']):errors.append('AMM settlement mismatch')
        elif not close(r['pool_fee'],r['notional']*r['pool_rate']) or not close(r['route_fee'],r['notional']*s['route_rate']) or not close(r['fixed_cost'],s['fixed_per_fill']):errors.append('Fee arithmetic mismatch')
        if not close(r['cash_delta'],(-r['notional'] if r['side']=='BUY' else r['notional'])-r['total_cost']):errors.append('Cash delta mismatch')
    net=cash(c);pending=c.execute("SELECT COALESCE(SUM(reserved_total),0) FROM paper_orders_v0720 WHERE side='BUY' AND status='PENDING'").fetchone()[0]
    if pending>max(0,net)+1e-8:errors.append('Entry reservations exceed net cash')
    positions=[dict(r) for r in c.execute('SELECT * FROM positions')];principal=sum(p['size'] for p in positions)
    total=sum(r['total_cost'] for r in rows)
    gross_cash=float(c.execute("SELECT v FROM meta WHERE k='cash'").fetchone()[0])
    if not math.isclose(net,gross_cash-total,rel_tol=1e-9,abs_tol=1e-9):errors.append('Gross/net reconciliation mismatch')
    for o in c.execute("SELECT size,reserved_total FROM paper_orders_v0720 WHERE side='BUY' AND status='PENDING'"):
        if not math.isclose(o['reserved_total'],budget(c,o['size']),rel_tol=1e-9,abs_tol=1e-9):errors.append('Wrong entry reservation')
    closed={tid for tid,side in trades if side=='SELL'};gross=sum(t['pnl'] for (tid,side),t in trades.items() if side=='SELL')
    closed_cost=sum(r['total_cost'] for r in rows if r['trade_id'] in closed)
    # Display-only freshness: cash and order budgets never include open marks.
    now=time.time();fresh=[];excluded=[]
    for p in positions:
        stamp=p.get('last_update_ts');price=p.get('current');entry=p.get('entry')
        valid=all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (stamp,price,entry))
        if valid and 0<=now-stamp<=1.0 and price>0 and entry>0:fresh.append(p)
        else:excluded.append(p)
    marked=sum(p['size']*p['current']/p['entry'] for p in fresh)
    valuation=dict(max_tick_age_s=1.0,priced_count=len(fresh),excluded_count=len(excluded),
        excluded_principal=sum(p['size'] for p in excluded),complete=not excluded,
        fresh_open_mark=marked,fresh_subtotal_before_exit_cost=net+marked,
        note='Доступно для входов только available: деньги минус резерв. Открытые позиции не являются доступными средствами. Полная оценка неизвестна при устаревшей/неизвестной цене; свежая цена также не гарантирует продажу.')
    return {'status':'ERROR' if errors else 'OK','errors':errors,'settings':s,'net_cash':net,'reserved':pending,'available':net-pending,'open_principal':principal,'total_cost':total,'pool_cost':sum(r['pool_fee'] for r in rows),'route_cost':sum(r['route_fee'] for r in rows),'fixed_cost':sum(r['fixed_cost'] for r in rows),'closed_net':gross-closed_cost,'open_entry_cost':total-closed_cost,'marked_equity_before_exit_cost':net+marked if not excluded else None,'valuation':valuation,'cash_if_open_value_zero':net,'missing_exit_rate_count':sum(r['rate_source'] not in ('provider','AMM_VERIFIED_ISOLATED','CURVE_VERIFIED_ISOLATED') for r in rows),'note':'PAPER-$ scenario only. Pool fee from unverified feed; service .25% + .005 per fill are fixed assumptions. Cash excludes spent entry costs; open marks are not guaranteed proceeds and exclude future exit fees. No rent, impact, SOL/USD conversion or failed-network-transaction costs.'}

def check(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN');return summary(c)
if __name__=='__main__':
    import config
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=config.DB);ap.add_argument('--out',default='capital_results.json');a=ap.parse_args();r=check(a.db)
    Path(a.out).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False,indent=2));raise SystemExit(1 if r['errors'] else 0)
