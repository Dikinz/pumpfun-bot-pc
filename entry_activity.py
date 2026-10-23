"""Fixed pre-entry hypotheses. No gating or threshold optimization."""
import json,math,sqlite3,argparse
from pathlib import Path
PLAN='activity_0721_v1'
SCHEMA='''CREATE TABLE IF NOT EXISTS entry_activity_v0721(order_id TEXT PRIMARY KEY,signal_ts REAL,plan_id TEXT,features TEXT);'''
LABELS={'ALL':'Все','ACT4':'4 события за 2 секунды','CONT5':'Последние 5: интервал 0.5–4с, паузы ≤1с','SELL10':'Есть продажа среди последних 10'}

def features(snapshot,ts):
    events=snapshot.get('model_events',[]);valid=True;clean=[]
    for e in events:
        if not isinstance(e,(list,tuple)) or len(e)!=3:valid=False;continue
        side,price,t=e
        if side not in ('buy','sell') or not isinstance(t,(int,float)) or not math.isfinite(t) or t>ts or not isinstance(price,(int,float)) or not math.isfinite(price) or price<=0:valid=False;continue
        clean.append((side,price,t))
    if any(a[2]>b[2] for a,b in zip(clean,clean[1:])):valid=False
    last5=clean[-5:];gaps=[b[2]-a[2] for a,b in zip(last5,last5[1:])]
    span=last5[-1][2]-last5[0][2] if len(last5)==5 else None
    n2=sum(ts-2<=x[2]<=ts for x in clean)
    flags={'ALL':True,'ACT4':valid and n2>=4,'CONT5':valid and span is not None and .5<=span<=4 and max(gaps)<=1,'SELL10':valid and any(x[0]=='sell' for x in clean[-10:])}
    return {'valid':valid and bool(clean),'events_available':len(clean),'events_2s_capped20':n2,'last5_span_s':span,'last5_max_gap_s':max(gaps) if gaps else None,'last10_sells':sum(x[0]=='sell' for x in clean[-10:]),'decisions':flags}

def record(c,oid,snapshot,ts):
    f=features(snapshot or {},ts)
    c.execute('INSERT INTO entry_activity_v0721 VALUES(?,?,?,?)',(oid,ts,PLAN,json.dumps(f)))

def summarize(c):
    orders=[dict(r) for r in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='BUY' ORDER BY signal_ts,order_id")]
    exits={r['trade_id']:dict(r) for r in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL'")}
    sells={r['trade_id']:dict(r) for r in c.execute("SELECT * FROM trades WHERE side='SELL'")}
    recorded={}
    has_recorded_table=bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='entry_activity_v0721'").fetchone())
    if has_recorded_table:
        recorded={r['order_id']:dict(r) for r in c.execute('SELECT * FROM entry_activity_v0721')}
    results=[];bad=0;errors=[]
    end=max([float(r[0]) for r in c.execute("SELECT v FROM meta WHERE k='heartbeat_ts'")]+[o['signal_ts'] for o in orders]+[0])
    for i,o in enumerate(orders):
        snap=json.loads(o['snapshot']);f=features(snap,o['signal_ts'])
        if has_recorded_table:
            r=recorded.get(o['order_id'])
            if not r or r['plan_id']!=PLAN or json.loads(r['features'])!=f:errors.append('Stored feature mismatch: '+o['order_id'])
        if not f['valid']:bad+=1
        e=exits.get(o['trade_id']);t=sells.get(o['trade_id'])
        wait=e['wait_ms']/1000 if e and e['status']=='FILLED' else max(0,end-e['signal_ts']) if e and e['status']=='PENDING' else None
        results.append({'order':o,'f':f,'exit':e,'trade':t,'wait':wait,'half':i>=len(orders)//2})
    rows=[]
    for key,label in LABELS.items():
        rr=[r for r in results if r['f']['valid'] and r['f']['decisions'][key]]
        closed=[r for r in rr if r['trade']];filled=[r for r in rr if r['order']['status']=='FILLED'];exited=[r for r in rr if r['exit']]
        def net(r):
            o=r['order'];t=r['trade'];turn=o['size']*(1+t['price']/o['fill_price'])
            return t['pnl']-.015*turn-.01 # fixed comparison scenario, not new wallet ledger
        rows.append({'variant':key,'label':label,'signals':len(rr),'filled':len(filled),'expired':sum(r['order']['status']=='EXPIRED' for r in rr),'closed':len(closed),'open':len(filled)-len(closed),'exit_signals':len(exited),'wait_gt5':sum(r['wait'] is not None and r['wait']>5 for r in rr),'wait_gt30':sum(r['wait'] is not None and r['wait']>30 for r in rr),'pending_exits':sum(r['exit'] is not None and r['exit']['status']=='PENDING' for r in rr),'closed_net_scenario':sum(net(r) for r in closed),'first_half_net':sum(net(r) for r in closed if not r['half']),'second_half_net':sum(net(r) for r in closed if r['half'])})
    return {'plan':PLAN,'signals':len(orders),'invalid_features':bad,'errors':errors,'rows':rows,'note':'Frozen shadow rules, not actual entry filters. Recent events are capped at 20. Closed subset PnL excludes unresolved positions and is NOT freed-capital replay. Fixed comparison costs 1.5% each side + .005 per fill, assumptions only. Future waits are outcome labels, never input features.'}

def check(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN');return summarize(c)
if __name__=='__main__':
    import config
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=config.DB);ap.add_argument('--out',default='entry_activity_results.json');a=ap.parse_args();r=check(a.db)
    Path(a.out).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False,indent=2));raise SystemExit(1 if r['errors'] else 0)
