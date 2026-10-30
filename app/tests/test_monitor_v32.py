import asyncio,json,time,subprocess,sys
from pathlib import Path
from contextlib import closing
import pytest
import position_monitor as pm,monitor_service as ms,cohort_report as cohort,main,paper_capital as cap
from test_primary_integrity import fresh
from test_monitor_v31 import open_case,checked

def test_whole_cohort_keeps_unresolved_and_fallback(fresh):
    tid=open_case(fresh)
    with closing(ms.connect()) as c:
        r=cohort.summary(c)
        assert r['entries']==1 and r['trial_unresolved']==1 and r['local_exits']==0
        assert r['trial_cash_fixed_entries']==pytest.approx(cap.cash(fresh))
        now=time.time();job=pm.next_job(c,now);pm.finish(c,job,{'status':'REQUEST_TIMEOUT'})
        assert cohort.health(c)['errors_in_row']==1
        main.sell('monitor-test-mint',.8,'TEST')
        cap.record(fresh,tid,'SELL',2.4,{'pool_fee_rate':.0125})
        r=cohort.summary(c)
        assert r['tick_fallback']==1 and r['trial_unresolved']==0
        assert r['trial_realized_pnl']==pytest.approx(r['paper_pnl'])
        assert r['trial_cash_fixed_entries']==pytest.approx(cap.cash(fresh))

def test_local_exit_changes_only_shadow_cash(fresh):
    tid=open_case(fresh);before=cap.cash(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job=pm.next_job(c,now);job['inventory']={'sol_usdc':'100','size_usdc':'3'}
        pm.finish(c,job,checked(now),now=now)
        r=cohort.summary(c)
        assert r['entries']==1 and r['local_exits']==1 and r['trial_unresolved']==0 and r['paper_open']==1
        assert r['trial_cash_fixed_entries']==pytest.approx(before+1.851156)
        assert cap.cash(fresh)==before

def test_child_failure_is_archived(tmp_path):
    r=asyncio.run(pm.launch_check({},tmp_path,timeout=5))
    assert r['status'] in ('CHILD_ERROR','DATA_REJECTED')
    assert (tmp_path/'diagnostic.zip').exists()
    assert r['progress']['stage'] in ('IMPORTS','TOKEN_METADATA','SAVE_RESULT')

def test_real_child_vm_with_parent_stdin_pipe():
    # Exercise actual child launch + VM while parent has an open input pipe, as on Windows launcher.
    p=subprocess.Popen([sys.executable,'monitor_selftest.py'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        p.wait(timeout=35)
        out=p.stdout.read().decode();err=p.stderr.read().decode()
        assert p.returncode==0,(out,err)
        assert json.loads(out)['passed'] is True
    finally:
        if p.poll() is None:p.kill();p.wait()
        p.stdin.close()

def test_timeout_has_progress_and_exportable_diagnostic(tmp_path):
    r=asyncio.run(pm.launch_check({},tmp_path,timeout=.001))
    assert r['status']=='WORKER_TIMEOUT'
    assert r['progress']['stage'] in ('SPAWN','CHILD_STARTED','IMPORTS','TOKEN_METADATA','SAVE_RESULT')
    assert (tmp_path/'diagnostic.zip').exists()
