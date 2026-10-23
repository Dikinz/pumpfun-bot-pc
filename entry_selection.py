"""Frozen prospective shadow gates. Never alters primary or re-entry trade decisions."""
import json,math,hashlib
from collections import defaultdict
PLAN={'id':'entry_selection_0717_v1','recovery_cap_pct':10.,'momentum_cap_pct':5.,
      'policy':'accept or skip first baseline signal; no later retry',
      'variants':['ALL','NO_REENTRY','R10','M5','R10_M5']}
PLAN_HASH=hashlib.sha256(json.dumps(PLAN,sort_keys=True,separators=(',',':')).encode()).hexdigest()
LABELS={'ALL':'Все повторные входы','NO_REENTRY':'Без повторного входа','R10':'Отскок ≤10%','M5':'Краткий рост ≤5%','R10_M5':'Оба ограничения'}

def decide(recovery_pct,momentum_pct):
 if not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in (recovery_pct,momentum_pct)):
  raise ValueError('Non-finite gate input')
 r=recovery_pct<=PLAN['recovery_cap_pct'];m=momentum_pct<=PLAN['momentum_cap_pct']
 return dict(ALL=True,NO_REENTRY=False,R10=r,M5=m,R10_M5=r and m)

def packet(recovery_pct,momentum_pct,buy_pressure,signal_age):
 return dict(plan_id=PLAN['id'],plan_hash=PLAN_HASH,
             features=dict(recovery_pct=recovery_pct,momentum_pct=momentum_pct,buy_pressure=buy_pressure,signal_age=signal_age),
             decisions=decide(recovery_pct,momentum_pct))

def valid_packet(p,result=None):
 try:
  f=p['features'];d=p['decisions']
  if p['plan_id']!=PLAN['id'] or p['plan_hash']!=PLAN_HASH:return False
  if set(d)!=set(PLAN['variants']) or not all(isinstance(v,bool) for v in d.values()):return False
  if d!=decide(f['recovery_pct'],f['momentum_pct']):return False
  if result:
   for a,b in [('recovery_pct','signal_recovery_pct'),('momentum_pct','signal_momentum_pct'),('buy_pressure','signal_buy_pressure'),('signal_age','reentry_age')]:
    if not math.isclose(f[a],result[b],rel_tol=1e-9,abs_tol=1e-9):return False
  return True
 except (KeyError,TypeError,ValueError):return False

def aggregate(cases):
 rows=[];baseline=sum(x['result']['base_reentry_pnl'] for x in cases)
 for variant in PLAN['variants']:
  kept=[x for x in cases if x['gate']['decisions'][variant]]
  skipped=[x for x in cases if not x['gate']['decisions'][variant]]
  net=sum(x['result']['base_reentry_pnl'] for x in kept)
  n=len(kept);big=sum(x['result']['base_reentry_pnl']<=-.30 for x in kept)
  rows.append(dict(variant=variant,label=LABELS[variant],total=len(cases),kept=n,skipped=len(skipped),
   retained_pct=100*n/len(cases) if cases else 0.,base=net,baseline=baseline,lift=net-baseline,
   low=sum(x['result']['low_reentry_pnl'] for x in kept),stress=sum(x['result']['stress_reentry_pnl'] for x in kept),
   avoided_loss=-sum(min(0.,x['result']['base_reentry_pnl']) for x in skipped),
   lost_profit=sum(max(0.,x['result']['base_reentry_pnl']) for x in skipped),
   wins=sum(x['result']['base_reentry_pnl']>0 for x in kept),big_loss=big,
   big_loss_rate=100*big/n if n else None,base_per_kept=net/n if n else None,
   first_half_base=sum(x['result']['base_reentry_pnl'] for x in cases[:len(cases)//2] if x['gate']['decisions'][variant]),
   second_half_base=sum(x['result']['base_reentry_pnl'] for x in cases[len(cases)//2:] if x['gate']['decisions'][variant])))
 return rows

def summarize(c):
 meta={r[0]:r[1:] for r in c.execute('SELECT watch_id,COUNT(*),MIN(seq),MAX(seq) FROM replay_v0716 GROUP BY watch_id')}
 watches=defaultdict(lambda:dict(entries=[]))
 for wid,seq,ts,kind,payload in c.execute("SELECT watch_id,seq,ts,kind,payload FROM replay_v0716 WHERE kind IN ('FAST_START','REENTRY','END') ORDER BY id"):
  q=watches[wid];d=json.loads(payload)
  if kind=='FAST_START':q['start']=d
  elif kind=='REENTRY':q['entries'].append((seq,ts,d))
  else:q['end']=(seq,ts,d)
 out=dict(plan=PLAN,plan_hash=PLAN_HASH,complete=0,pending=0,legacy=0,invalid=0,no_signal=0,recent=[])
 cases=[]
 for wid,q in watches.items():
  entries=q['entries'];end=q.get('end');new=any('entry_gate' in x[2] for x in entries)
  if not end:
   if new:out['pending']+=1
   continue
  result=end[2]['result']
  if not result['reentered']:out['no_signal']+=1;continue
  if not new:
   out['invalid' if q.get('start',{}).get('entry_gate_plan') else 'legacy']+=1;continue
  count,lo,hi=meta[wid]
  good=(len(entries)==1 and q.get('start',{}).get('entry_gate_plan')==PLAN_HASH and lo==1 and hi==count and end[0]==count and end[2]['expected_records']==count and not end[2]['dropped'])
  seq,ts,d=entries[0]
  if not good or seq>=end[0] or ts>end[1] or not valid_packet(d.get('entry_gate',{}),result):
   out['invalid']+=1;continue
  case=dict(watch_id=wid,ts=ts,mint=result['mint'],gate=d['entry_gate'],result=result)
  cases.append(case)
 cases.sort(key=lambda x:(x['ts'],x['watch_id']));out['complete']=len(cases);out['rows']=aggregate(cases)
 for x in reversed(cases[-20:]):
  out['recent'].append(dict(ts=x['ts'],mint=x['mint'],features=x['gate']['features'],decisions=x['gate']['decisions'],base=x['result']['base_reentry_pnl']))
 # A technical checkpoint, never an automatic profitability verdict.
 out['checkpoint']='CHECK_DATA' if out['invalid'] else 'REVIEW_100' if out['complete']>=100 else 'COLLECTING'
 out['note']='Shadow only. Skipped first signal stays skipped. No compounding or freed-capital replay. BASE costs are modeled.'
 return out
