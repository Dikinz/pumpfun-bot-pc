"""Pre-registered DCA search (PREREG_SEARCH.md): select on 2025, check top 3 on 2026.

Usage: python -I search.py DATA_DIR search OUT_SEARCH_JSON
       python -I search.py DATA_DIR check  SEARCH_JSON OUT_CHECK_JSON
"""
import hashlib,itertools,json,sys,time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))
import dca
from run_veles import rsi,wilder

D=86_400_000;H=3_600_000;CAP=300.0
def ms(s):return int(np.datetime64(s,'ms').astype(np.int64))
SEARCH=(ms('2025-01-01'),ms('2026-01-01'));CHECK=(ms('2026-01-01'),ms('2026-11-01'))
STRESS={y:(ms(f'{y}-01-01'),ms(f'{y+1}-01-01')) for y in (2022,2023,2024)}
EXCLUDE={'LUNAUSDT','MATICUSDT'}
GRID=[dict(mode=a,k=b,ov=c,mart=d,tp=e,sl=f,filt=g) for a,b,c,d,e,f,g in itertools.product(
    ('L','S','R'),(0.5,1.0,2.0,3.0),(0.10,0.25),(0.0,0.25),(0.005,0.01,0.02),(0.0,0.05),(False,True))]
N_ORDERS=5

def key(g):return f"{g['mode']}_k{g['k']}_ov{g['ov']}_m{g['mart']}_tp{g['tp']}_sl{g['sl']}_f{int(g['filt'])}"

def start_dirs(t,c):
    """start_dir arrays for (mode, filter) from the last completed daily and 1h bars."""
    day=t//D*D;st=np.r_[0,np.nonzero(np.diff(day))[0]+1];en=np.r_[st[1:],len(day)]
    dclose=c[en-1];dct=day[st]+D
    sma=np.full(len(dclose),np.nan)
    if len(dclose)>=50:sma[49:]=np.convolve(dclose,np.ones(50)/50,'valid')
    kd=np.searchsorted(dct,t,side='right')-1
    reg=np.zeros(len(t),dtype=np.int8);ok=kd>=0
    with np.errstate(invalid='ignore'):
        up=np.where(ok,dclose[np.maximum(kd,0)]>sma[np.maximum(kd,0)],False)
        dn=np.where(ok,dclose[np.maximum(kd,0)]<=sma[np.maximum(kd,0)],False)
    reg[up]=1;reg[dn]=-1
    hr=t//H*H;st=np.r_[0,np.nonzero(np.diff(hr))[0]+1];en=np.r_[st[1:],len(hr)]
    r=rsi(c[en-1]);kh=np.searchsorted(hr[st]+H,t,side='right')-1
    with np.errstate(invalid='ignore'):
        rv=np.where(kh>=0,r[np.maximum(kh,0)],np.nan);lo=rv<30;hi=rv>70
    one=np.ones(len(t),dtype=np.int8)
    return {('L',False):one,('S',False):-one,('R',False):reg,
            ('L',True):np.where(lo,1,0).astype(np.int8),('S',True):np.where(hi,-1,0).astype(np.int8),
            ('R',True):np.where((reg==1)&lo,1,np.where((reg==-1)&hi,-1,0)).astype(np.int8)}

def cfg_of(g,sd):
    total=g['k']*CAP;w=np.array([(1+g['mart'])**i for i in range(N_ORDERS)]);v0=total/w.sum()
    return dca.Cfg(base=v0,so_first=v0*(1+g['mart']),n_so=N_ORDERS-1,step=g['ov']/(N_ORDERS-1),vol_scale=1+g['mart'],
                   tp=g['tp'],sl=g['sl'],sl_mode='beyond_last',leverage=10.0,start_dir=sd)

def load(data,sym):
    z=np.load(Path(data)/(sym+'.npz'));return tuple(z[k] for k in ('t_ms','o','h','l','c','ft_ms','fr'))

def run_one(arrs,sds,g,window,keep_daily=False):
    t,o,h,l,c,ft,fr=arrs
    r=dca.run(t,o,h,l,c,ft,fr,cfg_of(g,sds[(g['mode'],g['filt'])]),CAP,start_ms=window[0],end_ms=window[1])
    out=dict(ret=float(r.equity/CAP-1),liq=r.liquidated_t is not None,deals=sum(1 for x in r.cycle_log if x[3]!='end'),
             stops=sum(1 for x in r.cycle_log if x[3]=='sl'),min_eq=float(r.min_equity))
    if keep_daily:out['daily']=r.daily
    return out

def search_coin(args):
    data,sym=args;arrs=load(data,sym);sds=start_dirs(arrs[0],arrs[4])
    return sym,{key(g):run_one(arrs,sds,g,SEARCH) for g in GRID}

def check_coin(args):
    data,sym,gs=args;arrs=load(data,sym);sds=start_dirs(arrs[0],arrs[4]);out={}
    for g in gs:
        out[key(g)]=dict(check=run_one(arrs,sds,g,CHECK,keep_daily=True),
                         stress={y:run_one(arrs,sds,g,w) for y,w in STRESS.items()})
    return sym,out

def bootstrap_p(x,n_boot=5000,mean_block=10,seed=20261106):
    rng=np.random.default_rng(seed);n=len(x);stats=np.empty(n_boot)
    for b in range(n_boot):
        idx=np.empty(n,dtype=np.int64);idx[0]=rng.integers(n);new=rng.random(n)<1/mean_block;st=rng.integers(n,size=n)
        for i in range(1,n):idx[i]=st[i] if new[i] else (idx[i-1]+1)%n
        stats[b]=x[idx].mean()
    return float((stats<=0).mean())

def syms_of(data):return sorted(p.stem for p in Path(data).glob('*.npz') if p.stem not in EXCLUDE)

def sha(name):return hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()

def do_search(data,out):
    t0=time.time();syms=syms_of(data)
    with ProcessPoolExecutor(10) as ex:res=dict(ex.map(search_coin,[(data,s) for s in syms]))
    table=[]
    for g in GRID:
        k=key(g);rows=[res[s][k] for s in syms];r=np.array([x['ret'] for x in rows])
        table.append(dict(key=k,cfg=g,median=float(np.median(r)),mean=float(r.mean()),worst=float(r.min()),
                          liq=int(sum(x['liq'] for x in rows)),eligible=bool(not any(x['liq'] for x in rows) and r.min()>-0.5),
                          deals=int(sum(x['deals'] for x in rows))))
    elig=sorted([x for x in table if x['eligible']],key=lambda x:(-x['median'],-x['mean']))
    top=elig[:3]
    Path(out).write_text(json.dumps(dict(prereg_sha256=sha('PREREG_SEARCH.md'),syms=syms,n_configs=len(GRID),
        n_eligible=len(elig),top3=top,table=table,per_coin=res,seconds=time.time()-t0),indent=1),encoding='utf-8')
    print('eligible',len(elig),'of',len(GRID));[print(x['key'],'median %+.1f%% mean %+.1f%% worst %+.1f%%'%(100*x['median'],100*x['mean'],100*x['worst'])) for x in top]

def do_check(data,search_json,out):
    t0=time.time();s=json.loads(Path(search_json).read_text(encoding='utf-8'));gs=[x['cfg'] for x in s['top3']];syms=s['syms']
    with ProcessPoolExecutor(10) as ex:res=dict(ex.map(check_coin,[(data,sym,gs) for sym in syms]))
    verdict={}
    for g in gs:
        k=key(g);rows=[res[sym][k]['check'] for sym in syms];r=np.array([x['ret'] for x in rows])
        days=sorted(set(d for x in rows for d,_ in x['daily']))
        val={d:0.0 for d in days}
        for x in rows:
            last=CAP;series=dict(x['daily'])
            for d in days:last=series.get(d,last);val[d]+=last
        v=np.r_[CAP*len(rows),[val[d] for d in days]];dr=np.diff(v)/v[:-1]
        p=bootstrap_p(dr);port=v[-1]/v[0]-1;ann=(1+port)**(365/len(dr))-1 if len(dr) else 0.0
        st={y:dict(median=float(np.median([res[sym][k]['stress'][y]['ret'] for sym in syms])),
                   mean=float(np.mean([res[sym][k]['stress'][y]['ret'] for sym in syms])),
                   liq=int(sum(res[sym][k]['stress'][y]['liq'] for sym in syms)),
                   worst=float(min(res[sym][k]['stress'][y]['ret'] for sym in syms))) for y in STRESS}
        insample=next(x for x in s['top3'] if x['key']==k)
        checks={'1_median_gt_0':bool(np.median(r)>0),'2_mean_gt_0':bool(r.mean()>0),
                '3_no_liq_worst_gt_-50':bool(not any(x['liq'] for x in rows) and r.min()>-0.5),
                '4_portfolio_p_lt_0.0167':bool(dr.mean()>0 and p<0.05/3),'5_annualized_gt_5pct':bool(ann>0.05)}
        verdict[k]=dict(cfg=g,in_sample_2025=dict(median=insample['median'],mean=insample['mean'],worst=insample['worst']),
            check_2026=dict(median=float(np.median(r)),mean=float(r.mean()),worst=float(r.min()),best=float(r.max()),
                            liq=int(sum(x['liq'] for x in rows)),profit_share=float((r>0).mean()),portfolio_return=float(port),
                            annualized=float(ann),bootstrap_p=p,max_dd=float(np.max(1-v/np.maximum.accumulate(v))),
                            per_coin={sym:round(res[sym][k]['check']['ret'],4) for sym in syms}),
            stress=st,checks=checks,passed=all(checks.values()))
        print(k,'PASSED' if verdict[k]['passed'] else 'FAILED',json.dumps(verdict[k]['check_2026']['median']),checks,flush=True)
    Path(out).write_text(json.dumps(dict(prereg_sha256=sha('PREREG_SEARCH.md'),verdict=verdict,seconds=time.time()-t0),indent=1),encoding='utf-8')

if __name__=='__main__':
    if sys.argv[2]=='search':do_search(sys.argv[1],sys.argv[3])
    else:do_check(sys.argv[1],sys.argv[3],sys.argv[4])
