import json,os,sys,sqlite3,subprocess,threading,time,copy
from pathlib import Path
import pytest
import config,branch_transport as bt,paper_branch as branch,run_audit as ra
from test_primary_integrity import fresh
from test_run_exit_v45 import fill

@pytest.fixture
def offline_worker(monkeypatch):
    real=subprocess.Popen
    script="""import asyncio,sys,json,config
for k,v in json.loads(sys.argv[4]).items():setattr(config,k,v)
import exit_quotes
async def fx():exit_quotes.FX_STATUS='READY'
exit_quotes.refresh_fx=fx
import paper_branch
asyncio.run(paper_branch.worker(sys.argv[1],sys.argv[2],sys.argv[3]))
"""
    def launch(cmd,**kwargs):
        if len(cmd)>2 and cmd[1]=='paper_branch.py' and cmd[2]=='--worker':
            import candidate_lab
            thresholds=json.dumps({k:getattr(config,k) for k in candidate_lab.PARAMETERS})
            return real([sys.executable,'-c',script,*cmd[3:],thresholds],**kwargs)
        return real(cmd,**kwargs)
    monkeypatch.setattr(bt.subprocess,'Popen',launch)
    yield
    bt.ACTIVE=None;bt.LAST=None


def test_real_child_stop_ack_and_database_commit(fresh,offline_worker):
    p=bt.Publisher(config.DB)
    try:r=p.close()
    finally:
        if p.child.poll() is None:p.child.kill();p.child.wait()
    assert r['status']=='STOPPED' and r['exit_code']==0 and not r['forced']
    with sqlite3.connect(branch.path(config.DB)) as c:
        meta=dict(c.execute('SELECT k,v FROM meta'))
        assert meta['runtime_state_v41']=='STOPPED' and meta['branch_shutdown_state']=='STOPPED'
        assert c.execute('SELECT status FROM runs_v45 ORDER BY rowid DESC LIMIT 1').fetchone()[0]=='STOPPED'
    assert branch.read_lifecycle(config.DB)['session']==p.session
    assert branch.read_lifecycle(config.DB)['reason']=='PARENT_STOP'
    assert p.close() is r
    report=branch.report(config.DB)
    assert report['runtime']['state']=='STOPPED' and report['transport']['status']=='STOPPED'


def test_wrong_stop_session_is_failed_not_user_stop(fresh,offline_worker):
    p=bt.Publisher(config.DB)
    p.child.stdin.write(json.dumps(dict(control='STOP',session='old-session'))+'\n');p.child.stdin.flush()
    try:
        p.child.wait(timeout=12);r=p.close()
    finally:
        if p.child.poll() is None:p.child.kill();p.child.wait()
    assert r['status']=='FAILED'
    with sqlite3.connect(branch.path(config.DB)) as c:
        assert c.execute("SELECT v FROM meta WHERE k='runtime_state_v41'").fetchone()[0]=='FAILED'


def fake_publisher(primary,child):
    p=bt.Publisher.__new__(bt.Publisher);p.primary=primary;p.child=child;p.session='current-session'
    p.close_lock=threading.Lock();p.stopping=threading.Event();p.closed=None;p.thread=None
    return p


def test_forced_stop_is_never_reported_stopped(fresh):
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    p=fake_publisher(config.DB,child)
    r=p.close(timeout=.01)
    assert r['status']=='FORCED_STOP' and r['forced'] and child.poll() is not None
    assert branch.read_lifecycle(config.DB)=={}
    assert branch.read_lifecycle(config.DB,True)['status']=='FORCED_STOP'


def test_old_ack_does_not_confirm_new_process(fresh):
    branch.write_lifecycle(config.DB,dict(session='previous-session',status='STOPPED'))
    child=subprocess.Popen([sys.executable,'-c','pass']);child.wait(timeout=5)
    r=fake_publisher(config.DB,child).close()
    assert r['status']=='UNCONFIRMED_EXIT'


def test_windows_and_posix_process_groups(monkeypatch):
    monkeypatch.setattr(subprocess,'CREATE_NEW_PROCESS_GROUP',512,raising=False)
    assert bt.process_options('nt')=={'creationflags':512}
    assert bt.process_options('posix')=={'start_new_session':True}


def test_carried_and_new_cohorts_reconcile_different_cash_scope(fresh):
    fill(fresh,'carry','BUY',1.,100)
    ra.begin(fresh);fill(fresh,'carry','SELL',.2,200)
    fill(fresh,'new','BUY',1.,300);fill(fresh,'new','SELL',1.2,400)
    ra.beat(fresh,'STOPPED',True)
    result=ra.summary(fresh)['current']['result'];groups=result['cohorts'];carry=groups['carried'];new=groups['new']
    assert carry['positions_at_start']==1 and carry['entries']==0 and carry['closed']==1
    assert new['entries']==1 and new['closed']==1 and not new['still_open']
    assert carry['net_closed']+new['net_closed']==pytest.approx(result['net_closed'])
    assert carry['cash_change_in_run']+new['cash_change_in_run']==pytest.approx(result['cash_change'])
    assert carry['cash_change_in_run']-carry['net_closed']==pytest.approx(3+carry['costs_before_run_for_closed'])
    frozen=copy.deepcopy(groups)
    # Legacy rows gain a read-only projection using saved ID boundaries.
    record=fresh.execute('SELECT id,result_json FROM runs_v45 ORDER BY rowid DESC LIMIT 1').fetchone()
    old=json.loads(record['result_json']);old.pop('cohorts')
    fresh.execute('UPDATE runs_v45 SET result_json=? WHERE id=?',(json.dumps(old),record['id']))
    fill(fresh,'future','BUY',1.,500)
    assert ra.summary(fresh)['current']['result']['cohorts']==frozen


def test_missing_entry_cost_is_not_zero_for_new_cohort(fresh):
    ra.begin(fresh);fill(fresh,'new','BUY',1.,100)
    fresh.execute("DELETE FROM capital_ledger_v0721 WHERE trade_id='new'")
    ra.beat(fresh,'STOPPED',True)
    group=ra.summary(fresh)['current']['result']['cohorts']['new']
    assert group['still_open']==1 and group['net_closed'] is None and group['accounting_missing']

def test_stop_control_bypasses_pending_market_queue():
    import io,queue
    class Pipe(io.StringIO):
        def close(self):pass
    p=bt.Publisher.__new__(bt.Publisher);p.queue=queue.Queue();p.queue.put({'must_not_be_processed':True})
    p.stopping=threading.Event();p.stopping.set();p.session='current';p.error=False
    p.child=type('Child',(),{'stdin':Pipe()})()
    p.send()
    assert p.child.stdin.getvalue()=='' # v72 uses independent session control, no STOP pipe write
    assert p.queue.qsize()==1 and not p.error


def test_unexpected_eof_is_not_reported_as_normal_stop(fresh,offline_worker):
    p=bt.Publisher(config.DB);p.child.stdin.close()
    try:
        p.child.wait(timeout=12);r=p.close()
    finally:
        if p.child.poll() is None:p.child.kill();p.child.wait()
    assert r['status']=='FAILED'
