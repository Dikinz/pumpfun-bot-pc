"""Download Binance USDT-M futures 5m klines and funding rates for the pre-registered universe (public archive, no key).

Usage: python -I fetch.py OUT_DIR
Writes OUT_DIR/<SYMBOL>.npz (t_ms int64, o,h,l,c float64 for 5m bars; ft_ms int64, fr float64 for funding) and
OUT_DIR/manifest.json with per-file SHA-256 (verified against Binance .CHECKSUM) and coverage.
"""
import csv,hashlib,io,json,sys,time,urllib.request,urllib.error,zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np

BASE='https://data.binance.vision/data/futures/um/monthly'
UNIVERSE=['BTC','ETH','BNB','SOL','ADA','XRP','LUNA','DOT','AVAX','DOGE','1000SHIB','MATIC','LTC','LINK','UNI','ALGO',
          'BCH','TRX','ATOM','NEAR']
MONTHS=[f'{y}-{m:02d}' for y in range(2021,2027) for m in range(1,13) if '2021-10'<=f'{y}-{m:02d}'<='2026-10']
UA={'User-Agent':'paper-research-backtest/1.0'}

def get(url,limit=64*1024*1024):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=60) as r:return r.read(limit)
        except urllib.error.HTTPError as e:
            if e.code==404:return None
            err=e
        except Exception as e:err=e
        time.sleep(2*(attempt+1))
    raise RuntimeError(f'FETCH_FAILED {url} {type(err).__name__}')

def rows_of(blob):
    z=zipfile.ZipFile(io.BytesIO(blob));name=z.namelist()[0]
    for r in csv.reader(io.TextIOWrapper(z.open(name),encoding='utf-8')):
        if r and r[0][:1].isdigit():yield r

def one(kind,sym,month):
    sub='klines/'+sym+'/5m/'+sym+'-5m-'+month if kind=='k' else 'fundingRate/'+sym+'/'+sym+'-fundingRate-'+month
    url=f'{BASE}/{sub}.zip';blob=get(url)
    if blob is None:return kind,sym,month,None,None
    digest=hashlib.sha256(blob).hexdigest();chk=get(url+'.CHECKSUM')
    if chk is not None and chk.split()[0].decode()!=digest:raise RuntimeError(f'CHECKSUM_MISMATCH {url}')
    if kind=='k':arr=np.array([[int(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4])] for r in rows_of(blob)],dtype=np.float64).reshape(-1,5)
    else:arr=np.array([[int(r[0]),float(r[2])] for r in rows_of(blob)],dtype=np.float64).reshape(-1,2)
    if len(arr) and arr[:,0].max()>1e14:arr[:,0]=np.floor(arr[:,0]/1000)  # microsecond timestamps -> ms
    return kind,sym,month,arr,dict(sha256=digest,checksum_verified=chk is not None)

def main(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);manifest={}
    jobs=[(k,c+'USDT',m) for c in UNIVERSE for k in ('k','f') for m in MONTHS]
    got={}
    with ThreadPoolExecutor(6) as ex:
        for i,(kind,sym,month,rows,meta) in enumerate(ex.map(lambda j:one(*j),jobs)):
            got[(kind,sym,month)]=(rows,meta)
            if i%100==0:print(i,len(jobs),flush=True)
    for c in UNIVERSE:
        sym=c+'USDT';k=[];f=[];files={};missing=dict(k=[],f=[])
        for m in MONTHS:
            for kind,acc in (('k',k),('f',f)):
                rows,meta=got[(kind,sym,m)]
                if rows is None:missing[kind].append(m);continue
                files[f'{kind}:{m}']=meta;acc.append(rows)
        ka=np.concatenate(k) if k else np.zeros((0,5))
        if len(ka):
            ka=ka[np.argsort(ka[:,0],kind='stable')];ka=ka[np.r_[True,np.diff(ka[:,0])>0]]
        fa=sorted({int(a):float(b) for a,b in (np.concatenate(f) if f else np.zeros((0,2)))}.items())
        np.savez(out/(sym+'.npz'),t_ms=ka[:,0].astype(np.int64),o=ka[:,1],h=ka[:,2],l=ka[:,3],c=ka[:,4],
                 ft_ms=np.array([a for a,_ in fa],dtype=np.int64),fr=np.array([b for _,b in fa],dtype=np.float64))
        gaps=int((np.diff(ka[:,0])!=300000).sum()) if len(ka)>1 else 0
        manifest[sym]=dict(bars=len(ka),first=int(ka[0,0]) if len(ka) else None,last=int(ka[-1,0]) if len(ka) else None,
                           gaps=gaps,funding=len(fa),missing_kline_months=missing['k'],missing_funding_months=missing['f'],
                           npz_sha256=hashlib.sha256((out/(sym+'.npz')).read_bytes()).hexdigest(),files=files)
        print(sym,len(ka),'bars',len(fa),'funding','gaps',gaps,'missing',missing,flush=True)
    (out/'manifest.json').write_text(json.dumps(dict(base=BASE,fetched_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        symbols=manifest),indent=1),encoding='utf-8')

if __name__=='__main__':main(sys.argv[1])
