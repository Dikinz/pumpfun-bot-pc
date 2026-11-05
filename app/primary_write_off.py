"""v0.7.101.8: PRIMARY writes off a position whose curve stays drained after the exit signal.

PRIMARY sells on the first fresh tick. A drained curve still prints ticks at its virtual-reserve price, or none:
03.11.2026 FPtv waited 1 h 51 min until the stop with 3.00 frozen, HUNT filled after 22 min at a price the empty
curve could not pay. Same rule as ROUTE (route_branch): every fresh PRIMARY monitor check after the signal shows
real SOL below the sale amount, at least 3 checks over at least 10 minutes. A fresh tick that comes first still
fills as before; entry and exit thresholds are unchanged."""
import asyncio,json,time
from contextlib import closing
from decimal import InvalidOperation
import paper_branch as common,route_branch as route
TABLE='primary_drained_checks_v1018'
SCHEMA=common.SETTLEMENTS+'CREATE TABLE IF NOT EXISTS '+TABLE+'(check_id TEXT PRIMARY KEY,trade_id TEXT,mint TEXT,available_ts REAL,decision TEXT,route TEXT);'
COST_MODEL='PRIMARY_WRITE_OFF_V1018'
DRAINED='INSUFFICIENT_LIQUIDITY_OPEN'

def classify(row,job,result,signal,now):
    code=route.unusable(row,job,result,signal,now)
    if code:return code
    if job.get('route')!='PUMP_CURVE' or result.get('route','PUMP_CURVE')!='PUMP_CURVE':return 'UNKNOWN_ROUTE'
    if (result.get('reserves') or {}).get('status')=='INSUFFICIENT_REAL_RESERVES':return DRAINED
    return 'CURVE_NOT_DRAINED'

def poll(main):
    import config,monitor_service,database as db
    local=db.con()
    if not local.execute("SELECT 1 FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING' LIMIT 1").fetchone():return
    if not monitor_service.paths(config.DB)['db'].exists():return
    signals={r['trade_id']:dict(r) for r in local.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING'")}
    # Read the PRIMARY monitor, mutate only the PRIMARY PAPER database.
    with closing(monitor_service.connect(config.DB,readonly=True)) as c:
        c.execute('BEGIN')
        rows=[dict(r) for r in c.execute("SELECT * FROM position_checks_v0730 WHERE finished_ts>=? AND status!='RUNNING' ORDER BY finished_ts,id",(time.time()-5,))]
    for row in rows:
        tid=row['trade_id'];sig=signals.get(tid)
        if not sig or local.execute('SELECT 1 FROM '+TABLE+' WHERE check_id=?',(row['id'],)).fetchone():continue
        try:
            j=json.loads(row['input_json']);r=json.loads(row['result_json'])
            decision=classify(row,j,r,sig['signal_ts'],time.time())
        except (ValueError,TypeError,KeyError,AttributeError):decision='INVALID_PAYLOAD';j={};r={}
        if decision==DRAINED:
            # v0.7.101.10: a real position would be in the curve and sell at the model value (curve_floor_v10110).
            floor=__import__('curve_floor_v10110').settle(main,local,row,j,r,sig,'PRIMARY')
            if floor is not None:decision='FLOOR_SETTLED' if floor else 'POSITION_OR_ORDER_CHANGED'
            start,checks=route.drained_streak(local,tid,sig['signal_ts'],row['finished_ts'],TABLE)
            if floor is None and checks>=route.WRITE_OFF_MIN_CHECKS and row['finished_ts']-start>=route.WRITE_OFF_AFTER_S:
                try:w=route.write_off_account(local,row,j,r,start,checks,COST_MODEL)
                except (KeyError,TypeError,ValueError,InvalidOperation):w=None
                if w:
                    evidence=dict(source='local_curve_reserves',price_basis='REAL_RESERVE_UPPER_BOUND_NO_SALE',clock_status='LOCAL_RESERVE_EVIDENCE')
                    settled=common.settle(main,row,w,row['id'],row['finished_ts'],dict(accounting=w,signal=sig,route=route.WRITE_OFF,scenario='PRIMARY'),
                        route.WRITE_OFF,reason='CURVE_WRITE_OFF',rate_source=route.WRITE_OFF,evidence=evidence)
                    decision='WRITTEN_OFF' if settled else 'POSITION_OR_ORDER_CHANGED'
        local.execute('INSERT OR IGNORE INTO '+TABLE+' VALUES(?,?,?,?,?,?)',(row['id'],tid,row['mint'],row['finished_ts'],decision,j.get('route')))

async def worker(main,primary):
    """Auxiliary: an error is recorded once per type and never stops trading; an unconfirmed rollback still does."""
    import system_health,database as db
    seen=set()
    while True:
        try:poll(main)
        except db.LocalDatabaseFailure:raise
        except Exception as exc:
            if type(exc).__name__ not in seen:seen.add(type(exc).__name__);system_health.failure(primary,'primary_write_off',exc)
        await asyncio.sleep(.5)
