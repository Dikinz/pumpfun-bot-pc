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

def assess(c,row,job,result,signal,inventory,now):
    import amm_exit_scenario as amm,signal_exit as se,exit_rules_v33 as costs
    if not __import__('recovery_gate').allowed(job):return 'RECOVERY_INVALIDATED',None
    f=se.finite;start=row.get('started_ts');end=row.get('finished_ts');snap=result.get('snapshot_received_ts');scheduled=job.get('scheduled_ts')
    if not all(f(x) for x in (start,end,snap,signal,now,scheduled)):return 'INVALID_TIMES',None
    if not signal<=scheduled<=start<=snap<=end<=now or now-snap>5 or now-end>5 or end-scheduled>25:return 'STALE_OR_PRE_SIGNAL',None
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
