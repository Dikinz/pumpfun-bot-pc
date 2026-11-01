"""Fail-closed PAPER entry gate. No VM, network or ledger mutation in hot path."""
import asyncio,json,time,sys,uuid,zipfile,sqlite3,re
from pathlib import Path
from decimal import Decimal,localcontext
import config,entry_diagnostics,entry_pipeline,rpc_timing
ROOT=Path(__file__).resolve().parent/'preentry_evidence'
SCHEMA='''CREATE TABLE IF NOT EXISTS preentry_v0729(
 job_id TEXT PRIMARY KEY,mint TEXT UNIQUE,created_ts REAL,input_json TEXT,status TEXT,
 started_ts REAL,finished_ts REAL,valid_until REAL,result_json TEXT,order_id TEXT UNIQUE,archive TEXT);'''
SCHEMA+='CREATE INDEX IF NOT EXISTS preentry_status_created_v60 ON preentry_v0729(status,created_ts);'
SCHEMA+=entry_diagnostics.SCHEMA+entry_pipeline.SCHEMA+rpc_timing.SCHEMA
MAX_QUEUE_AGE=10
CHILD_TIMEOUT=30
MAX_TOTAL_DELAY=10
MAX_SNAPSHOT_AGE=5
MAX_PRICE_MOVE=.05
MAX_LOSS_BPS=1500


def request(c,mint,price,size,now):
    import exit_quotes as q
    if not __import__('recovery_gate').allowed():return 'RECOVERY_BLOCKED'
    row=c.execute('SELECT * FROM preentry_v0729 WHERE mint=?',(mint,)).fetchone()
    if row:return row['status']
    fx=q.FX or {};rate=fx.get('rate');age=now-float(fx.get('ts',0))
    status='PENDING' if rate and 0<=age<=120 else 'MISSING_ENTRY_FX'
    inp=dict(mint=mint,signal_ts=now,signal_price=str(price),size_units=str(size),fx_rate=rate,fx_ts=fx.get('ts'),
             max_loss_bps=MAX_LOSS_BPS,min_reserve_multiple=1,buy_min_fraction='0.95')
    __import__('recovery_gate').stamp(inp)
    ident=uuid.uuid4().hex
    c.execute('INSERT INTO preentry_v0729(job_id,mint,created_ts,input_json,status) VALUES(?,?,?,?,?)',
              (ident,mint,now,json.dumps(inp),status))
    rpc_timing.queue_add(c,ident)
    return status


def _allow(c,mint,price,size,now,order_id=None):
    if not __import__('recovery_gate').allowed():return False,'RECOVERY_BLOCKED'
    import exit_quotes as q
    row=c.execute('SELECT * FROM preentry_v0729 WHERE mint=?',(mint,)).fetchone()
    if row is None:return False,'PREFLIGHT_'+request(c,mint,price,size,now)
    if row['status'] not in ('READY','USED'):return False,'PREFLIGHT_'+row['status']
    if row['status']=='USED' and row['order_id']!=order_id:return False,'PREFLIGHT_ALREADY_USED'
    if row['valid_until'] is None or not row['finished_ts']<=now<=row['valid_until']:return False,'PREFLIGHT_STALE'
    inp=json.loads(row['input_json'])
    if not __import__('recovery_gate').allowed(inp):return False,'PREFLIGHT_RECOVERY_INVALIDATED'
    if Decimal(str(size))!=Decimal(inp['size_units']):return False,'PREFLIGHT_SIZE_CHANGED'
    if str((q.FX or {}).get('rate'))!=str(inp['fx_rate']):return False,'PREFLIGHT_FX_CHANGED'
    if abs(Decimal(str(price))/Decimal(inp['signal_price'])-1)>Decimal(str(MAX_PRICE_MOVE)):return False,'PREFLIGHT_PRICE_MOVED'
    return True,'PREFLIGHT_READY'


def allow(c,mint,price,size,now,order_id=None):
    decision=_allow(c,mint,price,size,now,order_id)
    import exit_quotes as q
    row=c.execute('SELECT * FROM preentry_v0729 WHERE mint=?',(mint,)).fetchone()
    entry_pipeline.record(c,row,price,size,now,order_id,decision,q.FX,MAX_PRICE_MOVE)
    return decision


def consume(c,mint,oid):
    c.execute("UPDATE preentry_v0729 SET status='USED',order_id=? WHERE mint=? AND status='READY'",(oid,mint))


def ready(c,mint):
    if not __import__('recovery_gate').allowed():return False
    return c.execute("SELECT 1 FROM preentry_v0729 WHERE mint=? AND status='READY' AND valid_until>=?",(mint,time.time())).fetchone() is not None


def complete(c,j,report,path=None,now=None):
    now=time.time() if now is None else now
    rpc_timing.attach(report,ROOT/j['job_id']);rpc_timing.queue_finish(c,j,report)
    status=report['status'];valid=None
    if not __import__('recovery_gate').allowed(json.loads(j['input_json'])):
        report=dict(report,status='RECOVERY_INVALIDATED');status='RECOVERY_INVALIDATED'
    if report.get('reserve_gate')=='INSUFFICIENT_PREENTRY_RESERVES':status='INSUFFICIENT_PREENTRY_RESERVES'
    elif status=='ROUNDTRIP_EXECUTED':
        if report.get('reserve_gate')!='PASS':status=report.get('reserve_gate','RESERVE_NOT_CONFIRMED')
        elif report.get('net_gate')!='PASS':status=report.get('net_gate','NET_NOT_CONFIRMED')
        elif report.get('zero_token_control',{}).get('status')!='FAILED' or report.get('zero_token_control',{}).get('error_code')!=6023:status='CONTROL_NOT_CONFIRMED'
        elif not 0<=now-j['created_ts']<=MAX_TOTAL_DELAY:status='LATE_PREFLIGHT_RESULT'
        elif not 0<=now-report.get('snapshot_received_ts',0)<=MAX_SNAPSHOT_AGE:status='STALE_PREFLIGHT_SNAPSHOT'
        else:status='READY';valid=min(j['created_ts']+MAX_TOTAL_DELAY,report['snapshot_received_ts']+MAX_SNAPSHOT_AGE)
    c.execute("UPDATE preentry_v0729 SET status=?,finished_ts=?,valid_until=?,result_json=?,archive=? WHERE job_id=? AND status IN ('PENDING','RUNNING')",
             (status,now,valid,json.dumps(report),path,j['job_id']))


def recover(c):
    c.execute("UPDATE preentry_v0729 SET status='INTERRUPTED_RESTART',valid_until=NULL WHERE status IN ('PENDING','RUNNING','READY')")


def child(data,out,replay=None):
    rpc_timing.start(out);rpc_timing.stage('IMPORTS')
    import local_simulation as lab
    import roundtrip_simulation as rt
    bundle=None;report={}
    try:
        with localcontext() as ctx:
            ctx.prec=60
            budget=int(Decimal(data['size_units'])/Decimal(data['fx_rate'])*Decimal(10)**9)
        rpc_timing.stage('POOL_SNAPSHOT')
        # Capture derives token decimals and canonical pool in one final RPC bank.
        bundle=json.loads(Path(replay).read_text()) if replay else lab.capture(data['mint'],1,1)
        mint_account=bundle['snapshot']['accounts'][bundle['mint']];dec=lab.raw(mint_account)[44]
        with localcontext() as ctx:
            ctx.prec=60
            minimum=int(Decimal(budget)/Decimal(10)**9/Decimal(data['signal_price'])*Decimal(10)**dec*Decimal(data['buy_min_fraction']))
        rpc_timing.stage('ROUNDTRIP_SIMULATION')
        report=rt.run_roundtrip(bundle,budget,max(1,minimum),data['max_loss_bps'],data['min_reserve_multiple'])
        report['amount_decimals']=dec
        report['snapshot_received_ts']=bundle['snapshot']['received_ts']
        report['snapshot_request_ts']=bundle['snapshot']['started_ts']
        # Explicitly preserve VM failure and its raw program code.
        if report['status']=='BUY_SIMULATION_FAILED':report['category']='BUY_PRICE_OR_PROGRAM_REJECTION'
        elif report['status']=='POST_BUY_SELL_FAILED':report['category']='POST_BUY_PROGRAM_REJECTION'
        else:report['category']=report['status']
    except Exception as e:
        report={'status':str(e) if isinstance(e,(lab.LabError,lab.v.VerificationError)) else __import__('rpc_budget').lab_status(e,'REQUEST_OR_LAB_ERROR_')}
        if __import__('rpc_budget').deferral(e):report['rpc_deferred_v1012']=__import__('rpc_budget').deferral(e)
        report['exception_v96']=__import__('safe_error_v96').capture(e)
        if isinstance(e,lab.v.VerificationError):report['unsupported_details']=e.details
    rpc_timing.stage('SAVE_RESULT');report['rpc_timing']=rpc_timing.snapshot()
    report.update(finished_ts=time.time(),signal_ts=data['signal_ts'],paper_mutated=False,broadcast=False)
    with zipfile.ZipFile(out/'evidence.tmp','w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('input.json',json.dumps(data));z.writestr('result.json',json.dumps(report))
        if bundle:z.writestr('snapshot.json',json.dumps(bundle))
    (out/'evidence.tmp').replace(out/'evidence.zip')
    (out/'result.tmp').write_text(json.dumps(report));(out/'result.tmp').replace(out/'result.json')
    return report


async def run_job(j):
    import database as db
    if not re.fullmatch('[a-f0-9]{32}',j['job_id']):complete(db.con(),j,{'status':'INVALID_JOB_ID'});return
    out=ROOT/j['job_id']
    try:
        out.mkdir(parents=True,exist_ok=True);(out/'input.json').write_text(j['input_json'])
        import position_monitor as pm
        payload=json.loads(j['input_json']);payload.setdefault('id',j['job_id'])
        r=await pm.launch_check(payload,out,timeout=CHILD_TIMEOUT,child_script=__file__)
        if r.get('status')=='WORKER_TIMEOUT':r['status']='PREFLIGHT_TIMEOUT'
        complete(db.con(),j,r,str(out/'evidence.zip') if (out/'evidence.zip').exists() else None)
    except asyncio.TimeoutError:complete(db.con(),j,{'status':'PREFLIGHT_TIMEOUT'})
    except asyncio.CancelledError:
        complete(db.con(),j,{'status':'INTERRUPTED_STOP'});raise
    except Exception as e:
        complete(db.con(),j,{'status':'WORKER_ERROR_'+type(e).__name__,'error_type':type(e).__name__,'exception_v96':__import__('safe_error_v96').capture(e),'child_diagnostics_v73':getattr(e,'child_diagnostics_v73',{})})
        if str(e).startswith('SIMULATION_CHILD_CLEANUP_') or getattr(e,'child_diagnostics_v73',{}).get('cleanup_exception'):raise


async def worker():
    import database as db
    while True:
        now=time.time();c=db.con()
        for expired in c.execute("SELECT * FROM preentry_v0729 WHERE status='PENDING' AND created_ts<?",(now-MAX_QUEUE_AGE,)).fetchall():rpc_timing.queue_finish(c,dict(expired),{})
        c.execute("UPDATE preentry_v0729 SET status='QUEUE_EXPIRED' WHERE status='PENDING' AND created_ts<?",(now-MAX_QUEUE_AGE,))
        entry_diagnostics.expire(c,now)
        row=c.execute("SELECT * FROM preentry_v0729 WHERE status='PENDING' ORDER BY created_ts LIMIT 1").fetchone()
        if row:
            c.execute("UPDATE preentry_v0729 SET status='RUNNING',started_ts=? WHERE job_id=?",(now,row['job_id']))
            rpc_timing.queue_start(c,dict(row),now)
            await run_job(dict(row))
        else:await asyncio.sleep(.2)


def summary(c,limit=100):
    counts=dict(c.execute('SELECT status,COUNT(*) FROM preentry_v0729 GROUP BY status'))
    rows=[]
    for row in c.execute('SELECT * FROM preentry_v0729 ORDER BY created_ts DESC LIMIT ?',(limit,)):
        r=dict(row);r['result']=json.loads(r.pop('result_json') or '{}');r['input']=json.loads(r.pop('input_json'));r.pop('archive')
        r['approval_diagnostics']=entry_diagnostics.explanation(c,r['job_id']) if r['status']=='APPROVAL_EXPIRED' or (r['status']=='READY' and r['valid_until']<time.time()) else None
        rows.append(r)
    return dict(counts=counts,rows=rows,timing=rpc_timing.preentry_summary(c),max_loss_bps=MAX_LOSS_BPS,min_reserve_multiple=1,
                note='Pre-entry gate, not an entry fill or future sell guarantee. Immediate BUY->SELL uses actual VM post-buy tokens and reserves.')


def export(db_path,folder='exports'):
    folder=Path(folder);folder.mkdir(exist_ok=True,parents=True)
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;data=summary(c,-1);files=list(c.execute('SELECT job_id FROM preentry_v0729 WHERE started_ts IS NOT NULL OR archive IS NOT NULL'))
    target=folder/('preentry_'+time.strftime('%Y%m%d_%H%M%S')+'.zip');missing=[]
    with zipfile.ZipFile(target,'w',zipfile.ZIP_STORED) as z:
        z.writestr('summary.json',json.dumps(data,ensure_ascii=False))
        for (jid,) in files:
            p=ROOT/jid/'evidence.zip'
            if p.exists():z.write(p,jid+'/evidence.zip')
            else:missing.append(jid)
            for name in ('diagnostic.zip','child_lifecycle_v73.json'):
                f=ROOT/jid/name
                if f.exists():z.write(f,jid+'/'+name)
        z.writestr('missing_evidence.json',json.dumps(missing))
    return target

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--child');p.add_argument('--replay');args=p.parse_args()
    if args.child:
        out=Path(args.child);child(json.loads((out/'input.json').read_text()),out,args.replay)
    else:print(export(config.DB))
