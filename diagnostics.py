"""Read-only diagnostics and first-candidate observations; never portfolio trades."""
import json,time,sqlite3,argparse
from pathlib import Path
import config,entry_activity,execution_model as ex,paper_capital as cap

SCHEMA='''
CREATE TABLE IF NOT EXISTS exit_diagnostics_v0722(order_id TEXT PRIMARY KEY,raw_count INTEGER DEFAULT 0,rejected INTEGER DEFAULT 0,other_pool INTEGER DEFAULT 0,last_ts REAL,last_code TEXT,last_pool TEXT);
CREATE TABLE IF NOT EXISTS signal_observations_v0722(mint TEXT PRIMARY KEY,ts REAL,price REAL,score REAL,entry_result TEXT,features TEXT,last_ts REAL,last_price REAL,ticks INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS observation_marks_v0722(mint TEXT,horizon INTEGER,cutoff REAL,last_ts REAL,last_price REAL,ticks INTEGER,PRIMARY KEY(mint,horizon));
'''
HORIZONS=(30,120,300)

def observe(c,m,p,score,s,reason,now=None):
    now=time.time() if now is None else now
    f=entry_activity.features(s or {},now)
    c.execute('INSERT OR IGNORE INTO signal_observations_v0722 VALUES(?,?,?,?,?,?,?,?,0)',(m,now,p,score,reason,json.dumps(f),now,p))

def advance(c,now,m=None):
    query='SELECT * FROM signal_observations_v0722 WHERE mint=?' if m is not None else 'SELECT * FROM signal_observations_v0722 WHERE mint NOT IN (SELECT mint FROM observation_marks_v0722 WHERE horizon=300)'
    for r in c.execute(query,(m,) if m is not None else ()).fetchall():
        for h in HORIZONS:
            if now>=r['ts']+h:
                c.execute('INSERT OR IGNORE INTO observation_marks_v0722 VALUES(?,?,?,?,?,?)',(r['mint'],h,r['ts']+h,r['last_ts'],r['last_price'],r['ticks']))

def tick(c,m,p,now=None):
    now=time.time() if now is None else now
    # Freeze expired horizons BEFORE accepting a late price.
    advance(c,now,m)
    c.execute('UPDATE signal_observations_v0722 SET last_ts=?,last_price=?,ticks=ticks+1 WHERE mint=? AND ts+300>?',(now,p,m,now))

def evidence(c,m,code,pool='',now=None):
    order=ex.pending(c,m,'SELL')
    if not order:return
    now=time.time() if now is None else now
    c.execute('INSERT OR IGNORE INTO exit_diagnostics_v0722(order_id) VALUES(?)',(order['order_id'],))
    c.execute('UPDATE exit_diagnostics_v0722 SET raw_count=raw_count+1,rejected=rejected+?,other_pool=other_pool+?,last_ts=?,last_code=?,last_pool=? WHERE order_id=?',(int(code not in ('ACCEPTED','OTHER_POOL')),int(code=='OTHER_POOL'),now,code,pool,order['order_id']))

def summary(c,now=None):
    meta=dict(c.execute('SELECT k,v FROM meta'));heartbeat=float(meta.get('heartbeat_ts',0))
    now=time.time() if now is None else now
    import runtime_status,system_health
    runtime=runtime_status.summary(c,now)
    database_path=next((row[2] for row in c.execute('PRAGMA database_list') if row[1]=='main'),None)
    if database_path:system_health.apply_lifecycle(dict(runtime=runtime),database_path)
    # Runtime terminal state and a matching recorder stop take precedence over
    # a cancelled stream task's last state. Old recorder stops cannot match a new run.
    stopped=runtime['state']=='STOPPED' or (runtime['state']=='NOT_STARTED' and (meta.get('trade_stream_state')=='STOPPED' or meta.get('stream_state')=='STOPPED'))
    failed=runtime['state']=='FAILED'
    stale=not heartbeat or not 0<=now-heartbeat<=15
    at=heartbeat if (stopped or stale) and heartbeat else now
    market_age=max(0,at-float(meta.get('trade_last_market_ts',0)))
    connected=meta.get('trade_watchdog_state')=='RECEIVING' and market_age<config.PUMPAPI_STALL_SECONDS
    n,reserved=ex.reserve(c);opened=c.execute('SELECT COUNT(*) FROM positions').fetchone()[0]
    available=cap.cash(c)-reserved;blocks=[]
    if stopped:blocks.append('Бот остановлен')
    elif failed:blocks.append('Бот завершён с ошибкой')
    elif stale:blocks.append('Нет свежего heartbeat: состояние бота неизвестно')
    if not connected:blocks.append('Поток сделок не готов / нет свежих рыночных сообщений')
    if opened+n>=config.MAX_POSITIONS:blocks.append('Заняты все места для позиций')
    if available<cap.budget(c,config.POSITION_SIZE):blocks.append('Недостаточно капитала после расходов и резервов')
    rows=[]
    for o in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING'"):
        r=c.execute('SELECT * FROM exit_diagnostics_v0722 WHERE order_id=?',(o['order_id'],)).fetchone();d=dict(r) if r else {}
        flags=[]
        if not connected:flags.append('CONNECTION_UNAVAILABLE')
        if not d.get('raw_count'):flags.append('NO_TOKEN_EVENTS_OBSERVED')
        else:
            if d.get('other_pool'):flags.append('OTHER_POOL_OBSERVED')
            if d.get('rejected'):flags.append('REJECTED_DATA_OBSERVED')
            if at-d['last_ts']>ex.MAX_PRICE_AGE:flags.append('NO_RECENT_TOKEN_EVENTS')
            if not flags:flags.append('WAITING_ELIGIBLE_TICK')
        rows.append(dict(d,mint=o['mint'],reason=o['reason'],wait_s=max(0,at-o['signal_ts']),causes=flags))
    counts=[dict(r) for r in c.execute('SELECT entry_result,COUNT(*) n FROM signal_observations_v0722 GROUP BY entry_result')]
    return {'as_of':at,'frozen':bool(stopped or failed or stale),'runtime_state':runtime['state'],'stop_reason':runtime.get('stop_reason'),'entry_blocks':blocks,'entry_status':' / '.join(blocks) if blocks else 'Общих ограничений нет; ожидаем подходящий сигнал',
      'last_entry_result':meta.get('last_entry_result_v0722','Ещё нет сигналов'),'last_entry_ts':float(meta.get('last_entry_ts_v0722',0)),
      'available':available,'open':opened,'reserved_slots':n,'exits':rows,'observations':counts,
      'observation_total':sum(x['n'] for x in counts),'marks':c.execute('SELECT COUNT(*) FROM observation_marks_v0722').fetchone()[0],
      'note':'First scanner PASS candidate per mint, including gate rejects. Observations are not trades or hypothetical profit. Other-pool messages are feed evidence, not verified migration. No observed events is not proof of no on-chain trades. Stopped/stale reports freeze at heartbeat.'}

def check(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN');return summary(c)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=config.DB);ap.add_argument('--out',default='diagnostics_results.json');a=ap.parse_args()
    Path(a.out).write_text(json.dumps(check(a.db),ensure_ascii=False,indent=2),encoding='utf-8')
