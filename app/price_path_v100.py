"""At most 200 one-second price bars per pending exit; zero callback IO."""
import math
LIMIT=200
PATHS={}

def begin(tid,wall,mono,process):
    PATHS[tid]=dict(process=process,signal_wall=wall,origin_mono=mono,bars={},evicted_bars=0,observed=0)

def event(tid,wall,mono,price,source,basis,process):
    path=PATHS.get(tid)
    if not path or path['process']!=process or not all(type(v) in (int,float) and math.isfinite(v) for v in (wall,mono,price)) or price<=0:return
    elapsed=mono-path['origin_mono']
    if elapsed<0:return
    second=int(elapsed);bars=path['bars'];bar=bars.get(second)
    if bar is None:
        if len(bars)>=LIMIT:bars.pop(next(iter(bars)));path['evicted_bars']+=1
        bars[second]=dict(second=second,first_ts=wall,last_ts=wall,open=price,high=price,low=price,close=price,n=1,source=source,basis=basis)
    else:
        bar.update(last_ts=wall,high=max(bar['high'],price),low=min(bar['low'],price),close=price,n=bar['n']+1)
        if source!=bar['source'] or basis!=bar['basis']:bar.update(source='MIXED',basis='MIXED')
    path['observed']+=1

def snapshot(tid,process):
    p=PATHS.get(tid)
    if not p or p['process']!=process:return dict(available=False,reason='NO_CURRENT_PROCESS_PATH')
    return dict(schema='PRICE_PATH_V100',available=True,signal_wall=p['signal_wall'],observed=p['observed'],evicted_bars=p['evicted_bars'],bars=[dict(b) for b in p['bars'].values()],
        note='Accepted provider prices after signal, one-second OHLC by monotonic elapsed time. Silence is missing data; no interpolation or proof of executable reserves. Process restart starts a new evidence segment.')
