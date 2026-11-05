"""User-facing commands. Always resolve paths relative to this application."""
import os,sys,subprocess,json,zipfile,time,webbrowser,signal
from contextlib import closing
from pathlib import Path
APP=Path(__file__).resolve().parent
ROOT=APP.parent
os.chdir(APP)
sys.path.insert(0,str(APP))

def call(*args):return subprocess.call([sys.executable,*args],cwd=APP)
def setup_env():
    p=APP/'.env'
    if not p.exists():p.write_bytes((APP/'.env.example').read_bytes())

def export_release_checks(folder,source_root=None):
    """Preserve the actual check pair, including missing/stale/mismatching evidence."""
    import config,hashlib
    source=Path(source_root or ROOT)/'results';folder=Path(folder);rows={};parsed={}
    for name in ('recovery_check_v89.json','acceptance_v90.json'):
        path=source/name
        try:
            if path.stat().st_size>16*1024*1024:raise ValueError('CHECK_REPORT_OVERSIZE')
            raw=path.read_bytes();(folder/name).write_bytes(raw)
            rows[name]=dict(status='COPIED',sha256=hashlib.sha256(raw).hexdigest())
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError('CHECK_REPORT_NOT_OBJECT')
            parsed[name]=value
            rows[name].update(version=value.get('version'),passed=value.get('passed'),created_ts=value.get('created_ts'))
        except FileNotFoundError:rows[name]=dict(status='MISSING')
        except (OSError,ValueError) as exc:rows[name]=dict(rows.get(name,{}),status='UNREADABLE_OR_INVALID',error_type=type(exc).__name__)
    a=parsed.get('recovery_check_v89.json',{});b=parsed.get('acceptance_v90.json',{})
    matched=bool(a and b and a.get('version')==b.get('version')==config.VERSION and a.get('created_ts') is not None and a.get('created_ts')==b.get('created_ts') and a.get('passed') is b.get('passed'))
    last={}
    last_path=source/'recovery_last_attempt_v89.json'
    try:
        if last_path.stat().st_size<=1024*1024:
            raw=last_path.read_bytes();(folder/last_path.name).write_bytes(raw);last=json.loads(raw)
            if not isinstance(last,dict):last={}
    except (OSError,ValueError):last={}
    later_interrupted=bool(last.get('status')=='INTERRUPTED' and isinstance(last.get('created_ts'),(int,float)) and
        (not isinstance(a.get('created_ts'),(int,float)) or last['created_ts']>a['created_ts']))
    report=dict(schema='RELEASE_CHECK_EXPORT_V94',version=config.VERSION,files=rows,pair_consistent=matched,
        passed=matched and a.get('passed') is True,native_windows_verified=matched and a.get('passed') is True and b.get('native_windows_verified') is True,
        last_attempt_v1012=dict(status=last.get('status'),created_ts=last.get('created_ts'),finished_ts=last.get('finished_ts'),failure_evidence_zip=last.get('failure_evidence_zip')) if last else None,
        last_attempt_interrupted_after_report_v1012=later_interrupted,
        last_attempt_warning_v1012=('Last recovery attempt was INTERRUPTED (Ctrl+C) after the copied report'+(' (passed=%s, created_ts=%s)'%(a.get('passed'),a.get('created_ts')) if a else '')) if later_interrupted else None,
        note='Exact existing reports copied; missing, mismatching or failed checks are not replaced or synthesized.')
    if later_interrupted:print('WARNING:',report['last_attempt_warning_v1012'],flush=True)
    (folder/'release_checks_v94.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report

def results():
    import config,monitor_service,sqlite_preserve,sqlite_diagnostics
    database=Path(config.DB).resolve()
    # Same launcher lock as start: refuse capture while that installation runs.
    lock=database.with_name(database.stem+'_launcher.lock')
    with monitor_service.singleton(lock):
        folder=ROOT/'results'/('preservation_'+time.strftime('%Y%m%d_%H%M%S')+'_'+__import__('uuid').uuid4().hex[:6])
        target,manifest=sqlite_preserve.preserve(database.parent,folder,primary_name=database.name)
        rpc_evidence=folder/'rpc_control_evidence_v892.zip'
        __import__('rpc_evidence_v892').export(config.RPC_CONTROL_DIR,rpc_evidence)
        with zipfile.ZipFile(target,'a',zipfile.ZIP_DEFLATED) as z:z.write(rpc_evidence,rpc_evidence.name)
        print('PRESERVED EVIDENCE:',target,flush=True)
        volumes=__import__('review_bundle_v100').database_volumes(target,folder/'raw_database_volumes')
        print('RAW DATABASE VOLUMES:',folder/'raw_database_volumes',flush=True)
        damaged=any(x.get('integrity')!=['ok'] for x in manifest['databases']) or manifest['errors'] or any(not x['stable'] for x in manifest['files'])
        if damaged:
            print('PARTIAL DATA: inspect MANIFEST.json. No database repaired; trading results are not certified.');return 1
        try:return legacy_results()
        except Exception as exc:
            sqlite_diagnostics.record(database,'RESULTS_REPORT_FAILED',exc)
            details=sqlite_diagnostics.error(exc)
            # Only the launcher's own fixed operator guidance; never arbitrary exception text.
            reason=str(exc) if type(exc) is RuntimeError and str(exc) in RESULTS_GUIDANCE else None
            if reason:details['message']=reason
            (folder/'REPORT_ERROR.json').write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding='utf-8')
            with zipfile.ZipFile(target,'a',zipfile.ZIP_DEFLATED) as z:z.write(folder/'REPORT_ERROR.json','REPORT_ERROR.json')
            if reason:print('REPORT FAILED:',reason,flush=True)
            print('REPORT FAILED; preserved databases remain available:',target);return 1

RESULTS_GUIDANCE=('No test database. Start the bot first.','Stop the bot with Ctrl+C before collecting results.',
    'Monitor is still running. Wait for shutdown before collecting results.')

def legacy_results():
    import shadow_preflight
    import config,export_data,auto_simulation,preentry,position_monitor,monitor_service
    if not Path(config.DB).exists():raise RuntimeError('No test database. Start the bot first.')
    with closing(__import__('sqlite3').connect(Path(config.DB).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        meta=dict(c.execute('SELECT k,v FROM meta'))
    if meta.get('stream_state')!='STOPPED' and time.time()-float(meta.get('heartbeat_ts',0))<20:
        raise RuntimeError('Stop the bot with Ctrl+C before collecting results.')
    import psutil
    state=monitor_service.read_state(config.DB)
    if state.get('status') not in ('STOPPED','NOT_STARTED') and state.get('pid') and psutil.pid_exists(state['pid']):
        raise RuntimeError('Monitor is still running. Wait for shutdown before collecting results.')
    folder=ROOT/'results'/time.strftime('%Y%m%d_%H%M%S');folder.mkdir(parents=True,exist_ok=False)
    export_data.export(config.DB,folder)
    auto_simulation.export_evidence(config.DB,folder)
    preentry.export(config.DB,folder)
    shadow_preflight.export(config.DB,folder)
    import time_check
    time_check.export(folder)
    import run_audit,exit_audit,database as db
    with db.con() as c:
        (folder/"runs.json").write_text(json.dumps(run_audit.summary(c,1000000),ensure_ascii=False,indent=2),encoding="utf-8")
        audits=[dict(r,details=json.loads(r["data_json"])) for r in c.execute("SELECT * FROM exit_audit_v45")]
        for r in audits:r.pop("data_json")
        (folder/"exit_audit.json").write_text(json.dumps(audits,ensure_ascii=False,indent=2),encoding="utf-8")
    recovery=__import__('recovery_gate').path(config.DB)
    if recovery.exists():__import__('shutil').copy2(recovery,Path(folder)/recovery.name)
    controlled=ROOT/'results/recovery_check_v89.json'
    if controlled.exists():__import__('shutil').copy2(controlled,Path(folder)/controlled.name)
    __import__('rpc_evidence_v892').export(config.RPC_CONTROL_DIR,folder/'rpc_control_evidence_v892.zip')
    __import__('monitor_publication_v99').export(config.DB,folder)
    (folder/'rpc_control_v89.json').write_text(json.dumps(__import__('rpc_budget').snapshot(),ensure_ascii=False,indent=2),encoding='utf-8')
    __import__('spawn_report_v89').write_report(folder/'spawn_diagnostics_v89.json',include_events=False)
    monitor_service.export(config.DB,folder)
    import shutil
    if (APP/'data/monitor_selftest.json').exists():shutil.copy2(APP/'data/monitor_selftest.json',folder/'monitor_selftest.json')
    from dashboard.server import snap,PAGE,DETAILS
    snapshot=snap()
    (folder/'cost_price_audit.json').write_text(json.dumps(snapshot['monitor'].get('cost_price_audit',{}),ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'signal_exit.json').write_text(json.dumps(snapshot['monitor'].get('signal_exit',{}),ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'monitor_evidence.json').write_text(json.dumps(snapshot['evidence'],ensure_ascii=False,indent=2),encoding='utf-8')
    import entry_pipeline,exit_timeline
    with db.con() as c:
        (folder/'entry_pipeline.json').write_text(json.dumps(entry_pipeline.summary(c,-1),ensure_ascii=False,indent=2),encoding='utf-8')
        full_timeline=exit_timeline.summary(c,snapshot['monitor'],-1)
    (folder/"exit_timeline.json").write_text(json.dumps(full_timeline,ensure_ascii=False,indent=2),encoding="utf-8")
    import time_order_audit,clock_lab
    with closing(__import__('sqlite3').connect(Path(config.DB).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=__import__('sqlite3').Row
        (folder/'clock_reference_lab.json').write_text(json.dumps(clock_lab.summary(c,-1),ensure_ascii=False,indent=2),encoding='utf-8')
        (folder/'time_order_audit.json').write_text(json.dumps(time_order_audit.summary(c,-1),ensure_ascii=False,indent=2),encoding='utf-8')
    payload=json.dumps(snapshot,ensure_ascii=False).replace('<','\\u003c')
    html=PAGE.decode().replace('<script>','<script>window.REPORT_DATA='+payload+';</script><script>',1)
    (folder/'dashboard.html').write_text(html,encoding='utf-8',newline='') # template bytes already carry their line endings
    detail_html=DETAILS.decode().replace('<script>','<script>window.REPORT_DATA='+payload+';</script><script>',1).replace('<a href="/">← Терминал</a> · ','').replace('href="/classic"','href="dashboard.html"').replace('href="/"','href="dashboard.html"')
    (folder/'dashboard_details.html').write_text(detail_html,encoding='utf-8',newline='')
    (folder/'campaign_overview.json').write_text(json.dumps(snapshot['overview'],ensure_ascii=False,indent=2),encoding='utf-8')
    import system_health,primary_integrity
    (folder/'amm_signal_exit.json').write_text(json.dumps(snapshot.get('monitor',{}).get('amm_signal_exit',{}),ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'stream_diagnostics.json').write_text(json.dumps(snapshot.get('stream_diagnostics',{}),ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'system_strategy_diagnostics.json').write_text(json.dumps(snapshot['system_diagnostics'],ensure_ascii=False,indent=2),encoding='utf-8')
    system_health.export(config.DB,folder)
    import paper_branch
    paper_branch.export(config.DB,folder)
    __import__('route_branch').export(config.DB,folder)
    try:__import__('amm_observer').export(config.DB,folder)  # v0.7.102.0: observation-only data, never blocks the package
    except Exception as exc:(folder/'amm_observer_summary.json').write_text(json.dumps(dict(state='EXPORT_ERROR',error_type=type(exc).__name__)),encoding='utf-8')
    __import__('runtime_control').export(config.DB,folder)
    __import__('aggregate_export').export(config.DB,folder)
    review=__import__('exit_cost_review').current(config.DB,folder)
    __import__('execution_review_v92').current(config.DB,review,folder)
    (folder/'cost_model_audit_v89.json').write_text(json.dumps(__import__('cost_model_audit_v89').audit(review),ensure_ascii=False,indent=2),encoding='utf-8')
    __import__('exit_wait_v65').export(config.DB,folder,review)
    __import__('shared_lots').export(config.DB,folder)
    for diagnostic in Path(config.DB).resolve().parent.glob('*_counter_writer_v67.json'):
        shutil.copy2(diagnostic,folder/diagnostic.name)
    for pattern in ('*_shutdown_v68.json','*_integrity_*_v68.json','*_connection_close_v68.json','*_counter_writer_stop_v68.json','*_stall_evidence_v1012.json'):
        for diagnostic in Path(config.DB).resolve().parent.glob(pattern):shutil.copy2(diagnostic,folder/diagnostic.name)
    benchmark=APP/'data/runtime_benchmark_v60.json'
    if benchmark.exists():shutil.copy2(benchmark,folder/benchmark.name)
    target_check=APP/'data/target_amm_check_v59.json'
    if target_check.exists():shutil.copy2(target_check,folder/target_check.name)
    target_evidence=APP/'data/target_amm_evidence_v59.zip'
    if target_evidence.exists():shutil.copy2(target_evidence,folder/target_evidence.name)
    export_release_checks(folder)
    import execution_lab
    execution_lab.export(folder)
    for diagnostic_name in ('phase1_wallet_check.json','phase1_rpc_probe.json','rpc_resume_status_v101.json','rpc_fault_paper_v101.json'):
        source=APP/'data'/diagnostic_name
        if source.is_file():__import__('shutil').copy2(source,folder/diagnostic_name)
    (folder/'execution_readiness.json').write_text(json.dumps(execution_lab.status(),ensure_ascii=False,indent=2),encoding='utf-8')
    timing56=dict(primary_preentry=snapshot.get('preentry',{}).get('timing',{}),primary_monitor=snapshot.get('monitor',{}).get('rpc_timing',{}),branch_preentry=snapshot.get('parallel_paper',{}).get('preentry_timing',{}))
    (folder/'rpc_timing_results.json').write_text(json.dumps(timing56,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'summary.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    # Export each check without importing or copying credentials.
    import primary_integrity,paper_capital,execution_model,cost_evidence,entry_activity,diagnostics,exit_quotes
    for name,module in [('primary_integrity',primary_integrity),('capital',paper_capital),('execution_model',execution_model),('cost_evidence',cost_evidence),('entry_activity',entry_activity),('diagnostics',diagnostics),('exit_quote',exit_quotes)]:
        (folder/(name+'_results.json')).write_text(json.dumps(module.check(config.DB),ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'run_review_v100.json').write_text(json.dumps(__import__('run_review_v100').export(config.DB),ensure_ascii=False,indent=2),encoding='utf-8')
    bundles=__import__('review_bundle_v100').export(folder)
    for volume in bundles['volumes']:print('REVIEW BUNDLE:',folder/volume['name'])
    if bundles['errors']:print('REVIEW BUNDLE PARTIAL: inspect review_bundle_manifest.json')
    print('FULL EVIDENCE FOLDER:',folder)
    return 1 if bundles['errors'] else 0

def wait_dashboard(dash, timeout=12, launch_id=None):
    import config,system_health,urllib.request
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if dash.poll() is not None:raise RuntimeError('Dashboard process exited before readiness')
        try:
            with urllib.request.urlopen('http://127.0.0.1:'+str(config.PORT)+'/health',timeout=.5) as r:data=json.load(r)
            mismatches=[]
            if data.get('version')!=config.VERSION:mismatches.append('version')
            if data.get('database_identity')!=system_health.identity(config.DB):mismatches.append('database')
            if not launch_id or data.get('launch_id')!=launch_id:mismatches.append('launch_token')
            if mismatches:
                system_health.event(config.DB,'DASHBOARD_IDENTITY_MISMATCH',fields=mismatches,
                  expected_version=config.VERSION,reported_version=str(data.get('version','missing'))[:40],
                  launcher_child_pid=dash.pid,reported_server_pid=data.get('pid'))
                raise RuntimeError('Dashboard identity mismatch ('+', '.join(mismatches)+'); check port '+str(config.PORT))
            return
        except (OSError,ValueError):time.sleep(.2)
    raise RuntimeError('Dashboard readiness timeout')

BRANCH_ACK_WAIT_S=15  # Shutdown-only bound for direct branch STOP acks; no trading/RPC limit.

def direct_branch_stop(primary,primary_stalled,wait_s=None):
    """STOP branch workers through their own stop files and wait for their acks.

    The primary normally relays STOP over the branch pipe. When its main thread is
    blocked (run_20261101 fix user run: 53 s in SQLite COMMIT) that never happens, so
    the launcher asks the branches directly before the process tree is killed."""
    rc=__import__('runtime_control')
    record=dict(primary_stalled=bool(primary_stalled),requested_ts=time.time())
    try:record['requested']=rc.request_branch_stops(primary) or []
    except Exception as exc:record.update(requested=[],request_error=type(exc).__name__)
    try:record['acks']=rc.wait_branch_acks(primary,record['requested'],BRANCH_ACK_WAIT_S if wait_s is None else wait_s)
    except Exception as exc:record.update(acks={},ack_error=type(exc).__name__)
    acked=bool(record['requested']) and all(a.get('acked') for a in record['acks'].values())
    prefix='PRIMARY_STALLED' if primary_stalled else 'PRIMARY_STOP_TIMEOUT'
    record['classification']=prefix+('_NO_RUNNING_BRANCH' if not record['requested'] else '_ROUTE_ACKED' if acked else '_ROUTE_UNACKED')
    record['finished_ts']=time.time()
    return record

def stop_child(child, graceful=False,primary=None,primary_stalled=False):
    if child is None:return dict(forced=False,exit_code=None)
    if child.poll() is not None:return dict(forced=False,exit_code=child.returncode)
    import psutil
    try:family=psutil.Process(child.pid).children(recursive=True)
    except psutil.Error:family=[]
    branch_stop=None
    if graceful:
        try:
            if primary:__import__('runtime_control').request_primary_stop(primary)
            if primary and primary_stalled:branch_stop=direct_branch_stop(primary,True)
            child.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGINT)
            child.wait(timeout=35)
            _,remaining=psutil.wait_procs(family,timeout=1)
            remaining=[p for p in remaining if p.is_running() and p.status()!=psutil.STATUS_ZOMBIE]
            if not remaining:return dict(forced=False,exit_code=child.returncode,branch_direct_stop_v1012=branch_stop)
        except (OSError,subprocess.TimeoutExpired,psutil.Error):
            if primary and branch_stop is None:branch_stop=direct_branch_stop(primary,False)
            elif primary:
                try:__import__('runtime_control').request_branch_stops(primary)
                except Exception:pass
    if __import__('late_spawn').wait_before_force(child,family):
        try:
            child.wait(timeout=3)
            _,remaining=psutil.wait_procs(family,timeout=0)
            if not remaining:return dict(forced=False,exit_code=child.returncode)
        except subprocess.TimeoutExpired:pass
    # Keep the pre-stop descendants: a Windows venv redirector can exit first.
    from branch_transport import terminate_tree
    if child.poll() is None:terminate_tree(child)
    for p in family:
        try:
            if p.is_running() and p.status()!=psutil.STATUS_ZOMBIE:p.terminate()
        except psutil.Error:pass
    _,alive=psutil.wait_procs(family,timeout=1)
    for p in alive:
        try:p.kill()
        except psutil.Error:pass
    return dict(forced=True,exit_code=child.returncode,branch_direct_stop_v1012=branch_stop)

def start(stop_condition=None,open_browser=True):
    setup_env()
    import config,database as db,wallet_costs,monitor_service,system_health
    import release_guard
    release_guard.local_database(APP,config.DB)
    release_guard.available(config.PORT)
    release_guard.available(config.PUMPAPI_SINGLETON_PORT)
    # Lock this installation before schema initialization or startup diagnostics.
    lock=Path(config.DB).resolve().with_name(Path(config.DB).stem+'_launcher.lock')
    with monitor_service.singleton(lock):
        recorder=system_health.Recorder(config.DB);children={};code=1;reason='STARTUP_FAILED';stalled=False
        audit=__import__('shutdown_audit').Audit(config.DB)
        try:
            if call('monitor_selftest.py'):
                raise RuntimeError('Monitor self-test FAILED; see app/data/monitor_selftest.json')
            import clean_campaign
            clean_campaign.preflight(config.DB)
            db.init()
            clean_campaign.ensure(db.con(),config.DB)
            # Refuse broken primary accounting before starting market workers.
            import primary_integrity,paper_capital
            integrity=primary_integrity.check(config.DB);capital=paper_capital.check(config.DB)
            if integrity['errors'] or capital['status']=='ERROR':raise RuntimeError('Primary accounting check failed; export results before restart')
            wallet_costs.ensure(db.con())
            monitor_service.connect().close()
            # Windows venv may redirect python.exe to a child with a different
            # PID. A per-launch token binds the HTTP server to THIS launch.
            launch_id=__import__('uuid').uuid4().hex
            dash_env=dict(os.environ,PUMPFUN_DASHBOARD_LAUNCH_ID=launch_id)
            __import__('shutdown_audit').closed(config.DB,'launcher')
            children['dashboard']=subprocess.Popen([sys.executable,'dashboard/server.py'],cwd=APP,env=dash_env)
            wait_dashboard(children['dashboard'],launch_id=launch_id)
            flags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0
            children['bot']=subprocess.Popen([sys.executable,'main.py'],cwd=APP,creationflags=flags)
            children['monitor']=subprocess.Popen([sys.executable,'monitor_service.py','--parent-pid',str(children['bot'].pid),'--stdin-lifetime'],cwd=APP,stdin=subprocess.PIPE)
            if open_browser:webbrowser.open('http://127.0.0.1:'+str(config.PORT))
            print('PAPER startup requested. Trading readiness is checked inside the bot; inspect dashboard and subsequent messages. Ctrl+C once to stop.')
            recorder.sample(children,force=True);audit.capture(children)
            while children['bot'].poll() is None:
                if stop_condition is not None and stop_condition():raise KeyboardInterrupt
                for role in ('monitor','dashboard'):
                    if children[role].poll() is not None:
                        system_health.event(config.DB,'REQUIRED_PROCESS_EXIT',component=role,exit_code=children[role].returncode)
                        raise RuntimeError('Required process stopped: '+role)
                sampled=recorder.sample(children);audit.capture(children)
                if 'PRIMARY_HEARTBEAT_INVALID' in sampled.get('alerts',[]):
                    stalled=True
                    evidence=__import__('stall_evidence_v1012').capture(config.DB,children,sampled)
                    system_health.event(config.DB,'STALL_CONTROLLED_STOP',evidence_v1012=evidence.get('written'),
                        sql_v91=(evidence.get('primary_runtime_v60') or {}).get('sql_v91'))
                    raise RuntimeError('PRIMARY_EVENT_LOOP_STALLED; export diagnostics before restart')
                time.sleep(.5)
            code=children['bot'].returncode;reason='BOT_EXIT' if code==0 else 'BOT_FAILED'
        except KeyboardInterrupt:
            code=0;reason='USER_STOP'
        except Exception as exc:
            system_health.failure(config.DB,'launcher',exc)
            reason='COMPONENT_OR_STARTUP_FAILURE'
            print('FAILED:',type(exc).__name__,str(exc))
        finally:
            try:
                try:audit.begin_stop(children)
                except Exception as exc:
                    audit.before=dict(phase='before',status='PROBE_UNAVAILABLE',databases=[])
                    system_health.failure(config.DB,'before_stop_integrity',exc)
                bot_stop=stop_child(children.get('bot'),graceful=True,primary=config.DB,primary_stalled=stalled)
                shutdown=__import__('runtime_control').shutdown_status(config.DB,bot_stop)
                recorder.last['shutdown']=shutdown
                recorder.last['shutdown_confirmed']=shutdown['confirmed']
                if code==0 and not shutdown['confirmed']:code=1;reason='SHUTDOWN_UNCONFIRMED'
                monitor=children.get('monitor')
                if monitor and monitor.stdin:
                    try:monitor.stdin.close()
                    except OSError:pass
                if monitor and monitor.poll() is None:
                    try:monitor.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        monitor_stop=stop_child(monitor)
                        if monitor_stop.get('forced') or monitor_stop.get('exit_code')!=0:
                            code=1;reason='MONITOR_STOP_FORCED' if monitor_stop.get('forced') else 'MONITOR_STOP_FAILED'
                            recorder.last['shutdown_confirmed']=False
                            recorder.last['shutdown']['confirmed']=False
                        recorder.last['shutdown']['monitor']=monitor_stop
                if monitor and monitor.poll() is not None and 'monitor_stop' not in locals():monitor_stop=dict(forced=False,exit_code=monitor.returncode)
                dashboard_stop=stop_child(children.get('dashboard'))
            except Exception as exc:
                code=1;reason='SHUTDOWN_FAILED';system_health.failure(config.DB,'shutdown',exc)
            finally:
                __import__('shutdown_audit').closed(config.DB,'launcher')
                try:
                    audited=audit.finish(children,dict(bot=locals().get('bot_stop'),monitor=locals().get('monitor_stop'),dashboard=locals().get('dashboard_stop')))
                    recorder.last['sqlite_shutdown_v68']=dict(all_observed_writers_finished=audited['all_observed_writers_finished'],comparison=audited['comparison'])
                    if code==0 and not audited['all_observed_writers_finished']:code=1;reason='WRITER_SHUTDOWN_UNCONFIRMED'
                    elif code==0 and not audited['integrity_phases_ok']:code=1;reason='SQLITE_INTEGRITY_PHASE_FAILED'
                    print('SQLITE SHUTDOWN AUDIT: writers_finished='+str(audited['all_observed_writers_finished'])+' integrity_before_after_ok='+str(audited['integrity_phases_ok']),flush=True)
                except Exception as exc:
                    code=1;reason='SHUTDOWN_AUDIT_FAILED';system_health.failure(config.DB,'shutdown_audit',exc)
                recorder.stop(code,reason)
        return code

def main():
    command=sys.argv[1] if len(sys.argv)>1 else 'help'
    if command=='install':
        code=call('-m','pip','install','-r','requirements.txt')
        if code:return code
        from solders.litesvm import LiteSVM
        LiteSVM();setup_env();print('LiteSVM OK. Next: 02_SETTINGS.bat, then 04_CHECK.bat.');return 0
    if command=='execution-check':return call('execution_lab.py','check')
    if command=='settings':
        setup_env();return call('setup_quotes.py')
    if command=='check':
        print('CHECK PLAN:',json.dumps(__import__('check_plan_v972').summary()),flush=True)
        if call('recovery_selftest.py'):return 1
        time_code=call('time_check.py')
        if time_code not in (0,2):return 1
        if call('monitor_selftest.py'):return 1
        test_code=call('-m','pytest','-q',*__import__('check_plan_v972').technical_remainder())
        if test_code:return 1
        print('Technical tests PASSED. '+('Time check PASSED.' if time_code==0 else 'TIME WARNING: review app/data/time_check.txt.'))
        return time_code
    if command=='results':
        try:return results()
        except __import__('sqlite3').DatabaseError as exc:
            # A corrupt database must not prevent retrieval of the independent
            # failure journal. Do not copy a running DB or pretend export is full.
            import system_health,config
            system_health.failure(config.DB,'results_database',exc)
            folder=ROOT/'results'/('emergency_'+time.strftime('%Y%m%d_%H%M%S'));folder.mkdir(parents=True,exist_ok=True)
            system_health.export(config.DB,folder)
            target=folder.with_suffix('.zip')
            with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
                z.write(folder/'system_flight_recorder.zip','system_flight_recorder.zip')
                z.writestr('INCOMPLETE.json',json.dumps(dict(status='DATABASE_EXPORT_FAILED',error_type=type(exc).__name__,database_included=False)))
            print('INCOMPLETE EMERGENCY REPORT:',target);return 1
    if command=='start':
        return start()
    print('Commands: install, settings, start, check, results');return 1
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:print('FAILED:',type(e).__name__,str(e));raise SystemExit(1)
