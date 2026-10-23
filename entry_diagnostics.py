"""Observation only. Counts processing-time events; never authorizes an order."""
import time,math,json
import time_order_audit as ta
import execution_model as ex
import cost_evidence as ce
SCHEMA="""
CREATE TABLE IF NOT EXISTS entry_event_counts_v0738(code TEXT PRIMARY KEY,n INTEGER NOT NULL,last_ts REAL);
CREATE TABLE IF NOT EXISTS approval_counts_v0738(job_id TEXT,kind TEXT,code TEXT,n INTEGER NOT NULL,last_ts REAL,PRIMARY KEY(job_id,kind,code));
CREATE TABLE IF NOT EXISTS approval_expiry_v0738(job_id TEXT PRIMARY KEY,recorded_ts REAL,reason TEXT,details_json TEXT);
"""
LABELS={
'ACCEPTED':'Принято', 'INVALID_PRICE':'Некорректная цена', 'MISSING_EVIDENCE':'Нет метаданных',
**ta.LABELS,'INVALID_TIMESTAMPS':'Некорректные отметки времени','UNSUPPORTED_SOURCE':'Другой источник',
'OTHER_POOL':'Другой пул','OTHER_QUOTE':'Другая котируемая валюта','PRICE_BASIS':'Неподдерживаемая основа цены',
'MISSING_SIGNATURE':'Нет подписи сделки','CLOCK_NOT_OK':'Время не подтверждено',
'LOCAL_STALE':'Устарело при обработке','LOCAL_CLOCK_REVERSED':'Отрицательный локальный возраст',
'PROVIDER_STALE':'Задержка источника','PROVIDER_CLOCK_REVERSED':'Отрицательная задержка источника',
'FRESHNESS_OTHER':'Другая причина проверки свежести','OUT_OF_ORDER':'Нарушен порядок','DUPLICATE':'Дубликат',
'NO_OBSERVED_EVENT':'Не наблюдалось событий в окне разрешения',
'DATA_REJECTED':'Все наблюдавшиеся события отклонены',
'ENTRY_CONDITIONS_CHANGED':'Пригодные события были; условия входа не прошли',
'ELIGIBLE_WITHOUT_DECISION':'Пригодные события были, решение не записано — нужна проверка'}

def rejection(price,e,mono=None):
    mono=time.monotonic() if mono is None else mono
    if not isinstance(price,(int,float)) or not math.isfinite(price) or price<=0:return 'INVALID_PRICE'
    if not e:return 'MISSING_EVIDENCE'
    if e.get('timestamp_input_state')=='MISSING':return 'TIME_MISSING'
    if e.get('timestamp_input_state')=='INVALID':return 'TIME_INVALID'
    if e.get('clock_status')=='FUTURE':return 'TIME_FUTURE'
    if any(not isinstance(e.get(k),(int,float)) or not math.isfinite(e[k]) for k in ('received_mono','provider_lag_ms','provider_ts')):return 'INVALID_TIMESTAMPS'
    for ok,code in ((e.get('source')=='pumpapi_free','UNSUPPORTED_SOURCE'),(e.get('pool')=='pump','OTHER_POOL'),(e.get('quote_mint')==ce.SOL,'OTHER_QUOTE'),(e.get('price_basis')=='provider_price','PRICE_BASIS'),(bool(e.get('signature')),'MISSING_SIGNATURE'),(e.get('clock_status')=='OK','CLOCK_NOT_OK')):
        if not ok:return code
    age=mono-e['received_mono']
    if age<0:return 'LOCAL_CLOCK_REVERSED'
    if age>ex.MAX_PRICE_AGE:return 'LOCAL_STALE'
    if e['provider_lag_ms']<0:return 'PROVIDER_CLOCK_REVERSED'
    if e['provider_lag_ms']>ex.MAX_PRICE_AGE*1000:return 'PROVIDER_STALE'
    return 'FRESHNESS_OTHER'

def window(c,mint,now):
    return c.execute("SELECT job_id FROM preentry_v0729 WHERE mint=? AND status='READY' AND finished_ts<=? AND valid_until>=?",(mint,now,now)).fetchone()

def add(c,jid,kind,code,now):
    c.execute("INSERT INTO approval_counts_v0738 VALUES(?,?,?,1,?) ON CONFLICT(job_id,kind,code) DO UPDATE SET n=n+1,last_ts=excluded.last_ts",(jid,kind,code,now))

def event(c,mint,code,now=None,evidence=None):
    now=time.time() if now is None else now
    c.execute("INSERT INTO entry_event_counts_v0738 VALUES(?,1,?) ON CONFLICT(code) DO UPDATE SET n=n+1,last_ts=excluded.last_ts",(code,now))
    row=window(c,mint,now)
    if row:add(c,row[0],'event',code,now)
    ta.event(c,mint,code,evidence,now)

def decision(c,mint,code,now=None):
    now=time.time() if now is None else now
    row=window(c,mint,now)
    if row:add(c,row[0],'decision',code,now)

def explanation(c,jid):
    rows=[dict(r) for r in c.execute('SELECT kind,code,n,last_ts FROM approval_counts_v0738 WHERE job_id=?',(jid,))]
    events={r['code']:r['n'] for r in rows if r['kind']=='event'}
    decisions={r['code']:r['n'] for r in rows if r['kind']=='decision'}
    reason=('NO_OBSERVED_EVENT' if not events else 'DATA_REJECTED' if not events.get('ACCEPTED') else 'ENTRY_CONDITIONS_CHANGED' if decisions else 'ELIGIBLE_WITHOUT_DECISION')
    return dict(reason=reason,label=LABELS[reason],events=events,decisions=decisions,observations=rows)

def expire(c,now):
    for r in c.execute("SELECT job_id FROM preentry_v0729 WHERE status='READY' AND valid_until<?",(now,)).fetchall():
        d=explanation(c,r[0]);c.execute('INSERT OR IGNORE INTO approval_expiry_v0738 VALUES(?,?,?,?)',(r[0],now,d['reason'],json.dumps(d)))
    c.execute("UPDATE preentry_v0729 SET status='APPROVAL_EXPIRED' WHERE status='READY' AND valid_until<?",(now,))

def summary(c,now=None,limit=100):
    now=time.time() if now is None else now
    counts=dict(c.execute('SELECT code,n FROM entry_event_counts_v0738'))
    rows=[]
    for r in c.execute("SELECT job_id,mint,finished_ts,valid_until,status FROM preentry_v0729 WHERE status='APPROVAL_EXPIRED' OR (status='READY' AND valid_until<?) ORDER BY valid_until DESC LIMIT ?",(now,limit)):
        row=dict(r);saved=c.execute('SELECT details_json FROM approval_expiry_v0738 WHERE job_id=?',(r['job_id'],)).fetchone()
        row.update(json.loads(saved[0]) if saved else explanation(c,r['job_id']));rows.append(row)
    return dict(counts=counts,accepted=counts.get('ACCEPTED',0),rejected=sum(v for k,v in counts.items() if k!='ACCEPTED'),expired=rows,labels=LABELS,
      note='Only tracked-mint events reaching process_trade. One primary reason per event; processing-time approval window. No observed event does not prove no on-chain trades. Last 100 expiries in dashboard; all in preentry export.')

def feed(c,now):
    import config
    m=dict(c.execute('SELECT k,v FROM meta'))
    last=float(m.get('trade_last_market_ts',0));hb=float(m.get('heartbeat_ts',0))
    state=m.get('trade_watchdog_state','STARTING')
    if m.get('trade_stream_state')=='STOPPED' or m.get('stream_state')=='STOPPED':state='STOPPED'
    elif not hb or time.time()-hb>15:state='UNKNOWN_HEARTBEAT'
    elif not last or now-last>=config.PUMPAPI_STALL_SECONDS:state='NO_RECENT_MARKET_DATA'
    return dict(state=state,last_market_ts=last or None,market_age_s=max(0,now-last) if last else None,heartbeat_ts=hb or None,
                messages=int(float(m.get('pumpapi_messages',0))),reconnects=c.execute('SELECT COUNT(*) FROM reconnect_events').fetchone()[0])
