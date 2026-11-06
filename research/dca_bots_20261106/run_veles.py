"""Run the pre-registered Veles-style DCA variants (PREREG.md) on the futures_rules_20261105 data.

Usage: python -I run_veles.py DATA_DIR OUT_JSON
"""
import hashlib,json,sys,time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))  # -I does not add the script directory
import dca

M5=300_000;D=86_400_000
def ms(s):return int(np.datetime64(s,'ms').astype(np.int64))
YEARS=[(y,ms(f'{y}-01-01'),ms(f'{y+1}-01-01') if y<2026 else ms('2026-11-01')) for y in range(2022,2027)]
FULL=(ms('2022-01-01'),ms('2026-11-01'))
CAPITAL=300.0
FUT=dict(fee_taker=0.00055,fee_maker=0.0002,slip=0.0003)
SPOT=dict(fee_taker=0.001,fee_maker=0.001,slip=0.0003,funding=False)

VARIANTS={
 'V1_dollar_bot_20x':dict(dep=30,lev=20,n=5,ov=0.15,mart=0.20,tp=0.0075,sl=0.0,filt='v1',kw=FUT),
 'V2_x7_grid':dict(dep=30,lev=7,n=5,ov=0.10,mart=0.20,tp=0.005,sl=0.03,filt='v2',kw=FUT),
 'V3_conservative_4x':dict(dep=100,lev=4,n=4,ov=0.25,mart=0.05,tp=0.015,sl=0.0,filt=None,kw=FUT),
 'V4_grid_hard_stop_5x':dict(dep=30,lev=5,n=5,ov=0.15,mart=0.20,tp=0.01,sl=0.05,filt=None,kw=FUT),
 'V5_spot':dict(dep=300,lev=1,n=5,ov=0.15,mart=0.20,tp=0.01,sl=0.0,filt=None,kw=SPOT),
}

def cfg_of(v,entry_ok=None):
    total=v['dep']*v['lev'];k=np.array([(1+v['mart'])**i for i in range(v['n'])]);v0=total/k.sum()
    return dca.Cfg(direction=1,base=v0,so_first=v0*(1+v['mart']),n_so=v['n']-1,step=v['ov']/(v['n']-1),step_scale=1.0,
                   vol_scale=1+v['mart'],tp=v['tp'],sl=v['sl'],sl_mode='beyond_last',leverage=float(v['lev']),
                   entry_ok=entry_ok,**v['kw'])

# ---------- indicators on a higher timeframe, mapped to 5m bars ----------
def bars(t,o,h,l,c,step):
    b=t//step*step;st=np.r_[0,np.nonzero(np.diff(b))[0]+1];en=np.r_[st[1:],len(b)]
    return b[st]+step,np.maximum.reduceat(h,st),np.minimum.reduceat(l,st),c[en-1]

def wilder(x,n):
    out=np.full(len(x),np.nan)
    if len(x)<n:return out
    out[n-1]=np.nanmean(x[:n])
    for i in range(n,len(x)):out[i]=(out[i-1]*(n-1)+x[i])/n
    return out

def rsi(c,n=14):
    d=np.diff(c,prepend=c[0]);g=wilder(np.maximum(d,0),n);l=wilder(np.maximum(-d,0),n)
    with np.errstate(divide='ignore',invalid='ignore'):return 100-100/(1+g/l)

def cci(h,l,c,n=20):
    tp=(h+l+c)/3;out=np.full(len(c),np.nan)
    w=np.lib.stride_tricks.sliding_window_view(tp,n);sma=w.mean(1);md=np.abs(w-sma[:,None]).mean(1)
    with np.errstate(divide='ignore',invalid='ignore'):out[n-1:]=(tp[n-1:]-sma)/(0.015*md)
    return out

def adx(h,l,c,n=14):
    up=np.diff(h,prepend=h[0]);dn=-np.diff(l,prepend=l[0])
    pdm=np.where((up>dn)&(up>0),up,0.0);ndm=np.where((dn>up)&(dn>0),dn,0.0)
    pc=np.r_[c[0],c[:-1]];tr=np.maximum(h-l,np.maximum(abs(h-pc),abs(l-pc)))
    atr=wilder(tr,n)
    with np.errstate(divide='ignore',invalid='ignore'):
        pdi=100*wilder(pdm,n)/atr;ndi=100*wilder(ndm,n)/atr;dx=100*abs(pdi-ndi)/(pdi+ndi)
    dx=np.nan_to_num(dx,nan=0.0);a=np.full(len(c),np.nan);s=2*n-1
    if len(c)>s:
        a[s-1]=dx[n-1:s].mean()
        for i in range(s,len(c)):a[i]=(a[i-1]*(n-1)+dx[i])/n
    return a

def on_5m(t,close_t,cond):
    k=np.searchsorted(close_t,t,side='right')-1
    return np.where(k>=0,cond[np.maximum(k,0)],False)

def entry_filter(name,t,o,h,l,c):
    if name is None:return None
    m15=bars(t,o,h,l,c,15*60_000);m30=bars(t,o,h,l,c,30*60_000)
    with np.errstate(invalid='ignore'):
        if name=='v1':
            ct,hh,ll,cc=m15;cond=(rsi(cc)<30)&(cci(hh,ll,cc)<-100);return on_5m(t,ct,cond)
        ct,hh,ll,cc=m30;c30=(rsi(cc)<30)&(cci(hh,ll,cc)<-100)
        ct15,h15,l15,c15=m15;a15=adx(h15,l15,c15)>40
        return on_5m(t,ct,c30)&on_5m(t,ct15,a15)

# ---------- one run ----------
def veles_fee(cycle_log):
    by={}
    for t,pnl,_,kind in cycle_log:
        if kind=='tp' and pnl>0:
            m=str(np.datetime64(t,'ms').astype('datetime64[M]'));by[m]=by.get(m,0.0)+0.2*pnl
    capped={m:min(v,50.0) for m,v in by.items()}
    return sum(capped.values()),sum(1 for v in by.values() if v>=50.0)

def one(data,sym,vname,window,funding=True):
    z=np.load(Path(data)/(sym+'.npz'));t,o,h,l,c,ft,fr=(z[k] for k in ('t_ms','o','h','l','c','ft_ms','fr'))
    v=VARIANTS[vname];cfg=cfg_of(v,entry_filter(v['filt'],t,o,h,l,c))
    if not funding:cfg.funding=False
    i0=int(np.searchsorted(t,window[0]));i1=int(np.searchsorted(t,window[1]))
    if i1-i0<288*30:return None  # under 30 days of data in the window
    r=dca.run(t,o,h,l,c,ft,fr,cfg,CAPITAL,start_ms=window[0],end_ms=window[1])
    fee,capped=veles_fee(r.cycle_log)
    end=max(r.equity-fee,0.0) if r.liquidated_t is None else 0.0
    deals=[x for x in r.cycle_log if x[3] in ('tp','sl','liq')]
    daily=np.array([x[1] for x in r.daily]) if r.daily else np.array([CAPITAL])
    peak=np.maximum.accumulate(np.r_[CAPITAL,daily]);dd=float(np.max(1-np.r_[CAPITAL,daily]/peak))
    bh=(c[i1-1]/o[i0])*(1-0.001)**2-1
    return dict(sym=sym,variant=vname,start=int(t[i0]),end_t=int(t[i1-1]),funding=funding,equity_before_fee=float(r.equity),
                veles_fee=float(fee),fee_cap_months=capped,end=float(end),ret=float(end/CAPITAL-1),
                liquidated=r.liquidated_t is not None,liq_t=r.liquidated_t,deals=len(deals),
                wins=sum(1 for x in deals if x[3]=='tp'),stops=sum(1 for x in deals if x[3]=='sl'),
                full_grid=sum(1 for x in deals if x[2]==cfg.n_so),open_at_end=any(x[3]=='end' for x in r.cycle_log),
                max_dd_daily=dd,min_equity_intrabar=float(r.min_equity),buy_hold=float(bh))

def job(args):return one(*args)

def q(a,p):return float(np.percentile(a,p)) if len(a) else None

def main(data,out):
    t0=time.time();syms=sorted(p.stem for p in Path(data).glob('*.npz'))
    jobs=[(data,s,v,(a,b),True) for v in VARIANTS for s in syms for _,a,b in YEARS]
    jobs+=[(data,s,v,FULL,f) for v in VARIANTS for s in syms for f in (True,False)]
    with ProcessPoolExecutor(4) as ex:res=[r for r in ex.map(job,jobs,chunksize=2) if r]
    prereg=Path(__file__).with_name('PREREG.md').read_bytes()
    result=dict(prereg_sha256=hashlib.sha256(prereg).hexdigest(),capital=CAPITAL,variants={},runs=res)
    for v in VARIANTS:
        cy=[r for r in res if r['variant']==v and r['funding'] and (r['end_t']-r['start'])<370*D]
        full=[r for r in res if r['variant']==v and r['funding'] and (r['end_t']-r['start'])>=370*D]
        nof=[r for r in res if r['variant']==v and not r['funding']]
        rets=np.array([r['ret'] for r in cy]);hold=np.array([r['ret'] for r in cy if r['start']>=ms('2025-01-01')])
        bad=np.mean([r['liquidated'] or r['ret']<=-0.5 for r in cy])
        fe=np.array([r['end'] for r in full])
        best=max(cy,key=lambda r:r['ret']);worst=min(cy,key=lambda r:r['ret'])
        by_year={}
        for y,a,b in YEARS:
            rr=[r for r in cy if a<=r['start']<b]
            by_year[y]=dict(n=len(rr),median=q([r['ret'] for r in rr],50),mean=float(np.mean([r['ret'] for r in rr])) if rr else None,
                            liquidated=sum(r['liquidated'] for r in rr),profit_share=float(np.mean([r['ret']>0 for r in rr])) if rr else None,
                            buy_hold_median=q([r['buy_hold'] for r in rr],50))
        checks={'1_median_gt_0':bool(np.median(rets)>0),'2_mean_gt_0':bool(rets.mean()>0),'3_bad_le_5pct':bool(bad<=0.05),
                '4_2025_2026_median_and_mean_gt_0':bool(len(hold) and np.median(hold)>0 and hold.mean()>0),
                '5_full_median_end_gt_300':bool(np.median(fe)>CAPITAL)}
        result['variants'][v]=dict(
            coin_years=len(cy),ret_mean=float(rets.mean()),ret_q=[q(rets,p) for p in (10,25,50,75,90)],profit_share=float((rets>0).mean()),
            bad_share=float(bad),liquidated=int(sum(r['liquidated'] for r in cy)),
            best=dict(sym=best['sym'],start=best['start'],ret=best['ret']),worst=dict(sym=worst['sym'],start=worst['start'],ret=worst['ret']),
            holdout_mean=float(hold.mean()) if len(hold) else None,holdout_median=q(hold,50),by_year=by_year,
            buy_hold_median=q([r['buy_hold'] for r in cy],50),
            full=dict(n=len(full),median_end=q(fe,50),mean_end=float(fe.mean()),liquidated=int(sum(r['liquidated'] for r in full)),
                      ends={r['sym']:round(r['end'],2) for r in full},
                      median_end_without_funding=q([r['end'] for r in nof],50),
                      veles_fee_total_median=q([r['veles_fee'] for r in full],50)),
            deal_win_rate=float(sum(r['wins'] for r in cy)/max(1,sum(r['deals'] for r in cy))),
            deals=int(sum(r['deals'] for r in cy)),stops=int(sum(r['stops'] for r in cy)),full_grid=int(sum(r['full_grid'] for r in cy)),
            checks=checks,earns_robustly=all(checks.values()))
        x=result['variants'][v]
        print(v,'ROBUST' if x['earns_robustly'] else 'NOT ROBUST',json.dumps(dict(checks=checks,ret_q=x['ret_q'],mean=x['ret_mean'],
              bad=x['bad_share'],liq=x['liquidated'],full=x['full']['median_end'])),flush=True)
    result['seconds']=time.time()-t0
    Path(out).write_text(json.dumps(result,indent=1),encoding='utf-8')

if __name__=='__main__':main(sys.argv[1],sys.argv[2])
