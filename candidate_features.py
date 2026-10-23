"""Pure copies of the frozen primary scanner features; parity tested."""
import config
from collections import deque

def state():
    return {"born":0.0,"trades":0,"buys":0,"sells":0,"buyers":set(),"first":0.0,"last":0.0,"valid_prices":0,"price_source":"","milestone":0,"prices":deque(maxlen=8),"sides":deque(maxlen=8),"model_events":deque(maxlen=20)}

def calc(s):
    ratio=s["buys"]/max(1,s["sells"]); mom=((s["last"]/s["first"])-1)*100 if s["first"]>0 and s["last"]>0 and s["valid_prices"]>=2 else 0.0
    sc=0
    if s["trades"]>=config.MIN_TRADES:sc+=25
    if len(s["buyers"])>=config.MIN_BUYERS:sc+=20
    if ratio>=config.MIN_RATIO:sc+=20
    if s["valid_prices"]>=2 and mom>=config.MIN_MOMENTUM:sc+=25
    if s["trades"]>=6:sc+=10
    gate=("samples" if s["trades"]<config.MIN_TRADES else "buyers" if len(s["buyers"])<config.MIN_BUYERS else "buy/sell" if ratio<config.MIN_RATIO else "price data" if s["valid_prices"]<2 else "momentum" if mom<config.MIN_MOMENTUM else "PASS")
    return sc,gate,ratio,mom

def trajectory_features(s):
    prices=list(s.get("prices",())) if s else []
    sides=list(s.get("sides",())) if s else []
    rets=[]
    for a,b in zip(prices,prices[1:]):
        if a>0: rets.append((b/a-1)*100.0)
    n=len(rets); half=max(1,n//2)
    early=(sum(rets[:half])/len(rets[:half])) if rets[:half] else 0.0
    recent=(sum(rets[half:])/len(rets[half:])) if rets[half:] else early
    accel=recent-early
    mean=(sum(rets)/n) if n else 0.0
    vol=(sum((x-mean)**2 for x in rets)/n)**0.5 if n else 0.0
    last_ret=rets[-1] if rets else 0.0
    pullback=(prices[-1]/max(prices)-1)*100.0 if prices and max(prices)>0 else 0.0
    sh=max(1,len(sides)//2)
    es=sides[:sh]; rs=sides[sh:] or es
    ep=sum(x=="buy" for x in es)/max(1,len(es)); rp=sum(x=="buy" for x in rs)/max(1,len(rs))
    streak=0
    for x in reversed(sides):
        if x=="buy": streak+=1
        else: break
    return len(prices),accel,(rp-ep),pullback,vol,last_ret,streak,rp,ep

def model_snapshot_features(s,score=0):
    ev=list(s.get("model_events",())) if s else []
    prices=[float(x[1]) for x in ev if float(x[1])>0]
    sides=[x[0] for x in ev]
    rets=[(b/a-1)*100.0 for a,b in zip(prices,prices[1:]) if a>0]
    def mean(xs): return sum(xs)/len(xs) if xs else 0.0
    mu=mean(rets); vol=(mean([(x-mu)**2 for x in rets])**0.5) if rets else 0.0
    half=max(1,len(rets)//2); accel=mean(rets[half:])-mean(rets[:half]) if rets else 0.0
    last1=rets[-1] if rets else 0.0; last3=mean(rets[-3:])
    pullback=(prices[-1]/max(prices)-1)*100.0 if prices and max(prices)>0 else 0.0
    def bp(n):
        z=sides[-n:] if sides else []
        return sum(x=="buy" for x in z)/max(1,len(z))
    streak=0
    for x in reversed(sides):
        if x=="buy": streak+=1
        else: break
    ratio=(s["buys"]/max(1,s["sells"])) if s else 0.0
    mom=((s["last"]/s["first"])-1)*100 if s and s["first"] and s["last"] and s["valid_prices"]>=2 else 0.0
    return (len(ev),mu,vol,accel,last1,last3,pullback,bp(5),bp(10),bp(20),bp(5)-bp(20),streak,int(score),int(s["trades"] if s else 0),int(len(s["buyers"]) if s else 0),ratio,mom)
