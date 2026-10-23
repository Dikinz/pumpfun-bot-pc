import json
from collections import defaultdict

def summarize(c):
 cases=defaultdict(lambda:dict(cp={},fill={},cancel=set(),unresolved=set()))
 for r in c.execute("SELECT case_id,kind,payload FROM observation_v0714 WHERE kind!='TICK' ORDER BY id"):
  k,kind,payload=r;d=json.loads(payload);q=cases[k];sec=d.get('sec')
  if kind=='BASELINE_EXIT':q['exit']=d
  elif kind=='CHECKPOINT':q['cp'][sec]=d
  elif kind=='ABORT_FILL':q['fill'][sec]=d
  elif kind=='ABORT_CANCELLED':q['cancel'].add(sec)
  elif kind=='ABORT_UNRESOLVED':q['unresolved'].add(sec)
 closed=[q for q in cases.values() if 'exit' in q];base=sum(q['exit']['base'] for q in closed)
 rows=[]
 for sec in (.5,1.,2.,'FIRST'):
  r=dict(sec=sec,observed=0,usable=0,stale=0,late=0,transport_bad=0,signals=0,filled=0,cancelled=0,unresolved=0,lift=0.,base=base,net=base,benefit=0.,harm=0.,loss_saved=0.,profit_lost=0.,improved=0,worsened=0,price_status={})
  for q in cases.values():
   key=sec
   if sec=='FIRST':
    signals=sorted(k for k,d in q['cp'].items() if d['abort'])
    if not signals:continue
    key=signals[0]
   d=q['cp'].get(key)
   if not d:continue
   r['observed']+=1;r['usable']+=bool(d['usable']);r['stale']+=bool(d.get('stale',d['price_age']>.25));r['late']+=bool(d.get('timer_late',d['timer_lag']>.1));r['transport_bad']+=d.get('transport_ok') is False
   cause=d.get('price_status','legacy_unknown');r['price_status'][cause]=r['price_status'].get(cause,0)+1
   if not d['abort']:continue
   r['signals']+=1;r['cancelled']+=key in q['cancel'];r['unresolved']+=key in q['unresolved']
   f=q['fill'].get(key);e=q.get('exit')
   if f and e and f.get('base_net') is not None and f['fill_ts']<e['exit_ts']:
    delta=f['base_net']-e['base'];r['filled']+=1;r['lift']+=delta
    r['benefit']+=max(0.,delta);r['harm']+=max(0.,-delta)
    r['improved']+=delta>0;r['worsened']+=delta<0
    r['loss_saved']+=max(0.,max(0.,-e['base'])-max(0.,-f['base_net']))
    r['profit_lost']+=max(0.,max(0.,e['base'])-max(0.,f['base_net']))
  r['net']=base+r['lift'];rows.append(r)
 return dict(completed=len(closed),incomplete=len(cases)-len(closed),rows=rows,note='BASE costs modeled; FIRST counts each case once; unresolved retain baseline provisionally.')
