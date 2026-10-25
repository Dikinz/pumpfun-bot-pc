import json,time,sqlite3,urllib.error,email.utils
from contextlib import closing
import pytest
import main,config,monitor_service as ms,position_monitor as pm,priority_exit as pe,fresh_retry as retry,connection_audit as ca
from test_primary_integrity import fresh
from test_priority_exit_v65 import opened

def failure(http=None,delay=None,category='CONNECTION'):
    item={}
    if http is not None:item['http_status']=http;category='HTTP'
    if delay is not None:item['retry_after_s']=delay
    return dict(status='REQUEST_ERROR',network_error=dict(category=category,chain=[item]),rpc_timing=dict(requests=[dict(status='ERROR',method='getMultipleAccounts',stage='FRESH_TRADING_BANK',http_status=http)]))

def first(c,now):
    pe.observe(c,now);return pe.dispatch(c,now)

def test_429_honors_header_and_bypasses_only_normal_cadence(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now);pm.finish(c,j,failure(429,10),now=now+1)
        assert pe.dispatch(c,now+10.99) is None
        assert pm.next_job(c,now+10.99) is None
        assert pm.next_due(c,j['trade_id'],now-100)==pytest.approx(now+11)
        k=pe.dispatch(c,now+11.01)
        assert k and k['id']!=j['id'] and k['scheduled_ts']>=now+11
        assert k['retry_v69']['attempt']==1 and k['retry_v69']['parent_check_id']==j['id']
        assert retry.summary(c)['attempts'][0]['details']['rpc_stage']=='FRESH_TRADING_BANK'
        assert c.execute('SELECT status FROM priority_exit_requests_v65').fetchone()[0]=='REQUEST_ERROR'

def test_transient_retry_is_fresh_and_bounded_to_two_additional_attempts(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now);pm.finish(c,j,failure(),now=now+.1)
        assert pe.dispatch(c,now+.34) is None
        k=pe.dispatch(c,now+.36);assert k and k['retry_v69']['attempt']==1
        pm.finish(c,k,failure(),now=now+.5)
        last=pe.dispatch(c,now+1.51);assert last and last['retry_v69']['attempt']==2
        assert len({j['id'],k['id'],last['id']})==3
        pm.finish(c,last,failure(),now=now+2)
        r=retry.summary(c);assert r['rows'][0]['status']=='EXHAUSTED' and len(r['attempts'])==3
        assert pe.dispatch(c,now+50) is None
        assert ca.retry(c,j['trade_id'])['not_before_ts']==pytest.approx(now+32)

def test_auth_and_unknown_error_are_not_retried(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now);pm.finish(c,j,failure(401),now=now+.1)
        assert retry.summary(c)['rows'][0]['status']=='NONRETRYABLE'
        assert pe.dispatch(c,now+2) is None

def test_retry_after_larger_than_window_does_not_send_early(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now);pm.finish(c,j,failure(429,600),now=now+.1)
        assert pe.dispatch(c,now+59) is None and pm.next_job(c,now+100) is None
        assert retry.summary(c)['rows'][0]['status']=='WINDOW_EXHAUSTED'

def test_closed_position_cancels_waiting_retry_without_fill(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now);pm.finish(c,j,failure(),now=now+.1)
        fresh.execute('DELETE FROM positions');assert pe.dispatch(c,now+1) is None
        assert retry.summary(c)['rows'][0]['status']=='CLOSED_BEFORE_RETRY'
        assert fresh.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==0

def test_stop_restart_do_not_resurrect_chain(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now);pm.finish(c,j,failure(),now=now+.1);pe.recover(c)
        assert retry.summary(c)['rows'][0]['status']=='INTERRUPTED_RESTART'
        assert pe.dispatch(c,now+1) is None

def test_nontransport_result_does_not_retry_even_if_liquidity_is_bad(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now);pm.finish(c,j,dict(status='INSUFFICIENT_RESERVES'),now=now+.1)
        assert not retry.summary(c)['rows'] and pe.dispatch(c,now+1) is None

def test_valid_http_date_and_large_numeric_retry_after_are_retained():
    e=urllib.error.HTTPError('https://redacted',429,'ignored',{'Retry-After':'600'},None)
    assert ca.diagnostic(e)['chain'][0]['retry_after_s']==600
    e.headers={'Retry-After':email.utils.formatdate(time.time()+30,usegmt=True)}
    assert 28<=ca.diagnostic(e)['chain'][0]['retry_after_s']<=31

def test_fresh_retry_result_settles_exactly_once(fresh):
    import route_branch as route,paper_capital as cap
    from test_route_exit_v59 import candidate
    tid=opened(fresh);fresh.executescript(route.SCHEMA);row,j,result,inv=candidate(tid)
    fresh.execute('INSERT OR REPLACE INTO quote_inventory_v0723(trade_id,mint,size_usdc,sol_usdc,token_units,entry_sol_price,raw_amount,decimals) VALUES(?,?,?,?,?,?,?,?)',tuple(inv[k] for k in ('trade_id','mint','size_usdc','sol_usdc','token_units','entry_sol_price','raw_amount','decimals')))
    before=cap.cash(fresh);now=time.time()
    with closing(ms.connect()) as c:
        first_job=first(c,now-1);pm.finish(c,first_job,failure(),now=now-.8)
        retry_job=pe.dispatch(c,now-.1);assert retry_job['id']!=first_job['id']
        assert retry_job['inventory']==dict(fresh.execute('SELECT * FROM quote_inventory_v0723').fetchone())
        result.update(scheduled_ts=retry_job['scheduled_ts'],snapshot_received_ts=time.time(),minimum_net_lamports=30000000,net_status='NET_BELOW_FLOOR')
        pm.finish(c,retry_job,result,now=time.time())
        assert retry.summary(c)['rows'][0]['status']=='RESULT_RETURNED'
    route.poll(main);route.poll(main)
    assert not main.P and cap.cash(fresh)==pytest.approx(before+2.5)
    assert fresh.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==1
    assert __import__('primary_integrity').summarize(fresh)['status']=='OK'

def test_running_retry_is_recorded_and_cancelled_on_stop(fresh):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now);pm.finish(c,j,failure(),now=now+.1);k=pe.dispatch(c,now+.4)
        assert retry.summary(c)['attempts'][-1]['status']=='RUNNING'
        pe.stop(c,now+.5)
        assert retry.summary(c)['attempts'][-1]['status']=='INTERRUPTED_STOP'
        assert pe.dispatch(c,now+2) is None


def test_existing_technical_backoff_is_stronger_than_fast_retry(fresh,monkeypatch):
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        j=first(c,now)
        monkeypatch.setattr(ca,'retry',lambda *a:dict(not_before_ts=now+50))
        pm.finish(c,j,failure(),now=now+.1)
        assert pe.dispatch(c,now+49) is None
        k=pe.dispatch(c,now+50.01);assert k
        assert 0<k['deadline_timeout_s']<11
