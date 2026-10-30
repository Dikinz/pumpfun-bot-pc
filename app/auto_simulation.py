"""One durable, isolated, read-only local experiment per exit signal."""
import asyncio,json,sys,time,zipfile,sqlite3,argparse,re
from pathlib import Path
from decimal import Decimal,localcontext
import config,exit_comparison,wallet_costs
ROOT=Path(__file__).resolve().parent/'auto_simulations'
MAX_QUEUE_AGE=60
TIMEOUT=120
TIMELY_SECONDS=5
SCHEMA='''CREATE TABLE IF NOT EXISTS auto_sim_v0727(
 order_id TEXT PRIMARY KEY,mint TEXT,signal_ts REAL,input_json TEXT,
 status TEXT,started_ts REAL,finished_ts REAL,result_json TEXT,archive TEXT);'''

def enqueue(c,oid,tid,mint,price,now):
    inv=c.execute('SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(tid,)).fetchone()
    entry=c.execute("SELECT total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(tid,)).fetchone()
    data=dict(order_id=oid,trade_id=tid,entry_cost_units=str(entry[0]) if entry else None,mint=mint,signal_ts=now,signal_price=str(price),
              max_loss_bps=config.QUOTE_MAX_LOSS_BPS,inventory=dict(inv) if inv else None)
    c.execute("INSERT OR IGNORE INTO auto_sim_v0727(order_id,mint,signal_ts,input_json,status) VALUES(?,?,?,?,'PENDING')",
              (oid,mint,now,json.dumps(data)))

def recover(c):
    for row in c.execute("SELECT order_id FROM auto_sim_v0727 WHERE status IN ('PENDING','RUNNING')").fetchall():
        finish(c,row[0],{'status':'INTERRUPTED_RESTART'})

def claim(c,now):
    for row in c.execute("SELECT order_id FROM auto_sim_v0727 WHERE status='PENDING' AND signal_ts<?",(now-MAX_QUEUE_AGE,)).fetchall():
        finish(c,row[0],{'status':'QUEUE_EXPIRED'})
    row=c.execute("SELECT * FROM auto_sim_v0727 WHERE status='PENDING' ORDER BY signal_ts LIMIT 1").fetchone()
    if row:
        c.execute("UPDATE auto_sim_v0727 SET status='RUNNING',started_ts=? WHERE order_id=?",(now,row['order_id']))
        return dict(row)

def finish(c,oid,result,archive=None):
    row=c.execute('SELECT signal_ts,status FROM auto_sim_v0727 WHERE order_id=?',(oid,)).fetchone()
    if row and row[1] not in ('PENDING','RUNNING'):return
    result.setdefault('result_delay_s',time.time()-row[0] if row else None)
    result.setdefault('paper_mutated',False);result.setdefault('broadcast',False)
    available=time.time()
    c.execute('SAVEPOINT simulation_finish')
    try:
        c.execute('UPDATE auto_sim_v0727 SET status=?,finished_ts=?,result_json=?,archive=? WHERE order_id=?',
                  (result['status'],available,json.dumps(result),archive,oid))
        exit_comparison.record_simulation(c,oid,result,available)
        mint=c.execute('SELECT mint FROM auto_sim_v0727 WHERE order_id=?',(oid,)).fetchone()
        if mint:
            wallet_costs.record(c,oid,mint[0],result.get('wallet_scenario',{'status':'NOT_RUN_'+result['status']}),available)
        c.execute('RELEASE simulation_finish')
    except BaseException:
        c.execute('ROLLBACK TO simulation_finish');c.execute('RELEASE simulation_finish');raise

def timing(signal,snapshot_start,snapshot_end,finished):
    delay=snapshot_end-signal
    return dict(snapshot_request_delay_s=snapshot_start-signal,snapshot_received_delay_s=delay,
                result_delay_s=finished-signal,timing_status='CLOCK_ANOMALY' if min(snapshot_start-signal,delay,finished-signal)<0 else ('WITHIN_5S' if delay<=TIMELY_SECONDS else 'LATE_SNAPSHOT'))

def experiment(data,out):
    # Imported only in child: the feed process never loads or executes LiteSVM.
    import local_simulation as lab
    import roundtrip_simulation as rt
    import exit_quotes as q
    import execution_verification as v
    report=dict(status='NOT_STARTED',signal_ts=data['signal_ts'],child_started_ts=time.time(),
                paper_mutated=False,broadcast=False,mainnet_execution_proven=False,network_fee_lamports=None)
    bundle=None
    try:
        d,raw=q.resolve_amount(data['inventory'])
        with localcontext() as ctx:
            ctx.prec=60
            minimum=int(Decimal(raw)/Decimal(10)**d*Decimal(data['signal_price'])*Decimal(10)**9*Decimal(10000-data['max_loss_bps'])/Decimal(10000))
        if minimum<=0:raise lab.LabError('INVALID_PRICE_FLOOR')
        report.update(raw_amount=raw,minimum_lamports=str(minimum),sizing_finished_ts=time.time())
        bundle=lab.capture(data['mint'],int(raw),minimum,**({'wallet':data['wallet_state']['wallet']} if data.get('wallet_state') else {}))
        report.update(timing(data['signal_ts'],bundle['snapshot']['started_ts'],bundle['snapshot']['received_ts'],time.time()))
        scenarios={}
        for branch in ('FIRST','REPEAT'):
            try:
                r=lab.run_snapshot(bundle,volume_scenario=branch)
                r['primary_finished_ts']=time.time()
                control=lab.run_snapshot(bundle,balance_override=0,volume_scenario=branch)
                r['zero_token_control_status']=control['status'];r['zero_token_control_error']=control.get('err')
                r['failure_classification']=rt.failure_classification(r,bundle)
                r['outcome']='PRICE_REJECTED' if '6003' in str(r.get('err')) else r['status']
                r.update(timing(data['signal_ts'],bundle['snapshot']['started_ts'],bundle['snapshot']['received_ts'],time.time()))
                assessed=exit_comparison.evaluate(r,data['signal_ts'],time.time())
                r.update({k:assessed[k] for k in ('technical_status','net_status','net_margin_lamports')})
                # Final timing admission is decided only after the archive is durable.
                r['admission_source']='exit_comparison_v0728 after parent persistence'
            except Exception as e:
                r={'status':str(e) if isinstance(e,lab.LabError) else 'SCENARIO_ERROR_'+type(e).__name__}
            scenarios[branch]=r
        report.update(scenarios['FIRST'])
        report['scenarios']=scenarios
        if data.get('wallet_state'):
            try:
                wr=lab.run_snapshot(bundle,volume_scenario='WALLET',persisted_volume=data['wallet_state']['volume'])
                ctrl=lab.run_snapshot(bundle,balance_override=0,volume_scenario='WALLET',persisted_volume=data['wallet_state']['volume'])
                wr['zero_token_control_ok']=ctrl['status']=='LOCAL_SWAP_FAILED' and '6023' in str(ctrl.get('err'))
                wr['zero_token_control_error']=ctrl.get('err')
                wr['net_status']='NET_MEETS_FLOOR' if wr.get('seller_net_sol_delta_lamports',-1)>=minimum else 'NET_BELOW_FLOOR'
                report['wallet_scenario']=wr
            except Exception as e:report['wallet_scenario']={'status':'WALLET_ERROR_'+type(e).__name__}

        report['result_delay_s']=time.time()-data['signal_ts']
    except Exception as e:
        report['status']=str(e) if isinstance(e,(q.ProbeError,v.VerificationError)) else (str(e) if isinstance(e,lab.LabError) else 'REQUEST_OR_LAB_ERROR_'+type(e).__name__)
        report['result_delay_s']=time.time()-data['signal_ts']
        if isinstance(e,v.VerificationError):report['unsupported_details']=e.details
    report['finished_ts']=time.time()
    # Archive complete evidence before publishing result; unfinished files aren't exported.
    tmp=out/'evidence.tmp'
    with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('input.json',json.dumps(data))
        z.writestr('result.json',json.dumps(report))
        if bundle:z.writestr('snapshot.json',json.dumps(bundle))
    tmp.replace(out/'evidence.zip')
    tmp=out/'result.tmp';tmp.write_text(json.dumps(report));tmp.replace(out/'result.json')
    return report

async def run_job(job):
    import database as db
    oid=job['order_id']
    if not re.fullmatch('[a-f0-9]{32}',oid):
        finish(db.con(),oid,{'status':'INVALID_JOB_ID'});return
    out=ROOT/oid;out.mkdir(parents=True,exist_ok=True)
    data=json.loads(job['input_json']);data['wallet_state']=wallet_costs.ensure(db.con())
    job['input_json']=json.dumps(data)
    db.con().execute('UPDATE auto_sim_v0727 SET input_json=? WHERE order_id=?',(job['input_json'],oid))
    (out/'input.json').write_text(job['input_json'])
    try:
        import position_monitor as pm
        payload=json.loads(job['input_json']);payload.setdefault('id',job['order_id'])
        result=await pm.launch_check(payload,out,timeout=TIMEOUT,child_script=__file__)
        if result.get('status')=='WORKER_TIMEOUT':result['status']='SIMULATION_TIMEOUT'
        finish(db.con(),oid,result,str(out/'evidence.zip') if (out/'evidence.zip').exists() else None)
    except asyncio.TimeoutError:finish(db.con(),oid,{'status':'SIMULATION_TIMEOUT'})
    except asyncio.CancelledError:
        finish(db.con(),oid,{'status':'INTERRUPTED_STOP'});raise
    except Exception as e:
        finish(db.con(),oid,{'status':'WORKER_ERROR_'+type(e).__name__,'error_type':type(e).__name__,'child_diagnostics_v73':getattr(e,'child_diagnostics_v73',{})})
        if str(e).startswith('SIMULATION_CHILD_CLEANUP_') or getattr(e,'child_diagnostics_v73',{}).get('cleanup_exception'):raise

async def worker():
    import database as db
    while True:
        with db.atomic():job=claim(db.con(),time.time())
        if job:await run_job(job)
        else:await asyncio.sleep(.2)

def summary(c,limit=100):
    counts=dict(c.execute('SELECT status,COUNT(*) FROM auto_sim_v0727 GROUP BY status'))
    meta=dict(c.execute('SELECT k,v FROM meta'));now=time.time();hb=float(meta.get('heartbeat_ts',0))
    if hb and (now-hb>15 or meta.get('stream_state')=='STOPPED'):now=hb
    rows=[]
    for row in c.execute('''SELECT a.*,p.status paper_status,p.fill_ts FROM auto_sim_v0727 a
                            LEFT JOIN paper_orders_v0720 p ON p.order_id=a.order_id ORDER BY a.signal_ts DESC LIMIT ?''',(limit,)):
        r=dict(row);r['result']=json.loads(r.pop('result_json') or '{}');r.pop('input_json');r.pop('archive')
        r['paper_wait_s']=max(0,(r['fill_ts'] or now)-r['signal_ts'])
        r['result_before_paper_fill']=None if not r['finished_ts'] else (not r['fill_ts'] or r['finished_ts']<r['fill_ts'])
        rows.append(r)
    return dict(comparison=exit_comparison.summary(c,limit),counts=counts,rows=rows,timely_seconds=TIMELY_SECONDS,queue_max_age_s=MAX_QUEUE_AGE,timeout_s=TIMEOUT)

def export_evidence(db_path,folder='exports'):
    folder=Path(folder);folder.mkdir(exist_ok=True,parents=True)
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;report=summary(c,limit=-1)
        archives=list(c.execute('SELECT order_id,archive FROM auto_sim_v0727 WHERE started_ts IS NOT NULL OR archive IS NOT NULL'))
    target=folder/('auto_simulations_'+time.strftime('%Y%m%d_%H%M%S')+'.zip')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_STORED) as z:
        z.writestr('summary.json',json.dumps(report,ensure_ascii=False,indent=2))
        missing=[]
        for oid,path in archives:
            p=ROOT/oid/'evidence.zip'
            if p.is_file():z.write(p,oid+'/evidence.zip')
            else:missing.append(oid)
            for name in ('diagnostic.zip','child_lifecycle_v73.json'):
                f=ROOT/oid/name
                if f.exists():z.write(f,oid+'/'+name)
        z.writestr('missing_evidence.json',json.dumps(missing))
    return target

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--child');args=parser.parse_args()
    if args.child:
        out=Path(args.child);experiment(json.loads((out/'input.json').read_text()),out)
    else:print(export_evidence(config.DB))
