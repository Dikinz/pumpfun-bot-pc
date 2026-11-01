"""Bounded read-only SHADOW_100 BUY->SELL experiments, never primary approvals."""
import asyncio,json,time,uuid,sys,sqlite3,zipfile,hashlib,math
from pathlib import Path
import config
ROOT=Path(__file__).resolve().parent/'shadow_preflight_evidence'
MAX_PENDING=3
START_INTERVAL=30.
STARTS_PER_HOUR=60
QUEUE_AGE=10.
CHILD_TIMEOUT=30.
TOTAL_AGE=10.
SNAPSHOT_AGE=5.
MAX_RPC_CALLS=6 # Warm 5 / cold 6 HTTP calls, including genesis; shared 6/s stays unchanged.
MAX_LOSS_BPS=1500
BACKOFF_BASE=60.
BACKOFF_MAX=300.
SCHEMA='''
CREATE TABLE IF NOT EXISTS shadow_preflight_config_v0743(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT,sha256 TEXT);
CREATE TABLE IF NOT EXISTS shadow_preflight_v0743(
 job_id TEXT PRIMARY KEY,mint TEXT UNIQUE,created_ts REAL,status TEXT,input_json TEXT,
 strict_pass_at_signal INTEGER,started_ts REAL,finished_ts REAL,wait_s REAL,run_s REAL,total_s REAL,
 last_blocker TEXT,raw_status TEXT,timing TEXT,buy_ok INTEGER DEFAULT 0,sell_ok INTEGER DEFAULT 0,
 cost_pass INTEGER DEFAULT 0,assessed INTEGER DEFAULT 0,result_json TEXT,archive TEXT);
CREATE INDEX IF NOT EXISTS shadow_preflight_start_v0743 ON shadow_preflight_v0743(started_ts);
CREATE TABLE IF NOT EXISTS shadow_preflight_service_v0743(id INTEGER PRIMARY KEY CHECK(id=1),status TEXT,heartbeat REAL,blocker TEXT,failure_streak INTEGER DEFAULT 0,cooldown_until REAL DEFAULT 0);
INSERT OR IGNORE INTO shadow_preflight_service_v0743(id,status) VALUES(1,'NOT_STARTED');
'''
def parameters():
    return dict(max_pending=MAX_PENDING,concurrency=1,start_interval_s=START_INTERVAL,starts_per_rolling_hour=STARTS_PER_HOUR,
      max_rpc_calls_per_job=MAX_RPC_CALLS,queue_age_s=QUEUE_AGE,child_timeout_s=CHILD_TIMEOUT,total_age_s=TOTAL_AGE,
      snapshot_age_s=SNAPSHOT_AGE,size_units=config.POSITION_SIZE,max_loss_bps=MAX_LOSS_BPS,min_reserve_multiple=1,
      buy_min_fraction='0.95',fx_max_age_s=120,backoff_base_s=BACKOFF_BASE,backoff_max_s=BACKOFF_MAX,
      selection='first_SHADOW_100_FEATURE_PASS_per_mint_FIFO_no_retry_skip_known_unstartable',wallet='fresh_synthetic_SOL_tokens_from_BUY',orders=False)

def freeze(c):
    payload=json.dumps(parameters(),sort_keys=True);old=c.execute('SELECT payload FROM shadow_preflight_config_v0743 WHERE id=1').fetchone()
    if old and old[0]!=payload:raise RuntimeError('Shadow simulation parameters changed; use a new database')
    c.execute('INSERT OR IGNORE INTO shadow_preflight_config_v0743 VALUES(1,?,?)',(payload,hashlib.sha256(payload.encode()).hexdigest()))

def finite(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)

def request(c,mint,price,now,features):
    import exit_quotes
    old=c.execute('SELECT status FROM shadow_preflight_v0743 WHERE mint=?',(mint,)).fetchone()
    if old:return old[0]
    fx=exit_quotes.FX or {};rate=fx.get('rate');fx_ts=fx.get('ts')
    try:valid_fx=finite(float(rate)) and float(rate)>0 and finite(fx_ts) and 0<=now-fx_ts<=120
    except (TypeError,ValueError):valid_fx=False
    status='PENDING'
    if not finite(price) or price<=0:status='SKIPPED_INVALID_PRICE'
    elif not valid_fx:status='SKIPPED_MISSING_FX'
    elif c.execute('SELECT COUNT(*) FROM shadow_preflight_v0743 WHERE started_ts>?',(now-3600,)).fetchone()[0]>=STARTS_PER_HOUR:status='SKIPPED_HOURLY_LIMIT'
    elif c.execute("SELECT COUNT(*) FROM shadow_preflight_v0743 WHERE status='PENDING'").fetchone()[0]>=MAX_PENDING:status='SKIPPED_QUEUE_FULL'
    if status=='PENDING':
        service=c.execute('SELECT cooldown_until FROM shadow_preflight_service_v0743 WHERE id=1').fetchone()
        last=c.execute('SELECT MAX(started_ts) FROM shadow_preflight_v0743').fetchone()[0]
        if service and service[0]>now+QUEUE_AGE:status='SKIPPED_SAMPLING_BACKOFF'
        elif last is not None and last+START_INTERVAL>now+QUEUE_AGE:status='SKIPPED_SAMPLING_INTERVAL'
    strict=c.execute("SELECT 1 FROM candidate_first_v0742 WHERE variant='STRICT_0' AND mint=? AND stage='FEATURE_PASS'",(mint,)).fetchone() is not None
    data=dict(mint=mint,signal_ts=now,signal_price=str(price),size_units=str(config.POSITION_SIZE),fx_rate=rate,fx_ts=fx_ts,
      max_loss_bps=MAX_LOSS_BPS,min_reserve_multiple=1,buy_min_fraction='0.95',candidate_features=features)
    c.execute('INSERT INTO shadow_preflight_v0743(job_id,mint,created_ts,status,input_json,strict_pass_at_signal,finished_ts) VALUES(?,?,?,?,?,?,?)',
      (uuid.uuid4().hex,mint,now,status,json.dumps(data),int(strict),now if status!='PENDING' else None))
    return status

def next_job(c,now):
    c.execute("UPDATE shadow_preflight_v0743 SET status=CASE WHEN created_ts>? THEN 'SKIPPED_CLOCK_CHANGED' ELSE 'SKIPPED_QUEUE_EXPIRED' END,finished_ts=?,wait_s=?-created_ts,total_s=?-created_ts WHERE status='PENDING' AND (created_ts<? OR created_ts>?)",(now,now,now,now,now-QUEUE_AGE,now))
    row=c.execute("SELECT * FROM shadow_preflight_v0743 WHERE status='PENDING' ORDER BY created_ts,rowid LIMIT 1").fetchone()
    if not row:return None,'IDLE'
    blocker=None;service=c.execute('SELECT * FROM shadow_preflight_service_v0743 WHERE id=1').fetchone()
    if c.execute("SELECT 1 FROM shadow_preflight_v0743 WHERE status='RUNNING'").fetchone():blocker='CHILD_RUNNING'
    elif now<service['cooldown_until']:blocker='ERROR_BACKOFF'
    elif c.execute("SELECT 1 FROM preentry_v0729 WHERE status IN ('PENDING','RUNNING')").fetchone():blocker='PRIMARY_PREFLIGHT_BUSY'
    elif c.execute('SELECT COUNT(*) FROM shadow_preflight_v0743 WHERE started_ts>?',(now-3600,)).fetchone()[0]>=STARTS_PER_HOUR:blocker='HOURLY_LIMIT'
    else:
        last=c.execute('SELECT MAX(started_ts) FROM shadow_preflight_v0743').fetchone()[0]
        if last is not None and now-last<START_INTERVAL:blocker='START_INTERVAL'
    if blocker:
        c.execute("UPDATE shadow_preflight_v0743 SET last_blocker=? WHERE status='PENDING' AND (last_blocker IS NULL OR last_blocker!=?)",(blocker,blocker));return None,blocker
    changed=c.execute("UPDATE shadow_preflight_v0743 SET status='RUNNING',started_ts=?,wait_s=?-created_ts WHERE job_id=? AND status='PENDING'",(now,now,row['job_id'])).rowcount
    if not changed:return None,'CLAIM_LOST'
    return dict(c.execute('SELECT * FROM shadow_preflight_v0743 WHERE job_id=?',(row['job_id'],)).fetchone()),'RUNNING'

ASSESSED={'ROUNDTRIP_EXECUTED','BUY_SIMULATION_FAILED','POST_BUY_SELL_FAILED','NEGATIVE_CONTROL_FAILED'}
def classify(j,r,now):
    raw=r.get('status','INVALID_RESULT');snapshot=r.get('snapshot_received_ts')
    buy=r.get('buy',{}).get('status')=='EXECUTED';sell=r.get('sell',{}).get('status')=='EXECUTED';cost=r.get('net_gate')=='PASS'
    total=now-j['created_ts'];timing='TIMELY'
    if not 0<=total<=TOTAL_AGE:timing='LATE_RESULT' if total>=0 else 'CLOCK_CHANGED'
    elif raw in ASSESSED and (not finite(snapshot) or not 0<=now-snapshot<=SNAPSHOT_AGE):timing='STALE_SNAPSHOT'
    inp=json.loads(j['input_json'])
    identity=r.get('mint')==j['mint'] and r.get('signal_ts')==inp['signal_ts'] and r.get('paper_mutated') is False and r.get('broadcast') is False
    control=r.get('zero_token_control',{})
    passed=(raw=='ROUNDTRIP_EXECUTED' and identity and buy and sell and cost and r.get('reserve_gate')=='PASS'
      and control.get('status')=='FAILED' and control.get('error_code')==6023)
    assessed=identity and raw in ASSESSED and timing=='TIMELY'
    if raw in ASSESSED and not identity:status='INVALID_RESULT'
    elif timing!='TIMELY':status=timing
    elif passed:status='CHECK_PASSED'
    elif assessed:status='CHECK_REJECTED'
    else:status=raw
    return dict(status=status,raw_status=raw,timing=timing,buy_ok=int(buy),sell_ok=int(sell),cost_pass=int(cost),assessed=int(assessed))

def complete(c,j,r,path=None,now=None):
    now=time.time() if now is None else now;x=classify(j,r,now);r=dict(r);reason=x['status']
    if x['status']=='CHECK_REJECTED':
        if r.get('status')!='ROUNDTRIP_EXECUTED':reason=r.get('status')
        elif r.get('reserve_gate')!='PASS':reason=r.get('reserve_gate','RESERVE_NOT_CONFIRMED')
        elif r.get('net_gate')!='PASS':reason=r.get('net_gate','NET_NOT_CONFIRMED')
        else:reason='CONTROL_NOT_CONFIRMED'
    r['check_decision_reason']=reason
    c.execute('UPDATE shadow_preflight_v0743 SET '+','.join(k+'=?' for k in x)+",finished_ts=?,run_s=?-started_ts,total_s=?-created_ts,result_json=?,archive=? WHERE job_id=? AND status='RUNNING'",(*x.values(),now,now,now,json.dumps(r),path,j['job_id']))
    technical=r.get('status') in ('CHILD_TIMEOUT','CHILD_ERROR','WORKER_ERROR','RPC_CALL_LIMIT') or str(r.get('status','')).startswith(('RPC_ERROR','RPC_DEFERRED','REQUEST_OR_LAB_ERROR'))
    if technical:
        n=c.execute('SELECT failure_streak FROM shadow_preflight_service_v0743 WHERE id=1').fetchone()[0]+1
        c.execute('UPDATE shadow_preflight_service_v0743 SET failure_streak=?,cooldown_until=? WHERE id=1',(n,now+min(BACKOFF_MAX,BACKOFF_BASE*2**min(n-1,10))))
    elif x['assessed']:c.execute('UPDATE shadow_preflight_service_v0743 SET failure_streak=0,cooldown_until=0 WHERE id=1')

def recover(c):c.execute("UPDATE shadow_preflight_v0743 SET status='INTERRUPTED_RESTART',finished_ts=? WHERE status IN ('PENDING','RUNNING')",(time.time(),))
def stop(c):
    now=time.time();c.execute("UPDATE shadow_preflight_v0743 SET status='INTERRUPTED_STOP',finished_ts=?,total_s=?-created_ts WHERE status IN ('PENDING','RUNNING')",(now,now))
    c.execute("UPDATE shadow_preflight_service_v0743 SET status='STOPPED',heartbeat=? WHERE id=1",(now,))

def partial_report(out,status):
    r={'status':status,'rpc_calls':None,'rpc_activity':[]}
    try:
        calls=json.loads((out/'rpc_activity.json').read_text());r.update(rpc_calls=len(calls),rpc_activity=calls)
    except (OSError,ValueError,TypeError):pass
    return r

async def run_job(j):
    import database as db
    out=ROOT/j['job_id']
    try:
        if len(j['job_id'])!=32 or any(x not in '0123456789abcdef' for x in j['job_id']):raise ValueError('INVALID_JOB_ID')
        out.mkdir(parents=True,exist_ok=True);(out/'input.json').write_text(j['input_json'])
        import position_monitor as pm
        payload=json.loads(j['input_json']);payload.setdefault('id',j['job_id'])
        r=await pm.launch_check(payload,out,timeout=CHILD_TIMEOUT,child_script=__file__)
        if r.get('status')=='WORKER_TIMEOUT':r=dict(partial_report(out,'CHILD_TIMEOUT'),**r);r['status']='CHILD_TIMEOUT'
        complete(db.con(),j,r,str(out/'evidence.zip') if (out/'evidence.zip').exists() else None)
    except asyncio.TimeoutError:complete(db.con(),j,partial_report(out,'CHILD_TIMEOUT'))
    except asyncio.CancelledError:
        complete(db.con(),j,partial_report(out,'INTERRUPTED_STOP'));raise
    except Exception as e:
        complete(db.con(),j,{'status':'WORKER_ERROR_'+type(e).__name__,'error_type':type(e).__name__,'exception_v96':__import__('safe_error_v96').capture(e),'child_diagnostics_v73':getattr(e,'child_diagnostics_v73',{})})
        if str(e).startswith('SIMULATION_CHILD_CLEANUP_') or getattr(e,'child_diagnostics_v73',{}).get('cleanup_exception'):raise

async def worker():
    import database as db
    task=None;last_beat=0
    try:
        while True:
            now=time.time();c=db.con()
            if task is not None and task.done():await task;task=None
            j,blocker=next_job(c,now)
            if j:task=asyncio.create_task(run_job(j))
            if now-last_beat>=1 or now<last_beat:
                c.execute("UPDATE shadow_preflight_service_v0743 SET status='RUNNING',heartbeat=?,blocker=? WHERE id=1",(now,blocker));last_beat=now
            await asyncio.sleep(.25)
    finally:
        if task is not None:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
        stop(db.con())

def child(data,out,replay=None):
    import preentry,local_simulation as lab
    import exit_quotes as q
    original=lab.read_accounts;original_json=q.request_json;calls=[]
    def observed(method,count,operation):
        if len(calls)>=MAX_RPC_CALLS:raise lab.LabError('RPC_CALL_LIMIT')
        item=dict(method=method,accounts=count,started_ts=time.time(),status='STARTED');calls.append(item)
        (out/'rpc_activity.json').write_text(json.dumps(calls))
        try:
            result=operation();item['status']='OK';return result
        except Exception as e:
            item['status']='ERROR';item['error_type']=type(e).__name__
            if isinstance(getattr(e,'code',None),int):item['http_status']=e.code
            raise
        finally:item['finished_ts']=time.time();(out/'rpc_activity.json').write_text(json.dumps(calls))
    def bounded_read(addresses,min_slot=None,*,data_slice=None,commitment='confirmed'):
        # Keep the current capture_bank API, including rooted code and later headers.
        return observed('getMultipleAccounts',len(addresses),lambda:original(addresses,min_slot,data_slice=data_slice,commitment=commitment))
    def bounded_json(url,stage,body=None,key=None):
        return observed((body or {}).get('method','HTTP_JSON'),None,lambda:original_json(url,stage,body,key))
    lab.read_accounts=bounded_read;q.request_json=bounded_json
    try:r=preentry.child(data,out,replay)
    finally:lab.read_accounts=original;q.request_json=original_json
    r.update(rpc_calls=len(calls),rpc_activity=calls,scope='SHADOW_100_NO_ORDERS',network_costs_known=False)
    with zipfile.ZipFile(out/'evidence.zip') as source,zipfile.ZipFile(out/'shadow.tmp','w',zipfile.ZIP_DEFLATED) as z:
        for n in source.namelist():
            if n!='result.json':z.writestr(n,source.read(n))
        z.writestr('result.json',json.dumps(r));z.writestr('rpc_activity.json',json.dumps(calls))
    (out/'shadow.tmp').replace(out/'evidence.zip');(out/'result.tmp').write_text(json.dumps(r));(out/'result.tmp').replace(out/'result.json')
    return r

def summary(c,limit=100):
    query="SELECT j.*,EXISTS(SELECT 1 FROM candidate_first_v0742 f WHERE f.mint=j.mint AND f.variant='STRICT_0' AND f.stage='FEATURE_PASS') AS shared_at_report FROM shadow_preflight_v0743 j"
    allrows=[dict(r) for r in c.execute(query+' ORDER BY created_ts DESC')]
    missing=list(c.execute("SELECT f.mint,EXISTS(SELECT 1 FROM candidate_first_v0742 s WHERE s.mint=f.mint AND s.variant='STRICT_0' AND s.stage='FEATURE_PASS') shared FROM candidate_first_v0742 f WHERE f.variant='SHADOW_100' AND f.stage='FEATURE_PASS' AND NOT EXISTS(SELECT 1 FROM shadow_preflight_v0743 j WHERE j.mint=f.mint)"))
    groups={}
    for name,rows in [('ALL',allrows),('SHADOW_ONLY',[r for r in allrows if not r['shared_at_report']]),('SHARED',[r for r in allrows if r['shared_at_report']])]:
        absent=sum(name=='ALL' or (name=='SHARED')==bool(r['shared']) for r in missing)
        n=len(rows)+absent;assessed=sum(r['assessed'] for r in rows);passed=sum(r['status']=='CHECK_PASSED' for r in rows);statuses={}
        for r in rows:statuses[r['status']]=statuses.get(r['status'],0)+1
        if absent:statuses['NOT_RECORDED']=absent
        groups[name]=dict(total=n,started=sum(r['started_ts'] is not None for r in rows),timely_assessed=assessed,passed=passed,buy_executed_raw=sum(r['buy_ok'] for r in rows),sell_executed_raw=sum(r['sell_ok'] for r in rows),pass_fraction_all=passed/n if n else None,pass_fraction_assessed=passed/assessed if assessed else None,statuses=statuses)
    rows=allrows if limit<0 else allrows[:limit]
    for r in rows:r['input']=json.loads(r.pop('input_json'));r['result']=json.loads(r.pop('result_json') or '{}');r.pop('archive',None)
    cfg=c.execute('SELECT * FROM shadow_preflight_config_v0743 WHERE id=1').fetchone();service=dict(c.execute('SELECT * FROM shadow_preflight_service_v0743 WHERE id=1').fetchone())
    service['age_s']=time.time()-service['heartbeat'] if service['heartbeat'] is not None else None
    return dict(missing_jobs=len(missing),config=dict(cfg) if cfg else None,groups=groups,rows=rows,service=service,pending=sum(r['status']=='PENDING' for r in allrows),running=sum(r['status']=='RUNNING' for r in allrows),
      note='Immediate local BUY->SELL on post-buy reserves. Fresh synthetic SOL wallet per mint; tokens from BUY. Fees/rent included, network priority/landing costs unknown. Raw execution includes late results; CHECK_PASSED requires timely snapshot, reserves, net threshold and negative control. FIFO first candidate, no retries: skipped/late/error rows stay denominator. SHADOW_ONLY uses final observed membership. No PAPER orders/balance changes; not profit or future exit guarantee.')

def export(db_path,folder):
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;report=summary(c,-1);jobs=[(r[0],r[1]) for r in c.execute('SELECT job_id,started_ts FROM shadow_preflight_v0743')]
    target=Path(folder)/('shadow_preflight_'+time.strftime('%Y%m%d_%H%M%S')+'.zip');missing=[]
    with zipfile.ZipFile(target,'w',zipfile.ZIP_STORED) as z:
        z.writestr('summary.json',json.dumps(report,ensure_ascii=False))
        for jid,started in jobs:
            out=ROOT/jid
            for name in ('evidence.zip','rpc_activity.json','diagnostic.zip','child_lifecycle_v73.json'):
                if (out/name).exists():z.write(out/name,jid+'/'+name)
            if not (out/'evidence.zip').exists() and started is not None:missing.append(jid)
        z.writestr('missing_evidence.json',json.dumps(missing))
    return target

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--child',required=True);p.add_argument('--replay');args=p.parse_args()
    out=Path(args.child);child(json.loads((out/'input.json').read_text()),out,args.replay)
