"""Observation-only risk flags for a PAPER branch (v0.7.101.4).

Nothing here blocks entries or changes exits. WOULD_HALT_* mark where a campaign
drawdown limit or a loss-streak limit would have stopped trading, and how much
the branch made or lost after that point. The limits are reporting constants for
the fixed-threshold sample, not trading rules. SELL waits show how long exit
signals waited for a fill (the tick-proxy PAPER fill waits for the next market trade).
"""
DRAWDOWN_LIMITS=(0.20,0.30)
LOSS_STREAK_LIMIT=5
SLOW_SELL_MS=5000

def flags(nets,start):
    """nets: (closed trade number in the branch, net) for trades with complete accounting."""
    equity=peak=float(start);streak=0;max_streak=0;max_dd=0.;hits={}
    for i,net in nets:
        equity+=net;peak=max(peak,equity)
        dd=(peak-equity)/peak if peak>0 else 0.;max_dd=max(max_dd,dd)
        streak=streak+1 if net<0 else 0;max_streak=max(max_streak,streak)
        for limit in DRAWDOWN_LIMITS:
            code='WOULD_HALT_DD%d'%round(limit*100)
            if dd>=limit and code not in hits:hits[code]=dict(after_closed=i,equity=equity)
        code='WOULD_HALT_STREAK%d'%LOSS_STREAK_LIMIT
        if streak>=LOSS_STREAK_LIMIT and code not in hits:hits[code]=dict(after_closed=i,equity=equity)
    halts=[dict(code=k,after_closed=v['after_closed'],net_after=equity-v['equity']) for k,v in hits.items()]
    return dict(closed=len(nets),max_drawdown_pct=max_dd*100,current_drawdown_pct=((peak-equity)/peak*100) if peak>0 else 0.,
        max_loss_streak=max_streak,current_loss_streak=streak,would_halt=sorted(halts,key=lambda x:x['after_closed']))

def percentile(values,q):
    if not values:return None
    v=sorted(values);return v[min(len(v)-1,int(q*(len(v)-1)+.5))]

def sell_waits(c,fork,at):
    # Same scope as the branch's closed trades: SELLs after the fork trade id.
    waits=[r[0] for r in c.execute("SELECT wait_ms FROM paper_orders_v0720 WHERE side='SELL' AND status='FILLED' AND wait_ms IS NOT NULL AND trade_id IN (SELECT trade_id FROM trades WHERE side='SELL' AND id>?)",(fork,))]
    pending=[at-r[0] for r in c.execute("SELECT signal_ts FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING' AND signal_ts IS NOT NULL")]
    return dict(filled=len(waits),median_ms=percentile(waits,.5),p90_ms=percentile(waits,.9),max_ms=max(waits) if waits else None,
        slow=sum(w>SLOW_SELL_MS for w in waits),slow_ms=SLOW_SELL_MS,pending=len(pending),oldest_pending_s=max(pending) if pending else None)

def summary(c,nets,start,fork,at,accounting_missing=0):
    return dict(schema='RISK_WATCH_V1014',observation_only=True,limits=dict(drawdown_pct=[round(x*100) for x in DRAWDOWN_LIMITS],loss_streak=LOSS_STREAK_LIMIT),
        accounting_complete=not accounting_missing,**flags(nets,start),sell_wait=sell_waits(c,fork,at),
        note='Observation only: limits never block entries or change exits during the fixed-threshold sample.')
