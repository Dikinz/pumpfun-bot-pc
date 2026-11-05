"""Independent prospective PAPER exit on the actual verified pool route.

No tick SELL fallback. Empty reserves never create proceeds or release a slot.
"""
import asyncio,json,sqlite3,time,sys,os
from pathlib import Path
from contextlib import closing
from decimal import Decimal,localcontext,InvalidOperation
import paper_branch as common
SCHEMA=common.SCHEMA+'''CREATE TABLE IF NOT EXISTS route_exit_decisions_v59(
 check_id TEXT PRIMARY KEY,trade_id TEXT,mint TEXT,available_ts REAL,decision TEXT,route TEXT);
'''

def path(primary):
    p=Path(primary).resolve();return p.with_name(p.stem+'_route_paper.db')

def seed(primary):
    target=common.seed(primary,path(primary))
    with closing(sqlite3.connect(target)) as c:
        c.executescript(SCHEMA)
        with c:
            c.execute("INSERT OR IGNORE INTO branch_info VALUES('route_fork_trade_id',?)",(str(c.execute('SELECT COALESCE(MAX(id),0) FROM trades').fetchone()[0]),))
            c.execute("INSERT OR IGNORE INTO branch_info VALUES('route_carried_ids',?)",(json.dumps([r[0] for r in c.execute('SELECT trade_id FROM positions')]),))
    return target

def configure():
    import execution_model as execution
    original=execution.eligible
    # BUYs keep original gates. SELL signals persist until a verified settlement.
    execution.eligible=lambda order,e,mono=None:False if order['side']=='SELL' else original(order,e,mono)

COST_MODEL='ROUTE_WALLET_MODEL_V1016'
CLOSE_FEE_LAMPORTS=5000  # one signature to close the emptied token account and take its rent back

def wallet_account(c,account,route):
    """v0.7.101.6: the ledger follows one persistent PAPER wallet instead of a fresh wallet per sale.
    The token account rent comes back when the emptied account is closed (minus that transaction's fee);
    the volume account is paid by the first sale of this model on a route and kept for the later ones.
    The strict isolated values stay in the record (strict_*) for the sensitivity report."""
    retained=c.execute("SELECT 1 FROM sqlite_master WHERE name='branch_amm_settlements'").fetchone() is not None and c.execute("SELECT 1 FROM branch_amm_settlements WHERE json_extract(details,'$.accounting.cost_model')=? AND json_extract(details,'$.route')=? LIMIT 1",(COST_MODEL,route)).fetchone() is not None
    net=int(account['seller_net_lamports']);volume=int(account['volume_rent_lamports'] or 0)
    lamports=net-CLOSE_FEE_LAMPORTS+(volume if retained else 0)
    with localcontext() as ctx:
        ctx.prec=60
        fx=Decimal(account['fx']);receipt=Decimal(lamports)*fx/10**9
        pnl=receipt-Decimal(account['size_units'])-Decimal(account['entry_cost_units'])
    return dict(account,cost_model=COST_MODEL,strict_receipt_units=account['receipt_units'],strict_pnl_units=account['pnl_units'],
        receipt_units=str(receipt),pnl_units=str(pnl),wallet_receipt_lamports=lamports,close_fee_lamports=CLOSE_FEE_LAMPORTS,
        token_rent_refunded_lamports=int(account['token_rent_lamports']),volume_rent_retained_lamports=volume if retained else 0,
        note='Persistent PAPER wallet: token rent refunded on close minus one close fee; volume account paid once per route. '
            'Refund and wallet lifecycle are modelled, not proven on chain; priority/landing costs unknown. Strict isolated values in strict_*.')

WRITE_OFF='DRAINED_CURVE_WRITE_OFF'
WRITE_OFF_COST_MODEL='ROUTE_WRITE_OFF_V1017'
WRITE_OFF_AFTER_S=600.
WRITE_OFF_MIN_CHECKS=3
NO_EVIDENCE=('STALE_OR_PRE_SIGNAL','INVALID_TIMES','RECOVERY_INVALIDATED','INVALID_PAYLOAD')

def drained_streak(c,tid,signal,available,table='route_exit_decisions_v59'):
    """v0.7.101.7: unbroken run of fresh empty-curve results after the exit signal (this one included).
    Results without usable evidence neither extend nor break it. Returns (first available_ts, count).
    v0.7.101.8: PRIMARY keeps the same history in its own table (primary_write_off)."""
    start=available;n=1
    for ts,decision in c.execute('SELECT available_ts,decision FROM '+table+' WHERE trade_id=? AND available_ts>=? AND available_ts<? ORDER BY available_ts DESC',(tid,signal,available)):
        if decision in NO_EVIDENCE:continue
        if decision!='INSUFFICIENT_LIQUIDITY_OPEN':break
        start=ts;n+=1
    return start,n

def write_off_account(c,row,job,result,start,checks,cost_model=WRITE_OFF_COST_MODEL):
    """A drained curve cannot pay for the tokens (real SOL below the sale amount): zero receipt, the whole entry is lost.
    quote_received_lamports is the real-reserve upper bound (at least 1 lamport for the ledger price), not a sale.
    02.11–03.11.2026: two such positions held both ROUTE slots for 7–9 h and the branch stopped trading."""
    inv=job.get('inventory') or {};reserves=result.get('reserves') or {}
    if job.get('trade_id')!=row['trade_id'] or inv.get('trade_id')!=row['trade_id'] or inv.get('mint')!=row['mint']:return None
    entry=c.execute("SELECT notional,total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(row['trade_id'],)).fetchone()
    spendable=reserves.get('spendable_lamports');upper=reserves.get('sell_gross_upper_lamports')
    if not entry or type(spendable)!=int or type(upper)!=int or not 0<=spendable<upper:return None
    with localcontext() as ctx:
        ctx.prec=60
        size=Decimal(str(inv['size_usdc']));fx=Decimal(str(inv['sol_usdc']));cost=Decimal(str(entry[1]))
        if not all(v.is_finite() for v in (size,fx,cost)) or size<=0 or fx<=0 or size!=Decimal(str(entry[0])):return None
        pnl=-size-cost
    return dict(cost_model=cost_model,receipt_units='0',pnl_units=str(pnl),strict_receipt_units='0',strict_pnl_units=str(pnl),
        size_units=str(size),entry_cost_units=str(cost),fx=str(fx),quote_received_lamports=max(1,spendable),
        real_sol_lamports=reserves.get('real_sol_lamports'),spendable_lamports=spendable,sell_gross_upper_lamports=upper,
        drained_since_ts=start,drained_checks=checks,
        note='Curve real SOL below the sale amount on every fresh check for at least %d s after the exit signal: tokens unsellable. '
            'Written off with zero receipt; the token account keeps the tokens, so no rent refund.'%WRITE_OFF_AFTER_S)

def unusable(row,job,result,signal,now):
    """Why a monitor result cannot be used for this exit signal now (None when it can)."""
    import signal_exit as se
    if not __import__('recovery_gate').allowed(job):return 'RECOVERY_INVALIDATED'
    f=se.finite;start=row.get('started_ts');end=row.get('finished_ts');snap=result.get('snapshot_received_ts');scheduled=job.get('scheduled_ts')
    if not all(f(x) for x in (start,end,snap,signal,now,scheduled)):return 'INVALID_TIMES'
    if not signal<=scheduled<=start<=snap<=end<=now or now-snap>5 or now-end>5 or end-scheduled>25:return 'STALE_OR_PRE_SIGNAL'
    return None

def assess(c,row,job,result,signal,inventory,now):
    import amm_exit_scenario as amm,signal_exit as se,exit_rules_v33 as costs
    code=unusable(row,job,result,signal,now)
    if code:return code,None
    if job.get('route')=='PUMP_AMM':return amm.assess(c,row,job,result,signal,None,inventory)
    if job.get('route')!='PUMP_CURVE' or result.get('route','PUMP_CURVE')!='PUMP_CURVE':return 'UNKNOWN_ROUTE',None
    if result.get('reserves',{}).get('status')=='INSUFFICIENT_REAL_RESERVES':return 'INSUFFICIENT_LIQUIDITY_OPEN',None
    # Resolver metadata belongs to this fresh snapshot job; independent of Jupiter sizing.
    if not isinstance(inventory,dict):return 'INVENTORY_MISMATCH',None
    inventory=dict(inventory)
    decimals=result.get('amount_decimals',inventory.get('decimals'))
    if inventory.get('decimals') is not None and decimals!=inventory['decimals']:return 'AMOUNT_MISMATCH',None
    if inventory.get('raw_amount') is None:
        inventory.update(raw_amount=(result.get('sale') or {}).get('raw_amount'),decimals=decimals)
    decision=se.eligibility(row,job,result,signal,None,inventory)
    if decision!='ELIGIBLE':return decision,None
    sale=result['sale'];inv=job.get('inventory') or {}
    if any(x.get('broadcast') is not False or x.get('paper_mutated') is not False for x in (result,sale)):return 'NOT_ISOLATED_SIMULATION',None
    if sale.get('err') is not None or job.get('trade_id')!=row['trade_id'] or job.get('mint')!=row['mint']:return 'IDENTITY_MISMATCH',None
    try:
        raw=int(inventory['raw_amount']);decimals=inventory['decimals']
        if type(decimals)!=int or not 0<=decimals<=18:return 'AMOUNT_MISMATCH',None
        with localcontext() as ctx:
            ctx.prec=60
            if raw<=0 or raw>=2**64 or int(Decimal(inv['token_units'])*10**decimals)!=raw:return 'AMOUNT_MISMATCH',None
        if any(str(sale.get(k))!=str(raw) for k in ('raw_amount','tokens_debited','pool_tokens_credited')):return 'AMOUNT_MISMATCH',None
        if inventory.get('trade_id')!=row['trade_id'] or inventory.get('mint')!=row['mint']:return 'INVENTORY_MISMATCH',None
        fields=('pool_sol_delta_lamports','seller_net_sol_delta_lamports','synthetic_token_rent_lamports','local_fee_from_balance_diff_lamports','volume_account_rent_delta_lamports')
        if any(type(sale.get(k))!=int for k in fields):return 'MISSING_ACCOUNTING',None
        if min(sale[k] for k in fields[2:])<0:return 'ACCOUNTING_MISMATCH',None
        gross=-sale['pool_sol_delta_lamports'];receipt=sale['seller_net_sol_delta_lamports']-sale['synthetic_token_rent_lamports']
        if gross<=0 or receipt>gross:return 'ACCOUNTING_MISMATCH',None
        account=costs.accounting(c,job,result)
        if not account:return 'MISSING_ACCOUNTING',None
        account=wallet_account(c,account,'PUMP_CURVE')
        if int(account['wallet_receipt_lamports'])>gross:return 'ACCOUNTING_MISMATCH',None
        account['quote_received_lamports']=gross
        return 'ELIGIBLE',account
    except (KeyError,TypeError,ValueError,InvalidOperation,OverflowError):return 'INVALID_PAYLOAD',None

def poll(main):
    import config,monitor_service,database as db
    if not db.con().execute("SELECT 1 FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING' LIMIT 1").fetchone():return
    if not monitor_service.paths(config.DB)['db'].exists():return
    now=time.time();local=db.con()
    # Read monitor evidence, mutate only this scenario's own PAPER database.
    with closing(monitor_service.connect(config.DB,readonly=True)) as c:
        c.execute('BEGIN')
        inventories={r['trade_id']:dict(r) for r in c.execute('SELECT * FROM quote_inventory_v0723')}
        signals={r['trade_id']:dict(r) for r in local.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING'")}
        rows=[dict(r) for r in c.execute("SELECT * FROM position_checks_v0730 WHERE finished_ts>=? AND status!='RUNNING' ORDER BY finished_ts,id",(now-5,))]
        for row in rows:
            tid=row['trade_id'];sig=signals.get(tid)
            if not sig or local.execute('SELECT 1 FROM route_exit_decisions_v59 WHERE check_id=?',(row['id'],)).fetchone():continue
            try:
                j=json.loads(row['input_json']);r=json.loads(row['result_json'])
                decision,a=assess(local,row,j,r,sig['signal_ts'],inventories.get(tid),time.time())
            except (ValueError,TypeError,KeyError,AttributeError):decision,a='INVALID_PAYLOAD',None;j={}
            route=j.get('route')
            if decision=='ELIGIBLE':
                # Freshness rechecked at the mutation boundary, never old successful checks.
                decision,a=assess(local,row,j,r,sig['signal_ts'],inventories.get(tid),time.time())
                if decision=='ELIGIBLE':
                    if common.settle(main,row,a,row['id'],row['finished_ts'],dict(accounting=a,signal=sig,route=route,scenario='ROUTE_PAPER_V59'),route):decision='SETTLED'
                    else:decision='POSITION_OR_ORDER_CHANGED'
            elif decision=='INSUFFICIENT_LIQUIDITY_OPEN':
                # v0.7.101.10: a real position would be in the curve and sell at the model value (curve_floor_v10110).
                floor=__import__('curve_floor_v10110').settle(main,local,row,j,r,sig,'ROUTE_PAPER_V59')
                if floor is not None:decision='FLOOR_SETTLED' if floor else 'POSITION_OR_ORDER_CHANGED'
                # Old results without curve state: a curve that stays drained never pays; holding it forever froze the slot and hid the loss.
                start,checks=drained_streak(local,tid,sig['signal_ts'],row['finished_ts'])
                if floor is None and checks>=WRITE_OFF_MIN_CHECKS and row['finished_ts']-start>=WRITE_OFF_AFTER_S:
                    try:w=write_off_account(local,row,j,r,start,checks)
                    except (KeyError,TypeError,ValueError,InvalidOperation):w=None
                    if w:
                        evidence=dict(source='local_curve_reserves',price_basis='REAL_RESERVE_UPPER_BOUND_NO_SALE',clock_status='LOCAL_RESERVE_EVIDENCE')
                        settled=common.settle(main,row,w,row['id'],row['finished_ts'],dict(accounting=w,signal=sig,route=WRITE_OFF,scenario='ROUTE_PAPER_V59'),
                            WRITE_OFF,reason='CURVE_WRITE_OFF',rate_source=WRITE_OFF,evidence=evidence)
                        decision='WRITTEN_OFF' if settled else 'POSITION_OR_ORDER_CHANGED'
            local.execute('INSERT OR IGNORE INTO route_exit_decisions_v59 VALUES(?,?,?,?,?,?)',(row['id'],tid,row['mint'],row['finished_ts'],decision,route))

def lifecycle_path(primary,transport=False):
    p=path(primary);return p.with_name(p.stem+('_transport.json' if transport else '_lifecycle.json'))

def write_lifecycle(primary,data,transport=False):
    p=lifecycle_path(primary,transport);tmp=p.with_name(p.name+'.'+str(os.getpid())+'.tmp')
    tmp.write_text(json.dumps(data,allow_nan=False),encoding='utf-8');tmp.replace(p)

def read_lifecycle(primary,transport=False):
    try:return json.loads(lifecycle_path(primary,transport).read_text())
    except (ValueError,OSError):return {}

def write_transport(primary,data):write_lifecycle(primary,data,True)

def report(primary):
    p=path(primary)
    if not p.exists():return dict(status='NOT_STARTED')
    import runtime_status,paper_capital,run_audit
    with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN')
        info=dict(c.execute('SELECT k,v FROM branch_info'));meta=dict(c.execute('SELECT k,v FROM meta'))
        settlements=[dict(r) for r in c.execute('SELECT * FROM branch_amm_settlements ORDER BY settled_ts')]
        lifecycle=read_lifecycle(primary);transport=read_lifecycle(primary,True)
        if lifecycle.get('session')!=meta.get('branch_session'):lifecycle={}
        if transport.get('session')!=meta.get('branch_session'):transport={}
        return dict(runtime_perf=__import__('runtime_control').read(p),first_gap=json.loads(meta.get('branch_first_gap_v60','null')),status='AVAILABLE',runtime=runtime_status.summary(c),capital=paper_capital.summary(c),runs=run_audit.summary(c),
            fork_trade_id=int(info['route_fork_trade_id']),carried_ids=json.loads(info['route_carried_ids']),
            lifecycle=lifecycle,transport=transport,session=meta.get('branch_session'),
            settlements=settlements,persistent_wallet_model_v100=__import__('cost_classes_v100').wallet_model(settlements,paper_capital.summary(c)['closed_net']),decisions=dict(c.execute('SELECT decision,COUNT(*) FROM route_exit_decisions_v59 GROUP BY decision')),
            open_positions=[dict(r,**__import__('route_remainder_v96').state(r['signal_ts'],time.time(),r['last_decision'])) for r in c.execute('''SELECT p.trade_id,p.mint,p.size,o.reason,o.signal_ts,
                (SELECT decision FROM route_exit_decisions_v59 d WHERE d.trade_id=p.trade_id ORDER BY available_ts DESC LIMIT 1) last_decision
                FROM positions p LEFT JOIN paper_orders_v0720 o ON o.trade_id=p.trade_id AND o.side='SELL' AND o.status='PENDING' ''')],
            input_gap=meta.get('branch_input_gap'),note='Independent PAPER; no tick SELL fallback. Fork history is inherited, only post-fork trades measure this scenario. Synthetic inventory, modeled isolated costs, no network execution.')

def export(primary,folder):
    import zipfile,tempfile,monitor_service
    with zipfile.ZipFile(Path(folder)/'route_paper.zip','w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('summary.json',json.dumps(report(primary),ensure_ascii=False))
        for transport in (False,True):
            f=lifecycle_path(primary,transport)
            if f.exists():z.write(f,f.name)
        if path(primary).exists():
            with __import__('tempfile').TemporaryDirectory() as td:
                for src in (path(primary),monitor_service.paths(path(primary))['db']):
                    if not src.exists():continue
                    dst=Path(td)/src.name
                    with closing(sqlite3.connect(src.as_uri()+'?mode=ro',uri=True)) as a,closing(sqlite3.connect(dst)) as b:a.backup(b)
                    z.write(dst,dst.name)
                evidence=monitor_service.export(path(primary),Path(td)/'monitor')
                z.write(evidence,'route_monitor_evidence.zip')

if __name__=='__main__':
    asyncio.run(common.worker(sys.argv[2],sys.argv[3],sys.argv[4],scenario=sys.modules[__name__]))
