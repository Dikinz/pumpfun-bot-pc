"""Durable, commit-bound primary entries and SELL signals for paired PAPER exits."""
import json,hashlib,time,sqlite3,math,copy
from pathlib import Path
from contextlib import closing
from decimal import Decimal,localcontext
SCHEMA='''
CREATE TABLE IF NOT EXISTS comparison_lots_v64(id INTEGER PRIMARY KEY,trade_id TEXT UNIQUE,mint TEXT,created_ts REAL,lot_hash TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS comparison_signals_v64(trade_id TEXT PRIMARY KEY,payload TEXT);
CREATE TABLE IF NOT EXISTS comparison_receipts_v64(trade_id TEXT PRIMARY KEY,lot_hash TEXT,status TEXT,applied_ts REAL,signal_status TEXT,signal_delivery_ms REAL);
'''
FEATURE_TABLES=('entry_features','entry_trajectory_v064','entry_model_snapshot_v067','cost_decisions_v0719')

def enabled(c):
    row=c.execute("SELECT v FROM meta WHERE k='matched_mode_v64'").fetchone()
    return bool(row and row[0]=='1')
def source(c):return enabled(c) and not c.execute("SELECT 1 FROM meta WHERE k='branch_role' AND v!=''").fetchone()
def one(c,sql,args=()):return dict(c.execute(sql,args).fetchone() or {})
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()
def validate_payload(p):
    lot=p['lot'];inv=p['inventory'];buy=p['buy'];order=p['order'];ledger=p['ledger'];position=p['position'];memory=p['memory_position']
    same=lambda a,b:Decimal(str(a))==Decimal(str(b))
    def check(value):
        if not value:raise ValueError()
    try:
        check(type(lot['decimals'])==int and 0<=lot['decimals']<=18)
        check(all(Decimal(str(lot[k])).is_finite() and Decimal(str(lot[k]))>0 for k in ('entry_price','size','fx','token_units')))
        check(all(x.get('trade_id')==lot['trade_id'] and x.get('mint')==lot['mint'] for x in (inv,buy,order,position)))
        check(memory['trade_id']==lot['trade_id'])
        check(all(same(x,lot['entry_price']) for x in (inv['entry_sol_price'],buy['price'],order['fill_price'],position['entry'],memory['entry'])))
        check(all(same(x,lot['size']) for x in (inv['size_usdc'],buy['size'],order['size'],position['size'],memory['size'],ledger['notional'])))
        check(all(inv[k]==lot[v] for k,v in [('sol_usdc','fx'),('fx_ts','fx_ts'),('token_units','token_units'),('raw_amount','raw_amount'),('decimals','decimals')]))
        check(order['fill_signature']==lot['entry_signature'] and order['status']=='FILLED')
        check(ledger['trade_id']==lot['trade_id'] and ledger['side']=='BUY' and ledger['total_cost']>=0)
        check(math.isclose(-ledger['cash_delta'],float(lot['size'])+ledger['total_cost'],abs_tol=1e-9))
        with localcontext() as ctx:
            ctx.prec=80;check(int(Decimal(lot['token_units'])*10**lot['decimals'])==int(lot['raw_amount'])>0)
    except (AssertionError,KeyError,ValueError,TypeError,ArithmeticError):raise RuntimeError('MATCHED_LOT_PAYLOAD_MISMATCH') from None
def insert(c,table,row):
    row={k:v for k,v in row.items() if k!='id'}
    cols={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
    if not row or not set(row)<=cols:raise RuntimeError('MATCHED_LOT_SCHEMA_MISMATCH')
    c.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))

def publish_entry(c,position,order_id,born):
    if not source(c):return
    tid=position['trade_id'];inv=one(c,'SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(tid,))
    pre=one(c,'SELECT result_json FROM preentry_v0729 WHERE order_id=?',(order_id,))
    dec=json.loads(pre.get('result_json') or '{}').get('amount_decimals')
    status='READY'
    if type(dec)!=int or not 0<=dec<=18 or not inv.get('token_units') or not inv.get('sol_usdc'):status='EXCLUDED_UNSIZED_INVENTORY'
    else:
        with localcontext() as ctx:
            ctx.prec=80;raw=int(Decimal(inv['token_units'])*10**dec)
        if not 0<raw<2**64:status='EXCLUDED_INVALID_AMOUNT'
        else:
            c.execute("UPDATE quote_inventory_v0723 SET decimals=?,raw_amount=?,status='SIZED' WHERE trade_id=?",(dec,str(raw),tid))
            inv=one(c,'SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(tid,))
    order=one(c,'SELECT * FROM paper_orders_v0720 WHERE order_id=?',(order_id,))
    lot=dict(trade_id=tid,mint=inv.get('mint'),entry_signature=order.get('fill_signature'),
      entry_price=inv.get('entry_sol_price'),size=inv.get('size_usdc'),fx=inv.get('sol_usdc'),fx_ts=inv.get('fx_ts'),
      token_units=inv.get('token_units'),raw_amount=inv.get('raw_amount'),decimals=inv.get('decimals'))
    payload=dict(lot=lot,status=status,inventory=inv,position=one(c,'SELECT * FROM positions WHERE trade_id=?',(tid,)),
      memory_position=position,born=born,buy=one(c,"SELECT * FROM trades WHERE trade_id=? AND side='BUY'",(tid,)),
      order=order,ledger=one(c,"SELECT * FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(tid,)))
    payload['feature_rows']={name:[dict(r) for r in c.execute('SELECT * FROM '+name+' WHERE trade_id=?'+(" AND side='BUY'" if name=='cost_decisions_v0719' else ''),(tid,))] for name in FEATURE_TABLES}
    c.execute('INSERT INTO comparison_lots_v64(trade_id,mint,created_ts,lot_hash,payload) VALUES(?,?,?,?,?)',
      (tid,lot['mint'],time.time(),digest(lot),json.dumps(payload,allow_nan=False)))

def publish_signal(c,tid,oid,origin,evidence):
    if not source(c) or not c.execute('SELECT 1 FROM comparison_lots_v64 WHERE trade_id=?',(tid,)).fetchone():return
    payload=dict(order=one(c,'SELECT * FROM paper_orders_v0720 WHERE order_id=?',(oid,)),origin=origin,evidence=evidence or {})
    c.execute('INSERT OR IGNORE INTO comparison_signals_v64 VALUES(?,?)',(tid,json.dumps(payload,allow_nan=False)))

def apply_entry(main,row,now=None):
    import database as db,config,paper_capital as cap
    c=db.con();now=time.time() if now is None else now;tid=row['trade_id'];p=json.loads(row['payload']);lot=p['lot']
    prior=c.execute('SELECT status,lot_hash FROM comparison_receipts_v64 WHERE trade_id=?',(tid,)).fetchone()
    if prior:
        if prior['lot_hash']!=row['lot_hash']:raise RuntimeError('MATCHED_LOT_CHANGED')
        return prior['status']
    if digest(lot)!=row['lot_hash'] or tid!=lot['trade_id'] or row['mint']!=lot['mint']:raise RuntimeError('MATCHED_LOT_HASH_MISMATCH')
    status=p['status'];mint=lot['mint']
    if status=='READY':
        validate_payload(p)
        if int(db.getm('branch_input_gap',0)):status='EXCLUDED_INPUT_GAP'
        elif not 0<=now-row['created_ts']<=1:status='EXCLUDED_LATE_ENTRY'
        elif c.execute('SELECT 1 FROM positions WHERE mint=?',(mint,)).fetchone() or len(main.P)>=config.MAX_POSITIONS:status='EXCLUDED_LOCAL_SLOT'
        elif cap.cash(c)<-p['ledger']['cash_delta']:status='EXCLUDED_LOCAL_CAPITAL'
        elif c.execute("SELECT 1 FROM trades WHERE trade_id=? AND side='BUY'",(tid,)).fetchone():status='EXCLUDED_INHERITED_ENTRY'
        elif not lot.get('entry_signature') or p['order'].get('status')!='FILLED':status='EXCLUDED_UNVERIFIED_ENTRY'
    with db.atomic():
        if status=='READY':
            for table,key in [('trades','buy'),('positions','position'),('paper_orders_v0720','order'),('capital_ledger_v0721','ledger'),('quote_inventory_v0723','inventory')]:insert(c,table,p[key])
            for table in FEATURE_TABLES:
                for feature in p.get('feature_rows',{}).get(table,[]):insert(c,table,feature)
            db.setm('cash',float(db.getm('cash'))-float(lot['size']));db.inc('entries');status='APPLIED'
        c.execute('INSERT INTO comparison_receipts_v64 VALUES(?,?,?,?,NULL,NULL)',(tid,row['lot_hash'],status,now))
    if status=='APPLIED':
        main.P[mint]=copy.deepcopy(p['memory_position']);main.ENTERED.add(mint)
        main.S.setdefault(mint,main.state());main.S[mint]['born']=p['born']
    return status

def sync(main,primary):
    import database as db,execution_model as ex,exit_audit,exit_quotes
    c=db.con();cursor=int(db.getm('matched_cursor_v64',0))
    with closing(sqlite3.connect(Path(primary).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as src:
        src.row_factory=sqlite3.Row;src.execute('BEGIN')
        rows=src.execute('SELECT * FROM comparison_lots_v64 WHERE id>? ORDER BY id LIMIT 8',(cursor,)).fetchall()
        for row in rows:
            apply_entry(main,row);db.setm('matched_cursor_v64',row['id'])
        pending=c.execute("SELECT trade_id FROM comparison_receipts_v64 WHERE status='APPLIED' AND signal_status IS NULL").fetchall()
        for item in pending:
            tid=item['trade_id'];row=src.execute('SELECT payload FROM comparison_signals_v64 WHERE trade_id=?',(tid,)).fetchone()
            if not row:continue
            p=json.loads(row[0]);order=p['order'];mint=order['mint'];q=main.P.get(mint)
            if not q or q['trade_id']!=tid:raise RuntimeError('MATCHED_SIGNAL_WITHOUT_POSITION')
            if c.execute("SELECT 1 FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL'",(tid,)).fetchone():raise RuntimeError('MATCHED_SIGNAL_ALREADY_EXISTS')
            anchor=p.get('origin');valid=type(anchor) in (int,float) and math.isfinite(anchor) and abs((time.monotonic()-anchor)-(time.time()-order['signal_ts']))<2
            with db.atomic():
                insert(c,'paper_orders_v0720',order)
                exit_quotes.on_exit(c,order['order_id'],tid,mint,order['signal_price'],order['signal_ts'])
                exit_audit.signal(c,tid,mint,order['order_id'],q,p['evidence'])
                delay=(time.time()-order['signal_ts'])*1000
                c.execute('UPDATE comparison_receipts_v64 SET signal_status=?,signal_delivery_ms=? WHERE trade_id=?',('SHARED_SIGNAL' if valid else 'CLOCK_ANCHOR_UNAVAILABLE',delay,tid))
            # The same original delay boundary is used; restart fallback stays explicit.
            ex.ORIGINS[order['order_id']]=anchor if valid else time.monotonic()

def configure(main):
    original_sell=main.sell
    main.buy=lambda *a,**kw:(False,'MATCHED_PRIMARY_ENTRY_ONLY')
    def sell(*a,**kw):
        if kw.get('_fill'):return original_sell(*a,**kw)
        return None
    main.sell=sell

def restore_clocks(c):
    """After restart retain the original shared signal, flag reset delay boundaries."""
    c.execute("UPDATE comparison_receipts_v64 SET signal_status='RESTARTED_DELAY_BOUNDARY' WHERE signal_status IS NOT NULL AND trade_id IN (SELECT trade_id FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING')")

def export(primary,folder):
    import paper_branch,route_branch,csv
    rows=[]
    with closing(sqlite3.connect(Path(primary).resolve().as_uri()+'?mode=ro',uri=True)) as src:
        src.row_factory=sqlite3.Row
        if not src.execute("SELECT 1 FROM sqlite_master WHERE name='comparison_lots_v64'").fetchone():return
        lots=src.execute('SELECT * FROM comparison_lots_v64 ORDER BY id').fetchall()
        for role,target in [('amm',paper_branch.path(primary)),('route',route_branch.path(primary))]:
            if not target.exists():continue
            with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True)) as child:
                child.row_factory=sqlite3.Row
                gap=one(child,"SELECT v FROM meta WHERE k='branch_input_gap'").get('v','0')!='0'
                for lot in lots:
                    receipt=one(child,'SELECT * FROM comparison_receipts_v64 WHERE trade_id=?',(lot['trade_id'],))
                    entry=one(src,"SELECT * FROM paper_orders_v0720 WHERE trade_id=? AND side='BUY'",(lot['trade_id'],))
                    got=one(child,"SELECT * FROM paper_orders_v0720 WHERE trade_id=? AND side='BUY'",(lot['trade_id'],))
                    a=one(src,'SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(lot['trade_id'],));b=one(child,'SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(lot['trade_id'],))
                    signal=one(src,"SELECT * FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL'",(lot['trade_id'],))
                    got_signal=one(child,"SELECT * FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL'",(lot['trade_id'],))
                    same_signal=bool(signal and got_signal and all(signal.get(k)==got_signal.get(k) for k in ('order_id','signal_ts','signal_price','signal_signature','reason','size')))
                    same=bool(receipt.get('status')=='APPLIED' and receipt.get('lot_hash')==lot['lot_hash'] and
                      all(a.get(k)==b.get(k) for k in ('mint','size_usdc','entry_sol_price','sol_usdc','fx_ts','token_units','raw_amount','decimals')) and entry.get('fill_signature')==got.get('fill_signature'))
                    closed=[bool(c.execute("SELECT 1 FROM trades WHERE trade_id=? AND side='SELL'",(lot['trade_id'],)).fetchone()) for c in (src,child)]
                    rows.append(dict(role=role,trade_id=lot['trade_id'],mint=lot['mint'],lot_hash=lot['lot_hash'],status=receipt.get('status','NOT_RECEIVED'),
                      identical_entry_lot=same,signal_status=receipt.get('signal_status'),signal_delivery_ms=receipt.get('signal_delivery_ms'),
                      identical_sell_signal=same_signal,input_integrity_ok=not gap,complete_pair=all(closed),eligible_pair=same and same_signal and not gap and all(closed) and receipt.get('signal_status')=='SHARED_SIGNAL'))
    folder=Path(folder);(folder/'matched_lots_v64.json').write_text(json.dumps(dict(rows=rows,note='Only identical entry, shared signal and both closed qualify. Different cost models remain explicit; not a profitability claim.'),indent=2),encoding='utf-8')
    if rows:
        with (folder/'matched_lots_v64.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,list(rows[0]));w.writeheader();w.writerows(rows)
    return rows
