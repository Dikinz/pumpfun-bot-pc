"""Diagnostic data only; never modifies eligibility, clocks, or trading thresholds."""
import math,time,json
SCHEMA="""
CREATE TABLE IF NOT EXISTS clock_minutes_v0739(minute INTEGER,bucket TEXT,n INTEGER,min_ms REAL,max_ms REAL,sum_ms REAL,last_json TEXT,PRIMARY KEY(minute,bucket));
CREATE TABLE IF NOT EXISTS order_audit_v0739(order_id TEXT,kind TEXT,code TEXT,n INTEGER,first_ts REAL,last_ts REAL,last_json TEXT,PRIMARY KEY(order_id,kind,code));
"""
LABELS={'TIME_MISSING':'Время отсутствует','TIME_INVALID':'Неверный формат времени','TIME_FUTURE':'Время из будущего',
'DELAY_NOT_ELAPSED':'Не прошла задержка 250 мс','BUY_WINDOW_PASSED':'Прошло окно покупки 2 с','ORIGIN_MISSING':'Нет локальной точки отсчёта',
'SAME_SIGNAL_TX':'Повтор исходной сделки','OLDER_THAN_SIGNAL':'Событие старше сигнала','ELIGIBLE':'Событие подходит для исполнения',
'FRESHNESS_REJECTED':'Повторная проверка свежести не пройдена','EXPIRED':'Истёк срок заявки','CANCELLED_RESTART':'Отменена при перезапуске',
'CANCELLED_STOP':'Отменена при остановке','FILLED':'Исполнена в PAPER','REJECTED':'Отклонена','CREATED':'Создана','PENDING':'Ожидает'}

def finite(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)

def sample(e,now=None):
    e=e or {};now=time.time() if now is None else now
    lag=e.get('provider_lag_ms');raw=e.get('timestamp_raw_ms');mono=e.get('received_mono')
    return dict(processed_ts=now,received_ts=e.get('received_ts') if finite(e.get('received_ts')) else None,
      provider_ts=e.get('provider_ts') if finite(e.get('provider_ts')) else None,
      timestamp_raw_ms=raw if finite(raw) else None,timestamp_input_state=e.get('timestamp_input_state','UNKNOWN'),
      lag_ms=lag if finite(lag) else None,clock_status=e.get('clock_status','MISSING'),
      local_age_ms=(time.monotonic()-mono)*1000 if finite(mono) else None)

def bucket(s):
    if s['timestamp_input_state']=='MISSING':return 'TIME_MISSING'
    if s['timestamp_input_state']=='INVALID' or s['lag_ms'] is None:return 'TIME_INVALID'
    lag=s['lag_ms']
    if lag<0:
        mag=-lag
        return 'FUTURE_0_10' if mag<=10 else 'FUTURE_10_100' if mag<=100 else 'FUTURE_100_1000' if mag<=1000 else 'FUTURE_OVER_1000'
    return 'LAG_0_1000' if lag<=1000 else 'LAG_OVER_1000'

def clock(c,e,now=None):
    s=sample(e,now);b=bucket(s);v=s['lag_ms'];minute=int(s['processed_ts']//60)
    # Diagnostics only. Eligibility still uses the original event synchronously.
    writer=__import__('counter_writer').ACTIVE
    if writer is not None:
        writer.add_clock(minute,b,v,json.dumps(s,allow_nan=False))
        return
    c.execute("""INSERT INTO clock_minutes_v0739 VALUES(?,?,1,?,?,?,?) ON CONFLICT(minute,bucket) DO UPDATE SET
      n=n+1,min_ms=MIN(min_ms,excluded.min_ms),max_ms=MAX(max_ms,excluded.max_ms),sum_ms=sum_ms+excluded.sum_ms,last_json=excluded.last_json""",
      (minute,b,v,v,v,json.dumps(s,allow_nan=False)))

def add(c,oid,kind,code,e=None,now=None):
    now=time.time() if now is None else now
    c.execute("""INSERT INTO order_audit_v0739 VALUES(?,?,?,1,?,?,?) ON CONFLICT(order_id,kind,code)
       DO UPDATE SET n=n+1,last_ts=excluded.last_ts,last_json=excluded.last_json""",(oid,kind,code,now,now,json.dumps(sample(e,now),allow_nan=False)))

def event(c,mint,code,e=None,now=None):
    for r in c.execute("SELECT order_id FROM paper_orders_v0720 WHERE mint=? AND status='PENDING'",(mint,)).fetchall():add(c,r[0],'event',code,e,now)

def eligibility_reason(order,e,mono=None):
    import execution_model as ex
    mono=time.monotonic() if mono is None else mono
    start=ex.ORIGINS.get(order['order_id'])
    if start is None:return 'ORIGIN_MISSING'
    if not ex.fresh(e,mono):return 'FRESHNESS_REJECTED'
    elapsed=e['received_mono']-start
    if elapsed<ex.DELAY:return 'DELAY_NOT_ELAPSED'
    if order['side']=='BUY' and elapsed>ex.BUY_TIMEOUT:return 'BUY_WINDOW_PASSED'
    if e['signature']==order['signal_signature']:return 'SAME_SIGNAL_TX'
    if order['signal_provider_ts'] is not None and e['provider_ts']<order['signal_provider_ts']:return 'OLDER_THAN_SIGNAL'
    return 'ELIGIBLE'

def summary(c,limit=100):
    hist=[dict(r) for r in c.execute('SELECT bucket,SUM(n) n,MIN(min_ms) min_ms,MAX(max_ms) max_ms,SUM(sum_ms)/SUM(n) mean_ms FROM clock_minutes_v0739 GROUP BY bucket')]
    recent=[]
    for r in c.execute('SELECT * FROM clock_minutes_v0739 ORDER BY minute DESC,bucket LIMIT ?',(-1 if limit<0 else 100,)):
        x=dict(r);x['sample']=json.loads(x.pop('last_json'));recent.append(x)
    rows=[]
    for r in c.execute('SELECT order_id,mint,side,status,note,signal_ts,fill_ts FROM paper_orders_v0720 ORDER BY signal_ts DESC LIMIT ?',(limit,)):
        x=dict(r);obs=[]
        for item in c.execute('SELECT * FROM order_audit_v0739 WHERE order_id=? ORDER BY first_ts',(r['order_id'],)):
            q=dict(item);q['sample']=json.loads(q.pop('last_json'));obs.append(q)
        x['observations']=obs;x['observed_events']=sum(q['n'] for q in obs if q['kind']=='event');rows.append(x)
    return dict(histogram=hist,recent=recent,orders=rows,labels=LABELS,
      note='lag_ms = local receive time minus provider timestamp. Negative means provider time is ahead; not proof which clock is wrong. No clock correction or relaxed limits. Aggregates count all tracked process_trade events; one last sample per minute/bucket. Order stages are separate and must not be summed as unique events.')
