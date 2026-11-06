"""DCA ("усреднение") bot simulator on 5m bars: base order, safety orders, take profit from the average price,
optional stop loss, leverage with liquidation, funding, maker/taker fees. One bot per coin with its own allocated
capital (isolated margin = the whole allocation). Pure python/numpy, no network.

Intrabar order is conservative: within one 5m bar, for a long bot the high is processed first (take profit against
the average price *before* this bar's safety orders), then the low (safety orders, stop loss, liquidation). A new
cycle starts at the next bar's open. Short bots mirror this.
"""
from dataclasses import dataclass,field
import numpy as np

@dataclass
class Cfg:
    direction:int=1            # 1 long, -1 short
    base:float=10.0            # base order notional, USDT
    so_first:float=10.0        # first safety order notional, USDT
    n_so:int=5                 # number of safety orders
    step:float=0.01            # deviation of the first safety order from the base entry price
    step_scale:float=1.0       # each next step = previous step x step_scale (cumulative deviation from base entry)
    vol_scale:float=1.0        # each next safety order notional = previous x vol_scale
    tp:float=0.01              # take profit from the average entry price
    sl:float=0.0               # stop loss, 0 = none
    sl_mode:str='avg'          # 'avg': from the average price, always active; 'beyond_last': Veles simple stop,
                               # (last order deviation + sl) from the first order price, active after the full grid
    leverage:float=1.0         # 1 = spot-like, no liquidation
    fee_taker:float=0.00055
    fee_maker:float=0.0002
    slip:float=0.0003          # on market orders (base entry, stop loss)
    mm:float=0.005             # maintenance margin rate for liquidation
    funding:bool=True
    entry_ok:object=None       # optional bool array over 5m bars: a new cycle may start only where True
    start_dir:object=None      # optional int array over 5m bars: direction of a deal starting there (1/-1), 0 = wait;
                               # overrides direction and entry_ok

    def levels(self):
        dev=[];d=0.0;s=self.step
        for _ in range(self.n_so):d+=s;dev.append(d);s*=self.step_scale
        size=[self.so_first*self.vol_scale**i for i in range(self.n_so)]
        return dev,size
    def max_notional(self):
        return self.base+sum(self.levels()[1])

@dataclass
class Result:
    equity:float
    cycles:int=0
    wins:int=0
    stops:int=0
    liquidated_t:int=None
    max_so_hits:int=0
    min_equity:float=None
    daily:list=field(default_factory=list)   # (day_ms, mark-to-market equity at day end)
    cycle_log:list=field(default_factory=list)  # (close_t, pnl, n_so_filled, kind)

def run(t,o,h,l,c,ft,fr,cfg,capital,start_ms=None,end_ms=None):
    """Simulate one bot. Returns Result. Prices are arrays over 5m bars; ft/fr funding times and rates."""
    D=86_400_000;dirn=cfg.direction;dev,size=cfg.levels()
    i0=0 if start_ms is None else int(np.searchsorted(t,start_ms))
    i1=len(t) if end_ms is None else int(np.searchsorted(t,end_ms))
    eq=capital;res=Result(equity=capital,min_equity=capital)
    fi=int(np.searchsorted(ft,t[i0] if i0<len(t) else 0,side='left'))
    qty=0.0;cost=0.0;entry0=0.0;nfill=0;in_pos=False
    def avg():return cost/qty
    i=i0
    while i<i1:
        # funding settles at the start of this bar if a funding time falls inside (t[i-1], t[i]]
        while fi<len(ft) and ft[fi]<=t[i]:
            if in_pos and cfg.funding and ft[fi]>t[i0]:eq-=dirn*fr[fi]*qty*o[i]
            fi+=1
        if not in_pos:
            if eq<=0:break
            if cfg.start_dir is not None:
                if cfg.start_dir[i]==0:
                    _day(res,t,i,eq,D);i+=1;continue
                dirn=int(cfg.start_dir[i])
            elif cfg.entry_ok is not None and not cfg.entry_ok[i]:
                _day(res,t,i,eq,D);i+=1;continue
            px=o[i]*(1+dirn*cfg.slip)
            if cfg.base/cfg.leverage>eq:break  # cannot afford the base order
            qty=cfg.base/px;cost=cfg.base;entry0=px;nfill=0;in_pos=True
            eq-=cfg.base*cfg.fee_taker
        fav=h[i] if dirn==1 else l[i];adv=l[i] if dirn==1 else h[i]
        # 1) take profit against the current average
        tp_px=avg()*(1+dirn*cfg.tp)
        if (fav>=tp_px) if dirn==1 else (fav<=tp_px):
            fill=tp_px  # resting limit order: filled at its own price, never better
            pnl=dirn*(fill*qty-cost)-fill*qty*cfg.fee_maker
            eq+=pnl;res.cycles+=1;res.wins+=1;res.cycle_log.append((int(t[i]),pnl,nfill,'tp'))
            in_pos=False;qty=cost=0.0;i+=1
            res.min_equity=min(res.min_equity,eq);_day(res,t,i-1,eq,D);continue
        # 2) safety orders hit by the adverse extreme
        while nfill<cfg.n_so:
            lvl=entry0*(1-dirn*dev[nfill])
            if (adv<=lvl) if dirn==1 else (adv>=lvl):
                notional=size[nfill]
                upnl=dirn*(adv*qty-cost)
                used=cost/cfg.leverage
                if eq+upnl-used<notional/cfg.leverage:break  # not enough free margin: order not placed
                fill=lvl  # resting limit order: filled at its own price, never better
                q=notional/fill;qty+=q;cost+=q*fill;eq-=q*fill*cfg.fee_maker;nfill+=1
                res.max_so_hits=max(res.max_so_hits,nfill)
            else:break
        # 3) stop loss from the average
        if cfg.sl>0 and (cfg.sl_mode=='avg' or nfill==cfg.n_so):
            if cfg.sl_mode=='avg':sl_px=avg()*(1-dirn*cfg.sl)
            else:sl_px=entry0*(1-dirn*(dev[-1]+cfg.sl)) if cfg.n_so else entry0*(1-dirn*cfg.sl)
            if (adv<=sl_px) if dirn==1 else (adv>=sl_px):
                fill=(min(sl_px,o[i]) if dirn==1 else max(sl_px,o[i]))*(1-dirn*cfg.slip)
                pnl=dirn*(fill*qty-cost)-fill*qty*cfg.fee_taker
                eq+=pnl;res.cycles+=1;res.stops+=1;res.cycle_log.append((int(t[i]),pnl,nfill,'sl'))
                in_pos=False;qty=cost=0.0;i+=1
                res.min_equity=min(res.min_equity,eq);_day(res,t,i-1,eq,D);continue
        # 4) liquidation at the adverse extreme (leveraged bots)
        upnl_adv=dirn*(adv*qty-cost)
        if cfg.leverage>1 and eq+upnl_adv<=cfg.mm*adv*qty:
            res.cycle_log.append((int(t[i]),-eq,nfill,'liq'));eq=0.0;res.liquidated_t=int(t[i]);res.min_equity=0.0
            in_pos=False;qty=cost=0.0;_day(res,t,i,0.0,D);break
        if cfg.leverage<=1 and eq+upnl_adv<=0:  # spot-like: cannot go below zero, treat as wiped
            eq=0.0;res.liquidated_t=int(t[i]);res.min_equity=0.0;in_pos=False;_day(res,t,i,0.0,D);break
        res.min_equity=min(res.min_equity,eq+upnl_adv)
        _day(res,t,i,eq+(dirn*(c[i]*qty-cost) if in_pos else 0.0),D)
        i+=1
    # close an open position at the last close (market)
    if in_pos and i1>i0:
        last=min(i,i1)-1;fill=c[last]*(1-dirn*cfg.slip)
        pnl=dirn*(fill*qty-cost)-fill*qty*cfg.fee_taker;eq+=pnl;res.cycle_log.append((int(t[last]),pnl,nfill,'end'))
    res.equity=eq
    return res

def _day(res,t,i,value,D):
    """Record mark-to-market equity at the last 5m bar of each UTC day."""
    if (t[i]+300_000)%D==0:res.daily.append((int(t[i]+300_000),float(value)))
