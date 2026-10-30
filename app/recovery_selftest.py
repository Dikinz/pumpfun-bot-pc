"""Disposable, offline PAPER recovery scenario; never operates the running bot."""
import json,os,subprocess,sys,tempfile,time
import recovery_evidence,traceback
from pathlib import Path
from xml.etree import ElementTree as ET
APP=Path(__file__).resolve().parent
SUITE_TIMEOUT_S=300  # Entire offline suite only; no production deadline change.
REPORT=APP.parent/'results/recovery_check_v89.json'

def main():
    import config
    report=dict(schema='RECOVERY_SELFTEST_V89',build_revision='RPC_ADMISSION_V95',version=config.VERSION,created_ts=time.time(),
        passed=False,scope='OFFLINE_DISPOSABLE_PAPER_CLOCK_INJECTION',os_clock_modified=False,
        windows_suspend_resume_proven=False,live_rpc_proven=False,
        note='Time reports and the market are injected test data. Test DBs are temporary; no existing trading DB is opened. The running bot is not paused or controlled.')
    with tempfile.TemporaryDirectory(prefix='pump_recovery_v78_') as td:
        root=Path(td);xml=root/'tests.xml';evidence=root/'scenario.json';monitor_evidence=root/'monitor.json'
        p0_evidence=root/'p0.jsonl';cancel_evidence=root/'saved_result_cancel.jsonl';spawn_evidence=root/'spawn.jsonl';curve_evidence=root/'curve_replay.json';cleanup_evidence=root/'cleanup_v84.jsonl';ownership_evidence=root/'late_ownership_v85.jsonl';state_evidence=root/'monitor_state_v86.jsonl';io_evidence=root/'io87.jsonl'
        env=dict(os.environ,RPC_CONTROL_EVIDENCE_V891=str(root/'rpc_control_evidence_v891.jsonl'),IO_CHECK_EVIDENCE_V87=str(io_evidence),RECOVERY_PROGRESS_PATH=str(root/'progress.jsonl'),PYTHONUNBUFFERED='1',MONITOR_STATE_EVIDENCE=str(state_evidence),LATE_OWNERSHIP_EVIDENCE=str(ownership_evidence),CLEANUP_REGRESSION_EVIDENCE=str(cleanup_evidence),CURVE_REPLAY_EVIDENCE=str(curve_evidence),SPAWN_BOUNDARY_EVIDENCE=str(spawn_evidence),SAVED_RESULT_CANCEL_EVIDENCE=str(cancel_evidence),P0_CHECK_EVIDENCE=str(p0_evidence),DATABASE_PATH=str(root/'unused.db'),EXECUTION_MODE='paper',
            PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',RECOVERY_CHECK_EVIDENCE=str(evidence),MONITOR_ADMISSION_CHECK_EVIDENCE=str(monitor_evidence))
        for key in ('PYTEST_ADDOPTS','PYTHONPATH','PYTHONHOME'):env.pop(key,None)
        command=[sys.executable,'-m','pytest','-vv','-s','-rA','-p','recovery_progress','--basetemp',str(root/'cases'),
            '--junitxml',str(xml),'tests/test_rpc_admission_v95.py','tests/test_preparation_v95.py','tests/test_program_cache_v57.py','tests/test_candidate_writer_v94.py','tests/test_rpc_priority_v94.py','tests/test_cleanup_owner_v93.py','tests/test_sql_admission_v92.py','tests/test_route_io_v91.py','tests/test_exit_priority_v90.py','tests/test_rpc_outcomes_v892.py','tests/test_rpc_control_v89.py','tests/test_network_entrypoints_v89.py','tests/test_network_watchdog_v88.py','tests/test_io_dashboard_v87.py','tests/test_monitor_file_reader_v862.py','tests/test_monitor_state_v86.py','tests/test_late_spawn_ownership_v85.py','tests/test_cleanup_regressions_v84.py','tests/test_curve_overflow_v83.py','tests/test_spawn_boundaries_v82.py','tests/test_monitor_shutdown_v81.py','tests/test_saved_result_cancel_v80.py','tests/test_p0_isolation_v79.py','tests/test_windows_child_v78.py','tests/test_monitor_admission_v77.py','tests/test_recovery_classification_v76.py','tests/test_recovery_gate_v75.py']
        try:
            result=recovery_evidence.run(command,cwd=APP,env=env,root=root,report=report,timeout=SUITE_TIMEOUT_S)
            report.update(child_returncode=result.returncode,stdout=result.stdout,stderr=result.stderr)
            suites=ET.parse(xml).getroot()
            cases=suites.findall('.//testcase')
            report['tests']=[dict(name=c.get('name'),outcome='FAILED' if c.find('failure') is not None or c.find('error') is not None else 'SKIPPED' if c.find('skipped') is not None else 'PASSED') for c in cases]
            rpc_evidence=root/'rpc_control_evidence_v891.jsonl'
            report['rpc_control_evidence_v891']=[json.loads(line) for line in rpc_evidence.read_text(encoding='utf-8').splitlines()] if rpc_evidence.exists() else []
            report['scenario']=json.loads(evidence.read_text(encoding='utf-8'))
            report['monitor_admission']=json.loads(monitor_evidence.read_text(encoding='utf-8'))
            report['p0_audits']=[json.loads(line) for line in p0_evidence.read_text().splitlines()]
            report['p0_windows_verified_v79']=os.name=='nt' and sys.version_info[:2]==(3,14) and result.returncode==0
            report['saved_result_cancel_v80']=[json.loads(line) for line in cancel_evidence.read_text().splitlines()]
            exact=[c for c in report['tests'] if c['name']=='test_saved_result_sell_cancels_live_tree[native_windows]']
            report['saved_result_cancel_windows_verified_v80']=os.name=='nt' and sys.version_info[:2]==(3,14) and len(exact)==1 and exact[0]['outcome']=='PASSED' and result.returncode==0
            shutdown_cases=[c for c in report['tests'] if c['name'].startswith('test_lifetime_') or c['name'].startswith('test_branch_monitor_entrypoint_')]
            report['monitor_shutdown_windows_verified_v81']=os.name=='nt' and sys.version_info[:2]==(3,14) and len(shutdown_cases)==6 and all(c['outcome']=='PASSED' for c in shutdown_cases) and result.returncode==0
            report['spawn_boundaries_v82']=[json.loads(line) for line in spawn_evidence.read_text().splitlines()]
            late=[c for c in report['tests'] if c['name']=='test_native_late_spawn_after_timeout_cleans_tree']
            report['late_spawn_windows_verified_v82']=os.name=='nt' and sys.version_info[:2]==(3,14) and len(late)==1 and late[0]['outcome']=='PASSED' and result.returncode==0
            report['curve_overflow_replay_v83']=json.loads(curve_evidence.read_text())
            report['curve_replay_windows_verified_v83']=os.name=='nt' and sys.version_info[:2]==(3,14) and report['curve_overflow_replay_v83']['passed'] and result.returncode==0
            native=[c for c in report['tests'] if c['name'].startswith('test_native_job_')]
            report['cleanup_regressions_v84']=[json.loads(line) for line in cleanup_evidence.read_text().splitlines()]
            cleanup_cases=[c for c in report['tests'] if c['name'].startswith(('test_sell_cancels_inside_unreturned_createprocess','test_empty_job_unsignaled_launcher_is_not_cleanup_success'))]
            report['cleanup_regressions_model_passed_v84']=len(cleanup_cases)==3 and all(c['outcome']=='PASSED' for c in cleanup_cases) and len(report['cleanup_regressions_v84'])==3
            report['cleanup_regressions_windows_verified_v84']=os.name=='nt' and sys.version_info[:2]==(3,14) and report['cleanup_regressions_model_passed_v84'] and result.returncode==0
            report['windows_cleanup_root_cause_fixed_v84']=False
            report['late_spawn_ownership_v85']=[json.loads(line) for line in ownership_evidence.read_text().splitlines()]
            ownership=[c for c in report['tests'] if c['name'].startswith(('test_deadline_retains_owner','test_sell_cancel_retains_owner','test_late_creation_error','test_native_late_owner','test_stale_or_reused_monitor','test_unknown_pending_state','test_actual_shutdown_does_not_kill_retained_monitor','test_failed_late_cleanup_remains_fatal_at_stop'))]
            report['late_spawn_ownership_model_passed_v85']=len(ownership)==8 and all(c['outcome']=='PASSED' or (os.name!='nt' and c['name'] in ('test_native_late_owner_survives_stop_and_reaps_tree','test_actual_shutdown_does_not_kill_retained_monitor') and c['outcome']=='SKIPPED') for c in ownership)
            report['late_spawn_ownership_windows_verified_v85']=os.name=='nt' and sys.version_info[:2]==(3,14) and len(ownership)==8 and all(c['outcome']=='PASSED' for c in ownership) and result.returncode==0
            report['windows_kernel_createprocess_hang_fixed_v85']=False
            report['monitor_state_evidence_v86']=[json.loads(line) for line in state_evidence.read_text().splitlines()]
            reader_cases=[c for c in report['tests'] if c['name'].startswith('test_native_reader_windows_')]
            report['native_reader_windows_verified_v862']=os.name=='nt' and sys.version_info[:2]==(3,14) and len(reader_cases)==5 and all(c['outcome']=='PASSED' for c in reader_cases) and result.returncode==0
            io_cases=[c for c in report['tests'] if c['name'].startswith('test_io87_')]
            report['io_evidence_v87']=[json.loads(x) for x in io_evidence.read_text().splitlines()]
            report['io_models_passed_v87']=len(io_cases)==13 and all(c['outcome']=='PASSED' or (os.name!='nt' and c['name']=='test_io87_native_windows_control_state_replace_recovers_after_release' and c['outcome']=='SKIPPED') for c in io_cases)
            report['native_state_io_windows_verified_v87']=os.name=='nt' and sys.version_info[:2]==(3,14) and len(io_cases)==13 and all(c['outcome']=='PASSED' for c in io_cases) and result.returncode==0
            state_cases=[c for c in report['tests'] if c['name'].startswith('test_monitor_state_')]
            report['monitor_state_models_passed_v86']=len(state_cases)==28 and all(c['outcome']=='PASSED' or (os.name!='nt' and c['name']=='test_monitor_state_native_windows_sharing_violation_preserves_heartbeat' and c['outcome']=='SKIPPED') for c in state_cases)
            report['monitor_state_windows_verified_v86']=os.name=='nt' and sys.version_info[:2]==(3,14) and len(state_cases)==28 and all(c['outcome']=='PASSED' for c in state_cases) and result.returncode==0


            cleanup_owner_cases=[c for c in report['tests'] if c['name'] in ('test_known_launcher_setup_slower_than_scaled_deadline_keeps_cleanup_scenario', 'test_known_launcher_stop_retains_handles_until_signal', 'test_known_launcher_confirmation_resumes_only_new_checks', 'test_native_known_launcher_owner_survives_delayed_signal', 'test_descendants_keep_job_owned_after_launcher_has_exited', 'test_known_launcher_cancellation_retains_owner_and_rejects_saved_result', 'test_monitor_cause_has_priority_over_pipe_write_consequence', 'test_pending_handle_query_error_does_not_release_ownership', 'test_stop_before_pid_publication_keeps_native_setup_owner')]
            report['cleanup_owner_models_passed_v93']=len(cleanup_owner_cases)==9 and all(c['outcome']=='PASSED' or (os.name!='nt' and c['name']=='test_native_known_launcher_owner_survives_delayed_signal' and c['outcome']=='SKIPPED') for c in cleanup_owner_cases)
            report['native_cleanup_owner_windows_verified_v93']=os.name=='nt' and sys.version_info[:2]==(3,14) and len(cleanup_owner_cases)==9 and all(c['outcome']=='PASSED' for c in cleanup_owner_cases) and result.returncode==0

            report['native_job_verified_v78']=os.name=='nt' and len(native)==2 and all(c['outcome']=='PASSED' for c in native)
            report['process_backend_v78']='WINDOWS_JOB_OBJECT' if os.name=='nt' else 'LINUX_WITH_INJECTED_WINDOWS_OWNER'
            v94_cases=[c for c in report['tests'] if c['name'].startswith(('test_candidate94_','test_rpc94_'))]
            report['candidate_rpc_models_passed_v94']=len(v94_cases)==17 and all(c['outcome']=='PASSED' for c in v94_cases)
            report['candidate_rpc_windows_verified_v94']=os.name=='nt' and sys.version_info[:2]==(3,14) and report['candidate_rpc_models_passed_v94'] and result.returncode==0
            new_cases=[c for c in report['tests'] if c['name'].startswith(('test_rpc95_','test_prep95_'))]
            report['rpc_coordination_models_passed_v95']=len(new_cases)==16 and all(c['outcome']=='PASSED' for c in new_cases)
            bank_names=('test_verified_cached_real_programs_preserve_vm_effects[pump]','test_verified_cached_real_programs_preserve_vm_effects[amm]','test_metadata_proof_does_not_refresh_trading_snapshot_time','test_stale_live_bank_rejected')
            bank_cases=[c for c in report['tests'] if c['name'] in bank_names]
            report['program_cache_freshness_passed_v95']=len(bank_cases)==4 and all(c['outcome']=='PASSED' for c in bank_cases)
            report['rpc_coordination_windows_verified_v95']=os.name=='nt' and sys.version_info[:2]==(3,14) and report['rpc_coordination_models_passed_v95'] and report['program_cache_freshness_passed_v95'] and result.returncode==0
            report['passed']=report['rpc_coordination_models_passed_v95'] and report['program_cache_freshness_passed_v95'] and report['candidate_rpc_models_passed_v94'] and report['cleanup_owner_models_passed_v93'] and report['io_models_passed_v87'] and report['monitor_state_models_passed_v86'] and report['late_spawn_ownership_model_passed_v85'] and report['cleanup_regressions_model_passed_v84'] and result.returncode==0 and bool(cases) and all(c['outcome']=='PASSED' or (os.name!='nt' and ((c in io_cases and c['name']=='test_io87_native_windows_control_state_replace_recovers_after_release') or c in reader_cases or c in native or c['name'] in ('test_windows_timeout_kills_descendants','test_saved_result_sell_cancels_live_tree[native_windows]','test_native_late_spawn_after_timeout_cleans_tree','test_native_late_owner_survives_stop_and_reaps_tree','test_actual_shutdown_does_not_kill_retained_monitor','test_native_known_launcher_owner_survives_delayed_signal','test_monitor_state_native_windows_sharing_violation_preserves_heartbeat')) and c['outcome']=='SKIPPED') for c in report['tests']) and report['curve_overflow_replay_v83']['passed'] and report['curve_overflow_replay_v83'].get('paper_ledger_unchanged') is True and report['scenario'].get('financial_integrity')=='OK' and report['monitor_admission'].get('unknown_result_status')=='RECOVERY_INVALIDATED'
        except Exception as exc:
            report.update(error_type=type(exc).__name__,error=str(exc),error_traceback_v86_1=traceback.format_exc())
        finally:
            recovery_evidence.preserve(root,report,REPORT.parent)
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    tmp=REPORT.with_suffix('.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(REPORT)
    __import__('roadmap_progress').write_acceptance(report,REPORT)
    print('CONTROLLED PAPER RECOVERY: '+('PASS' if report['passed'] else 'FAILED'))
    print('Offline clock injection; no Windows suspension, clock modification or live RPC.')
    print('Report:',REPORT)
    print('Suite elapsed/budget:',report.get('suite_elapsed_s_v86_1'), '/', SUITE_TIMEOUT_S, 'seconds')
    if report.get('tests'):
        counts=__import__('collections').Counter(x['outcome'] for x in report['tests'])
        print('Tests:',dict(counts))
    if report.get('error_type'):print('Reason:',report['error_type'],report.get('error',''))
    if report.get('last_test_phase_v86_1'):print('Last test phase:',json.dumps(report['last_test_phase_v86_1'],ensure_ascii=True))
    if report.get('failure_evidence_zip_v86_1'):print('Partial evidence:',report['failure_evidence_zip_v86_1'])
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
