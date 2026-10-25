"""Diagnostics only: exact observed preflight comparisons and complete BUY outcomes."""
import json,math
from decimal import Decimal,InvalidOperation
from collections import Counter
SCHEMA='''CREATE TABLE IF NOT EXISTS entry_gate_audit_v47(
 job_id TEXT,order_id TEXT,stage TEXT,code TEXT,n INTEGER,first_ts REAL,last_ts REAL,first_json TEXT,last_json TEXT,
 PRIMARY KEY(job_id,order_id,stage,code));'''

def dec(x):
    try:
        d=Decimal(str(x));return d if d.is_finite() else None
    except (ValueError,InvalidOperation,TypeError):return None

def change(current,original):
    a,b=dec(current),dec(original)
    if a is None or b is None or b==0:return None
    f=float((a/b-1)*10000)
    return f if math.isfinite(f) else None

def diff_ms(a,b):
    if isinstance(a,(int,float)) and isinstance(b,(int,float)) and math.isfinite(a) and math.isfinite(b):return (a-b)*1000
    return None

def record(c,row,price,size,now,order_id,decision,fx,max_move):
    if row is None or row['status'] not in ('READY','USED'):return
    if row['status']=='USED' and row['order_id']!=order_id:return
    inp=json.loads(row['input_json']);fx=fx or {};old=inp.get('fx_rate');new=fx.get('rate')
    same_numeric=dec(old)==dec(new) if dec(old) is not None and dec(new) is not None else None
    payload=dict(observed_ts=now,approval_status=row['status'],approved_price=inp.get('signal_price'),current_price=str(price),
      price_change_bps=change(price,inp.get('signal_price')),price_limit_bps=max_move*10000,
      approved_size=inp.get('size_units'),current_size=str(size),approved_fx=str(old),current_fx=str(new),
      fx_change_bps=change(new,old),fx_string_equal=str(old)==str(new),fx_numeric_equal=same_numeric,
      fx_comparison='EXACT_STRING_EQUALITY_UNCHANGED',approved_fx_ts=inp.get('fx_ts'),current_fx_ts=fx.get('ts'),
      current_fx_age_ms=diff_ms(now,fx.get('ts')),preflight_created_ts=row['created_ts'],preflight_started_ts=row['started_ts'],
      preflight_finished_ts=row['finished_ts'],valid_until=row['valid_until'],approval_remaining_ms=diff_ms(row['valid_until'],now),
      preflight_queue_ms=diff_ms(row['started_ts'],row['created_ts']),preflight_work_ms=diff_ms(row['finished_ts'],row['started_ts']),
      since_result_ms=diff_ms(now,row['finished_ts']),passed=bool(decision[0]),code=decision[1],
      note='Observed values at the unchanged gate, not hypothetical eligibility under relaxed rules.')
    raw=json.dumps(payload,ensure_ascii=False,allow_nan=False);stage='FILL_RECHECK' if order_id else 'PRE_ORDER'
    c.execute('''INSERT INTO entry_gate_audit_v47 VALUES(?,?,?,?,1,?,?,?,?) ON CONFLICT(job_id,order_id,stage,code)
      DO UPDATE SET n=n+1,last_ts=excluded.last_ts,last_json=excluded.last_json''',
      (row['job_id'],order_id or '',stage,decision[1],now,now,raw,raw))

def outcome(order,observations):
    status=order['status']
    if status=='FILLED':return 'BOUGHT'
    if status=='REJECTED':return order.get('note') or 'REJECTED_UNSPECIFIED'
    if status!='EXPIRED':return status
    events={r['code']:r['n'] for r in observations if r['kind']=='event'}
    eligibility={r['code']:r['n'] for r in observations if r['kind']=='eligibility'}
    total=sum(events.values());accepted=events.get('ACCEPTED',0)
    if not total:return 'EXPIRED_NO_OBSERVED_EVENT'
    if not accepted:return 'EXPIRED_ALL_EVENTS_REJECTED'
    if eligibility.get('DELAY_NOT_ELAPSED',0)==accepted and total==accepted:return 'EXPIRED_ONLY_BEFORE_250MS'
    if eligibility.get('ELIGIBLE',0):return 'EXPIRED_ELIGIBLE_EVENT_SEEN_REVIEW'
    return 'EXPIRED_NO_ELIGIBLE_EVENT_MIXED_REASONS'

def summary(c,limit=100):
    has=c.execute("SELECT 1 FROM sqlite_master WHERE name='entry_gate_audit_v47'").fetchone() is not None
    obs={}
    for r in c.execute('SELECT order_id,kind,code,n,first_ts,last_ts FROM order_audit_v0739'):
        obs.setdefault(r['order_id'],[]).append(dict(r))
    pre={r['order_id']:dict(r) for r in c.execute('SELECT * FROM preentry_v0729 WHERE order_id IS NOT NULL')}
    gates={}
    if has:
        for r in c.execute('SELECT * FROM entry_gate_audit_v47'):
            d=dict(r);d['first']=json.loads(d.pop('first_json'));d['last']=json.loads(d.pop('last_json'));gates.setdefault(d['job_id'],[]).append(d)
    rows=[];counts=Counter();statuses=Counter()
    for r in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='BUY' ORDER BY rowid DESC"):
        o=dict(r);oid=o['order_id'];observations=obs.get(oid,[]);code=outcome(o,observations);counts[code]+=1;statuses[o['status']]+=1
        p=pre.get(oid) or {};g=[x for x in gates.get(p.get('job_id'),[]) if x['order_id'] in ('',oid)]
        end=o.get('fill_ts')
        if end is None:
            endings=[x['last_ts'] for x in observations if x['kind']=='lifecycle' and x['code'] in ('EXPIRED','REJECTED','CANCELLED_RESTART','CANCELLED_STOP')]
            end=max(endings) if endings else None
        rows.append(dict(order_id=oid,mint=o['mint'],status=o['status'],reason=code,signal_ts=o['signal_ts'],finished_ts=end,
          elapsed_ms=diff_ms(end,o['signal_ts']),preflight_job_id=p.get('job_id'),
          preflight_finished_ts=p.get('finished_ts'),valid_until=p.get('valid_until'),
          approval_remaining_at_order_ms=diff_ms(p.get('valid_until'),o['signal_ts']),
          result_to_order_ms=diff_ms(o['signal_ts'],p.get('finished_ts')),observations=observations,gate_details=g,
          detail_state='RECORDED' if g else 'NOT_RECORDED_LEGACY_OR_GATE_NOT_REACHED'))
    import entry_diagnostics
    expired={}
    for row in c.execute("SELECT job_id FROM preentry_v0729 WHERE status='APPROVAL_EXPIRED'"):
        saved=c.execute('SELECT reason FROM approval_expiry_v0738 WHERE job_id=?',(row[0],)).fetchone()
        reason=saved[0] if saved else entry_diagnostics.explanation(c,row[0])['reason']
        expired[reason]=expired.get(reason,0)+1
    pc=dict(c.execute('SELECT status,COUNT(*) FROM preentry_v0729 GROUP BY status'))
    return dict(total_orders=len(rows),status_counts=dict(statuses),outcome_counts=dict(counts),preflight_counts=pc,
      rows=rows if limit<0 else rows[:limit],limit=limit,detail_schema_present=has,approval_expiry_counts=expired,
      note='Order counts and preflight job counts are different denominators. USED means assigned to an order, not bought. Event counts are recorded observations only, not proof of market inactivity. First/last gate samples and occurrence counts are retained; old runs have no invented FX/current-price values. Whole database, not just current run.')
