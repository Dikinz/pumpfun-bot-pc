"""Observational only; no trade decisions. All timestamps are local reception times."""
import time,json,uuid
from collections import deque
PENDING=deque()
ACTIVE={}
CHECKPOINTS=(.5,1.,2.)
FRESH=.25
MAX_TIMER_LAG=.10
FILL_DELAY=.75
FILL_WINDOW=10.

def start(m,price,now=None):
 now=time.time() if now is None else now
 ACTIVE[m]=dict(case=uuid.uuid4().hex,mint=m,entry=price,start=now,last=price,last_ts=now,mom=0.,bp=0.,events=0,done=set(),requests=[],closed=False)
 record('START',ACTIVE[m],entry=price,start_ts=now)

def record(kind,q,**data):
 PENDING.append((q['case'],q['mint'],time.time(),kind,json.dumps(data,allow_nan=False)))

def tick(m,price,mom,bp,now=None,cost_fn=None,allow_fill=True):
 q=ACTIVE.get(m)
 if not q:return
 now=time.time() if now is None else now
 q.update(last=price,last_ts=now,mom=mom,bp=bp,events=q['events']+1)
 record('TICK',q,received_ts=now,hold=now-q['start'],price=price,mom=mom,bp=bp)
 for request in q['requests']:
  if allow_fill and request['status']=='PENDING' and request['ready']<=now<=request['deadline']:
   request['status']='FILLED'
   record('ABORT_FILL',q,sec=request['sec'],decision_ts=request['decision'],fill_ts=now,price=price,return_pct=100*(price/q['entry']-1),base_net=cost_fn(m,q['entry'],price) if cost_fn else None)

def poll(now=None,transport_ok=True,market_ts=None,market_stale_after=5.):
 now=time.time() if now is None else now
 for m,q in list(ACTIVE.items()):
  if not q['closed']:
   for sec in CHECKPOINTS:
    if sec in q['done'] or now<q['start']+sec:continue
    q['done'].add(sec);lag=now-q['start']-sec;age=now-q['last_ts'];ret=100*(q['last']/q['entry']-1)
    market_age=None if market_ts is None else now-market_ts
    market_stale=market_age is not None and (market_age<0 or market_age>=market_stale_after)
    cause="transport_unavailable" if not transport_ok else "market_stale" if market_stale else "clock_invalid" if age<0 else "no_post_entry_tick" if age>FRESH and q["events"]==0 else "mint_idle" if age>FRESH else "fresh"
    valid=transport_ok and 0<=age<=FRESH and 0<=lag<=MAX_TIMER_LAG
    abort=valid and ret<0 and q['mom']<0
    record('CHECKPOINT',q,sec=sec,decision_ts=now,hold=now-q['start'],timer_lag=lag,price_age=age,ret=ret,mom=q['mom'],bp=q['bp'],events=q['events'],usable=valid,abort=abort,transport_ok=transport_ok,stale=age>FRESH,timer_late=lag>MAX_TIMER_LAG,market_age=market_age,market_stale=market_stale,price_status=cause)
    if abort:q['requests'].append(dict(sec=sec,decision=now,ready=now+FILL_DELAY,deadline=now+FILL_DELAY+FILL_WINDOW,status='PENDING'))
  for request in q['requests']:
   if request['status']=='PENDING' and now>request['deadline']:
    request['status']='UNRESOLVED';record('ABORT_UNRESOLVED',q,sec=request['sec'],decision_ts=request['decision'])
  if q['closed'] and all(r['status']!='PENDING' for r in q['requests']):ACTIVE.pop(m,None)

def finish(m,reason,base,now=None):
 q=ACTIVE.get(m)
 if not q:return
 q['closed']=True;record('BASELINE_EXIT',q,reason=reason,base=base,exit_ts=time.time() if now is None else now)
 for request in q['requests']:
  if request['status']=='PENDING':
   request['status']='CANCELLED'
   record('ABORT_CANCELLED',q,sec=request['sec'],decision_ts=request['decision'],reason='BASELINE_EXIT')
 ACTIVE.pop(m,None)

def flush(connection):
 items=list(PENDING)
 if not items:return
 connection.executemany('INSERT INTO observation_v0714(case_id,mint,ts,kind,payload) VALUES(?,?,?,?,?)',items)
 for _ in items:PENDING.popleft()
