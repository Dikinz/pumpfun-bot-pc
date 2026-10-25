"""One durable fresh-check request per SELL; no fill or price fallback."""
import time
SCHEMA='''CREATE TABLE IF NOT EXISTS priority_exit_requests_v65(
 order_id TEXT PRIMARY KEY,trade_id TEXT,mint TEXT,signal_ts REAL,detected_ts REAL,
 dispatched_ts REAL,worker_started_ts REAL,finished_ts REAL,check_id TEXT,status TEXT,
 blocker TEXT,preempted_check_id TEXT);
 CREATE INDEX IF NOT EXISTS priority_exit_waiting_v65 ON priority_exit_requests_v65(status,signal_ts);'''

def observe(c,now):
    c.execute('''INSERT OR IGNORE INTO priority_exit_requests_v65(order_id,trade_id,mint,signal_ts,detected_ts,status)
      SELECT o.order_id,o.trade_id,o.mint,o.signal_ts,?,'QUEUED' FROM paper_orders_v0720 o
      JOIN positions p ON p.trade_id=o.trade_id WHERE o.side='SELL' AND o.status='PENDING' ''',(now,))
    c.execute('''UPDATE priority_exit_requests_v65 SET status='CLOSED_BEFORE_DISPATCH',blocker=NULL
      WHERE status='QUEUED' AND NOT EXISTS (SELECT 1 FROM paper_orders_v0720 o JOIN positions p ON p.trade_id=o.trade_id
      WHERE o.order_id=priority_exit_requests_v65.order_id AND o.status='PENDING')''')

def waiting(c):
    return c.execute("SELECT * FROM priority_exit_requests_v65 WHERE status='QUEUED' ORDER BY signal_ts,order_id").fetchall()

def preempt(c,active):
    # Cleanup is awaited in the existing loop before a replacement is dispatched.
    import connection_audit
    now=time.time()
    if now<__import__('fresh_retry').cooldown(c):return
    for req in waiting(c):
        if now<(connection_audit.retry(c,req['trade_id'])['not_before_ts'] or 0):continue
        candidates=[(task,job) for task,job in active.values() if not task.done() and not job.get('priority_exit') and not job.get('cancel_status')]
        same=[x for x in candidates if x[1]['trade_id']==req['trade_id']]
        choice=same[0] if same else (candidates[-1] if len(active)>=2 and candidates else None)
        if choice:
            task,job=choice;job['cancel_status']='SUPERSEDED_BY_EXIT_SIGNAL';task.cancel()
            c.execute('UPDATE priority_exit_requests_v65 SET preempted_check_id=?,blocker=? WHERE order_id=?',(job['id'],'PREEMPTING_BACKGROUND_CHECK',req['order_id']))

def dispatch(c,now):
    import position_monitor as pm,connection_audit
    for req in waiting(c):
        blocker=None
        if now<req['signal_ts']:blocker='SIGNAL_IN_FUTURE'
        elif now<__import__('fresh_retry').cooldown(c):blocker='RPC_RETRY_AFTER'
        elif now<(connection_audit.retry(c,req['trade_id'])['not_before_ts'] or 0):blocker='TECHNICAL_BACKOFF'
        elif c.execute("SELECT 1 FROM position_checks_v0730 WHERE trade_id=? AND status='RUNNING'",(req['trade_id'],)).fetchone():blocker='CHECK_ALREADY_RUNNING'
        if blocker:
            c.execute('UPDATE priority_exit_requests_v65 SET blocker=? WHERE order_id=?',(blocker,req['order_id']));continue
        job=pm.next_job(c,now,priority=req)
        if job:
            c.execute("UPDATE priority_exit_requests_v65 SET status='DISPATCHED',dispatched_ts=?,check_id=?,blocker=NULL WHERE order_id=?",(now,job['id'],req['order_id']))
            return job
    return __import__('fresh_retry').dispatch(c,now)

def worker_started(c,job,now):
    if job.get('priority_exit'):c.execute('UPDATE priority_exit_requests_v65 SET worker_started_ts=? WHERE check_id=?',(now,job['id']))

def finished(c,job,status,now):
    if job.get('priority_exit'):c.execute('UPDATE priority_exit_requests_v65 SET finished_ts=?,status=? WHERE check_id=?',(now,status,job['id']))

def recover(c):
    __import__('fresh_retry').terminal(c,'INTERRUPTED_RESTART')
    c.execute("UPDATE priority_exit_requests_v65 SET status='INTERRUPTED_RESTART' WHERE status='DISPATCHED'")

def stop(c,now,status='INTERRUPTED_STOP'):
    __import__('fresh_retry').terminal(c,status)
    c.execute("UPDATE priority_exit_requests_v65 SET status=?,finished_ts=? WHERE status IN ('QUEUED','DISPATCHED')",(status,now))

def summary(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='priority_exit_requests_v65'").fetchone():return dict(status='NOT_RECORDED',rows=[])
    rows=[]
    for r in c.execute('SELECT * FROM priority_exit_requests_v65 ORDER BY signal_ts'):
        x=dict(r)
        for name,a,b in [('signal_to_detection_ms','signal_ts','detected_ts'),('queue_wait_ms','detected_ts','dispatched_ts'),('dispatch_to_worker_ms','dispatched_ts','worker_started_ts'),('worker_ms','worker_started_ts','finished_ts'),('signal_to_result_ms','signal_ts','finished_ts')]:
            x[name]=(x[b]-x[a])*1000 if x[a] is not None and x[b] is not None and x[b]>=x[a] else None
        if x['status']=='QUEUED' and not x.get('blocker'):x['blocker']='WORKER_CAPACITY_OR_SCHEDULER_WAIT'
        rows.append(x)
    return dict(retry_v69=__import__('fresh_retry').summary(c),status='RECORDED',rows=rows,max_simultaneous_workers=2,
      note='One initial priority check per SELL plus the bounded v69 retry chain. Retry-After and technical backoff retained. Pre-signal results stay ineligible. Local queue times, no live execution claim.')
