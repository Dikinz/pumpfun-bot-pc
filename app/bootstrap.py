"""All Windows launch buttons enter here before dependencies or settings."""
import os,sys
from pathlib import Path
APP=Path(__file__).resolve().parent
ROOT=APP.parent
sys.path.insert(0,str(APP))
from release_guard import verify,local_database

def main():
    verify(ROOT)
    if Path(sys.prefix).resolve()!=(ROOT/'.venv').resolve():
        raise RuntimeError('CLEAN_ENVIRONMENT_MISMATCH: run 01_INSTALL.bat in this folder')
    # Old shell variables must not redirect this installation to another DB/UI.
    for key in ('PYTHONPATH','PYTHONHOME','DATABASE_PATH','DASHBOARD_PORT'):
        os.environ.pop(key,None)
    os.chdir(APP)
    command=sys.argv[1] if len(sys.argv)>1 else 'help'
    if command=='verify-release':
        print('CLEAN RELEASE VERIFIED:',ROOT);return 0
    if command=='spawn-diagnostics':
        import spawn_report_v89
        return spawn_report_v89.main(sys.argv[2:])
    if command=='network-check':
        import network_check_v88
        return network_check_v88.main(sys.argv[2:])
    if command=='perf-check':
        import perf_check_v87
        return perf_check_v87.main()
    if command=='recovery-check':
        import recovery_selftest
        return recovery_selftest.main()
    if command=='preserve-data':
        import sqlite_preserve
        selected=input('Stopped OLD app/data folder: ').strip().strip(chr(34))
        if not selected:print('Cancelled: no folder selected.');return 1
        return sqlite_preserve.main([selected])
    if command=='review-archive':
        import exit_cost_review
        return exit_cost_review.main(sys.argv[2:])
    import launcher
    if command!='install':
        launcher.setup_env()
        import config
        if local_database(APP,config.DB)!=(APP/'data/clean_0762.db').resolve():
            raise RuntimeError('CLEAN_DATABASE_PROFILE_MISMATCH: configure settings in this new folder; do not copy the old .env')
        print('CLEAN RELEASE:',config.VERSION,flush=True)
        print('FOLDER:',ROOT,flush=True)
        print('DASHBOARD: http://127.0.0.1:'+str(config.PORT),flush=True)
    if command=='test-amm-stop':
        print(__import__('branch_isolation').inject_stop(config.DB),flush=True);return 0
    if command=='amm-check':return launcher.call('target_amm_check.py')
    return launcher.main()

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print('START BLOCKED:',str(exc));raise SystemExit(1)
