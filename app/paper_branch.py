"""Independent durable PAPER database. Same strategy, shared normalized live input."""
import asyncio,json,sqlite3,time,sys,os,queue,threading
from pathlib import Path
from contextlib import closing

SCHEMA='''CREATE TABLE IF NOT EXISTS branch_info(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS branch_amm_settlements(trade_id TEXT PRIMARY KEY,check_id TEXT UNIQUE,available_ts REAL,settled_ts REAL,gross REAL,receipt REAL,cost REAL,details TEXT);
'''

def path(primary):
    p=Path(primary).resolve();return p.with_name(p.stem+'_amm_paper.db')

def _source_id(c):
    row=c.execute("SELECT v FROM meta WHERE k='paper_source_identity'").fetchone()
    return row[0] if row else None

def _legacy_pair(src,dst):
    """A shared UUID run and immutable start snapshot anchor the original fork.

    Final run states and SELLs deliberately differ between the two strategies.
    Reject conflicting common BUYs; do not infer kinship from file names/balances.
    """
    try:
        runs={r[0]:r[1:] for r in src.execute('SELECT id,started_ts,first_id,start_json FROM runs_v45')}
        shared=any(r[0] in runs and runs[r[0]]==r[1:] for r in dst.execute('SELECT id,started_ts,first_id,start_json FROM runs_v45'))
        if not shared:return False
        buys={r[0]:r[1:] for r in src.execute("SELECT trade_id,ts,mint,size,price FROM trades WHERE side='BUY'")}
        return all(r[0] not in buys or buys[r[0]]==r[1:] for r in dst.execute("SELECT trade_id,ts,mint,size,price FROM trades WHERE side='BUY'"))
    except sqlite3.OperationalError:return False

def _relabel_source_runs(src,c):
    """Repair branches seeded before v0.7.101.3: a copied primary run marked INTERRUPTED by the branch."""
    try:
        source=dict(src.execute('SELECT id,status FROM runs_v45'))
        stale=[r[0] for r in c.execute("SELECT id FROM runs_v45 WHERE status='INTERRUPTED'") if source.get(r[0]) not in (None,'INTERRUPTED')]
        with c:c.executemany("UPDATE runs_v45 SET status='SOURCE_RUN' WHERE id=?",[(x,) for x in stale])
    except sqlite3.OperationalError:pass

def seed(primary,target=None):
    import uuid
    primary=Path(primary).resolve();target=Path(target).resolve() if target is not None else path(primary)
    # Source must exist: never silently create an empty replacement database.
    with closing(sqlite3.connect(primary.as_uri()+'?mode=rw',uri=True)) as src:
        identity=_source_id(src)
        if not target.exists():
            identity=identity or uuid.uuid4().hex
            with src:src.execute("INSERT OR IGNORE INTO meta VALUES('paper_source_identity',?)",(identity,))
            temp=target.with_suffix('.seed.tmp')
            temp.unlink(missing_ok=True)
            with closing(sqlite3.connect(temp)) as dst:
                src.backup(dst)
                # A copied RUNNING run belongs to the primary process; run_audit.begin in the branch
                # would otherwise record it as a false INTERRUPTED branch run.
                try:
                    with dst:dst.execute("UPDATE runs_v45 SET status='SOURCE_RUN' WHERE status='RUNNING'")
                except sqlite3.OperationalError:pass
            temp.replace(target)
        with closing(sqlite3.connect(target)) as c:
            c.executescript(SCHEMA)
            _relabel_source_runs(src,c)
            info=dict(c.execute('SELECT k,v FROM branch_info'))
            branch_identity=info.get('source_identity') or _source_id(c)
            old=info.get('source')
            if identity and branch_identity:
                if identity!=branch_identity:raise RuntimeError('BRANCH_SOURCE_IDENTITY_MISMATCH: preserve both databases; unrelated source')
            elif old is not None and old!=str(primary):
                if not _legacy_pair(src,c):raise RuntimeError('BRANCH_SOURCE_MISMATCH: relocation ancestry unproven; preserve both databases')
            identity=identity or branch_identity or uuid.uuid4().hex
            # Only identity/path metadata changes. No reseeding existing history.
            with src:src.execute("INSERT OR IGNORE INTO meta VALUES('paper_source_identity',?)",(identity,))
            with c:
                c.execute("INSERT OR REPLACE INTO branch_info VALUES('source_identity',?)",(identity,))
                if old is not None and old!=str(primary):
                    c.execute("INSERT OR IGNORE INTO branch_info VALUES('original_source',?)",(old,))
                    c.execute("INSERT OR REPLACE INTO branch_info VALUES('last_relocation',?)",(json.dumps(dict(previous=old,current=str(primary),ts=time.time())),))
                c.execute("INSERT OR REPLACE INTO branch_info VALUES('source',?)",(str(primary),))
                c.execute("INSERT OR IGNORE INTO branch_info VALUES('created_ts',?)",(str(time.time()),))
    return target

def settle(main,row,account,check_id,available,details,route="PUMP_AMM"):
    """Single transaction with the original sell side effects and reconciled costs."""
    import database as db,paper_capital as cap
    tid=row['trade_id'];mint=row['mint'];c=db.con();q=main.P.get(mint)
    if not q or q['trade_id']!=tid:return False
    gross=float(account['quote_received_lamports'])*float(account['fx'])/1e9
    receipt=float(account['receipt_units']);cost=gross-receipt
    if cost<0:raise RuntimeError('NEGATIVE_AMM_COST')
    import copy
    before=copy.deepcopy(main.P)
    # _sell and all nested DB helpers respect this outer transaction.
    try:
        with db.atomic():
            if c.execute('SELECT 1 FROM branch_amm_settlements WHERE trade_id=?',(tid,)).fetchone():return False
            main.S.setdefault(mint,main.state())
            price=q['entry']*gross/q['size']
            pending=c.execute("SELECT order_id FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL' AND status='PENDING'",(tid,)).fetchone()
            if not pending:return False
            c.execute('INSERT INTO branch_amm_settlements VALUES(?,?,?,?,?,?,?,?)',(tid,check_id,available,time.time(),gross,receipt,cost,json.dumps(details)))
            main.sell(mint,price,'CURVE_VERIFIED_EXIT' if route=='PUMP_CURVE' else 'AMM_VERIFIED_EXIT',_fill=True,_order_id=pending[0],_settlement_evidence=dict(source='local_curve' if route=='PUMP_CURVE' else 'local_amm',price_basis='POOL_QUOTE_CREDIT',received_ts=available,provider_ts=None,signature=None,clock_status='LOCAL_SIMULATION',pool=details.get('pool'),check_id=check_id))
            c.execute("UPDATE capital_ledger_v0721 SET pool_rate=0,pool_fee=0,route_fee=0,fixed_cost=?,total_cost=?,cash_delta=?,rate_source=? WHERE trade_id=? AND side='SELL'",(cost,cost,receipt,'CURVE_VERIFIED_ISOLATED' if route=='PUMP_CURVE' else 'AMM_VERIFIED_ISOLATED',tid))
            cap.rebalance(c)
    except BaseException:
        main.P.clear();main.P.update(before);raise
    return True

def poll(main):
    import config,monitor_service,amm_exit_scenario as amm,database as db
    if not db.con().execute("SELECT 1 FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING' LIMIT 1").fetchone():return
    target=monitor_service.paths(config.DB)['db']
    if not target.exists():return
    with closing(monitor_service.connect(config.DB,readonly=True)) as c:
        c.execute('BEGIN')
        inventories={r['trade_id']:dict(r) for r in c.execute('SELECT * FROM quote_inventory_v0723')}
        signals={r['trade_id']:dict(r) for r in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING'")}
        # Only recent results: old successes never close a carried position now.
        rows=[dict(r) for r in c.execute("SELECT * FROM position_checks_v0730 WHERE finished_ts>=? AND status='AMM_SALE_EXECUTED' ORDER BY finished_ts,id",(time.time()-5,))]
        for row in rows:
            sig=signals.get(row['trade_id'])
            if not sig:continue
            j=json.loads(row['input_json']);r=json.loads(row['result_json'])
            if not 0<=time.time()-r.get('snapshot_received_ts',0)<=5:continue
            decision,a=amm.assess(c,row,j,r,sig['signal_ts'],None,inventories.get(row['trade_id']))
            if decision=='ELIGIBLE':settle(main,row,a,row['id'],row['finished_ts'],dict(accounting=a,signal=sig,route='PUMP_AMM'))

def lifecycle_path(primary,transport=False):
    p=path(primary)
    return p.with_name(p.stem+('_transport.json' if transport else '_lifecycle.json'))

def write_lifecycle(primary,data,transport=False):
    p=lifecycle_path(primary,transport);tmp=p.with_name(p.name+'.'+str(os.getpid())+'.tmp')
    tmp.write_text(json.dumps(data,allow_nan=False),encoding='utf-8');tmp.replace(p)

def write_transport(primary,data):write_lifecycle(primary,data,True)

def read_lifecycle(primary,transport=False):
    try:
        d=json.loads(lifecycle_path(primary,transport).read_text(encoding='utf-8'))
        return d if isinstance(d,dict) else {}
    except (ValueError,OSError):return {}

async def worker(primary,ready=None,session=None,scenario=None):
    with __import__('recovery_gate').reader(primary):
        return await _worker(primary,ready,session,scenario)

async def _worker(primary,ready=None,session=None,scenario=None):
    engine=scenario or sys.modules[__name__]
    import config,uuid,runtime_control
    strict_stop=session is not None
    session=session or uuid.uuid4().hex
    target=engine.seed(primary);config.DB=str(target)
    import database as db,main,preentry,shadow_preflight,execution_model as ex,exit_quotes,run_audit,runtime_status,monitor_service
    db.init()
    import shared_lots
    matched=shared_lots.enabled(db.con())
    if scenario:scenario.configure()
    preentry.recover(db.con());shadow_preflight.recover(db.con());ex.restart(db.con());main.restore_positions()
    if matched:
        shared_lots.configure(main);shared_lots.restore_clocks(db.con())
    runtime_status.begin(db.con());run_audit.begin(db.con(),None)
    db.setmany(dict(branch_role='ROUTE_PAPER' if scenario else 'AMM_PAPER',branch_input_gap=int(db.getm('branch_input_gap',0)),trade_stream_state='STARTING',branch_input_seq=0,branch_session=session,branch_shutdown_state='RUNNING'))
    if matched:db.setm('last_entry_result_v0722','MATCHED_PRIMARY_ENTRY_ONLY')
    db.runtime_mode()
    await exit_quotes.refresh_fx()
    if exit_quotes.FX_STATUS!='READY':raise RuntimeError('BRANCH_FX_NOT_READY')
    monitor=None
    engine.write_lifecycle(primary,dict(session=session,status='RUNNING',pid=os.getpid(),ts=time.time()))
    if ready:
        # Atomic: the primary polls for the name and must never read a half-written file (v0.7.101.5).
        part=Path(str(ready)+'.part');part.write_text(json.dumps(dict(session=session,pid=os.getpid())),encoding='utf-8');os.replace(part,ready)
    incoming=runtime_control.ByteInbox()
    input_state=runtime_control.InputState()
    probe=runtime_control.Probe(config.DB,extra=lambda:dict(input=input_state.snapshot(),queue_depth=incoming.qsize(),queue_payload_bytes=incoming.bytes),control=(target,session)).start()
    baseline_ready=main.transport_entry_ready
    main.transport_entry_ready=lambda: input_state.first is None and not int(db.getm('branch_input_gap',0)) and baseline_ready()
    def persist_input():
        values=dict(branch_input_seq=input_state.seq,branch_stale_input=input_state.stale)
        if input_state.first:
            values.update(branch_input_gap=1,branch_first_gap_v60=json.dumps(dict(input_state.first,session=session)))
        db.setmany(values)
    def input_reader():
        pending=b''
        try:
            while not probe.stop.is_set():
                chunk=os.read(sys.stdin.fileno(),65536)
                if not chunk:break
                pending+=chunk
                while b'\n' in pending:
                    line,pending=pending.split(b'\n',1)
                    if len(line)>65536:raise ValueError()
                    while not probe.stop.is_set():
                        try:incoming.put(line,timeout=.1);break
                        except queue.Full:pass
                if len(pending)>65536:raise ValueError()
            if pending:incoming.put_nowait(pending)
        except Exception:
            try:incoming.put_nowait(b'{INVALID_INPUT')
            except queue.Full:pass
        finally:
            try:incoming.put_nowait(None)
            except queue.Full:pass
    threading.Thread(target=input_reader,daemon=True).start()
    stop_reason='INPUT_EOF'
    async def receive():
        nonlocal stop_reason
        yielded=time.monotonic();processed=0
        while True:
            if probe.stop.is_set():stop_reason='PARENT_STOP';return
            # Even a permanently nonempty queue must yield to heartbeat/checks/STOP.
            if processed>=32 or time.monotonic()-yielded>=.005:
                await asyncio.sleep(0);processed=0;yielded=time.monotonic()
                if probe.stop.is_set():stop_reason='PARENT_STOP';return
            try:line=incoming.get_nowait()
            except queue.Empty:
                await asyncio.sleep(.005);continue
            if line is None:
                if runtime_control.stop_requested(config.DB,session):
                    stop_reason='PARENT_STOP';return
                if strict_stop:raise RuntimeError('BRANCH_INPUT_EOF_WITHOUT_STOP')
                return
            x=json.loads(line)
            if x.get('control')=='STOP':
                if x.get('session')!=session:raise RuntimeError('BRANCH_STOP_SESSION_MISMATCH')
                stop_reason='PARENT_STOP';return
            processed+=1
            first_before=input_state.first
            usable=input_state.observe(x,incoming.qsize())
            if input_state.first is not first_before:persist_input()
            if not usable:continue
            m=x['mint']
            if m not in main.S:
                main.S[m]=main.state();main.S[m]['born']=x['born']
            now=time.time()
            db.setmany(dict(trade_stream_state='RECEIVING',trade_watchdog_state='RECEIVING',trade_last_market_ts=now))
            main.process_trade(m,x['side'],x['price'],x['trader'],x['source'],x['evidence'])
            main.cleanup()
    async def check():
        nonlocal monitor
        while True:
            __import__('recovery_gate').status()
            if matched:shared_lots.sync(main,primary)
            if monitor is None and main.P:
                monitor=await asyncio.to_thread(__import__('subprocess').Popen,[sys.executable,'paper_branch.py','--monitor',str(target),'--parent',str(os.getpid())],stdin=__import__('subprocess').PIPE)
            if monitor is not None and monitor.poll() is not None:
                exc=RuntimeError('BRANCH_MONITOR_EXITED: exit_code='+str(monitor.returncode))
                exc.monitor_failure_v81=monitor_service.read_state(config.DB)
                raise exc
            persist_input()
            engine.poll(main);await asyncio.sleep(.25)
    work=[main.heartbeat(),main.observation_timer(),main.clock_lab.local_watch(),exit_quotes.worker(),check()]
    if not matched:work.append(preentry.worker())
    tasks=[asyncio.create_task(t) for t in work]
    feed=asyncio.create_task(receive())
    counter_sink=__import__('counter_writer').start(config.DB,session=session)
    failed=False;branch_failure=None
    try:
        done,_=await asyncio.wait([feed]+tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in done:task.result()
    except BaseException as exc:
        failed=True;stop_reason='WORKER_ERROR'
        branch_failure=dict(exception=__import__('child_diagnostics').exception(exc),monitor_state=getattr(exc,'monitor_failure_v81',None))
        raise
    finally:
        tasks_stopped=True
        for task in [feed]+tasks:task.cancel()
        try:await asyncio.wait_for(asyncio.gather(feed,*tasks,return_exceptions=True),timeout=5)
        except (asyncio.TimeoutError,asyncio.CancelledError):tasks_stopped=False;failed=True;stop_reason='TASK_SHUTDOWN_TIMEOUT'
        monitor_forced=False;monitor_exit_code=None
        if monitor is not None:
            try:
                monitor.stdin.close()
                monitor_exit_code=await asyncio.to_thread(monitor.wait,4)
            except Exception:
                monitor_forced=True
                from branch_transport import terminate_tree
                monitor_forced=(await asyncio.to_thread(terminate_tree,monitor)) is not False
                monitor_exit_code=monitor.poll()
        if monitor_exit_code not in (None,0):
            failed=True
            if branch_failure is None:
                stop_reason='MONITOR_CLEANUP_FAILED'
                branch_failure=dict(monitor_exit_code=monitor_exit_code,monitor_state=monitor_service.read_state(config.DB))
        await __import__('counter_writer').stop(counter_sink)
        persist_input()
        await probe.close()
        final='FAILED' if failed or monitor_forced else 'STOPPED'
        # Final state, run boundary and shutdown metadata commit together.
        with db.atomic():
            if tasks_stopped:main.execution.cancel_unfilled_buys_on_stop(db.con(),reason=stop_reason)
            runtime_status.beat(db.con(),stopped=True)
            db.setm('runtime_state_v41',final)
            run_audit.beat(db.con(),status=final,force=True)
            db.setmany(dict(trade_stream_state=final,stream_state=final,branch_shutdown_state=final,
                branch_shutdown_reason=stop_reason,branch_shutdown_ts=time.time(),branch_monitor_forced=int(monitor_forced)))
        __import__('wal_checkpoint_v1014').close()
        __import__('shutdown_audit').closed(config.DB,'branch',session)
        engine.write_lifecycle(primary,dict(session=session,status=final,reason=stop_reason,run_id=run_audit.ACTIVE,
            monitor_forced=monitor_forced,failure_v81=branch_failure,pid=os.getpid(),ts=time.time()))

def report(primary):
    p=path(primary)
    if not p.exists():return dict(status='NOT_STARTED')
    import paper_capital,runtime_status,run_audit,primary_integrity
    with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=.2)) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN')
        meta=dict(c.execute('SELECT k,v FROM meta'))
        lifecycle=read_lifecycle(primary);transport=read_lifecycle(primary,True)
        session=meta.get('branch_session')
        if lifecycle.get('session')!=session:lifecycle={}
        if transport.get('session')!=session:transport={}
        return dict(runtime_perf=__import__('runtime_control').read(p),first_gap=json.loads(meta.get('branch_first_gap_v60','null')),preentry_timing=__import__('rpc_timing').preentry_summary(c),status='AVAILABLE',lifecycle=lifecycle,transport=transport,runtime=runtime_status.summary(c),capital=paper_capital.summary(c),runs=run_audit.summary(c),integrity=primary_integrity.summarize(c),
            open_positions=[dict(r) for r in c.execute('SELECT trade_id,mint,size FROM positions')],
            settlements=[dict(r) for r in c.execute('SELECT * FROM branch_amm_settlements ORDER BY settled_ts DESC LIMIT 50')],
            input_gap=meta.get('branch_input_gap'),input_seq=meta.get('branch_input_seq'),
            stopped_with_open=meta.get('runtime_state_v41')=='STOPPED' and c.execute('SELECT count(*) FROM positions').fetchone()[0]>0)

def export(primary,folder):
    import zipfile,tempfile,monitor_service
    p=path(primary)
    with zipfile.ZipFile(Path(folder)/'parallel_paper.zip','w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('summary.json',json.dumps(report(primary),ensure_ascii=False))
        for transport in (False,True):
            f=lifecycle_path(primary,transport)
            if f.exists():z.write(f,f.name)
        if p.exists():
            with __import__('tempfile').TemporaryDirectory() as td:
                for src in (p,monitor_service.paths(p)['db']):
                    if not src.exists():continue
                    dst=Path(td)/src.name
                    with closing(sqlite3.connect(src.as_uri()+'?mode=ro',uri=True)) as a,closing(sqlite3.connect(dst)) as b:a.backup(b)
                    z.write(dst,dst.name)
                if monitor_service.paths(p)['db'].exists():
                    evidence=monitor_service.export(p,Path(td)/'monitor')
                    z.write(evidence,'amm_monitor_evidence.zip')
                    with zipfile.ZipFile(evidence) as nested:
                        name=__import__('monitor_artifacts').MANIFEST
                        z.writestr('amm_monitor_export_v74.json',nested.read(name))
                else:z.writestr('amm_monitor_export_v74.json',json.dumps(dict(schema='MONITOR_ARTIFACTS_V74',status='MONITOR_DATABASE_MISSING',check_count=None,note='Cannot verify checks or artifact completeness; monitor may not have started.')))

if __name__=='__main__':
    if sys.argv[1]=='--monitor':
        import config
        config.DB=sys.argv[2]
        import monitor_service
        asyncio.run(monitor_service.run_with_stdin(config.DB,int(sys.argv[4])))
    else:
        asyncio.run(worker(sys.argv[2],sys.argv[3] if len(sys.argv)>3 else None,sys.argv[4] if len(sys.argv)>4 else None))
