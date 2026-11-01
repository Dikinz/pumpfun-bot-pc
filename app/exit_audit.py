"""Bounded diagnostic evidence only; no orders, RPC or strategy decisions."""
import json,time,uuid,math
from collections import deque
SCHEMA="""CREATE TABLE IF NOT EXISTS exit_audit_v45(trade_id TEXT PRIMARY KEY, mint TEXT, status TEXT, flagged INTEGER, data_json TEXT);"""
LIMIT=200
LOSS_FRACTION=.20
GAP_PERCENT=-20.
PROCESS_ID=uuid.uuid4().hex
BUFFERS={}
TOTAL={}
LAST_CHECKPOINT=0.
ELIGIBILITY_COUNTS={}
KEYS=('source','pool','pool_address','quote','quote_mint','priority_fee_raw','metadata_status','price_basis','signature','provider_ts','received_ts','received_mono','provider_lag_ms','clock_status','pool_fee_rate')
def safe(e):return {k:v for k,v in (e or {}).items() if k in KEYS and isinstance(v,(str,int,float,bool,type(None)))}
def event(tid,side,price,source,evidence,status):
    if not tid:return
    q=BUFFERS.setdefault(tid,deque(maxlen=LIMIT));TOTAL[tid]=TOTAL.get(tid,0)+1
    wall=time.time();mono=time.monotonic()
    q.append(dict(wall_ts=wall,mono=mono,side=side,price=price,source=source,validation=status,evidence=safe(evidence)))
    if status=='ACCEPTED':__import__('price_path_v100').event(tid,wall,mono,price,source,(evidence or {}).get('price_basis'),PROCESS_ID)
def clean(x):
    if isinstance(x,float) and not math.isfinite(x):return str(x)
    if isinstance(x,dict):return {k:clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple,deque)):return [clean(v) for v in x]
    return x
def dump(x):return json.dumps(clean(x),ensure_ascii=False,default=str,allow_nan=False)
def signal(c,tid,mint,order_id,position,evidence):
    if c.execute('SELECT 1 FROM exit_audit_v45 WHERE trade_id=?',(tid,)).fetchone():return
    order=c.execute('SELECT * FROM paper_orders_v0720 WHERE order_id=?',(order_id,)).fetchone()
    data=dict(schema='exit_audit_v45',signal_wall=time.time(),signal_mono=time.monotonic(),process_id=PROCESS_ID,
      position=dict(position),order=dict(order) if order else None,signal_evidence=safe(evidence),
      preentry=dict(c.execute('SELECT * FROM preentry_v0729 WHERE mint=?',(mint,)).fetchone() or {}),pre_signal_events=list(BUFFERS.get(tid,[])),events_seen_at_signal=TOTAL.get(tid,0),
      limitations='Observed provider events and PAPER fill, not proof of on-chain execution. Buffers restart with the process; max 200 events per position. Network fees remain modeled.')
    c.execute('INSERT INTO exit_audit_v45 VALUES(?,?,?,?,?)',(tid,mint,'WAITING_FILL',0,dump(data)))
    __import__('price_path_v100').begin(tid,data['signal_wall'],data['signal_mono'],PROCESS_ID)
def close(c,tid,mint,position,evidence):
    row=c.execute('SELECT data_json FROM exit_audit_v45 WHERE trade_id=?',(tid,)).fetchone();data=json.loads(row[0]) if row else dict(schema='exit_audit_v45',signal_missing=True)
    trade=c.execute("SELECT * FROM trades WHERE trade_id=? AND side='SELL'",(tid,)).fetchone()
    fees=[dict(r) for r in c.execute('SELECT * FROM capital_ledger_v0721 WHERE trade_id=?',(tid,))]
    t=dict(trade);net=t['pnl']-sum(r['total_cost'] for r in fees) if len(fees)==2 else None
    flagged=(net is not None and net<=-LOSS_FRACTION*position['size']) or t['slippage_gap_pct']<=GAP_PERCENT
    events=list(BUFFERS.get(tid,[]));seen=TOTAL.get(tid,0)
    data['checkpoint']=checkpoint_data(c,tid,data)
    data['loss_attribution']=__import__('exit_timeline').attribution(dict(c.execute("SELECT * FROM trades WHERE trade_id=? AND side='BUY'",(tid,)).fetchone() or {}),t,data.get('order'),fees)
    data.update(cost_decisions=[dict(r) for r in c.execute('SELECT * FROM cost_decisions_v0719 WHERE trade_id=?',(tid,))],fill=t,position=dict(position),entry_fill=dict(c.execute("SELECT * FROM trades WHERE trade_id=? AND side='BUY'",(tid,)).fetchone() or {}),costs=fees,net_pnl=net,fill_evidence=safe(evidence),
      signal_to_fill_monotonic_ms=(time.monotonic()-data['signal_mono'])*1000 if data.get('signal_mono') is not None and data.get('process_id')==PROCESS_ID else None,
      diagnostic_rule=dict(net_loss_fraction=LOSS_FRACTION,adverse_signal_gap_percent=GAP_PERCENT,strategy_changed=False),
      events_seen=seen,events_dropped=max(0,seen-len(events)),events_before_fill=events if flagged else [],
      classification='LARGE_MODEL_LOSS_OR_ADVERSE_GAP' if flagged else 'OTHER_EXIT',
      note='Cause not inferred. No reserve or actual execution claim from a price jump. Entry and exit costs are PAPER-model costs.')
    c.execute('INSERT OR REPLACE INTO exit_audit_v45 VALUES(?,?,?,?,?)',(tid,mint,'CLOSED',int(flagged),dump(data)))
    BUFFERS.pop(tid,None);TOTAL.pop(tid,None);ELIGIBILITY_COUNTS.pop(tid,None);__import__('price_path_v100').PATHS.pop(tid,None)
def latency_basis(order):
    # A PAPER tick-proxy SELL fills on the next market trade: its latency includes market silence.
    return 'NEXT_MARKET_TRADE' if str((order or {}).get('note') or '').startswith('tick proxy') else None
def summary(c,limit=100):
    rows=[]
    for r in c.execute('SELECT trade_id,mint,status,flagged,data_json FROM exit_audit_v45 ORDER BY rowid DESC LIMIT ?', (limit,)):
        d=json.loads(r['data_json']);t=d.get('fill',{});rows.append(dict(trade_id=r['trade_id'],mint=r['mint'],status=r['status'],flagged=bool(r['flagged']),reason=t.get('reason'),net_pnl=d.get('net_pnl'),signal_price=t.get('stop_trigger_price'),fill_price=t.get('price'),gap_pct=t.get('slippage_gap_pct'),latency_ms=t.get('exit_latency_ms'),latency_basis=latency_basis(d.get('order')),events_seen=d.get('events_seen'),events_dropped=d.get('events_dropped')))
    return dict(rows=rows,flagged=c.execute('SELECT COUNT(*) FROM exit_audit_v45 WHERE flagged=1').fetchone()[0],diagnostic_only=True)


def eligibility(tid,order,evidence,code):
    """Annotate the already observed event. Never re-evaluate or change a decision."""
    counts=ELIGIBILITY_COUNTS.setdefault(tid,{})
    counts[code]=counts.get(code,0)+1
    for event in reversed(BUFFERS.get(tid,[])):
        if event['evidence'].get('signature')==(evidence or {}).get('signature'):
            event['eligibility']=code;event['order_id']=order['order_id'];break


def checkpoint_data(c,tid,data):
    import execution_model as ex
    now=time.time();mono=time.monotonic();order=c.execute("SELECT * FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL' ORDER BY rowid DESC LIMIT 1",(tid,)).fetchone()
    order=dict(order) if order else data.get('order') or {}
    events=list(BUFFERS.get(tid,[]));origin=ex.ORIGINS.get(order.get('order_id'))
    counters=[dict(r) for r in c.execute('SELECT kind,code,n,first_ts,last_ts FROM order_audit_v0739 WHERE order_id=?',(order.get('order_id'),))]
    eligibility_counts={r['code']:r['n'] for r in counters if r['kind']=='eligibility'}
    after=[e for e in events if origin is not None and isinstance(e['evidence'].get('received_mono'),(int,float)) and e['evidence']['received_mono']>=origin]
    before_delay=[e for e in after if e['evidence']['received_mono']-origin<ex.DELAY]
    late=[e for e in after if e not in before_delay]
    if not after:reason='NO_OBSERVED_EVENT_AFTER_ORDER_IN_BUFFER'
    elif not late:reason='ONLY_EVENTS_BEFORE_250MS_IN_BUFFER'
    elif not any(e['validation']=='ACCEPTED' for e in late):reason='DATA_REJECTED_AFTER_DELAY_IN_BUFFER'
    elif any(e.get('eligibility')=='ELIGIBLE' for e in late):reason='ELIGIBLE_EVENT_OBSERVED'
    else:reason='WAITING_ELIGIBLE_EVENT'
    position=c.execute('SELECT * FROM positions WHERE trade_id=?',(tid,)).fetchone()
    return dict(price_path_v100=__import__('price_path_v100').snapshot(tid,PROCESS_ID),captured_ts=now,process_id=PROCESS_ID,order=order,position=dict(position) if position else None,
      process_restarted=data.get('process_id')!=PROCESS_ID,events=events,events_seen_this_process=TOTAL.get(tid,0),
      events_dropped_this_process=max(0,TOTAL.get(tid,0)-len(events)),order_counters=counters,
      eligibility_counts=eligibility_counts,buffer_reason=reason,events_before_delay_in_buffer=len(before_delay),events_after_delay_in_buffer=len(late),
      order_origin_mono=origin,delay_ms=ex.DELAY*1000,
      wait_wall_s=now-order['signal_ts'] if order.get('signal_ts') is not None else None,
      note='Counters cover the order; bounded events cover this process only. A restart resets the execution delay, not the original signal. Wall wait is diagnostic and may reflect clock corrections. Not proof of sale availability.')


def checkpoint(c,force=False):
    global LAST_CHECKPOINT
    mono=time.monotonic()
    if not force and mono-LAST_CHECKPOINT<5:return
    for row in c.execute("SELECT trade_id,data_json FROM exit_audit_v45 WHERE status='WAITING_FILL'").fetchall():
        data=json.loads(row['data_json']);previous=data.get('checkpoint')
        if previous and previous.get('process_id')!=PROCESS_ID:
            data['previous_process_checkpoint']=previous
        data['checkpoint']=checkpoint_data(c,row['trade_id'],data)
        c.execute('UPDATE exit_audit_v45 SET data_json=? WHERE trade_id=?',(dump(data),row['trade_id']))
    LAST_CHECKPOINT=mono
