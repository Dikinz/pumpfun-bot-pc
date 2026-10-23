"""Read-only open-position evidence; no new prices or PAPER accounting."""
import execution_model as ex
def summary(c,monitor,as_of):
    history=(monitor.get('history') or {}).get('by_trade') or {}
    rows=[]
    for raw in c.execute('SELECT * FROM positions'):
        p=dict(raw);stamp=p.get('last_update_ts')
        age=as_of-stamp if isinstance(stamp,(int,float)) and as_of>=stamp else None
        h=history.get(p['trade_id']) or {}
        rows.append(dict(trade_id=p['trade_id'],mint=p['mint'],principal=p['size'],
          tick_price=p['current'],tick_ts=stamp,tick_age_s=age,
          tick_state='UNKNOWN' if age is None else 'STALE' if age>ex.MAX_PRICE_AGE else 'RECENT',
          last_attempt=h.get('latest'),last_result=h.get('latest_result'),last_sale=h.get('latest_sale'),
          post_signal_counts=h.get('status_by_timing',{}).get('AFTER_SIGNAL_OPEN',{}),
          first_sale_after_signal=h.get('first_sale_buckets',{}).get('AFTER_SIGNAL_OPEN')))
    return dict(as_of=as_of,rows=rows,open_count=len(rows),
      stale_ticks=sum(r['tick_state']=='STALE' for r in rows),unknown_ticks=sum(r['tick_state']=='UNKNOWN' for r in rows),
      note='Tick age uses frozen primary heartbeat for stopped/stale reports; monitor ages use its own report time. Recent tick is not executable price. Local-sale accounting uses entry FX, synthetic tokens and isolated exit costs, not current cash or full network costs. No portfolio mutation.')
