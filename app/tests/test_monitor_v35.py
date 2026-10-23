import time,json,asyncio,threading
from contextlib import closing
import pytest
import main,monitor_service as ms,position_monitor as pm,entry_cost_delta as delta,cohort_report as cohort
from test_primary_integrity import fresh
from test_delta_v34 import prepare,sample

def test_reserved_lane_and_no_duplicate_running_check(fresh):
    tid=prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job=pm.next_job(c,now,lane='initial')
        assert job['trade_id']==tid
        assert pm.next_job(c,now+30,lane='regular') is None
        assert main.buy('second-mint',1)[0]
        second=pm.next_job(c,now+1,lane='initial')
        assert second and second['trade_id']!=tid
        assert pm.next_job(c,now+30) is None

def test_coverage_and_followup_are_distinct(fresh):
    prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=sample(c,now);pm.finish(c,job,r,now=now)
        out=delta.summary(c,now)
        assert out['baseline_fraction']==1 and out['comparable_positions']==0
        job,r=sample(c,now+11,29346200,0,101);pm.finish(c,job,r,now=now+11)
        assert delta.summary(c,now+11)['comparable_positions']==1

def test_coverage_early_close_is_not_deadline_miss(fresh):
    prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time();job,r=sample(c,now)
        main.sell('monitor-test-mint',.9,'TEST')
        pm.finish(c,job,r,now=time.time()+.1)
        out=delta.summary(c)
        assert out['coverage_reasons']=={'CLOSED_BEFORE_RESULT':1}

def test_coverage_counts_positions_not_failed_checks(fresh):
    prepare(fresh)
    with closing(ms.connect()) as c:
        now=time.time()+11
        for t in (now,now+11):
            job,r=sample(c,t);pm.finish(c,job,r,now=t)
        assert delta.summary(c,now+12)['coverage_reasons']=={'BASELINE_WINDOW_MISSED':1}

def test_stop_not_counted_as_technical_error(fresh):
    prepare(fresh)
    with closing(ms.connect()) as c:
        job=pm.next_job(c,time.time());pm.finish(c,job,{'status':'INTERRUPTED_STOP'})
        assert cohort.health(c)['errors_in_row']==0

def test_real_scheduler_initial_lane_while_regular_busy(fresh,monkeypatch,tmp_path):
    tid=prepare(fresh);stop=threading.Event();started=[]
    with closing(ms.connect()) as c:
        job=pm.next_job(c,time.time());pm.finish(c,job,{'status':'REQUEST_TIMEOUT'})
        c.execute('UPDATE position_checks_v0730 SET started_ts=started_ts-20 WHERE id=?',(job['id'],))
    async def fake(job,c):
        started.append(job['trade_id'])
        if len(started)==1:
            assert main.buy('new-during-slow-check',1)[0]
        else:stop.set()
        try:await asyncio.sleep(10)
        except asyncio.CancelledError:
            pm.finish(c,job,{'status':'INTERRUPTED_STOP'});raise
    monkeypatch.setattr(pm,'run_job',fake)
    async def run():await asyncio.wait_for(ms.run(None,0,stop_event=stop),3)
    asyncio.run(run())
    assert len(started)==2 and started[0]==tid and started[1]!=tid
    with closing(ms.connect()) as c:
        assert c.execute("SELECT count(*) FROM position_checks_v0730 WHERE status='RUNNING'").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM monitor_events_v0731 WHERE kind='CHECK_FINISHED'").fetchone()[0]==2
