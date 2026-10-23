"""Run boundaries use trade sequence IDs, never wall-clock filters. Read-only metrics."""
import json,time,uuid
import paper_capital
SCHEMA="""CREATE TABLE IF NOT EXISTS runs_v45(id TEXT PRIMARY KEY, started_ts REAL, updated_ts REAL, elapsed_s REAL, status TEXT, first_id INTEGER, last_id INTEGER, start_json TEXT, result_json TEXT, clock_json TEXT);"""
ACTIVE=None
ORIGIN=None
LAST=0.
def dumps(x):return json.dumps(x,ensure_ascii=False,allow_nan=False)
def max_id(c):return c.execute('SELECT COALESCE(MAX(id),0) FROM trades').fetchone()[0]
def balance(c):
    cash=paper_capital.cash(c)
    reserved=c.execute("SELECT COALESCE(SUM(reserved_total),0) FROM paper_orders_v0720 WHERE side='BUY' AND status='PENDING'").fetchone()[0]
    positions=[dict(r) for r in c.execute('SELECT trade_id,mint,size FROM positions')]
    return dict(cash=cash,available=max(0,cash-reserved),reserved=reserved,positions=positions,open_principal=sum(r['size'] for r in positions))
def cohorts(c,first,last,start,end):
    rows=[dict(r) for r in c.execute('SELECT * FROM trades WHERE id>? AND id<=? ORDER BY id',(first,last))]
    carry={r['trade_id'] for r in start.get('positions',[])}
    new={r['trade_id'] for r in rows if r['side']=='BUY'}
    groups={}
    for label,ids in (('carried',carry),('new',new)):
        selected=[r for r in rows if r['trade_id'] in ids];sells=[r for r in selected if r['side']=='SELL']
        missing=[];gross=sum(r['pnl'] for r in sells);lifecycle=0.;costs=0.;delta=0.;before=0.
        for r in selected:
            fee=c.execute('SELECT total_cost,cash_delta FROM capital_ledger_v0721 WHERE trade_id=? AND side=?',(r['trade_id'],r['side'])).fetchone()
            if fee is None:missing.append(r['trade_id']+':'+r['side'])
            else:costs+=fee[0];delta+=fee[1]
        for r in sells:
            pair=list(c.execute('SELECT side,total_cost FROM capital_ledger_v0721 WHERE trade_id=?',(r['trade_id'],)))
            if len(pair)!=2 or {x['side'] for x in pair}!={'BUY','SELL'}:missing.append(r['trade_id']+':PAIR')
            lifecycle+=sum(x['total_cost'] for x in pair)
            if label=='carried':before+=sum(x['total_cost'] for x in pair if x['side']=='BUY')
        opened=[r for r in end.get('positions',[]) if r['trade_id'] in ids]
        groups[label]=dict(positions_at_start=len(carry) if label=='carried' else 0,
            entries=sum(r['side']=='BUY' for r in selected),closed=len(sells),still_open=len(opened),
            open_principal=sum(r['size'] for r in opened),gross_closed=gross,
            lifecycle_costs_closed=lifecycle,net_closed=None if missing else gross-lifecycle,
            costs_in_run=costs,costs_before_run_for_closed=before,cash_change_in_run=None if missing else delta,accounting_missing=missing)
    unknown=sorted({r['trade_id'] for r in rows}-carry-new)
    return dict(groups,unclassified_trade_ids=unknown,
        note='Границы по ID сделок. Перенесённые — позиции на старте; новые — BUY внутри запуска. PNL закрытий включает расходы полного цикла. Денежное изменение запуска — отдельная величина; расходы входа перенесённых позиций оплачены раньше.')

def metrics(c,first,last,start):
    rows=[dict(r) for r in c.execute('SELECT * FROM trades WHERE id>? AND id<=? ORDER BY id',(first,last))]
    buys=[r for r in rows if r['side']=='BUY'];sells=[r for r in rows if r['side']=='SELL'];buyids={r['trade_id'] for r in buys}
    costs=0.;delta=0.;net=0.;missing=[]
    for r in rows:
        fee=c.execute('SELECT total_cost,cash_delta FROM capital_ledger_v0721 WHERE trade_id=? AND side=?',(r['trade_id'],r['side'])).fetchone()
        if fee is None:missing.append(r['trade_id']+':'+r['side'])
        else:costs+=fee[0];delta+=fee[1]
        if r['side']=='SELL':
            pair=c.execute('SELECT COUNT(*),COALESCE(SUM(total_cost),0) FROM capital_ledger_v0721 WHERE trade_id=?',(r['trade_id'],)).fetchone()
            if pair[0]!=2:missing.append(r['trade_id']+':PAIR')
            net+=r['pnl']-pair[1]
    end=balance(c)
    return dict(entries=len(buys),closed=len(sells),carried_closed=sum(r['trade_id'] not in buyids for r in sells),
      entered_still_open=sum(r['trade_id'] in buyids for r in end['positions']),gross_closed=sum(r['pnl'] for r in sells),
      net_closed=None if missing else net,costs_in_run=costs,cash_change=delta,accounting_missing=missing,
      start=start,end=end,first_id=first,last_id=last,cohorts=cohorts(c,first,last,start,end),
      note='PNL: full lifecycle costs of positions closed in this run, including carried entries. Costs in run: fills during this run. Cash change includes purchases and is not PNL. Open principal is not recoverable value.')
def begin(c,clock=None):
    global ACTIVE,ORIGIN,LAST
    c.executescript(SCHEMA)
    # Keep the last confirmed snapshot after a crash; do not invent an exact stop time.
    for old in c.execute("SELECT * FROM runs_v45 WHERE status='RUNNING'").fetchall():
        last=max_id(c);recovered=metrics(c,old['first_id'],last,json.loads(old['start_json']))
        recovered['recovered_after_interruption']=True
        c.execute("UPDATE runs_v45 SET status='INTERRUPTED',last_id=?,result_json=? WHERE id=?",(last,dumps(recovered),old['id']))
    ACTIVE=uuid.uuid4().hex;ORIGIN=time.monotonic();LAST=0.;now=time.time();start=balance(c);n=max_id(c)
    c.execute('INSERT INTO runs_v45 VALUES(?,?,?,?,?,?,?,?,?,?)',(ACTIVE,now,now,0.,'RUNNING',n,n,dumps(start),dumps(metrics(c,n,n,start)),dumps(clock)))
    return ACTIVE
def beat(c,status='RUNNING',force=False):
    global LAST
    if ACTIVE is None:return
    mono=time.monotonic()
    if not force and status=='RUNNING' and mono-LAST<5:return
    r=c.execute('SELECT * FROM runs_v45 WHERE id=?',(ACTIVE,)).fetchone()
    if r is None:return
    n=max_id(c);result=metrics(c,r['first_id'],n,json.loads(r['start_json']))
    c.execute('UPDATE runs_v45 SET updated_ts=?,elapsed_s=?,status=?,last_id=?,result_json=? WHERE id=?',(time.time(),max(0,mono-ORIGIN),status,n,dumps(result),ACTIVE));LAST=mono

def summary(c,limit=100):
    rows=[]
    for r in c.execute('SELECT * FROM runs_v45 ORDER BY rowid DESC LIMIT ?', (limit,)):
        r=dict(r);r['result']=json.loads(r.pop('result_json'));
        if 'cohorts' not in r['result']:
            start=json.loads(r['start_json']);r['result']['cohorts']=cohorts(c,r['first_id'],r['last_id'],start,r['result']['end'])
        r['clock']=json.loads(r.pop('clock_json'));r.pop('start_json')
        if r['status']=='RUNNING' and not 0<=time.time()-r['updated_ts']<=15:r['status']='NO_HEARTBEAT'
        rows.append(r)
    return dict(current=rows[0] if rows else None,rows=rows,limit=limit,total=c.execute('SELECT COUNT(*) FROM runs_v45').fetchone()[0],
      note='Each stopped run is frozen. Interrupted runs show the last confirmed snapshot; inspect raw trades for a possible tail after the last heartbeat. Legacy trades before the first run remain outside run totals.')
