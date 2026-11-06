"""Pre-registered USDT-M perpetual futures rules test (see PREREG.md). Pure numpy, no network.

Usage: python -I backtest.py DATA_DIR OUT_JSON
"""
import hashlib,json,sys,time
from pathlib import Path
import numpy as np

M5=300_000;H=3_600_000;D=86_400_000;W=7*D;MON_OFFSET=4*D  # 1970-01-05 was a Monday
TF={'1h':H,'4h':4*H,'1D':D,'1W':W}
MAXHOLD={'T':{'1h':72,'4h':60,'1D':60,'1W':26},'M':{'1h':20,'4h':20,'1D':20,'1W':20}}
DMULT={'T':2.0,'M':1.5}
DECISION=[(f,tf) for f in ('T','M') for tf in ('1h','4h','1D')]
DESCRIPTIVE=[('T','1W'),('M','1W')]
DELAY={'1h':15*60_000,'4h':15*60_000,'1D':4*H,'1W':4*H}
FEE=0.00055;SLIP=0.0003;STOP_EXTRA=0.0005;MIN_D=0.001
def ms(s):return int(np.datetime64(s,'ms').astype(np.int64))
DISC=(ms('2022-01-01'),ms('2025-01-01'));HOLD=(ms('2025-01-01'),ms('2026-11-01'))
ALPHA=0.05/6

# ---------- data ----------
class Sym:
    def __init__(self,name,t,o,h,l,c,ft,fr):
        self.name=name;self.t=t;self.o=o;self.h=h;self.l=l;self.c=c;self.ft=ft;self.fr=fr
        self.fcum=np.concatenate([[0.0],np.cumsum(fr)])
    def funding_sum(self,t0,t1):
        """Sum of funding rates with t0 < ft <= t1."""
        a=np.searchsorted(self.ft,t0,side='right');b=np.searchsorted(self.ft,t1,side='right')
        return self.fcum[b]-self.fcum[a]

def load(data,name):
    z=np.load(Path(data)/(name+'.npz'))
    return Sym(name,z['t_ms'],z['o'],z['h'],z['l'],z['c'],z['ft_ms'],z['fr'])

def resample(s,tf):
    """Signal-timeframe bars from 5m bars. Returns dict of arrays; the last bar is dropped if incomplete."""
    step=TF[tf];off=MON_OFFSET if tf=='1W' else 0
    b=(s.t-off)//step*step+off
    starts=np.r_[0,np.nonzero(np.diff(b))[0]+1];ends=np.r_[starts[1:],len(b)]
    bt=b[starts]
    bars=dict(t=bt,close_t=bt+step,o=s.o[starts],h=np.maximum.reduceat(s.h,starts),l=np.minimum.reduceat(s.l,starts),
              c=s.c[ends-1])
    if len(bt) and s.t[-1]+M5<bt[-1]+step:  # data ended inside the last bar
        bars={k:v[:-1] for k,v in bars.items()}
    return bars

# ---------- indicators ----------
def atr_wilder(h,l,c,n=14):
    tr=np.maximum(h-l,np.maximum(np.abs(h-np.r_[np.nan,c[:-1]]),np.abs(l-np.r_[np.nan,c[:-1]])));tr[0]=h[0]-l[0]
    a=np.full(len(c),np.nan)
    if len(c)<n:return a
    a[n-1]=tr[:n].mean()
    for i in range(n,len(c)):a[i]=(a[i-1]*(n-1)+tr[i])/n
    return a

def roll_prev(x,n,fn):
    """fn over the previous n bars, excluding the current one."""
    out=np.full(len(x),np.nan)
    if len(x)<=n:return out
    v=np.lib.stride_tricks.sliding_window_view(x,n)  # v[k]=x[k:k+n]
    out[n:]=fn(v[:-1],axis=1)
    return out

def roll_incl(x,n,fn):
    out=np.full(len(x),np.nan)
    if len(x)<n:return out
    out[n-1:]=fn(np.lib.stride_tricks.sliding_window_view(x,n),axis=1)
    return out

def next_true(mask):
    """nt[k] = smallest j>=k with mask[j], else len(mask)."""
    n=len(mask);nt=np.full(n+1,n,dtype=np.int64)
    for k in range(n-1,-1,-1):nt[k]=k if mask[k] else nt[k+1]
    return nt

def signals(fam,bars):
    c=bars['c'];h=bars['h'];l=bars['l'];atr=atr_wilder(h,l,c)
    with np.errstate(invalid='ignore'):
        if fam=='T':
            hh20=roll_prev(h,20,np.max);ll20=roll_prev(l,20,np.min);hh10=roll_prev(h,10,np.max);ll10=roll_prev(l,10,np.min)
            long_in=c>hh20;short_in=c<ll20;exit_long=c<ll10;exit_short=c>hh10
        else:
            sma=roll_incl(c,20,np.mean);sd=roll_incl(c,20,np.std)
            long_in=c<sma-2*sd;short_in=c>sma+2*sd;exit_long=c>sma;exit_short=c<sma
    valid=~np.isnan(atr)
    return dict(atr=atr,long_in=long_in&valid,short_in=short_in&valid&~long_in,
                nt_exit={1:next_true(exit_long),-1:next_true(exit_short)})

# ---------- one trade ----------
def trade(s,bars,sig,fam,tf,i,direction,delay=0,dmult=None):
    """Enter on signal bar i. Returns dict or None (skipped). Exit is never before entry."""
    t_sig=bars['close_t'][i]+delay
    j0=int(np.searchsorted(s.t,t_sig,side='left'))
    if j0>=len(s.t):return None
    entry=s.o[j0];d=(dmult or DMULT[fam])*sig['atr'][i]
    if not np.isfinite(d) or d/entry<MIN_D:return None
    stop=entry-direction*d
    nb=len(bars['t']);k_first=int(np.searchsorted(bars['close_t'],s.t[j0],side='right'))  # first bar closing after entry
    k_rule=min(int(sig['nt_exit'][direction][min(k_first,nb)]),k_first+MAXHOLD[fam][tf]-1)
    if k_rule<nb:j_rule=int(np.searchsorted(s.t,bars['close_t'][k_rule],side='left'))
    else:j_rule=len(s.t)  # data ends: close at last 5m close
    seg=s.l[j0:j_rule] if direction==1 else s.h[j0:j_rule]
    hit=seg<=stop if direction==1 else seg>=stop
    if hit.any():
        j=j0+int(np.argmax(hit));gap=(s.o[j]<=stop) if direction==1 else (s.o[j]>=stop)
        exit_px=s.o[j] if gap else stop;exit_t=s.t[j];kind='stop'
    elif j_rule<len(s.t):exit_px=s.o[j_rule];exit_t=s.t[j_rule];kind='rule'
    else:exit_px=s.c[-1];exit_t=s.t[-1]+M5;kind='end'
    return dict(sym=s.name,i=i,dir=direction,entry_t=int(s.t[j0]),exit_t=int(exit_t),entry=float(entry),exit=float(exit_px),
                d=float(d),kind=kind,fund=float(s.funding_sum(s.t[j0],exit_t)))

def r_net(tr,mult=1.0,funding=True):
    e,x,d=tr['entry'],tr['exit'],tr['d']
    gross=tr['dir']*(x-e)/d
    cost=(e*(FEE+SLIP)+x*(FEE+SLIP+(STOP_EXTRA if tr['kind']=='stop' else 0)))*mult/d
    fund=-tr['dir']*tr['fund']*e/d if funding else 0.0
    return gross-cost+fund

# ---------- rule run ----------
def run_rule(s,bars,sig,fam,tf):
    trades=[];nxt=0;n=len(bars['t'])
    for i in range(n):
        if bars['close_t'][i]<nxt:continue
        if sig['long_in'][i]:direction=1
        elif sig['short_in'][i]:direction=-1
        else:continue
        tr=trade(s,bars,sig,fam,tf,i,direction)
        if tr is None:continue
        trades.append(tr);nxt=tr['exit_t']
    return trades

def in_period(tr,p):return p[0]<=tr['entry_t']<p[1]

# ---------- statistics ----------
def week_of(t):return (t-MON_OFFSET)//W

def bootstrap_p(trades,period,n_boot=5000,mean_block=4,seed=20261105):
    """One-sided p for mean R > 0: stationary block bootstrap over calendar weeks of entry (percentile method)."""
    w0=week_of(period[0]);w1=week_of(period[1]-1);nw=int(w1-w0+1)
    sr=np.zeros(nw);cnt=np.zeros(nw)
    for tr in trades:
        k=int(week_of(tr['entry_t'])-w0);sr[k]+=tr['r'];cnt[k]+=1
    if cnt.sum()==0:return 1.0
    rng=np.random.default_rng(seed);stats=np.empty(n_boot);p_new=1/mean_block
    for b in range(n_boot):
        idx=np.empty(nw,dtype=np.int64);idx[0]=rng.integers(nw)
        new=rng.random(nw)<p_new;starts=rng.integers(nw,size=nw)
        for k in range(1,nw):idx[k]=starts[k] if new[k] else (idx[k-1]+1)%nw
        n=cnt[idx].sum();stats[b]=sr[idx].sum()/n if n else 0.0
    return float((stats<=0).mean())

def summary(trades,key='r'):
    if not trades:return dict(n=0)
    r=np.array([t[key] for t in trades])
    hold=np.array([(t['exit_t']-t['entry_t'])/H for t in trades])
    return dict(n=len(r),mean_r=float(r.mean()),win_rate=float((r>0).mean()),median_hold_h=float(np.median(hold)),
                long_share=float(np.mean([t['dir']==1 for t in trades])),total_r=float(r.sum()))

def portfolio(trades,start=300.0,risk=0.005,max_open=3):
    ev=sorted(trades,key=lambda t:t['entry_t']);eq=start;open_=[];monthly={};peak=start;mdd=0.0;taken=0
    def close_until(t):
        nonlocal eq,peak,mdd
        for o in sorted([o for o in open_ if o[0]<=t]):
            eq+=o[1];m=str(np.datetime64(o[0],'ms').astype('datetime64[M]'));monthly[m]=monthly.get(m,0.0)+o[1]
            peak=max(peak,eq);mdd=max(mdd,1-eq/peak);open_.remove(o)
    for tr in ev:
        close_until(tr['entry_t'])
        if len(open_)>=max_open:continue
        open_.append((tr['exit_t'],eq*risk*tr['r']));taken+=1
    close_until(2**62)
    vals=list(monthly.values())
    return dict(start=start,end=eq,taken=taken,skipped=len(ev)-taken,max_drawdown=mdd,
                mean_month_usd=float(np.mean(vals)) if vals else 0.0,worst_month_usd=float(min(vals)) if vals else 0.0,
                months=len(vals))

# ---------- main ----------
def main(data,out):
    t0=time.time()
    prereg=Path(__file__).with_name('PREREG.md').read_bytes()
    syms=[load(data,p.stem) for p in sorted(Path(data).glob('*.npz'))]
    result=dict(prereg_sha256=hashlib.sha256(prereg).hexdigest(),symbols=[s.name for s in syms],variants={})
    rng=np.random.default_rng(1105)
    for fam,tf in DECISION+DESCRIPTIVE:
        key=f'{fam}_{tf}';per={};alltr=[]
        for s in syms:
            bars=resample(s,tf);sig=signals(fam,bars);trs=run_rule(s,bars,sig,fam,tf)
            for tr in trs:tr['r']=r_net(tr);tr['r_stress']=r_net(tr,2.0);tr['r_nofund']=r_net(tr,funding=False)
            per[s.name]=(bars,sig,trs);alltr+=trs
        disc=[t for t in alltr if in_period(t,DISC)];hold=[t for t in alltr if in_period(t,HOLD)]
        v=dict(discovery=summary(disc),holdout=summary(hold),holdout_stress=summary(hold,'r_stress'),
               holdout_nofund=summary(hold,'r_nofund'))
        if hold:
            fr=np.array([abs(-t['dir']*t['fund']*t['entry']/t['d']) for t in hold])
            cr=np.array([abs(t['r_nofund']-t['dir']*(t['exit']-t['entry'])/t['d']) for t in hold])
            v['funding_share_of_costs']=float(fr.sum()/(fr.sum()+cr.sum())) if fr.sum()+cr.sum() else 0.0
            dist=np.array([t['d']/t['entry'] for t in hold])
            v['liq_before_stop_share']={'10x':float((dist>1/10-0.005).mean()),'15x':float((dist>1/15-0.005).mean())}
            v['stop_distance_median_pct']=float(np.median(dist)*100)
            yrs={}
            for t in hold+disc:yrs.setdefault(str(np.datetime64(t['entry_t'],'ms').astype('datetime64[Y]')),[]).append(t['r'])
            v['mean_r_by_year']={y:dict(n=len(r),mean_r=float(np.mean(r))) for y,r in sorted(yrs.items())}
            v['exit_kinds']={k:sum(t['kind']==k for t in hold) for k in ('stop','rule','end')}
            v['portfolio_holdout']=portfolio(hold)
            # delayed entry
            dl=[]
            for t in hold:
                bars,sig,_=per[t['sym']];s=next(x for x in syms if x.name==t['sym'])
                tr=trade(s,bars,sig,fam,tf,t['i'],t['dir'],delay=DELAY[tf])
                if tr:tr['r']=r_net(tr);dl.append(tr)
            v['delayed']=summary(dl);v['delayed_ms']=DELAY[tf]
            if (fam,tf) in DECISION:
                v['bootstrap_p']=bootstrap_p(hold,HOLD)
                # random-entry control
                long_share=v['holdout']['long_share'];counts={}
                for t in hold:counts[t['sym']]=counts.get(t['sym'],0)+1
                pools={}
                for s in syms:
                    bars,sig,_=per[s.name];ok=np.nonzero((bars['close_t']>=HOLD[0])&(bars['close_t']<HOLD[1])&~np.isnan(sig['atr']))[0]
                    pools[s.name]=ok
                means=[]
                for rep in range(200):
                    rs=[]
                    for name,n in counts.items():
                        pool=pools[name]
                        if not len(pool):continue
                        bars,sig,_=per[name];s=next(x for x in syms if x.name==name)
                        for i in rng.choice(pool,size=n,replace=True):
                            tr=trade(s,bars,sig,fam,tf,int(i),1 if rng.random()<long_share else -1)
                            if tr:rs.append(r_net(tr))
                    means.append(float(np.mean(rs)) if rs else 0.0)
                means=np.array(means)
                v['random']=dict(reps=len(means),mean_of_means=float(means.mean()),p95=float(np.percentile(means,95)),
                                 p_random=float((1+(means>=v['holdout']['mean_r']).sum())/(len(means)+1)))
                checks={'1_trades_ge_150':v['holdout']['n']>=150,
                        '2_mean_r_pos_p_lt_alpha':v['holdout']['mean_r']>0 and v['bootstrap_p']<ALPHA,
                        '3_stress_mean_r_pos':v['holdout_stress']['mean_r']>0,
                        '4_p_random_lt_0.05':v['random']['p_random']<0.05,
                        '5_delayed_mean_r_pos':v['delayed'].get('mean_r',-1)>0,
                        '6_discovery_mean_r_pos':v['discovery'].get('mean_r',-1)>0}
                v['checks']=checks;v['passed']=all(checks.values())
        result['variants'][key]=v
        print(key,json.dumps({k:v.get(k) for k in ('holdout','bootstrap_p','passed')}),f'{time.time()-t0:.0f}s',flush=True)
    result['seconds']=time.time()-t0
    Path(out).write_text(json.dumps(result,indent=1),encoding='utf-8')

if __name__=='__main__':main(sys.argv[1],sys.argv[2])
