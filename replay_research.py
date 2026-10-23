"""Read-only reproduction of recorded re-entry policy, not a whole-market backtest."""
import json,sqlite3,math,sys
from pathlib import Path
from collections import deque
from types import SimpleNamespace
from contextlib import closing

def verify(c):
 import main as engine
 import config
 import entry_selection
 original={k:getattr(engine,k) for k in ('time','db','observation','replay_log','FAST_REENTRY','SHADOW_DB_QUEUE')}
 settings_before={}
 noop=lambda *a,**k:None
 active={}
 engine.db=SimpleNamespace(inc=noop)
 engine.observation=SimpleNamespace(ACTIVE=active,start=lambda m,*a:active.update({m:{'case':'replay'}}),tick=noop,finish=noop)
 engine.replay_log=SimpleNamespace(decision=noop,entered=noop,finish=noop)
 report=dict(completed=0,matched=0,incomplete=0,corrupt=0,mismatched=0,failures=[])
 try:
  watches=[r[0] for r in c.execute('SELECT DISTINCT watch_id FROM replay_v0716')]
  for wid in watches:
   rows=[(seq,ts,kind,json.loads(p)) for seq,ts,kind,p in c.execute('SELECT seq,ts,kind,payload FROM replay_v0716 WHERE watch_id=? ORDER BY seq',(wid,))]
   if not any(r[2]=='END' for r in rows):report['incomplete']+=1;continue
   report['completed']+=1
   if rows[0][2]!='FAST_START' or rows[-1][2]!='END' or [r[0] for r in rows]!=list(range(1,len(rows)+1)) or rows[-1][3]['dropped'] or rows[-1][3]['expected_records']!=len(rows):
    report['corrupt']+=1;report['failures'].append(dict(watch=wid,error='incomplete sequence or dropped input'));continue
   seed=rows[0][3];expected=rows[-1][3]['result'];m=expected['mint']
   for k,v in seed['settings'].items():
    if k.startswith('FAST_REENTRY_') or k in ('POSITION_SIZE','EXEC_AUDIT_PROFILES'):
     if k not in settings_before:settings_before[k]=getattr(config,k)
     setattr(config,k,v)
   q=seed['state'];q['prices']=deque(q['prices'],maxlen=12);q['sides']=deque(q['sides'],maxlen=12)
   engine.FAST_REENTRY={m:q};engine.SHADOW_DB_QUEUE=deque();last_event=None
   malformed=False
   for seq,ts,kind,d in rows[1:]:
    engine.time=SimpleNamespace(time=lambda t=ts:t)
    if kind=='EVENT':last_event=d
    elif kind=='EVALUATE':
     if not last_event or last_event['price']<=0 or m not in engine.FAST_REENTRY:malformed=True;break
     engine.fast_reentry_tick(m,last_event['price'],last_event['side']);last_event=None
    elif kind=='END' and d['result']['final_source']=='WATCHDOG_LAST':
     state=engine.FAST_REENTRY.get(m)
     if state:
      if not state['reentered'] and ts-state['started']>=config.FAST_REENTRY_WATCH_SECONDS:
       engine.fast_reentry_finalize(m,'NO_SIGNAL','WATCHDOG_LAST',ts)
      elif state['reentered'] and ts-state['reentry_ts']>=config.FAST_REENTRY_MAX_HOLD:
       engine.fast_reentry_finalize(m,'REENTRY_TIMEOUT','WATCHDOG_LAST',ts)
   outcomes=[v for kind,v in engine.SHADOW_DB_QUEUE if kind=='fast_reentry_done']
   diffs=[]
   if malformed or len(outcomes)!=1:diffs=['missing/extra decision or outcome']
   else:
    actual=outcomes[0]
    for key in ('reentered','exit_reason','final_source','events_after_fast','reentry_price','exit_price','reentry_age','reentry_hold','base_reentry_pnl','signal_recovery_pct','signal_momentum_pct','signal_buy_pressure'):
     a,b=actual[key],expected[key]
     if (not math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-9)) if isinstance(a,(int,float)) else a!=b:diffs.append(key)
   gates=[d.get('entry_gate') for _,_,kind,d in rows if kind=='REENTRY']
   if seed.get('entry_gate_plan') and expected['reentered'] and (seed['entry_gate_plan']!=entry_selection.PLAN_HASH or len(gates)!=1 or gates[0] is None):diffs.append('missing_or_unknown_entry_gate')
   for _,_,kind,d in rows:
    if kind=='REENTRY' and 'entry_gate' in d and not entry_selection.valid_packet(d['entry_gate'],expected):diffs.append('entry_gate')
   if diffs:report['mismatched']+=1;report['failures'].append(dict(watch=wid,error=diffs))
   else:report['matched']+=1
 finally:
  for k,v in original.items():setattr(engine,k,v)
  for k,v in settings_before.items():setattr(config,k,v)
 return report

if __name__=='__main__':
 import config
 path=Path(sys.argv[1] if len(sys.argv)>1 else config.DB).resolve()
 with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
  result=verify(c)
 print(json.dumps(result,ensure_ascii=False,indent=2))
 sys.exit(1 if result['corrupt'] or result['mismatched'] else 0)
