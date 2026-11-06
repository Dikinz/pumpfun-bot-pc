"""Download Binance spot daily klines for the pre-registered universe (public endpoint, no key).

Usage: python -I fetch.py OUT_DIR
Writes OUT_DIR/<SYMBOL>.csv (open_time_ms,open,high,low,close,volume) and OUT_DIR/manifest.json with SHA-256.
"""
import csv,hashlib,json,sys,time,urllib.request,urllib.parse
from pathlib import Path

ENDPOINT='https://data-api.binance.vision/api/v3/klines'
UNIVERSE=['BTC','ETH','XRP','BCH','LTC','EOS','BNB','XTZ','XLM','ADA','TRX','LINK','ETC','NEO','ATOM']
START_MS=int(time.mktime(time.strptime('2019-08-01','%Y-%m-%d')))*1000  # 100+ days of warm-up before 2020-01-01
END_MS=int(time.mktime(time.strptime('2026-11-01','%Y-%m-%d')))*1000

def get(params):
    url=ENDPOINT+'?'+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={'User-Agent':'paper-research-backtest/1.0'})
    with urllib.request.urlopen(req,timeout=20) as r:
        data=json.loads(r.read(8*1024*1024))
    if not isinstance(data,list):raise RuntimeError('UNEXPECTED_RESPONSE')
    return data

def fetch(symbol):
    rows=[];start=START_MS
    while start<END_MS:
        batch=get(dict(symbol=symbol,interval='1d',startTime=start,endTime=END_MS-1,limit=1000))
        if not batch:break
        for k in batch:rows.append([int(k[0]),k[1],k[2],k[3],k[4],k[5]])
        start=int(batch[-1][0])+86400000
        time.sleep(.3)
    return rows

def main(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);manifest={}
    for coin in UNIVERSE:
        sym=coin+'USDT';rows=fetch(sym);path=out/(sym+'.csv')
        with path.open('w',newline='',encoding='utf-8') as f:
            w=csv.writer(f);w.writerow(['open_time_ms','open','high','low','close','volume']);w.writerows(rows)
        manifest[sym]=dict(rows=len(rows),first=rows[0][0] if rows else None,last=rows[-1][0] if rows else None,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        print(sym,len(rows),flush=True)
    (out/'manifest.json').write_text(json.dumps(dict(endpoint=ENDPOINT,fetched_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),files=manifest),indent=2),encoding='utf-8')

if __name__=='__main__':main(sys.argv[1])
