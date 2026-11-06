"""Backtest of the pre-registered rules (PREREG.md). Usage: python -I backtest.py DATA_DIR OUT_JSON

Weekly signals on Sunday close (UTC), trades at Monday open, long-only spot, cash earns 0,
cost per side on traded notional. Daily close-to-close portfolio returns with drifting weights.
"""
import csv,datetime as dt,hashlib,json,math,random,sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
UNIVERSE=['BTC','ETH','XRP','BCH','LTC','EOS','BNB','XTZ','XLM','ADA','TRX','LINK','ETC','NEO','ATOM']
SIM_START=dt.date(2019,12,30)     # Monday; first trades use the Sunday 2019-12-29 signal
EVAL_START=dt.date(2020,1,1)
SIM_END=dt.date(2026,10,31)
PERIODS={'discovery':(dt.date(2020,1,1),dt.date(2023,12,31)),'holdout':(dt.date(2024,1,1),dt.date(2026,10,31))}
BASE_COST=.0015;STRESS_COST=.003;HURDLE=.05;ALPHA=.006;BLOCK=20;RESAMPLES=5000;SEED=20261105

def load(data):
    px={}
    for coin in UNIVERSE:
        rows={}
        with (Path(data)/(coin+'USDT.csv')).open(encoding='utf-8') as f:
            for r in csv.DictReader(f):
                d=dt.datetime.fromtimestamp(int(r['open_time_ms'])/1000,dt.timezone.utc).date()
                rows[d]=(float(r['open']),float(r['close']))
        px[coin]=rows
    return px

def days(a,b):
    d=a
    while d<=b:yield d;d+=dt.timedelta(days=1)

class Series:
    """Close/open lookup with explicit availability; a missing day inside the series carries the last close."""
    def __init__(self,rows):
        self.rows=rows;self.first=min(rows);self.last=max(rows);self.cache={}
    def bar(self,d):
        if d<self.first or d>self.last:return None
        if d in self.rows:return self.rows[d]
        prev=d
        while prev not in self.rows:prev-=dt.timedelta(days=1)
        c=self.rows[prev][1];return (c,c)
    def close(self,d):
        b=self.bar(d);return b[1] if b else None
    def gaps(self):
        return sum(1 for d in days(self.first,self.last) if d not in self.rows)

def ret28(s,d):
    a=s.close(d);b=s.close(d-dt.timedelta(days=28))
    return a/b-1 if a and b else None

def sma(s,d,n):
    vals=[s.close(d-dt.timedelta(days=i)) for i in range(n)]
    return sum(vals)/n if all(v is not None for v in vals) else None

def signal(rule,S,sunday):
    if rule=='R1':
        return {c:1/15 for c,s in S.items() if (r:=ret28(s,sunday)) is not None and r>0}
    if rule=='R2':
        btc=S['BTC'];m=sma(btc,sunday,100)
        if m is None or btc.close(sunday)<=m:return {}
        ranked=sorted(((r,c) for c,s in S.items() if (r:=ret28(s,sunday)) is not None),reverse=True)[:5]
        return {c:.2 for _,c in ranked}
    if rule=='EW':
        live=[c for c,s in S.items() if s.close(sunday) is not None]
        return {c:1/len(live) for c in live}
    if rule=='BTC':return {'BTC':1.}
    raise ValueError(rule)

def simulate(rule,S,cost):
    cash=1.;units={c:0. for c in S};values=[];prev=None;turnover=0.;held_btc=False
    for d in days(SIM_START,SIM_END):
        # Delisting: close at the last available close, with cost, on the first day after the series ends.
        for c,s in S.items():
            if units[c] and d>s.last:
                v=units[c]*s.close(s.last);cash+=v*(1-cost);turnover+=v;units[c]=0.
        if d.weekday()==0 and not (rule=='BTC' and held_btc):
            target=signal(rule,S,d-dt.timedelta(days=1));held_btc=held_btc or rule=='BTC'
            opens={c:(S[c].bar(d) or (None,None))[0] for c in S}
            value=cash+sum(units[c]*opens[c] for c in S if units[c])
            for c in S:
                if opens[c] is None:continue
                want=target.get(c,0.)*value/(1+cost)/opens[c];delta=want-units[c]  # leave room for fees: no borrowed cash
                if abs(delta*opens[c])<1e-12:continue
                notional=abs(delta*opens[c]);fee=notional*cost
                cash-=delta*opens[c]+fee;units[c]=want;turnover+=notional
        value=cash+sum(units[c]*S[c].close(d) for c in S if units[c])
        if prev is not None and d>=EVAL_START:values.append((d,value/prev-1))
        prev=value
    return values,turnover

def stats(rets):
    r=[x for _,x in rets];n=len(r)
    if n<2:return None
    eq=1.;peak=1.;dd=0.
    for x in r:eq*=1+x;peak=max(peak,eq);dd=max(dd,1-eq/peak)
    mean=sum(r)/n;sd=math.sqrt(sum((x-mean)**2 for x in r)/(n-1))
    return dict(days=n,cagr=eq**(365/n)-1,sharpe=mean/sd*math.sqrt(365) if sd else None,max_dd=dd,total=eq-1)

def window(rets,period):
    a,b=PERIODS[period];return [(d,x) for d,x in rets if a<=d<=b]

def sharpe(r):
    n=len(r);m=sum(r)/n;sd=math.sqrt(sum((x-m)**2 for x in r)/(n-1));return m/sd*math.sqrt(365) if sd else 0.

def bootstrap_p(a,b):
    """One-sided p for Sharpe(a)-Sharpe(b)>0: stationary block bootstrap on paired days (percentile method)."""
    rng=random.Random(SEED);n=len(a);p=1/BLOCK;worse=0
    for _ in range(RESAMPLES):
        idx=[];i=rng.randrange(n)
        while len(idx)<n:
            idx.append(i);i=rng.randrange(n) if rng.random()<p else (i+1)%n
        if sharpe([a[j] for j in idx])-sharpe([b[j] for j in idx])<=0:worse+=1
    return (worse+1)/(RESAMPLES+1)

def main(data,out):
    prereg=hashlib.sha256((HERE/'PREREG.md').read_bytes()).hexdigest()
    S={c:Series(rows) for c,rows in load(data).items()}
    result=dict(prereg_sha256=prereg,data_manifest=json.loads((Path(data)/'manifest.json').read_text(encoding='utf-8')),
        series={c:dict(first=str(s.first),last=str(s.last),gaps=s.gaps()) for c,s in S.items()},runs={},verdict={})
    runs={}
    for rule in ('R1','R2','EW','BTC'):
        for label,cost in (('base',BASE_COST),('stress',STRESS_COST)):
            rets,turn=simulate(rule,S,cost);runs[(rule,label)]=rets
            result['runs'][rule+'_'+label]=dict(turnover_x=turn,**{p:stats(window(rets,p)) for p in PERIODS},full=stats(rets))
    for rule in ('R1','R2'):
        h=window(runs[(rule,'base')],'holdout');e=window(runs[('EW','base')],'holdout')
        assert [d for d,_ in h]==[d for d,_ in e]
        p=bootstrap_p([x for _,x in h],[x for _,x in e])
        rh=result['runs'][rule+'_base']['holdout'];rs=result['runs'][rule+'_stress']['holdout'];eh=result['runs']['EW_base']['holdout']
        rd=result['runs'][rule+'_base']['discovery'];ed=result['runs']['EW_base']['discovery']
        checks={'1_holdout_cagr_base_gt_hurdle':rh['cagr']>HURDLE,'1_holdout_cagr_stress_gt_0':rs['cagr']>0,
            '2_holdout_sharpe_diff_p_lt_0.006':p<ALPHA,'3_holdout_maxdd_le_ew':rh['max_dd']<=eh['max_dd'],
            '4_discovery_cagr_gt_hurdle':rd['cagr']>HURDLE,'4_discovery_sharpe_gt_ew':rd['sharpe']>ed['sharpe']}
        result['verdict'][rule]=dict(passed=all(checks.values()),checks=checks,holdout_sharpe_diff=rh['sharpe']-eh['sharpe'],bootstrap_p=p)
    Path(out).write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')
    print('PREREG',prereg)
    for k,v in result['runs'].items():
        print(f"{k:11s} turnover x{v['turnover_x']:.1f}",' | '.join(f"{p}: CAGR {v[p]['cagr']*100:7.1f}% Sharpe {v[p]['sharpe']:5.2f} MaxDD {v[p]['max_dd']*100:5.1f}%" for p in ('discovery','holdout')))
    for rule,v in result['verdict'].items():print(rule,'PASSED' if v['passed'] else 'FAILED',v['checks'],'sharpe diff %.2f p=%.4f'%(v['holdout_sharpe_diff'],v['bootstrap_p']))

if __name__=='__main__':main(sys.argv[1],sys.argv[2])
