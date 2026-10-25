import json,sqlite3
import pytest
import execution_model as ex,database as db,main,paper_capital as cap,entry_pipeline
from test_primary_integrity import fresh
from test_execution_model import env

def snapshot(c,table):return [tuple(r) for r in c.execute('SELECT * FROM '+table)]

def test_stop_releases_exact_reservation_and_is_idempotent(env):
    c,clock,tick,entry=env;entry();oid=ex.pending(c,'a','BUY')['order_id']
    before=cap.summary(c);assert before['reserved']==pytest.approx(3.1025)
    originals={t:snapshot(c,t) for t in ('trades','positions','capital_ledger_v0721','meta')}
    with db.atomic():assert ex.cancel_unfilled_buys_on_stop(c,'USER_STOP')==[oid]
    o=c.execute('select * from paper_orders_v0720 where order_id=?',(oid,)).fetchone()
    assert o['status']=='CANCELLED_STOP' and o['reserved_total']==0 and o['fill_ts'] is None
    note=json.loads(o['note']);assert note['reason']=='USER_STOP' and note['released_reservation']==pytest.approx(3.1025)
    after=cap.summary(c);assert after['reserved']==0 and after['available']==30 and after['net_cash']==30
    for t,v in originals.items():assert snapshot(c,t)==v
    assert ex.cancel_unfilled_buys_on_stop(c)==[]
    assert c.execute("select n from order_audit_v0739 where order_id=? and code='CANCELLED_STOP'",(oid,)).fetchone()[0]==1
    row=entry_pipeline.summary(c)['rows'][0];assert row['finished_ts']==note['ts'] and entry_pipeline.outcome(dict(o),[])=='CANCELLED_STOP'
    tick();assert not main.P and c.execute('select count(*) from trades').fetchone()[0]==0

def test_filled_buy_and_pending_sell_untouched(env):
    c,clock,tick,entry=env;entry();tick();main.sell('a',1,'TEST')
    originals={t:snapshot(c,t) for t in ('paper_orders_v0720','trades','positions','capital_ledger_v0721','meta','order_audit_v0739')}
    assert ex.pending(c,'a','SELL')
    oid=ex.request(c,'other','BUY',1,3,'TEST',{})
    assert ex.cancel_unfilled_buys_on_stop(c)==[oid]
    for t,v in originals.items():
        if t=='paper_orders_v0720':assert [tuple(r) for r in c.execute("select * from paper_orders_v0720 where mint='a'")]==v
        elif t=='order_audit_v0739':assert [tuple(r) for r in c.execute('select * from order_audit_v0739 where order_id<>?',(oid,))]==v
        else:assert snapshot(c,t)==v

def test_audit_failure_rolls_back_status_and_reserve(env,monkeypatch):
    c,clock,tick,entry=env;entry();original=snapshot(c,'paper_orders_v0720')
    add=ex.ta.add
    def fail(*a,**k):add(*a,**k);raise RuntimeError('audit write failed')
    monkeypatch.setattr(ex.ta,'add',fail)
    with pytest.raises(RuntimeError,match='audit write failed'):ex.cancel_unfilled_buys_on_stop(c)
    assert snapshot(c,'paper_orders_v0720')==original and ex.reserve(c)[1]==pytest.approx(3.1025)
    assert not c.execute("select 1 from order_audit_v0739 where code='CANCELLED_STOP'").fetchone()

def test_outer_rollback_not_committed_by_cancel(env):
    c,clock,tick,entry=env;entry();before=dict(ex.ORIGINS)
    with pytest.raises(RuntimeError):
        with db.atomic():ex.cancel_unfilled_buys_on_stop(c);raise RuntimeError()
    assert ex.pending(c,'a','BUY') and ex.reserve(c)[1]==pytest.approx(3.1025) and ex.ORIGINS==before
    tick();assert 'a' in main.P

def test_rejected_expired_filled_and_sell_are_preserved(env):
    c,clock,tick,entry=env;entry()
    c.execute("update paper_orders_v0720 set status='REJECTED'")
    for status in ('EXPIRED','FILLED','CANCELLED_RESTART'):
        ex.request(c,status,'BUY',1,3,'TEST',{})
        c.execute("update paper_orders_v0720 set status=? where mint=?",(status,status))
    a=ex.request(c,'pending1','BUY',1,3,'TEST',{});b=ex.request(c,'pending2','BUY',1,3,'TEST',{})
    original=[tuple(r) for r in c.execute("select * from paper_orders_v0720 where status<>'PENDING'")]
    assert set(ex.cancel_unfilled_buys_on_stop(c))=={a,b}
    assert [tuple(r) for r in c.execute("select * from paper_orders_v0720 where status<>'CANCELLED_STOP'")]==original

def test_restart_does_not_resurrect_cancelled_buy(env):
    c,clock,tick,entry=env;entry();ex.cancel_unfilled_buys_on_stop(c);before=snapshot(c,'paper_orders_v0720')
    ex.restart(c);assert snapshot(c,'paper_orders_v0720')==before and ex.reserve(c)==(0,0)
