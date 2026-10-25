"""Timer-driven shadow sell checks; no PAPER mutation and no stream dependency."""
import asyncio,json,time,uuid,sys,zipfile,zlib,hashlib,sqlite3,argparse
from pathlib import Path
import wallet_costs,price_exit_trial as trial
ROOT=Path(__file__).resolve().parent/'monitor_evidence'
INTERVAL=10.0
TIMEOUT=25.0
FRESH_SECONDS=5.0
SCHEMA='''CREATE TABLE IF NOT EXISTS position_checks_v0730(
 id TEXT PRIMARY KEY,trade_id TEXT,mint TEXT,scheduled_ts REAL,started_ts REAL,finished_ts REAL,
 status TEXT,input_json TEXT,result_json TEXT,archive TEXT);
 CREATE INDEX IF NOT EXISTS position_checks_trade_time ON position_checks_v0730(trade_id,scheduled_ts);
 CREATE INDEX IF NOT EXISTS position_checks_trade_started_v51 ON position_checks_v0730(trade_id,started_ts DESC);
 CREATE TABLE IF NOT EXISTS position_health_v0730(trade_id TEXT PRIMARY KEY,first_problem_ts REAL,first_problem_status TEXT,first_error_ts REAL,last_good_ts REAL);'''
SCHEMA+=__import__('priority_exit').SCHEMA+__import__('fresh_retry').SCHEMA
SCHEMA+='CREATE INDEX IF NOT EXISTS checks_finished_v60 ON position_checks_v0730(finished_ts,id);'
import connection_audit
SCHEMA+=connection_audit.SCHEMA
RISK={'INSUFFICIENT_RESERVES','NET_BELOW_FLOOR','RESERVE_DROP','SALE_REJECTED'}
GOOD={'SELLABLE','RESERVE_DROP','NET_BELOW_FLOOR','INSUFFICIENT_RESERVES','SALE_REJECTED'}

MIGRATION_STATUS='CURVE_COMPLETED_NEW_POOL_UNCHECKED'
AMM_INTERVAL=60.0
TECHNICAL={'REQUEST_TIMEOUT','REQUEST_ERROR','REQUEST_INVALID_JSON','DATA_REJECTED','STALE_RESULT','CHILD_ERROR','WORKER_ERROR','INTERRUPTED_RESTART','NEGATIVE_CONTROL_FAILED','AMM_REQUEST_ERROR','AMM_REQUEST_INVALID_JSON','AMM_DATA_REJECTED','AMM_STALE_RESULT','AMM_NEGATIVE_CONTROL_FAILED','AMM_EFFECTS_MISMATCH'}

def migrated(c,tid):
    return c.execute("SELECT 1 FROM position_checks_v0730 WHERE trade_id=? AND status IN ('POOL_COMPLETED','CURVE_COMPLETED_NEW_POOL_UNCHECKED') LIMIT 1",(tid,)).fetchone() is not None

def next_due(c,tid,opened):
    row=c.execute('SELECT started_ts,finished_ts,status,input_json FROM position_checks_v0730 WHERE trade_id=? ORDER BY started_ts DESC LIMIT 1',(tid,)).fetchone()
    import fresh_retry
    backoff=max(connection_audit.retry(c,tid)['not_before_ts'] or 0,fresh_retry.cooldown(c))
    urgent=fresh_retry.waiting_due(c,tid)
    if urgent is not None:return max(backoff,urgent)
    if not row:return max(backoff,opened)
    if row['status'] in ('POOL_COMPLETED',MIGRATION_STATUS):return max(backoff,(row['finished_ts'] or row['started_ts'])+1)
    interval=AMM_INTERVAL if migrated(c,tid) else INTERVAL
    return max(backoff,row['started_ts']+interval)

def recover(c,status='INTERRUPTED_RESTART'):
    c.execute("UPDATE position_checks_v0730 SET status=?,finished_ts=? WHERE status='RUNNING'",(status,time.time()))

def next_job(c,now,lane=None,priority=None):
    import fresh_retry
    if now<fresh_retry.cooldown(c):return
    # Dedicated first-check lane; one running check per position, no queued jobs.
    rows=c.execute('''SELECT p.*,MAX(m.started_ts) last_check FROM positions p LEFT JOIN position_checks_v0730 m ON m.trade_id=p.trade_id GROUP BY p.trade_id ORDER BY COALESCE(MAX(m.started_ts),0),p.opened_ts''').fetchall()
    for p in rows:
        if priority and p['trade_id']!=priority['trade_id']:continue
        if lane=='initial' and p['last_check'] is not None:continue
        if lane=='regular' and p['last_check'] is None:continue
        if c.execute("SELECT 1 FROM position_checks_v0730 WHERE trade_id=? AND status='RUNNING'",(p['trade_id'],)).fetchone():continue
        if not priority and fresh_retry.blocks_regular(c,p['trade_id']):continue
        if priority:
            if now<(connection_audit.retry(c,p['trade_id'])['not_before_ts'] or 0):continue
        elif now<next_due(c,p['trade_id'],p['opened_ts']):continue
        inv=c.execute('SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(p['trade_id'],)).fetchone()
        prior=c.execute("SELECT result_json FROM position_checks_v0730 WHERE trade_id=? AND result_json IS NOT NULL ORDER BY started_ts DESC LIMIT 1",(p['trade_id'],)).fetchone()
        previous=json.loads(prior[0]).get('reserves',{}).get('real_sol_lamports') if prior else None
        import config
        data=dict(route='PUMP_AMM' if migrated(c,p['trade_id']) else 'PUMP_CURVE',trial_loss_bps=config.QUOTE_MAX_LOSS_BPS,id=uuid.uuid4().hex,trade_id=p['trade_id'],mint=p['mint'],opened_ts=p['opened_ts'],scheduled_ts=now,
          inventory=dict(inv) if inv else None,entry_price=str(p['entry']),previous_reserve=previous,wallet_state=wallet_costs.ensure(c))
        __import__('recovery_gate').stamp(data)
        if priority:data['priority_exit']=dict(order_id=priority['order_id'],signal_ts=priority['signal_ts'],detected_ts=priority['detected_ts'],dispatched_ts=now,queue_wait_ms=(now-priority['detected_ts'])*1000)
        c.execute("INSERT INTO position_checks_v0730(id,trade_id,mint,scheduled_ts,started_ts,status,input_json) VALUES(?,?,?,?,?,'RUNNING',?)",(data['id'],p['trade_id'],p['mint'],now,now,json.dumps(data)))
        return data

def classify(r,now):
    if r.get('status')=='AMM_SALE_EXECUTED' and not (0<=now-r.get('snapshot_received_ts',0)<=FRESH_SECONDS and 0<=now-r.get('scheduled_ts',0)<=TIMEOUT):return 'AMM_STALE_RESULT'
    if r.get('status')!='CHECKED':return r.get('status','UNKNOWN')
    age=now-r['snapshot_received_ts']
    if age<0 or age>FRESH_SECONDS or now-r['scheduled_ts']<0 or now-r['scheduled_ts']>TIMEOUT:return 'STALE_RESULT'
    if r['reserves']['status']=='INSUFFICIENT_REAL_RESERVES':return 'INSUFFICIENT_RESERVES'
    if r['sale']['status']!='LOCAL_SWAP_EXECUTED':return 'SALE_REJECTED'
    if not r.get('zero_token_control_ok'):return 'NEGATIVE_CONTROL_FAILED'
    if r['net_status']!='NET_MEETS_FLOOR':return 'NET_BELOW_FLOOR'
    if r.get('reserve_drop'):return 'RESERVE_DROP'
    return 'SELLABLE'

def finish(c,job,r,archive=None,now=None):
    now=time.time() if now is None else now
    status=classify(r,now)
    admission=__import__('recovery_gate').admission(job)
    r=dict(r,recovery_admission_v76=dict(admission,classified_status=status))
    # Cancellation/worker/transport failures explain why no result exists.
    # Preserve that outcome even when the execution gate is independently closed.
    no_result=TECHNICAL|{'SUPERSEDED_BY_EXIT_SIGNAL','INTERRUPTED_STOP','CLEANUP_FAILED','WORKER_EXCEPTION'}
    if not admission['allowed'] and status not in no_result:
        status='RECOVERY_INVALIDATED';r=dict(r,recovery_invalidated_v75=True)
    c.execute('SAVEPOINT monitor_finish')
    try:
        updated=c.execute("UPDATE position_checks_v0730 SET status=?,finished_ts=?,result_json=?,archive=? WHERE id=? AND status='RUNNING'",(status,now,json.dumps(r),archive,job['id'])).rowcount
        if updated:
            __import__('priority_exit').finished(c,job,status,now)
            connection_audit.record_alert(c,job,status,now)
            __import__('fresh_retry').completed(c,job,r,status,now)
            state=trial.timing(c,job['trade_id'],now)
            if job.get('route')=='PUMP_AMM':
                c.execute('INSERT OR IGNORE INTO monitor_timing_v0731 VALUES(?,?,?,?,?)',(job['id'],job['trade_id'],now,state,None))
            else:
                trial.record(c,job,r,status,now)
                import exit_rules_v33
                exit_rules_v33.record(c,job,r,status,now)
            c.execute('INSERT OR IGNORE INTO position_health_v0730(trade_id) VALUES(?)',(job['trade_id'],))
            if state=='BEFORE_CLOSE' and status in RISK:c.execute('UPDATE position_health_v0730 SET first_problem_ts=COALESCE(first_problem_ts,?),first_problem_status=COALESCE(first_problem_status,?) WHERE trade_id=?',(now,status,job['trade_id']))
            elif state=='BEFORE_CLOSE' and status in TECHNICAL:c.execute('UPDATE position_health_v0730 SET first_error_ts=COALESCE(first_error_ts,?) WHERE trade_id=?',(now,job['trade_id']))
            if state=='BEFORE_CLOSE' and status=='SELLABLE':c.execute('UPDATE position_health_v0730 SET last_good_ts=? WHERE trade_id=?',(now,job['trade_id']))
        c.execute('RELEASE monitor_finish')
    except BaseException:
        c.execute('ROLLBACK TO monitor_finish');c.execute('RELEASE monitor_finish');raise

def pack_bundle(bundle,root=ROOT):
    # Deduplicate unchanged large program binaries; every sample remains replayable.
    bundle=json.loads(json.dumps(bundle));folder=root/'blobs';folder.mkdir(parents=True,exist_ok=True)
    for a in bundle['snapshot']['accounts'].values():
        if a and len(json.dumps(a.get('data',[])))>100000:
            raw=json.dumps(a['data']).encode();digest=hashlib.sha256(raw).hexdigest();path=folder/(digest+'.zlib')
            if not path.exists():
                tmp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp');tmp.write_bytes(zlib.compress(raw));tmp.replace(path)
            a['data']={'blob_sha256':digest}
    return bundle

def unpack_bundle(bundle,root=ROOT):
    bundle=json.loads(json.dumps(bundle))
    for a in bundle['snapshot']['accounts'].values():
        if a and isinstance(a.get('data'),dict):
            digest=a['data']['blob_sha256']
            if len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest):raise ValueError('INVALID_BLOB_ID')
            raw=zlib.decompress((root/'blobs'/(digest+'.zlib')).read_bytes())
            if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('BLOB_HASH_MISMATCH')
            a['data']=json.loads(raw)
    return bundle

def reserve_evidence(bundle):
    import struct,local_simulation as lab
    from solders.rent import Rent
    accounts=bundle['snapshot']['accounts']
    curve=lab.v.decode_curve(accounts[bundle['account_names']['bonding_curve']])
    rent=Rent(*struct.unpack('<QdB',lab.raw(accounts[lab.RENT]))).minimum_balance(curve['data_length'])
    spendable=max(0,curve['lamports']-rent-curve['creator_fee_held']-curve['protocol_fee_held'])
    amount=int(bundle['amount']);den=curve['virtual_token']+amount
    gross=(amount*curve['virtual_sol']+den-1)//den
    return dict(real_sol_lamports=curve['real_sol'],spendable_lamports=spendable,rent_lamports=rent,
      held_fees_lamports=curve['creator_fee_held']+curve['protocol_fee_held'],sell_gross_upper_lamports=gross,
      status='INSUFFICIENT_REAL_RESERVES' if min(curve['real_sol'],spendable)<gross else 'RESERVES_COVER_GROSS')

def stage(out,name):
    import rpc_timing
    rpc_timing.stage(name)
    p=out/'progress.json';tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(dict(stage=name,ts=time.time())),encoding='utf-8');tmp.replace(p)

def experiment(data,out):
    import rpc_timing
    rpc_timing.start(out)
    if data.get('route')=='PUMP_AMM':return amm_experiment(data,out)
    stage(out,'IMPORTS')
    import local_simulation as lab,roundtrip_simulation as rt,exit_quotes as q,execution_verification as v
    from decimal import Decimal,localcontext
    bundle=None;r=dict(status='NOT_STARTED',scheduled_ts=data['scheduled_ts'],paper_mutated=False,broadcast=False)
    try:
        stage(out,'TOKEN_METADATA')
        d,raw=q.resolve_amount(data['inventory'])
        with localcontext() as ctx:
            ctx.prec=60
            import config
            floor=max(1,int(Decimal(raw)/Decimal(10)**d*Decimal(data['entry_price'])*10**9*Decimal(10000-data.get('trial_loss_bps',config.QUOTE_MAX_LOSS_BPS))/10000))
        stage(out,'POOL_SNAPSHOT')
        bundle=lab.capture(data['mint'],int(raw),1,wallet=data['wallet_state']['wallet'])
        stage(out,'SALE_SIMULATION')
        sale=lab.run_snapshot(bundle,volume_scenario='WALLET',persisted_volume=data['wallet_state']['volume'])
        stage(out,'NEGATIVE_CONTROL')
        ctrl=lab.run_snapshot(bundle,balance_override=0,volume_scenario='WALLET',persisted_volume=data['wallet_state']['volume'])
        stage(out,'RESERVES')
        reserves=reserve_evidence(bundle)
        prev=data.get('previous_reserve')
        r.update(status='CHECKED',amount_decimals=d,snapshot_received_ts=bundle['snapshot']['received_ts'],snapshot_started_ts=bundle['snapshot']['started_ts'],slot=bundle['snapshot']['slot'],reserves=reserves,
          reserve_drop=prev is not None and prev>0 and reserves['real_sol_lamports']<prev*.5,
          sale=sale,minimum_net_lamports=floor,net_status='NET_MEETS_FLOOR' if sale.get('seller_net_sol_delta_lamports',-1)>=floor else 'NET_BELOW_FLOOR',
          zero_token_control_ok=ctrl['status']=='LOCAL_SWAP_FAILED' and '6023' in str(ctrl.get('err')),
          zero_token_control_error=ctrl.get('err'),failure=rt.failure_classification(sale,bundle))
    except Exception as e:
        code=str(e) if isinstance(e,(lab.LabError,q.ProbeError,v.VerificationError)) else type(e).__name__
        category='REQUEST_INVALID_JSON' if isinstance(e,json.JSONDecodeError) else MIGRATION_STATUS if code=='COMPLETE_CURVE_UNSUPPORTED' else 'UNSUPPORTED_POOL' if 'UNSUPPORTED' in code or code=='NON_SOL_CURVE' else 'REQUEST_ERROR' if 'RPC' in code or code in ('TimeoutError','URLError','HTTPError','ProbeError') else 'DATA_REJECTED'
        r.update(status=category,error_code=code,network_error=connection_audit.diagnostic(e),error_stage=json.loads((out/'progress.json').read_text())['stage'])
        if isinstance(e,v.VerificationError):r['unsupported_details']=e.details
    stage(out,'SAVE_RESULT')
    r['rpc_timing']=rpc_timing.snapshot()
    r['finished_ts']=time.time()
    with zipfile.ZipFile(out/'evidence.tmp','w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('input.json',json.dumps(data));z.writestr('result.json',json.dumps(r))
        if bundle:z.writestr('snapshot.json',json.dumps(pack_bundle(bundle,ROOT)))
    (out/'evidence.tmp').replace(out/'evidence.zip')
    (out/'result.tmp').write_text(json.dumps(r));(out/'result.tmp').replace(out/'result.json')
    return r

def amm_experiment(data,out):
    import rpc_timing
    if rpc_timing.ACTIVE is None or rpc_timing.ACTIVE.path.parent!=Path(out):rpc_timing.start(out)
    import amm_simulation as amm,exit_quotes as q
    evidence={};r=dict(status='AMM_NOT_STARTED',scheduled_ts=data['scheduled_ts'],route='PUMP_AMM',broadcast=False,paper_mutated=False)
    try:
        stage(out,'AMM_TOKEN_METADATA');decimals,raw=q.resolve_amount(data['inventory']);r['amount_decimals']=decimals
        stage(out,'AMM_POOL_SNAPSHOT');bundle=amm.capture(data['mint'],int(raw),data['wallet_state']['wallet'],evidence)
        r.update(snapshot_received_ts=bundle['snapshot']['received_ts'],slot=bundle['snapshot']['slot'],pool=bundle['account_names']['pool'],reserves=amm.reserves(bundle),raw_amount=str(raw),address_status='CANONICAL_POOL_VERIFIED')
        if r['reserves']['status']!='AMM_RESERVES_AVAILABLE':r['status']='AMM_INSUFFICIENT_RESERVES'
        else:
            stage(out,'AMM_SALE_SIMULATION');sale=amm.run_snapshot(bundle)
            stage(out,'AMM_NEGATIVE_CONTROL');ctrl=amm.run_snapshot(bundle,zero=True)
            r.update(status=sale['status'],sale=sale,zero_token_control_status=ctrl['status'],zero_token_control_error=ctrl.get('err'),zero_token_control_ok=ctrl['status']=='AMM_SIMULATION_FAILED')
            if sale['status']=='AMM_SALE_EXECUTED' and not r['zero_token_control_ok']:r['status']='AMM_NEGATIVE_CONTROL_FAILED'
    except Exception as e:
        code=str(e) if isinstance(e,(amm.AmmError,amm.lab.LabError,q.ProbeError,amm.lab.v.VerificationError)) else type(e).__name__
        market={'AMM_POOL_NOT_FOUND','AMM_POOL_FEATURE_UNSUPPORTED','AMM_MINT_EXTENSIONS_UNSUPPORTED','AMM_VAULT_EXTENSIONS_UNSUPPORTED','AMM_SELL_DISABLED','AMM_BUYBACK_ACCOUNT_MISSING','AMM_UNSUPPORTED_LAYOUT','AMM_UNKNOWN_EXTENSION'}
        status=code if code in market else 'AMM_REQUEST_INVALID_JSON' if isinstance(e,json.JSONDecodeError) else 'AMM_REQUEST_ERROR' if 'RPC' in code or code in ('TimeoutError','URLError','HTTPError') else 'AMM_DATA_REJECTED'
        r.update(status=status,error_code=code,network_error=connection_audit.diagnostic(e),error_stage=json.loads((out/'progress.json').read_text())['stage'],error_details=getattr(e,'details',{}))
    stage(out,'SAVE_RESULT');r['rpc_timing']=rpc_timing.snapshot();r['finished_ts']=time.time()
    with zipfile.ZipFile(out/'evidence.tmp','w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('input.json',json.dumps(data));z.writestr('result.json',json.dumps(r))
        for name,snapshot in evidence.items():
            if name=='bundle':snapshot=pack_bundle(snapshot,ROOT)
            else:snapshot=pack_bundle({'snapshot':snapshot},ROOT)['snapshot']
            z.writestr(name+'.json',json.dumps(snapshot))
    (out/'evidence.tmp').replace(out/'evidence.zip')
    (out/'result.tmp').write_text(json.dumps(r));(out/'result.tmp').replace(out/'result.json')
    return r

async def stop_simulation(proc,diagnostic=None):
    """Reap the known simulation launcher and its descendants (Windows venv wrapper)."""
    import psutil
    family=[]
    def note(phase,**fields):
        if diagnostic:diagnostic.event(phase,**fields)
    note('CLEANUP_INSPECT',child_pid=proc.pid)
    try:family=psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:pass
    except psutil.Error as exc:
        note('CLEANUP_INSPECT_FAILED',exception=__import__('child_diagnostics').exception(exc))
        # The direct child is still reaped; inability to inspect descendants is explicit.
        family=None
    inspection_failed=family is None
    note('CLEANUP_KILL_DESCENDANTS',inspection_failed=inspection_failed,descendants=[__import__('child_diagnostics').identity(p) for p in (family or [])])
    for child in reversed(family or []):
        try:child.kill()
        except psutil.NoSuchProcess:pass
        except psutil.Error as exc:
            inspection_failed=True;note('CLEANUP_KILL_DESCENDANT_FAILED',pid=child.pid,exception=__import__('child_diagnostics').exception(exc))
    note('CLEANUP_KILL_LAUNCHER',returncode=proc.returncode)
    if proc.returncode is None:
        try:proc.kill()
        except ProcessLookupError:pass
        except PermissionError as exc:
            note('CLEANUP_KILL_LAUNCHER_FAILED',exception=__import__('child_diagnostics').exception(exc));raise RuntimeError('SIMULATION_CHILD_CLEANUP_UNCONFIRMED') from exc
    note('CLEANUP_WAIT_LAUNCHER',timeout_s=3);wait_started=time.monotonic()
    try:await asyncio.wait_for(proc.wait(),timeout=3)
    except asyncio.TimeoutError:raise RuntimeError('SIMULATION_CHILD_CLEANUP_TIMEOUT') from None
    note('CLEANUP_LAUNCHER_EXIT',returncode=proc.returncode,wait_ms=(time.monotonic()-wait_started)*1000)
    if inspection_failed:raise RuntimeError('SIMULATION_CHILD_CLEANUP_UNCONFIRMED')
    # Process handles include creation identity; no broad name-based termination.
    note('CLEANUP_WAIT_DESCENDANTS',timeout_s=2)
    deadline=time.monotonic()+2
    while True:
        alive=[]
        for child in family:
            try:
                if child.is_running() and child.status()!=psutil.STATUS_ZOMBIE:alive.append(child)
            except psutil.NoSuchProcess:pass
        if not alive:
            note('CLEANUP_CONFIRMED',returncode=proc.returncode,descendants_remaining=[]);return
        if time.monotonic()>=deadline:
            note('CLEANUP_DESCENDANTS_REMAIN',pids=[p.pid for p in alive]);raise RuntimeError('SIMULATION_CHILD_CLEANUP_TIMEOUT')
        await asyncio.sleep(.02)

async def launch_check(job,out,replay=None,timeout=None):
    """Same launch path for production and offline startup test. Never inherit stdin."""
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    (out/'input.json').write_text(json.dumps(job),encoding='utf-8')
    from child_diagnostics import Trace
    trace=Trace(out,job);trace.event('SPAWN');cleanup_error=None
    stage(out,'SPAWN');proc=None;result=None
    options={}
    if sys.platform=='win32':
        import subprocess
        options['creationflags']=subprocess.CREATE_NEW_PROCESS_GROUP
    args=[sys.executable,'-u',str(Path(__file__).resolve()),'--child',str(out)]
    if replay:args+=['--replay',str(replay)]
    try:
        with (out/'child.log').open('wb') as log:
            proc=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.DEVNULL,stdout=log,stderr=log,cwd=str(Path(__file__).resolve().parent),**options)
            trace.data['child_pid']=proc.pid
            try:trace.data['child_created_ts']=__import__('psutil').Process(proc.pid).create_time()
            except __import__('psutil').Error:pass
            trace.event('WAIT_RESULT',timeout_s=TIMEOUT if timeout is None else timeout)
            wait_started=time.monotonic()
            await asyncio.wait_for(proc.wait(),TIMEOUT if timeout is None else timeout)
            trace.event('CHILD_EXIT',returncode=proc.returncode,wait_ms=(time.monotonic()-wait_started)*1000)
        result=json.loads((out/'result.json').read_text()) if proc.returncode==0 and (out/'result.json').exists() else {'status':'CHILD_ERROR'}
    except asyncio.TimeoutError as e:
        trace.error('worker_exception',e);result={'status':'REQUEST_TIMEOUT','network_error':{'category':'CHILD_DEADLINE','chain':[],'note':'Whole child exceeded deadline; transport cause unknown.'}}
    except asyncio.CancelledError as e:
        trace.error('cancellation',e)
        result={'status':'INTERRUPTED_STOP'}
        raise
    except Exception as e:
        trace.error('worker_exception',e);result={'status':'WORKER_ERROR','error_code':type(e).__name__}
    finally:
        try:
            if proc is not None and proc.returncode is None:await stop_simulation(proc,diagnostic=trace)
        except Exception as e:
            cleanup_error=e;trace.error('cleanup_exception',e)
            result=dict(status='CLEANUP_FAILED',worker_result=result)
        trace.event('SAVE_DIAGNOSTICS',returncode=proc.returncode if proc else None)
        result=result or {'status':'WORKER_ERROR'}
        result['child_diagnostics_v73']=trace.data
        result=result or {'status':'WORKER_ERROR'}
        try:
            import rpc_timing
            rpc_timing.attach(result,out)
            try:result['progress']=json.loads((out/'progress.json').read_text())
            except (OSError,ValueError):pass
            result['child_returncode']=proc.returncode if proc else None
            # URLs may contain credentials. Keep error types, stack locations and stage, redact URLs.
            import re
            logpath=out/'child.log'
            if logpath.exists():
                with logpath.open('rb') as f:
                    f.seek(0,2);size=f.tell();f.seek(max(0,size-12000));tail=f.read(12000).decode(errors='replace')
                clean=__import__('child_diagnostics').clean(tail,12000)
                logpath.write_text(clean,encoding='utf-8');result['child_log_tail']=clean[-3000:]
            if result['status']=='CHILD_ERROR' and result.get('child_returncode') in (3221225786,-1073741510):
                result['status']='INTERRUPTED_STOP'
            (out/'diagnostic.json').write_text(json.dumps(result),encoding='utf-8')
            with zipfile.ZipFile(out/'diagnostic.zip','w',zipfile.ZIP_DEFLATED) as z:
                for name in ('input.json','progress.json','rpc_timing.json','child.log','diagnostic.json','child_lifecycle_v73.json'):
                    if (out/name).exists():z.write(out/name,name)
        except Exception as e:
            trace.error('diagnostic_exception',e)
            if cleanup_error is None:cleanup_error=e
        if cleanup_error is not None:
            cleanup_error.child_diagnostics_v73=trace.data
            raise cleanup_error
    return result

async def run_job(job,connection=None):
    if connection is None:
        import database as db
        connection=db.con()
    out=ROOT/job['id']
    worker_ts=time.time();worker_mono=time.monotonic()
    __import__('priority_exit').worker_started(connection,job,worker_ts)
    try:r=await launch_check(job,out,timeout=job.get('deadline_timeout_s'))
    except Exception as exc:
        from child_diagnostics import exception
        detail=dict(status='CLEANUP_FAILED' if str(exc).startswith('SIMULATION_CHILD_CLEANUP_') or getattr(exc,'child_diagnostics_v73',{}).get('cleanup_exception') else 'WORKER_ERROR',worker_exception=exception(exc),child_diagnostics_v73=getattr(exc,'child_diagnostics_v73',{}))
        finish(connection,job,detail,str(out/'diagnostic.zip'))
        raise
    except asyncio.CancelledError:
        finish(connection,job,{'status':job.get('cancel_status','INTERRUPTED_STOP')},str(out/'diagnostic.zip'));raise
    r['scheduler_timing_v65']=dict(scheduled_ts=job['scheduled_ts'],worker_started_ts=worker_ts,event_loop_dispatch_ms=max(0,(worker_ts-job['scheduled_ts'])*1000),worker_duration_ms=(time.monotonic()-worker_mono)*1000)
    archive=out/'evidence.zip'
    finish(connection,job,r,str(archive if archive.exists() else out/'diagnostic.zip'))

async def worker():
    import database as db
    while True:
        with db.atomic():job=next_job(db.con(),time.time())
        if job:await run_job(job)
        else:await asyncio.sleep(.5)

def summary(c,now=None):
    now=time.time() if now is None else now
    counts=dict(c.execute('SELECT status,COUNT(*) FROM position_checks_v0730 GROUP BY status'))
    rows=[]
    for p in c.execute('SELECT * FROM positions ORDER BY opened_ts'):
        last=c.execute('SELECT * FROM position_checks_v0730 WHERE trade_id=? ORDER BY started_ts DESC LIMIT 1',(p['trade_id'],)).fetchone()
        health=c.execute('SELECT * FROM position_health_v0730 WHERE trade_id=?',(p['trade_id'],)).fetchone()
        r=dict(last) if last else {};r.pop('input_json',None);r.pop('archive',None)
        r['result']=json.loads(r.pop('result_json',None) or '{}')
        r.update(mint=p['mint'],trade_id=p['trade_id'],principal=p['size'],age_s=max(0,now-p['opened_ts']),health=dict(health) if health else {})
        r['next_due_ts']=next_due(c,p['trade_id'],p['opened_ts'])
        r['route']='PUMP_AMM' if migrated(c,p['trade_id']) else 'PUMP_CURVE'
        r['stale']=now>r['next_due_ts']+TIMEOUT
        if r.get('finished_ts'):r['check_age_s']=max(0,now-r['finished_ts'])
        rows.append(r)
    recent=[dict(r) for r in c.execute('SELECT id,trade_id,mint,started_ts,finished_ts,status FROM position_checks_v0730 ORDER BY started_ts DESC LIMIT 100')]
    timing_counts={};before_counts={};after_counts={}
    for x in c.execute('SELECT trade_id,finished_ts,status FROM position_checks_v0730'):
        timing=trial.timing(c,x['trade_id'],x['finished_ts']) if x['finished_ts'] is not None else 'RUNNING'
        timing_counts[timing]=timing_counts.get(timing,0)+1
        target=before_counts if timing=='BEFORE_CLOSE' else after_counts if timing=='AFTER_CLOSE' else None
        if target is not None:target[x['status']]=target.get(x['status'],0)+1
    for x in recent:x['timing']=trial.timing(c,x['trade_id'],x['finished_ts']) if x['finished_ts'] is not None else 'RUNNING'
    amm_rows=[]
    for check in c.execute('SELECT id,trade_id,mint,status,input_json,result_json,finished_ts FROM position_checks_v0730 ORDER BY started_ts DESC'):
        job=json.loads(check['input_json'] or '{}')
        if job.get('route')!='PUMP_AMM':continue
        detail=json.loads(check['result_json'] or '{}');amm_rows.append(dict(id=check['id'],trade_id=check['trade_id'],mint=check['mint'],status=check['status'],finished_ts=check['finished_ts'],timing=trial.timing(c,check['trade_id'],check['finished_ts']) if check['finished_ts'] else 'RUNNING',result=detail))
    from collections import Counter
    migration=dict(counts=dict(Counter(x['status'] for x in amm_rows)),recent=amm_rows[:50],open_positions=sum(migrated(c,p['trade_id']) for p in rows),interval_s=AMM_INTERVAL)
    return dict(migration=migration,counts=counts,positions=rows,recent=recent,timing_counts=timing_counts,before_close_counts=before_counts,after_close_counts=after_counts,interval_s=INTERVAL,timeout_s=TIMEOUT,
      note='Один приоритетный свежий запрос после SELL без ожидания таймера; technical backoff сохранён. В остальное время таймер 10 с на позицию; при медленном RPC фактический интервал больше. Проверяется прямой Pump-маршрут с синтетическими токенами. Проверка не закрывает PAPER-позицию и не обещает исполнение в сети.')

def export_evidence(db_path,folder='exports'):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;report=summary(c)
        rows=[dict(r) for r in c.execute('SELECT * FROM position_checks_v0730 ORDER BY started_ts')]
    target=folder/('position_monitor_'+time.strftime('%Y%m%d_%H%M%S')+'.zip')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_STORED) as z:
        z.writestr('summary.json',json.dumps(report,ensure_ascii=False));z.writestr('checks.json',json.dumps(rows));missing=[]
        for r in rows:
            f=ROOT/r['id']/'evidence.zip'
            if f.exists():z.write(f,r['id']+'/evidence.zip')
            elif r['archive']:missing.append(r['id'])
        for f in (ROOT/'blobs').glob('*.zlib'):z.write(f,'blobs/'+f.name)
        z.writestr('missing_evidence.json',json.dumps(missing))
    return target
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--child');ap.add_argument('--replay');a=ap.parse_args()
    if a.child:
        out=Path(a.child);stage(out,'CHILD_STARTED')
        if a.replay:
            import local_simulation as lab, socket
            with zipfile.ZipFile(a.replay) as z:bundle=json.loads(z.read('snapshot.json'))
            def offline(*args,**kwargs):
                bundle['snapshot']['received_ts']=time.time();bundle['snapshot']['started_ts']=time.time()
                return bundle
            if bundle.get('schema')=='amm_sell_v1':
                import amm_simulation
                def amm_offline(mint,amount,wallet,evidence=None):
                    if (mint,str(amount),wallet)!=(bundle['mint'],bundle['amount'],bundle['wallet']):raise RuntimeError('REPLAY_INPUT_MISMATCH')
                    bundle['snapshot']['received_ts']=time.time();bundle['snapshot']['started_ts']=time.time()
                    if evidence is not None:evidence['bundle']=bundle
                    return bundle
                amm_simulation.capture=amm_offline
            else:lab.capture=offline
            def blocked(*args,**kwargs):raise RuntimeError('NETWORK_FORBIDDEN_IN_SELFTEST')
            socket.socket.connect=blocked
        experiment(json.loads((out/'input.json').read_text()),out)
    else:
        import config
        print(export_evidence(config.DB))
