"""Bounded fresh simulation retries, never transaction resends or PAPER fills."""
import json,time,math,sqlite3
MAX_RETRIES=2
WINDOW=60.
SCHEMA='''CREATE TABLE IF NOT EXISTS exit_retry_v69(
 order_id TEXT PRIMARY KEY,trade_id TEXT,mint TEXT,signal_ts REAL,root_check_id TEXT,
 first_error_ts REAL,expires_ts REAL,not_before_ts REAL,retries INTEGER,status TEXT,blocker TEXT);
 CREATE TABLE IF NOT EXISTS exit_retry_attempts_v69(
 check_id TEXT PRIMARY KEY,order_id TEXT,parent_check_id TEXT,attempt INTEGER,
 scheduled_ts REAL,finished_ts REAL,status TEXT,decision TEXT,details_json TEXT);
 CREATE TABLE IF NOT EXISTS monitor_rpc_pause_v69(id INTEGER PRIMARY KEY,not_before_ts REAL,reason TEXT);
'''

def details(result):
    network=result.get('network_error') or {}
    requests=(result.get('rpc_timing') or {}).get('requests') or []
    last=next((x for x in reversed(requests) if x.get('status') in ('ERROR','RPC_ERROR')), {})
    if not network:network=last.get('error') or {}
    chain=network.get('chain') or []
    http=next((x.get('http_status') for x in chain if isinstance(x,dict) and type(x.get('http_status')) is int),last.get('http_status'))
    delay=max([float(x['retry_after_s']) for x in chain if isinstance(x,dict) and type(x.get('retry_after_s')) in (int,float) and math.isfinite(x['retry_after_s']) and x['retry_after_s']>=0] or [0.])
    return dict(category=network.get('category'),http_status=http,retry_after_s=delay,error_type=result.get('error_code'),
      error_stage=result.get('error_stage'),rpc_stage=last.get('stage'),rpc_method=last.get('method'))

def policy(status,d):
    if d.get('category')=='LOCAL_RPC_ADMISSION' and status in ('DATA_REJECTED','AMM_DATA_REJECTED','REQUEST_ERROR','AMM_REQUEST_ERROR'):
        return True,'LOCAL_RPC_ADMISSION',d['retry_after_s']
    if status not in ('REQUEST_ERROR','AMM_REQUEST_ERROR','REQUEST_TIMEOUT','AMM_REQUEST_TIMEOUT'):return False,'NOT_TRANSPORT_FAILURE',0.
    if d['http_status']==429:return True,'HTTP_RATE_LIMIT',max(1.,d['retry_after_s'] or 5.)
    if d['http_status'] in (500,502,503,504):return True,'HTTP_TEMPORARY',d['retry_after_s']
    if d['http_status'] is not None:return False,'HTTP_NONRETRYABLE',0.
    if status in ('REQUEST_TIMEOUT','AMM_REQUEST_TIMEOUT') or d['category'] in ('TIMEOUT','CONNECTION','DNS','LOCAL_RPC_ADMISSION'):return True,'TRANSPORT_TEMPORARY',d['retry_after_s']
    return False,'UNKNOWN_OR_NONRETRYABLE',0.

def cooldown(c):
    try:r=c.execute('SELECT not_before_ts FROM monitor_rpc_pause_v69 WHERE id=1').fetchone()
    except sqlite3.OperationalError as exc:
        if str(exc).startswith('no such table:'):return 0. # Legacy monitor reports.
        raise
    return r[0] if r else 0.

def completed(c,job,result,status,now):
    import connection_audit
    d=details(result);retryable,reason,delay=policy(status,d)
    # Apply Retry-After to all subsequent monitor checks, including background jobs.
    if delay>0 and d.get('category')!='LOCAL_RPC_ADMISSION':
        c.execute('INSERT INTO monitor_rpc_pause_v69 VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET not_before_ts=MAX(not_before_ts,excluded.not_before_ts),reason=excluded.reason',(now+delay,reason))
    meta=job.get('retry_v69')
    root=c.execute('SELECT * FROM priority_exit_requests_v65 WHERE check_id=?',(job['id'],)).fetchone() if not meta else None
    if not meta and (not root or not retryable and status not in ('REQUEST_ERROR','AMM_REQUEST_ERROR','REQUEST_TIMEOUT','AMM_REQUEST_TIMEOUT')):return
    if meta:
        chain=c.execute('SELECT * FROM exit_retry_v69 WHERE order_id=?',(meta['order_id'],)).fetchone()
        if not chain:return
        order_id=chain['order_id'];attempt=meta['attempt'];parent=meta['parent_check_id']
    else:
        order_id=root['order_id'];attempt=0;parent=None
        c.execute('INSERT OR IGNORE INTO exit_retry_v69 VALUES(?,?,?,?,?,?,?,?,0,?,NULL)',(order_id,root['trade_id'],root['mint'],root['signal_ts'],job['id'],now,now+WINDOW,now,'WAITING'))
        chain=c.execute('SELECT * FROM exit_retry_v69 WHERE order_id=?',(order_id,)).fetchone()
    existing=c.execute('SELECT status FROM exit_retry_attempts_v69 WHERE check_id=?',(job['id'],)).fetchone()
    if existing and existing['status']!='RUNNING':return
    if existing:c.execute('UPDATE exit_retry_attempts_v69 SET finished_ts=?,status=?,decision=?,details_json=? WHERE check_id=?',(now,status,reason,json.dumps(d),job['id']))
    else:c.execute('INSERT INTO exit_retry_attempts_v69 VALUES(?,?,?,?,?,?,?,?,?)',(job['id'],order_id,parent,attempt,job.get('scheduled_ts'),now,status,reason,json.dumps(d)))
    if not retryable:
        c.execute('UPDATE exit_retry_v69 SET status=?,blocker=? WHERE order_id=?',(status if status.startswith('INTERRUPTED') or status=='CLEANUP_FAILED' else 'RESULT_RETURNED' if status not in ('REQUEST_ERROR','AMM_REQUEST_ERROR','REQUEST_TIMEOUT','AMM_REQUEST_TIMEOUT') else 'NONRETRYABLE',reason,order_id));return
    if attempt>=MAX_RETRIES:
        c.execute("UPDATE exit_retry_v69 SET status='EXHAUSTED',blocker='ATTEMPT_LIMIT' WHERE order_id=?",(order_id,));return
    backoff=connection_audit.retry(c,chain['trade_id'])['not_before_ts'] or 0.
    due=max(now+(.25 if attempt==0 else 1.),now+delay,cooldown(c),backoff)
    state='WINDOW_EXHAUSTED' if due>=chain['expires_ts'] else 'WAITING'
    c.execute('UPDATE exit_retry_v69 SET status=?,not_before_ts=?,blocker=? WHERE order_id=?',(state,due,reason,order_id))

def blocks_regular(c,tid):
    return bool(c.execute("SELECT 1 FROM exit_retry_v69 WHERE trade_id=? AND status IN ('WAITING','RUNNING')",(tid,)).fetchone())

def waiting_due(c,tid):
    try:r=c.execute("SELECT not_before_ts FROM exit_retry_v69 WHERE trade_id=? AND status='WAITING'",(tid,)).fetchone()
    except sqlite3.OperationalError as exc:
        if str(exc).startswith('no such table:'):return None
        raise
    return r[0] if r else None

def dispatch(c,now):
    import position_monitor as pm,connection_audit
    for row in c.execute("SELECT * FROM exit_retry_v69 WHERE status='WAITING' ORDER BY signal_ts,order_id").fetchall():
        pending=c.execute("SELECT 1 FROM paper_orders_v0720 o JOIN positions p ON p.trade_id=o.trade_id WHERE o.order_id=? AND o.side='SELL' AND o.status='PENDING'",(row['order_id'],)).fetchone()
        terminal='CLOSED_BEFORE_RETRY' if not pending else 'WINDOW_EXHAUSTED' if now>=row['expires_ts'] or now<row['first_error_ts'] else 'EXHAUSTED' if row['retries']>=MAX_RETRIES else None
        if terminal:
            c.execute('UPDATE exit_retry_v69 SET status=? WHERE order_id=?',(terminal,row['order_id']));continue
        due=max(row['not_before_ts'],cooldown(c),connection_audit.retry(c,row['trade_id'])['not_before_ts'] or 0.)
        if due>=row['expires_ts']:
            c.execute("UPDATE exit_retry_v69 SET status='WINDOW_EXHAUSTED',blocker='COOLDOWN_EXCEEDS_WINDOW' WHERE order_id=?",(row['order_id'],));continue
        if now<due:
            c.execute('UPDATE exit_retry_v69 SET blocker=? WHERE order_id=?',('RETRY_NOT_DUE',row['order_id']));continue
        parent=c.execute('SELECT check_id FROM exit_retry_attempts_v69 WHERE order_id=? ORDER BY attempt DESC LIMIT 1',(row['order_id'],)).fetchone()
        priority=dict(trade_id=row['trade_id'],order_id=row['order_id'],signal_ts=row['signal_ts'],detected_ts=due)
        job=pm.next_job(c,now,priority=priority)
        if not job:
            c.execute("UPDATE exit_retry_v69 SET blocker='CHECK_OR_WORKER_UNAVAILABLE' WHERE order_id=?",(row['order_id'],));continue
        meta=dict(order_id=row['order_id'],root_check_id=row['root_check_id'],parent_check_id=parent[0],attempt=row['retries']+1,not_before_ts=due,expires_ts=row['expires_ts'])
        job.update(retry_v69=meta,deadline_timeout_s=min(pm.TIMEOUT,row['expires_ts']-now))
        c.execute('UPDATE position_checks_v0730 SET input_json=? WHERE id=?',(json.dumps(job),job['id']))
        c.execute('INSERT INTO exit_retry_attempts_v69 VALUES(?,?,?,?,?,NULL,?,?,?)',(job['id'],row['order_id'],parent[0],meta['attempt'],now,'RUNNING','DISPATCHED',json.dumps({})))
        c.execute("UPDATE exit_retry_v69 SET status='RUNNING',retries=retries+1,blocker=NULL WHERE order_id=?",(row['order_id'],))
        return job

def terminal(c,state):
    c.execute("UPDATE exit_retry_attempts_v69 SET status=?,decision=?,finished_ts=? WHERE status='RUNNING'",(state,state,time.time()))
    c.execute("UPDATE exit_retry_v69 SET status=?,blocker=? WHERE status IN ('WAITING','RUNNING')",(state,state))

def summary(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='exit_retry_v69'").fetchone():return dict(status='NOT_RECORDED')
    rows=[dict(r) for r in c.execute('SELECT * FROM exit_retry_v69 ORDER BY signal_ts')]
    attempts=[]
    for r in c.execute('SELECT * FROM exit_retry_attempts_v69 ORDER BY scheduled_ts'):
        d=dict(r);d['details']=json.loads(d.pop('details_json'));attempts.append(d)
    return dict(rows=rows,attempts=attempts,monitor_cooldown_until=cooldown(c),max_additional_attempts=MAX_RETRIES,window_s=WINDOW,
      scope='Bounded fresh-check retries; shared PAPER RPC admission remains mandatory. Monitor-local HTTP cooldown complements common endpoint cooldown. In-flight checks are not replayed or converted into fills.')
