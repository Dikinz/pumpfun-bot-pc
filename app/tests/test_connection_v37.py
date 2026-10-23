import json,socket,time,urllib.error
from contextlib import closing
import pytest
import connection_audit as ca,position_monitor as pm,monitor_service as ms,cohort_report as co,main,paper_capital as cap
from test_primary_integrity import fresh
from test_delta_v34 import prepare,sample

def test_dns_and_http_diagnostics_never_contain_credentials():
    d=ca.diagnostic(urllib.error.URLError(socket.gaierror(11001,'https://host/secret?api-key=SECRET')))
    assert d['category']=='DNS' and d['chain'][-1]['errno']==11001
    h=ca.diagnostic(urllib.error.HTTPError('https://secret',429,'SECRET',{'Retry-After':'90','Authorization':'SECRET'},None))
    assert h['chain'][0]['http_status']==429 and h['chain'][0]['retry_after_s']==90
    assert 'SECRET' not in json.dumps([d,h]) and 'https' not in json.dumps([d,h])

def test_backoff_cap_warning_and_recovery(fresh):
    tid=prepare(fresh)
    with closing(ms.connect()) as c:
        t=time.time()
        for count,delay in enumerate([0,0,30,60,120,240,300,300],1):
            job=pm.next_job(c,t);assert job
            pm.finish(c,job,{'status':'REQUEST_ERROR'},now=t+1)
            r=ca.retry(c,tid);assert r['delay_s']==delay
            if delay:
                assert pm.next_job(c,t+delay) is None
                assert not ms.overdue(c,t+delay)
            t=max(t+11,t+delay+1)
        assert c.execute('SELECT count(*) FROM monitor_alerts_v0737').fetchone()[0]==1
        assert ca.summary(c,t)['warning'] is True
        job=pm.next_job(c,t);pm.finish(c,job,{'status':pm.MIGRATION_STATUS},now=t+1)
        assert ca.retry(c,tid)['streak']==0 and ca.retry(c,tid)['delay_s']==0
        assert ca.summary(c,t+1)['warning'] is False

def test_recent_evidence_gaps_ignore_stale_and_late_results(fresh):
    tid=prepare(fresh)
    with closing(ms.connect()) as c:
        opened=c.execute("SELECT ts FROM trades WHERE trade_id=? AND side='BUY'",(tid,)).fetchone()[0]
        job,r=sample(c,opened+2);pm.finish(c,job,r,now=opened+2)
        # Initial 2 seconds + 18 seconds after the 30-second evidence window.
        gaps=ca.observation_gaps(c,opened+50);row=gaps['rows'][0]
        assert row['uncovered_s']==pytest.approx(20) and row['current_uncovered_s']==pytest.approx(18)
        job,r=sample(c,opened+40);r['snapshot_received_ts']=opened+1;pm.finish(c,job,r,now=opened+40)
        assert ca.observation_gaps(c,opened+50)['rows'][0]['uncovered_s']==pytest.approx(20)
        fresh.execute("INSERT INTO trades(trade_id,side,mint,ts,size,price,pnl) VALUES(?,?,?,?,?,?,?)",(tid,'SELL','monitor-test-mint',opened+45,3,1,0))
        job,r=sample(c,opened+51);pm.finish(c,job,r,now=opened+51)
        row=ca.observation_gaps(c,opened+70)['rows'][0]
        assert row['uncovered_s']==pytest.approx(15) and row['current_uncovered_s']==0

def test_matched_closed_and_released_capital_are_separate(fresh):
    tid=prepare(fresh)
    with closing(ms.connect()) as c:
        selected={tid:dict(available_ts=time.time(),receipt_units='2.5')}
        report=co.summary(c,selected)
        assert report['matched_closed']['count']==0
        assert report['released_while_paper_open']['receipts']==2.5
        assert report['released_while_paper_open']['paper_principal']==3
        main.sell('monitor-test-mint',.9,'TEST');cap.record(fresh,tid,'SELL',2.7,{'pool_fee_rate':.0125})
        report=co.summary(c,selected)
        assert report['matched_closed']['count']==1 and report['released_while_paper_open']['count']==0
        assert report['matched_closed']['delta']==pytest.approx(report['trial_realized_pnl']-report['paper_pnl'])
