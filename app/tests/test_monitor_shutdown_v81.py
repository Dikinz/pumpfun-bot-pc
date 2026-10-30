"""Real subprocess finalization with parent stdin pipe deliberately left open."""
import asyncio,json,os,subprocess,sys,time
from pathlib import Path
from contextlib import closing
import pytest
import monitor_lifetime as life,monitor_service as ms,config,main,position_monitor as pm
from test_primary_integrity import fresh

def wait_event(event):assert event.wait(2)

def test_lifetime_idle_close_does_not_need_parent_eof():
    r,w=os.pipe();watch=life.StdinLifetime(r)
    try:
        result=watch.close()
        assert result['confirmed'] and result['reason']=='OBSERVER_CLOSED' and not result['buffered_stdin_used']
        os.fstat(r);os.fstat(w)
    finally:os.close(w);os.close(r)

def test_lifetime_parent_eof_is_observed():
    r,w=os.pipe();watch=life.StdinLifetime(r)
    try:
        os.close(w);w=None;wait_event(watch.stop)
        result=watch.close();assert result['confirmed'] and result['reason']=='PIPE_EOF'
    finally:
        watch.close()
        if w is not None:os.close(w)
        os.close(r)

def test_lifetime_data_is_observed_without_buffered_stdin():
    r,w=os.pipe();watch=life.StdinLifetime(r)
    try:
        os.write(w,b'x');wait_event(watch.stop)
        result=watch.close();assert result['confirmed'] and result['reason']=='PIPE_DATA'
    finally:watch.close();os.close(w);os.close(r)

@pytest.mark.parametrize('outcome',['normal','SIMULATION_CHILD_CLEANUP_TIMEOUT','SIMULATION_CHILD_CLEANUP_UNCONFIRMED'])
def test_branch_monitor_entrypoint_exits_with_parent_pipe_open(tmp_path,outcome):
    target=tmp_path/'temporary.db'
    script='''import asyncio,sys,config,runpy
config.DB=sys.argv[1]
import monitor_service as ms
async def injected(*args,**kwargs):
 await asyncio.sleep(.1)
 if sys.argv[2]!='normal':raise RuntimeError(sys.argv[2])
ms.run=injected
sys.argv=['paper_branch.py','--monitor',config.DB,'--parent','0']
runpy.run_path('paper_branch.py',run_name='__main__')
'''
    # Save injection separately: entrypoint rewrites sys.argv, as the real launcher does.
    script=script.replace("async def injected(*args,**kwargs):","outcome=sys.argv[2]\nasync def injected(*args,**kwargs):").replace("sys.argv[2]!='normal':raise RuntimeError(sys.argv[2])","outcome!='normal':raise RuntimeError(outcome)")
    child=subprocess.Popen([sys.executable,'-c',script,str(target),outcome],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        child.wait(timeout=8) # Crucial: do NOT communicate()/close stdin before child exits.
        assert not child.stdin.closed
        stdout=child.stdout.read().decode(errors='replace');stderr=child.stderr.read().decode(errors='replace')
        assert 'Fatal Python error' not in stderr and '_enter_buffered_busy' not in stderr,stderr
        assert (child.returncode==0)==(outcome=='normal'),stderr
        if outcome!='normal':assert outcome in stderr
        report=json.loads(ms.paths(target)['stdin_lifetime'].read_text())
        assert report['confirmed'] and not report['thread_alive'] and report['reason']=='OBSERVER_CLOSED'
    finally:
        child.stdin.close()
        if child.poll() is None:child.kill();child.wait(timeout=5)
        child.stdout.close();child.stderr.close()

def test_monitor_failure_keeps_cleanup_cause_and_financial_state(fresh,tmp_path,monkeypatch):
    __import__('wallet_costs').ensure(fresh);assert main.buy('open',1)[0]
    from paper_capital import record,summary
    record(fresh,main.P['open']['trade_id'],'BUY',3,{'pool_fee_rate':.0125})
    before=summary(fresh);monkeypatch.setattr(__import__('recovery_gate'),'ACTIVE',None)
    async def fail(job,c):
        pm.finish(c,job,dict(status='CLEANUP_FAILED'))
        raise RuntimeError('SIMULATION_CHILD_CLEANUP_UNCONFIRMED')
    monkeypatch.setattr(pm,'run_job',fail)
    r,w=os.pipe()
    try:
        with pytest.raises(RuntimeError,match='SIMULATION_CHILD_CLEANUP_UNCONFIRMED'):
            asyncio.run(ms.run_with_stdin(config.DB,0,fd=r))
        state=ms.read_state(config.DB)
        assert state['status']=='FAILED' and state['failure_v81']['chain'][0]['message']=='SIMULATION_CHILD_CLEANUP_UNCONFIRMED'
        report=json.loads(ms.paths(config.DB)['stdin_lifetime'].read_text());assert report['confirmed']
        after=summary(fresh)
        for key in ('net_cash','available','reserved','open_principal','total_cost'):assert before[key]==after[key]
        assert len(main.P)==1 and not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()
    finally:os.close(w);os.close(r)

def test_observer_shutdown_failure_does_not_mask_child_cleanup(fresh,monkeypatch):
    class Stuck:
        stop=__import__('threading').Event()
        def __init__(self,*a):pass
        def close(self):return dict(confirmed=False,thread_alive=True,error=None)
    async def fail(*a):raise RuntimeError('SIMULATION_CHILD_CLEANUP_UNCONFIRMED')
    monkeypatch.setattr(life,'StdinLifetime',Stuck);monkeypatch.setattr(ms,'run',fail)
    with pytest.raises(RuntimeError,match='SIMULATION_CHILD_CLEANUP_UNCONFIRMED'):
        asyncio.run(ms.run_with_stdin(config.DB,0))
    report=json.loads(ms.paths(config.DB)['stdin_lifetime'].read_text());assert report['confirmed'] is False
