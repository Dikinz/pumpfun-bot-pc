"""Offline PAPER replay of a previously recorded bot SQLite DB. No network calls."""
import sqlite3, sys, math
from collections import defaultdict, deque
from pathlib import Path
import config

def f(v,d=0.0):
    try:return float(v)
    except:return d

def main(src):
    src=Path(src)
    if not src.exists(): raise SystemExit(f"Source DB not found: {src}")
    c=sqlite3.connect(src); c.row_factory=sqlite3.Row
    rows=c.execute("SELECT ts,mint,side,price,trader FROM events WHERE price>0 ORDER BY ts,id").fetchall()
    if not rows: raise SystemExit("No recorded price events found in source DB.")
    S={}; P={}; entered=set(); cash=config.START_BALANCE; realized=0.0; closed=[]
    def state(ts): return {"born":ts,"trades":0,"buys":0,"sells":0,"buyers":set(),"first":0.0,"last":0.0,"prices":deque(maxlen=4)}
    def sell(m,p,reason,ts):
        nonlocal cash,realized
        q=P.pop(m,None)
        if not q:return
        r=p/q['entry']-1; pnl=config.POSITION_SIZE*r; cash += config.POSITION_SIZE+pnl; realized += pnl
        closed.append((m,pnl,reason,ts-q['ts'],q['mfe'],q['mae']))
    for x in rows:
        ts=float(x['ts']); m=x['mint']; side=(x['side'] or '').lower(); p=f(x['price']); trader=x['trader'] or ''
        if not m or p<=0: continue
        s=S.setdefault(m,state(ts)); s['trades']+=1; s['buys']+=side=='buy'; s['sells']+=side=='sell'
        if side=='buy' and trader:s['buyers'].add(trader)
        if not s['first']:s['first']=p
        s['last']=p;s['prices'].append(p)
        if m in P:
            q=P[m];q['post']+=1;q['pb']+=side=='buy';q['ps']+=side=='sell';r=p/q['entry']-1;q['mfe']=max(q['mfe'],r);q['mae']=min(q['mae'],r);q['high']=max(q['high'],p);age=ts-q['ts'];pr=q['pb']/max(1,q['ps'])
            if age<=config.FAST_PROTECT_SECONDS and q['post']>=config.FAST_STOP_MIN_EVENTS and r<=config.FAST_STOP:sell(m,p,'FAST_STOP',ts)
            elif r<=config.STOP_LOSS:sell(m,p,'STOP_LOSS',ts)
            elif config.PROBATION_MIN_SECONDS<=age<=config.PROBATION_MAX_SECONDS and ((q['post']>=config.EARLY_FAIL_MIN_POST_TRADES and q['mfe']<config.EARLY_FAIL_MAX_MFE and r<=config.EARLY_FAIL_RETURN and pr<=config.EARLY_FAIL_MAX_POST_RATIO) or (q['mfe']<config.EARLY_FAIL_HARD_MAX_MFE and r<=config.EARLY_FAIL_HARD_RETURN)):sell(m,p,'EARLY_FAIL',ts)
            elif r>=config.TP2:sell(m,p,'TP2',ts)
            elif q['high']/q['entry']-1>=config.TP1 and p/q['high']-1<=-config.TRAIL:sell(m,p,'TRAIL',ts)
            elif age>=config.MAX_HOLD:sell(m,p,'TIME',ts)
        ratio=s['buys']/max(1,s['sells']);mom=(s['last']/s['first']-1)*100 if s['first'] else 0
        score=(25 if s['trades']>=config.MIN_TRADES else 0)+(20 if len(s['buyers'])>=config.MIN_BUYERS else 0)+(20 if ratio>=config.MIN_RATIO else 0)+(25 if mom>=config.MIN_MOMENTUM else 0)+(10 if s['trades']>=6 else 0)
        gate=s['trades']>=config.MIN_TRADES and len(s['buyers'])>=config.MIN_BUYERS and ratio>=config.MIN_RATIO and mom>=config.MIN_MOMENTUM and score>=config.MIN_SCORE
        jump=(s['prices'][-1]/s['prices'][-2]-1)*100 if len(s['prices'])>=2 else 0
        if gate and m not in P and m not in entered and len(P)<config.MAX_POSITIONS and cash>=config.POSITION_SIZE and ts-s['born']>=config.MIN_ENTRY_AGE and mom<=config.MAX_ENTRY_MOMENTUM and jump<=config.MAX_ENTRY_LAST_JUMP:
            entered.add(m);cash-=config.POSITION_SIZE;P[m]={"entry":p,"ts":ts,"high":p,"mfe":0.0,"mae":0.0,"post":0,"pb":0,"ps":0}
    last_ts=float(rows[-1]['ts'])
    for m,q in list(P.items()): sell(m,q['entry']*(1+q['mfe']), 'REPLAY_END', last_ts)
    wins=sum(1 for x in closed if x[1]>0); gp=sum(x[1] for x in closed if x[1]>0); gl=-sum(x[1] for x in closed if x[1]<0);pf=gp/gl if gl else float('inf')
    print("\n=== v0.4.8.8 OFFLINE REPLAY ===")
    print(f"Source: {src}")
    print(f"Events: {len(rows)} | Closed: {len(closed)} | W/L: {wins}/{len(closed)-wins}")
    print(f"Realized: ${realized:+.2f} | Final cash: ${cash:.2f} | PF: {pf:.2f}")
    by=defaultdict(lambda:[0,0.0])
    for _,pnl,r,*_ in closed:by[r][0]+=1;by[r][1]+=pnl
    for r,(n,pnl) in sorted(by.items(), key=lambda kv:-kv[1][1]):print(f"{r:20s} {n:4d}  ${pnl:+.2f}")
    print("\nNOTE: replay uses recorded trade events only; discovery timing/network gaps are not recreated exactly.")
if __name__=='__main__':
    if len(sys.argv)<2: raise SystemExit('Usage: python replay_existing_db.py path\\to\\rapid_0487.db')
    main(sys.argv[1])
