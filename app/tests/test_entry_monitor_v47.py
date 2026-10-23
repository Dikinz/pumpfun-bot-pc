import sqlite3,json
import pytest
import monitor_history as mh
import entry_pipeline as ep

@pytest.mark.parametrize('finished,expected',[(9,'BEFORE_SIGNAL'),(10,'BEFORE_SIGNAL'),(11,'AFTER_SIGNAL_BEFORE_CLOSE'),(12,'AFTER_SIGNAL_BEFORE_CLOSE'),(13,'AFTER_CLOSE'),(None,'UNFINISHED')])
def test_result_availability_not_request_start(finished,expected):
    assert mh.timing(finished,10,12)==expected

def test_closed_history_complete_separate_trade_ids_and_views():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.executescript("""
    CREATE TABLE orders(trade_id,signal_ts,side);
    CREATE TEMP VIEW paper_orders_v0720 AS SELECT * FROM orders;
    CREATE TABLE trades(trade_id,ts,side);
    CREATE TABLE position_checks_v0730(id,trade_id,mint,started_ts,finished_ts,status,result_json);
    INSERT INTO orders VALUES('old',10,'SELL'),('new',200,'SELL');
    INSERT INTO trades VALUES('old',12,'SELL'),('new',202,'SELL');
    """)
    for i in range(105):
        c.execute('INSERT INTO position_checks_v0730 VALUES(?,?,?,?,?,?,?)',(str(i),'old','same',1,9 if i<104 else 13,'SELLABLE','{}'))
    c.execute('INSERT INTO position_checks_v0730 VALUES(?,?,?,?,?,?,?)',('new','new','same',199,203,'FAILED','[]'))
    before=c.total_changes
    d=mh.summary(c)['by_trade']
    assert d['old']['total']==105 and d['old']['timing_counts']=={'BEFORE_SIGNAL':104,'AFTER_CLOSE':1}
    assert d['old']['latest']['timing']=='AFTER_CLOSE'
    assert d['old']['latest_before_close']['finished_ts']==9
    assert d['new']['total']==1 and d['new']['latest']['payload_error']=='INVALID_JSON_OBJECT'
    assert c.total_changes==before

@pytest.mark.parametrize('obs,expected',[
([], 'EXPIRED_NO_OBSERVED_EVENT'),
([dict(kind='event',code='ACCEPTED',n=2),dict(kind='eligibility',code='DELAY_NOT_ELAPSED',n=2)],'EXPIRED_ONLY_BEFORE_250MS'),
([dict(kind='event',code='PROVIDER_STALE',n=3)],'EXPIRED_ALL_EVENTS_REJECTED'),
([dict(kind='event',code='ACCEPTED',n=1),dict(kind='eligibility',code='ELIGIBLE',n=1)],'EXPIRED_ELIGIBLE_EVENT_SEEN_REVIEW')])
def test_expiry_reason(obs,expected):
    assert ep.outcome(dict(status='EXPIRED'),obs)==expected

def test_gate_preserves_first_and_latest_exact_fx_comparison():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.executescript(ep.SCHEMA)
    row=dict(status='READY',order_id=None,job_id='j',input_json=json.dumps(dict(signal_price='1',size_units='3',fx_rate='100.0',fx_ts=1)),created_ts=1,started_ts=2,finished_ts=3,valid_until=20)
    ep.record(c,row,1.1,3,4,'o',(False,'PREFLIGHT_FX_CHANGED'),dict(rate='100.00',ts=4),.05)
    ep.record(c,row,1.2,3,5,'o',(False,'PREFLIGHT_FX_CHANGED'),dict(rate='101',ts=5),.05)
    r=dict(c.execute('SELECT * FROM entry_gate_audit_v47').fetchone())
    first=json.loads(r['first_json']);last=json.loads(r['last_json'])
    assert r['n']==2 and r['stage']=='FILL_RECHECK'
    assert first['fx_numeric_equal'] and not first['fx_string_equal']
    assert first['price_change_bps']==pytest.approx(1000)
    assert last['fx_change_bps']==pytest.approx(100)
    assert last['approval_remaining_ms']==15000
