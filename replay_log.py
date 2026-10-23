"""Scoped normalized input journal. No network access or trading decisions."""
import json,uuid
import entry_selection
from collections import deque
PENDING=deque()
ACTIVE={}
CURRENT=None
MAX_BUFFER=50000
SCHEMA='''CREATE TABLE IF NOT EXISTS replay_v0716(
 id INTEGER PRIMARY KEY, watch_id TEXT NOT NULL, mint TEXT NOT NULL,
 seq INTEGER NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(watch_id,seq));
 CREATE INDEX IF NOT EXISTS replay_watch_v0716 ON replay_v0716(watch_id,id);'''

def emit(q,kind,ts,data):
 q['seq']+=1
 if kind=='EVENT' and len(PENDING)>=MAX_BUFFER:
  q['dropped']+=1;return
 PENDING.append((q['watch'],q['mint'],q['seq'],ts,kind,json.dumps(data,allow_nan=False)))

def capture(m,side,price,trader,source,now):
 global CURRENT
 CURRENT=dict(mint=m,side=side,price=price,trader=trader,source=source,received_ts=now)
 q=ACTIVE.get(m)
 if q:emit(q,'EVENT',now,CURRENT)

def begin(m,state,settings):
 q=dict(watch=uuid.uuid4().hex,mint=m,seq=0,dropped=0);ACTIVE[m]=q
 seed={k:list(v) if isinstance(v,deque) else v for k,v in state.items()}
 emit(q,'FAST_START',state['started'],dict(state=seed,settings=settings,entry_gate_plan=entry_selection.PLAN_HASH,trigger=CURRENT if CURRENT and CURRENT['mint']==m else None))
 return q['watch']

def decision(m,now):
 # Exactly the timestamp supplied to fast_reentry_tick, independent of capture overhead.
 q=ACTIVE.get(m)
 if q:emit(q,'EVALUATE',now,{})

def entered(m,now,case_id,price,recovery_pct=None,momentum_pct=None,buy_pressure=None,signal_age=None):
 q=ACTIVE.get(m)
 if q:
  data=dict(case_id=case_id,price=price)
  if recovery_pct is not None:
   data['entry_gate']=entry_selection.packet(recovery_pct,momentum_pct,buy_pressure,signal_age)
  emit(q,'REENTRY',now,data)

def finish(m,payload):
 q=ACTIVE.pop(m,None)
 if q:emit(q,'END',payload['ts'],dict(result=payload,dropped=q['dropped'],expected_records=q['seq']+1))

def flush(c):
 rows=list(PENDING)
 if not rows:return
 own=not c.in_transaction
 try:
  if own:c.execute('BEGIN IMMEDIATE')
  c.executemany('INSERT INTO replay_v0716(watch_id,mint,seq,ts,kind,payload) VALUES(?,?,?,?,?,?)',rows)
  if own:c.commit()
 except Exception:
  if own:c.rollback()
  raise
 for _ in rows:PENDING.popleft()

def summary(c):
 out=dict(watches=0,completed=0,incomplete=0,events=0,dropped=0,broken=0)
 for r in c.execute('SELECT watch_id,COUNT(*),MIN(seq),MAX(seq),SUM(kind="FAST_START"),SUM(kind="END"),SUM(kind="EVENT") FROM replay_v0716 GROUP BY watch_id'):
  _,n,lo,hi,starts,ends,events=r
  out['watches']+=1;out['events']+=events
  out['completed']+=bool(ends);out['incomplete']+=not bool(ends)
  out['broken']+=bool(lo!=1 or hi!=n or starts!=1)
 for (p,) in c.execute('SELECT payload FROM replay_v0716 WHERE kind="END"'):
  out['dropped']+=json.loads(p)['dropped']
 return out
