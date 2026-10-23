import sqlite3,json
import monitor_history as mh
import evidence_report as er
def fixture():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.executescript("""
    CREATE TABLE paper_orders_v0720(trade_id,signal_ts,side);
    CREATE TABLE trades(trade_id,ts,side);
    CREATE TABLE position_checks_v0730(id,trade_id,mint,started_ts,finished_ts,status,result_json);
    INSERT INTO paper_orders_v0720 VALUES('t',10,'SELL');
    CREATE TABLE positions(trade_id,mint,size,current,last_update_ts);
    INSERT INTO positions VALUES('t','m',3,1,1);
    """)
    return c
def add(c,i,ts,status):
    c.execute('INSERT INTO position_checks_v0730 VALUES(?,?,?,?,?,?,?)',(i,'t','m',ts-1,ts,status,json.dumps(dict(sale=dict(status='LOCAL_SWAP_EXECUTED'),net_status='NET_BELOW_FLOOR'))))
def test_stop_and_error_keep_separate_result_and_sale():
    c=fixture()
    add(c,'1',9,'NET_BELOW_FLOOR');add(c,'2',11,'NET_BELOW_FLOOR')
    add(c,'3',12,'INSUFFICIENT_RESERVES');add(c,'4',13,'REQUEST_ERROR');add(c,'5',14,'INTERRUPTED_STOP')
    before=c.total_changes;g=mh.summary(c,20)['by_trade']['t']
    assert g['latest']['id']=='5'
    assert g['latest_result']['id']=='3' and g['latest_sale']['id']=='2'
    assert g['first_sale_buckets']['AFTER_SIGNAL_OPEN']['id']=='2'
    assert g['latest_sale']['age_s']==9
    assert g['status_by_timing']['AFTER_SIGNAL_OPEN']['REQUEST_ERROR']==1
    assert c.total_changes==before
def test_late_sale_not_used_as_timely():
    c=fixture();add(c,'1',9,'NET_BELOW_FLOOR');add(c,'2',13,'NET_BELOW_FLOOR')
    c.execute("INSERT INTO trades VALUES('t',12,'SELL')")
    g=mh.summary(c,20)['by_trade']['t']
    assert g['latest_sale']['timing']=='AFTER_CLOSE'
    assert 'AFTER_SIGNAL_BEFORE_CLOSE' not in g['sale_buckets']
    assert g['sale_buckets']['BEFORE_SIGNAL']['id']=='1'
def test_stopped_report_age_and_error_only_evidence():
    c=fixture();add(c,'1',12,'REQUEST_ERROR')
    m=dict(history=mh.summary(c,20));d=er.summary(c,m,20)
    assert d['stale_ticks']==1 and d['rows'][0]['tick_age_s']==19
    assert d['rows'][0]['last_sale'] is None and d['rows'][0]['last_result'] is None
    assert er.summary(c,m,20)==d
def test_missing_or_future_tick_is_unknown():
    c=fixture()
    for ts in (None,30):
        c.execute('UPDATE positions SET last_update_ts=?',(ts,))
        assert er.summary(c,{},20)['unknown_ticks']==1
def test_malformed_payload_and_unfinished_not_sale():
    c=fixture()
    c.execute("INSERT INTO position_checks_v0730 VALUES('1','t','m',1,2,'REQUEST_ERROR','[]')")
    c.execute("INSERT INTO position_checks_v0730 VALUES('2','t','m',3,NULL,'RUNNING','{}')")
    g=mh.summary(c,10)['by_trade']['t']
    assert g['latest']['payload_error']=='INVALID_JSON_OBJECT'
    assert g['latest_sale'] is None and g['timing_counts']['UNFINISHED']==1
