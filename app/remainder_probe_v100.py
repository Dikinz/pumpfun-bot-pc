"""Bounded ROUTE remainder reads. No execution, balance or slot authority."""
import json,math,time
SCHEMA='''CREATE TABLE IF NOT EXISTS remainder_probe_v100(
 trade_id TEXT PRIMARY KEY,signal_ts REAL,identity TEXT,context_json TEXT,
 negatives INTEGER,last_dispatch_ts REAL,next_due_ts REAL,last_event_ts REAL,last_status TEXT);'''
INTERVALS=(10.,30.,60.,300.)
EVENT_MIN_SECONDS=2.
GLOBAL_MIN_SECONDS=2.

def interval(n):return INTERVALS[min(max(0,int(n)),len(INTERVALS)-1)]

def pending_signal(c,tid,now):
    row=c.execute("SELECT signal_ts FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL' AND status='PENDING' ORDER BY signal_ts LIMIT 1",(tid,)).fetchone()
    if not row or type(row[0]) not in (int,float) or not math.isfinite(row[0]):return None
    return row[0] if now>=row[0]+__import__('route_remainder_v96').EXIT_WAIT_DEADLINE_S else None

def enabled(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='remainder_probe_v100'").fetchone():return False
    try:row=c.execute("SELECT v FROM meta WHERE k='branch_role'").fetchone()
    except __import__('sqlite3').OperationalError:return False
    return bool(row and row[0]=='ROUTE_PAPER')

def due(c,tid,now=None):
    now=time.time() if now is None else now
    if not enabled(c):return None
    state=c.execute('SELECT * FROM remainder_probe_v100 WHERE trade_id=?',(tid,)).fetchone()
    if not state:return None
    sig=pending_signal(c,tid,now)
    if sig is None or sig!=state['signal_ts']:return None
    import reserve_hint_v98 as hints,recovery_gate,wallet_costs
    job=json.loads(state['context_json']);inventory=c.execute('SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(tid,)).fetchone()
    job['inventory']=dict(inventory) if inventory else None;job['wallet_state']=wallet_costs.state(c);recovery_gate.stamp(job)
    if hints.identity(job)!=state['identity']:return None
    p=c.execute('SELECT last_update_ts FROM positions WHERE trade_id=?',(tid,)).fetchone()
    if not p:return None
    value=state['next_due_ts'];event=p[0]
    if type(event) in (int,float) and math.isfinite(event) and state['last_event_ts']<event<=now:
        value=min(value,max(event,state['last_dispatch_ts']+EVENT_MIN_SECONDS))
    last=c.execute('SELECT MAX(r.last_dispatch_ts) FROM remainder_probe_v100 r JOIN positions p ON p.trade_id=r.trade_id').fetchone()[0]
    return max(value,(last or 0)+GLOBAL_MIN_SECONDS)

def dispatched(c,job,now):
    if not enabled(c):return
    sig=pending_signal(c,job['trade_id'],now)
    identity=__import__('reserve_hint_v98').identity(job)
    if sig is None or identity is None:return
    event=c.execute('SELECT last_update_ts FROM positions WHERE trade_id=?',(job['trade_id'],)).fetchone()
    job.setdefault('remainder_event_ts_v100',event[0] if event else now)
    if not job.get('reserve_probe_v96'):return
    old=c.execute('SELECT * FROM remainder_probe_v100 WHERE trade_id=?',(job['trade_id'],)).fetchone()
    n=old['negatives'] if old and old['identity']==identity and old['signal_ts']==sig else 0
    p=c.execute('SELECT last_update_ts FROM positions WHERE trade_id=?',(job['trade_id'],)).fetchone()
    context={k:job[k] for k in ('trade_id','mint','route','opened_ts','inventory','wallet_state','recovery_epoch_v75','recovery_session_v77','recovery_authorized_v75') if k in job}
    c.execute('INSERT OR REPLACE INTO remainder_probe_v100 VALUES(?,?,?,?,?,?,?,?,?)',(job['trade_id'],sig,identity,json.dumps(context),n,now,now+interval(n),job['remainder_event_ts_v100'],'DISPATCHED'))
    job['remainder_probe_policy_v100']=dict(negatives=n,interval_s=interval(n),event_min_s=EVENT_MIN_SECONDS,global_min_s=GLOBAL_MIN_SECONDS)

def completed(c,job,result,status,now):
    if not enabled(c):return
    tid=job['trade_id'];old=c.execute('SELECT * FROM remainder_probe_v100 WHERE trade_id=?',(tid,)).fetchone()
    if status=='INSUFFICIENT_RESERVES':
        import reserve_hint_v98 as hints
        row=c.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone();sig=pending_signal(c,tid,now);signature=hints.identity(job)
        if sig is None or signature is None or not hints.source_valid(row,job,result,signature):return
        # Full fresh failures can create the state too; first-check priority is
        # unchanged. A later read must still revalidate the canonical hint.
        if old is None or old['identity']!=signature or old['signal_ts']!=sig:
            candidate=dict(job,reserve_probe_v96=True);dispatched(c,candidate,job['scheduled_ts'] if job['scheduled_ts']>=sig+120 else now)
        old=c.execute('SELECT * FROM remainder_probe_v100 WHERE trade_id=?',(tid,)).fetchone()
        if old:
            n=old['negatives']+1;c.execute('UPDATE remainder_probe_v100 SET negatives=?,next_due_ts=?,last_status=? WHERE trade_id=?',(n,now+interval(n),status,tid))
    elif old:
        network=result.get('network_error') or {}
        preserve=status in hints_no_result() or (status=='DATA_REJECTED' and network.get('category')=='LOCAL_RPC_ADMISSION')
        if not preserve:c.execute('DELETE FROM remainder_probe_v100 WHERE trade_id=?',(tid,))
    c.execute('DELETE FROM remainder_probe_v100 WHERE trade_id NOT IN (SELECT trade_id FROM positions)')

def hints_no_result():return __import__('reserve_hint_v98').NO_RESULT

def summary(c):
    if not enabled(c):return dict(enabled=False,rows=[])
    return dict(enabled=True,intervals_s=INTERVALS,event_min_s=EVENT_MIN_SECONDS,global_min_s=GLOBAL_MIN_SECONDS,
        rows=[{k:r[k] for k in ('trade_id','signal_ts','negatives','last_dispatch_ts','next_due_ts','last_event_ts','last_status')} for r in c.execute('SELECT * FROM remainder_probe_v100 r WHERE EXISTS(SELECT 1 FROM positions p WHERE p.trade_id=r.trade_id)')],
        note='Scheduler only; existing technical backoff, recovery, single-check ownership and common RPC limit still apply.')
