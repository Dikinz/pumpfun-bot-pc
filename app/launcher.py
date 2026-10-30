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
    report=dict(schema='RELEASE_CHECK_EXPORT_V94',version=config.VERSION,files=rows,pair_consistent=matched,
        passed=matched and a.get('passed') is True,native_windows_verified=matched and a.get('passed') is True and b.get('native_windows_verified') is True,
        note='Exact existing reports copied; missing, mismatching or failed checks are not replaced or synthesized.')
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
        damaged=any(x.get('integrity')!=['ok'] for x in manifest['databases']) or manifest['errors'] or any(not x['stable'] for x in manifest['files'])
        if damaged:
            print('PARTIAL DATA: inspect MANIFEST.json. No database repaired; trading results are not certified.');return 1
        try:return legacy_results()
        except Exception as exc:
            sqlite_diagnostics.record(database,'RESULTS_REPORT_FAILED',exc)
            (folder/'REPORT_ERROR.json').write_text(json.dumps(sqlite_diagnostics.error(exc),indent=2),encoding='utf-8')
            with zipfile.ZipFile(target,'a',zipfile.ZIP_DEFLATED) as z:z.write(folder/'REPORT_ERROR.json','REPORT_ERROR.json')
            print('REPORT FAILED; preserved databases remain available:',target);return 1

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
    (folder/'dashboard.html').write_text(html,encoding='utf-8')
    detail_html=DETAILS.decode().replace('<script>','<script>window.REPORT_DATA='+payload+';</script><script>',1).replace('href="/"','href="dashboard.html"')
    (folder/'dashboard_details.html').write_text(detail_html,encoding='utf-8')
    (folder/'campaign_overview.json').write_text(json.dumps(snapshot['overview'],ensure_ascii=False,indent=2),encoding='utf-8')
    import system_health,primary_integrity
    (folder/'amm_signal_exit.json').write_text(json.dumps(snapshot.get('monitor',{}).get('amm_signal_exit',{}),ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'stream_diagnostics.json').write_text(json.dumps(snapshot.get('stream_diagnostics',{}),ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'system_strategy_diagnostics.json').write_text(json.dumps(snapshot['system_diagnostics'],ensure_ascii=False,indent=2),encoding='utf-8')
    system_health.export(config.DB,folder)
    import paper_branch
    paper_branch.export(config.DB,folder)
    __import__('route_branch').export(config.DB,folder)
    __import__('runtime_control').export(config.DB,folder)
    __import__('aggregate_export').export(config.DB,folder)
    review=__import__('exit_cost_review').current(config.DB,folder)
    __import__('execution_review_v92').current(config.DB,review,folder)
    (folder/'cost_model_audit_v89.json').write_text(json.dumps(__import__('cost_model_audit_v89').audit(review),ensure_ascii=False,indent=2),encoding='utf-8')
    __import__('exit_wait_v65').export(config.DB,folder,review)
    __import__('shared_lots').export(config.DB,folder)
    for diagnostic in Path(config.DB).resolve().parent.glob('*_counter_writer_v67.json'):
        shutil.copy2(diagnostic,folder/diagnostic.name)
    for pattern in ('*_shutdown_v68.json','*_integrity_*_v68.json','*_connection_close_v68.json','*_counter_writer_stop_v68.json'):
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
    for diagnostic_name in ('phase1_wallet_check.json','phase1_rpc_probe.json'):
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
    target=folder.with_suffix('.zip')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_STORED) as z:
        for f in folder.iterdir():z.write(f,f.name)
    print('RESULTS:',target)
    return 0

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

def stop_child(child, graceful=False,primary=None):
    if child is None:return dict(forced=False,exit_code=None)
    if child.poll() is not None:return dict(forced=False,exit_code=child.returncode)
    import psutil
    try:family=psutil.Process(child.pid).children(recursive=True)
    except psutil.Error:family=[]
    if graceful:
        try:
            if primary:__import__('runtime_control').request_primary_stop(primary)
            child.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGINT)
            child.wait(timeout=35)
            _,remaining=psutil.wait_procs(family,timeout=1)
            remaining=[p for p in remaining if p.is_running() and p.status()!=psutil.STATUS_ZOMBIE]
            if not remaining:return dict(forced=False,exit_code=child.returncode)
        except (OSError,subprocess.TimeoutExpired,psutil.Error):
            if primary:__import__('runtime_control').request_branch_stops(primary)
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
    return dict(forced=True,exit_code=child.returncode)

def start():
    setup_env()
    import config,database as db,wallet_costs,monitor_service,system_health
    import release_guard
    release_guard.local_database(APP,config.DB)
    release_guard.available(config.PORT)
    release_guard.available(config.PUMPAPI_SINGLETON_PORT)
    # Lock this installation before schema initialization or startup diagnostics.
    lock=Path(config.DB).resolve().with_name(Path(config.DB).stem+'_launcher.lock')
    with monitor_service.singleton(lock):
        recorder=system_health.Recorder(config.DB);children={};code=1;reason='STARTUP_FAILED'
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
            webbrowser.open('http://127.0.0.1:'+str(config.PORT))
            print('PAPER startup requested. Trading readiness is checked inside the bot; inspect dashboard and subsequent messages. Ctrl+C once to stop.')
            recorder.sample(children,force=True);audit.capture(children)
            while children['bot'].poll() is None:
                for role in ('monitor','dashboard'):
                    if children[role].poll() is not None:
                        system_health.event(config.DB,'REQUIRED_PROCESS_EXIT',component=role,exit_code=children[role].returncode)
                        raise RuntimeError('Required process stopped: '+role)
                sampled=recorder.sample(children);audit.capture(children)
                if 'PRIMARY_HEARTBEAT_INVALID' in sampled.get('alerts',[]):
                    system_health.event(config.DB,'STALL_CONTROLLED_STOP')
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
                bot_stop=stop_child(children.get('bot'),graceful=True,primary=config.DB)
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
        if call('recovery_selftest.py'):return 1
        time_code=call('time_check.py')
        if time_code not in (0,2):return 1
        if call('monitor_selftest.py'):return 1
        test_code=call('-m','pytest','-q','tests/test_rpc_admission_v95.py','tests/test_preparation_v95.py','tests/test_candidate_writer_v94.py','tests/test_rpc_priority_v94.py','tests/test_cleanup_owner_v93.py','tests/test_sql_admission_v92.py','tests/test_route_io_v91.py','tests/test_exit_priority_v90.py','tests/test_rpc_outcomes_v892.py','tests/test_rpc_control_v89.py','tests/test_network_entrypoints_v89.py','tests/test_network_watchdog_v88.py','tests/test_recovery_evidence_v861.py','tests/test_amm_export_v74.py','tests/test_child_diagnostics_v73.py','tests/test_coordinated_stop_v72.py','tests/test_stop_buy_release_v71.py','tests/test_event_counters_v70.py','tests/test_fresh_retry_v69.py','tests/test_shutdown_audit_v68.py','tests/test_counter_writer_v67.py','tests/test_sqlite_preserve_v66.py','tests/test_priority_exit_v65.py','tests/test_shared_lots_v64.py','tests/test_exit_cost_review_v63.py','tests/test_clean_release_v62.py','tests/test_worker_identity_v611.py','tests/test_watchdog_lifecycle_v61.py','tests/test_runtime_delivery_v60.py','tests/test_route_exit_v59.py','tests/test_clean_campaign_v58.py','tests/test_program_cache_v57.py','tests/test_stop_consistency_v57.py','tests/test_rpc_timing_v56.py','tests/test_branch_shutdown_v541.py','tests/test_parallel_paper_v54.py','tests/test_phase2_v54.py','tests/test_dashboard_launch_hotfix.py','tests/test_execution_safety_v52.py','tests/test_wallet_rpc_phase1.py','tests/test_system_strategy_v51.py','tests/test_primary_integrity.py','tests/test_execution_model.py','tests/test_cost_price_v50.py','tests/test_signal_exit_v49.py','tests/test_monitor_evidence_v48.py','tests/test_entry_monitor_v47.py','tests/test_exit_timeline_v46.py','tests/test_dashboard_start_v46.py','tests/test_run_exit_v45.py','tests/test_time_check_v44.py','tests/test_shadow_preflight_v43.py','tests/test_candidates_v42.py','tests/test_runtime_v41.py','tests/test_clock_reference_v40.py','tests/test_time_order_v39.py','tests/test_entry_diagnostics_v38.py','tests/test_connection_v37.py','tests/test_migration_v36.py','tests/test_monitor_v35.py','tests/test_delta_v34.py','tests/test_rules_v33.py','tests/test_monitor_v32.py','tests/test_monitor_v31.py','tests/test_monitor_v30.py','tests/test_preentry.py','tests/test_local_simulation.py','tests/test_exit_comparison.py')
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
