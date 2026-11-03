"""v0.7.101.8: observation only. Would skipping entries with fewer than 10 buyers have helped?

Entries and exits are unchanged. Two earlier samples agree: under 10 buyers lost −10.40 on 39 closes
(02–03.11.2026, one-sided permutation p≈0.07) and −7.11 on 16 (03–04.11.2026, p≈0.05); 10 and more lost −2.57 on 63
and made +7.64 on 30. This campaign is the third check, with the criterion fixed in advance (verdict below)."""
import json
THRESHOLD=10
MIN_CLOSED=40

def entry_buyers(snapshot):
    try:b=json.loads(snapshot or '{}').get('buyers')
    except (ValueError,TypeError,AttributeError):return None
    return len(b) if isinstance(b,list) else None

def group(nets):
    return dict(closed=len(nets),net=sum(nets),mean=sum(nets)/len(nets) if nets else None,wins=sum(n>0 for n in nets))

def summary(c,fork=0):
    buyers={r[0]:entry_buyers(r[1]) for r in c.execute("SELECT trade_id,snapshot FROM paper_orders_v0720 WHERE side='BUY' AND status='FILLED'")}
    below=[];above=[];unknown=0
    for tid,pnl,legs,fees in c.execute("""SELECT t.trade_id,t.pnl,COALESCE(l.legs,0),l.fees FROM trades t
      LEFT JOIN (SELECT trade_id,COUNT(*) legs,SUM(total_cost) fees FROM capital_ledger_v0721 GROUP BY trade_id) l ON l.trade_id=t.trade_id
      WHERE t.side='SELL' AND t.id>? ORDER BY t.id""",(fork,)):
        n=buyers.get(tid)
        if n is None or legs!=2 or fees is None:unknown+=1;continue
        (below if n<THRESHOLD else above).append(pnl-fees)
    b=group(below);a=group(above);closed=len(below)+len(above)
    if closed<MIN_CLOSED or not below or not above:verdict='TOO_FEW'
    elif b['net']<0 and b['mean']<a['mean']:verdict='SUPPORTED'
    else:verdict='NOT_SUPPORTED'
    return dict(observation_only=True,threshold=THRESHOLD,
        below=b,at_or_above=a,unknown=unknown,verdict=verdict)
# SUPPORTED: at least MIN_CLOSED closes with known entry buyers, the group under THRESHOLD is negative in total and worse
# per close than the rest. Only then is the filter proposed; nothing changes automatically. Buyers = distinct buying
# wallets seen before entry (capped per mint); association, not cause; net after recorded costs.
# The overview stays light (test_overview_v1001), so the explanation lives here and in RELEASE_NOTES_v1018.md.
