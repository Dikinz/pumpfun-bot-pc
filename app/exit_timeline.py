"""Read-only decomposition. Positive values are gains; negative values are losses."""
import json,math,time

def number(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
def attribution(buy,sell,order,costs):
    entry=buy.get('price');size=buy.get('size');signal=(order or {}).get('signal_price');fill=(sell or {}).get('price')
    if not all(number(x) and x>0 for x in (entry,size,signal)):
        return dict(status='MISSING_ENTRY_OR_SIGNAL',before_signal=None,after_signal=None,net=None)
    before=size*(signal/entry-1)
    after=size*(fill-signal)/entry if number(fill) and fill>0 else None
    fees={r['side']:r['total_cost'] for r in costs if number(r.get('total_cost'))}
    full=after is not None and 'BUY' in fees and 'SELL' in fees
    net=before+after-fees['BUY']-fees['SELL'] if full else None
    return dict(status='CLOSED' if full else 'OPEN_OR_MISSING_COST',before_signal=before,after_signal=after,
      entry_cost=fees.get('BUY'),exit_cost=fees.get('SELL'),net=net,
      reconciled=math.isclose(net,sell['pnl']-sum(fees.values()),abs_tol=1e-9) if full else None,
      signal_return_pct=100*(signal/entry-1),
      note='Price decomposition, not attribution of cause or a claim that the signal price was executable. No hypothetical sale replaces PAPER.')

def summary(c,monitor=None,limit=100):
    monitor=monitor or {};history=monitor.get('history') or {};bytid={r['trade_id']:r for r in monitor.get('positions',[])}
    rows=[];all_closed=0;missing=0;before=after=cost_total=net=0.
    for r in c.execute('SELECT * FROM exit_audit_v45 ORDER BY rowid DESC'):
        d=json.loads(r['data_json']);tid=r['trade_id']
        buy=dict(c.execute("SELECT * FROM trades WHERE trade_id=? AND side='BUY'",(tid,)).fetchone() or {})
        sell=dict(c.execute("SELECT * FROM trades WHERE trade_id=? AND side='SELL'",(tid,)).fetchone() or {})
        order=d.get('order') or {};fees=[dict(x) for x in c.execute('SELECT * FROM capital_ledger_v0721 WHERE trade_id=?',(tid,))]
        a=attribution(buy,sell,order,fees)
        if sell:
            all_closed+=1
            if a['net'] is None:missing+=1
            else:before+=a['before_signal'];after+=a['after_signal'];cost_total+=a['entry_cost']+a['exit_cost'];net+=a['net']
        cp=d.get('checkpoint') or {};m=bytid.get(tid) or {};mr=m.get('result') or {};res=mr.get('reserves') or {}
        historical=(history.get('by_trade') or {}).get(tid) or {}
        latest=historical.get('latest') or {}
        history_known=history.get('state')=='AVAILABLE'
        rows.append(dict(trade_id=tid,mint=r['mint'],status=r['status'],flagged=bool(r['flagged']),attribution=a,
          signal_ts=order.get('signal_ts'),reason=order.get('reason'),wait_s=cp.get('wait_wall_s'),
          checkpoint_ts=cp.get('captured_ts'),checkpoint_process_restarted=cp.get('process_restarted'),
          buffer_reason=cp.get('buffer_reason','NO_CHECKPOINT'),eligibility_counts=cp.get('eligibility_counts',{}),
          events_seen=cp.get('events_seen_this_process'),events_dropped=cp.get('events_dropped_this_process'),
          before_delay=cp.get('events_before_delay_in_buffer'),after_delay=cp.get('events_after_delay_in_buffer'),
          monitor_history=historical,monitor_history_state=history.get('state','HISTORY_NOT_LOADED'),
          monitor_status=latest.get('status') or m.get('status') or ('NO_COMPLETED_RESULT' if historical else 'NO_MONITOR_RESULT' if history_known else 'MONITOR_HISTORY_UNKNOWN'),monitor_finished_ts=latest.get('finished_ts',m.get('finished_ts')),
          monitor_timing=latest.get('timing'),
          monitor_stale=m.get('stale'),first_problem_ts=(m.get('health') or {}).get('first_problem_ts'),
          real_sol_lamports=latest.get('real_sol_lamports',res.get('real_sol_lamports')),sell_gross_upper_lamports=latest.get('sell_gross_upper_lamports',res.get('sell_gross_upper_lamports')),
          simulation_status=latest.get('simulation_status',(mr.get('sale') or {}).get('status')),
          monitor_note='History matched by trade_id; latest can be AFTER_CLOSE. Use explicit timing buckets, not latest status, to judge what was known before exit. No on-chain execution proof.'))
    return dict(rows=rows if limit<0 else rows[:limit],row_count=len(rows),
      totals=dict(closed=all_closed,missing=missing,before_signal=before,after_signal=after,closed_costs=cost_total,net=net if not missing else None),
      note='Totals cover the database, not only this run. Pending positions have no realized exit PNL. Checkpoint every 5 s and on orderly shutdown. Buffer limit 200 events per process/position; counters cover the order. Main strategy and 250 ms delay unchanged.')
