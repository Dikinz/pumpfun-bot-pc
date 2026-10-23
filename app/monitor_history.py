"""Read-only evidence history; lifecycle attempts never replace substantive results."""
import json,math,time
SALE_STATUSES={'SELLABLE','NET_BELOW_FLOOR','RESERVE_DROP','AMM_SALE_EXECUTED'}
RESULT_STATUSES=SALE_STATUSES|{'INSUFFICIENT_RESERVES','SALE_REJECTED','NEGATIVE_CONTROL_FAILED','AMM_INSUFFICIENT_RESERVES','AMM_SIMULATION_FAILED','AMM_NEGATIVE_CONTROL_FAILED'}
def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
def timing(finished,signal,closed):
    if not finite(finished):return 'UNFINISHED'
    if finite(closed) and finished>closed:return 'AFTER_CLOSE'
    if finite(signal) and finished<=signal:return 'BEFORE_SIGNAL'
    if finite(signal):return 'AFTER_SIGNAL_BEFORE_CLOSE' if finite(closed) else 'AFTER_SIGNAL_OPEN'
    return 'BEFORE_CLOSE_NO_SIGNAL' if finite(closed) else 'OPEN_NO_SIGNAL'
def obj(raw):
    try:
        d=json.loads(raw or '{}')
        return (d,None) if isinstance(d,dict) else ({},'INVALID_JSON_OBJECT')
    except (TypeError,ValueError):return {},'INVALID_JSON'
def summary(c,now=None):
    now=time.time() if now is None else now
    signals={r['trade_id']:r['signal_ts'] for r in c.execute("SELECT trade_id,signal_ts FROM paper_orders_v0720 WHERE side='SELL' ORDER BY signal_ts")}
    closes={r['trade_id']:r['ts'] for r in c.execute("SELECT trade_id,ts FROM trades WHERE side='SELL'")}
    groups={};selected={}
    for row in c.execute('SELECT id,trade_id,mint,started_ts,finished_ts,status FROM position_checks_v0730 ORDER BY finished_ts,id'):
        r=dict(row);tid=r['trade_id'];bucket=timing(r['finished_ts'],signals.get(tid),closes.get(tid));r['timing']=bucket
        g=groups.setdefault(tid,dict(trade_id=tid,mint=r['mint'],signal_ts=signals.get(tid),closed_ts=closes.get(tid),total=0,timing_counts={},status_counts={},status_by_timing={},latest=None,latest_before_close=None,latest_result=None,latest_sale=None,buckets={},result_buckets={},sale_buckets={},first_sale_buckets={}))
        g['total']+=1;g['timing_counts'][bucket]=g['timing_counts'].get(bucket,0)+1
        g['status_counts'][r['status']]=g['status_counts'].get(r['status'],0)+1
        counts=g['status_by_timing'].setdefault(bucket,{})
        counts[r['status']]=counts.get(r['status'],0)+1
        if bucket=='UNFINISHED':continue
        g['latest']=r;g['buckets'][bucket]=r
        if bucket!='AFTER_CLOSE':g['latest_before_close']=r
        if r['status'] in RESULT_STATUSES:g['latest_result']=r;g['result_buckets'][bucket]=r
        if r['status'] in SALE_STATUSES:
            g['latest_sale']=r;g['sale_buckets'][bucket]=r;g['first_sale_buckets'].setdefault(bucket,r)
    for g in groups.values():
        for r in [g['latest'],g['latest_before_close'],g['latest_result'],g['latest_sale'],*g['buckets'].values(),*g['result_buckets'].values(),*g['sale_buckets'].values(),*g['first_sale_buckets'].values()]:
            if r:selected[r['id']]=r
    columns={x[1] for x in c.execute('PRAGMA table_info(position_checks_v0730)')}
    for ident,r in selected.items():
        raw=c.execute('SELECT result_json'+(',input_json' if 'input_json' in columns else '')+' FROM position_checks_v0730 WHERE id=?',(ident,)).fetchone()
        d,error=obj(raw[0])
        if error:r['payload_error']=error
        res=d.get('reserves') or {};sale=d.get('sale') or {}
        if not isinstance(res,dict):res={}
        if not isinstance(sale,dict):sale={}
        r.update(reserve_status=res.get('status'),real_sol_lamports=res.get('real_sol_lamports'),sell_gross_upper_lamports=res.get('sell_gross_upper_lamports'),simulation_status=sale.get('status'),
          net_status=d.get('net_status'),snapshot_received_ts=d.get('snapshot_received_ts'),
          age_s=now-r['finished_ts'] if finite(r['finished_ts']) and now>=r['finished_ts'] else None,
          zero_token_control_ok=d.get('zero_token_control_ok'),accounting=None)
        # Same isolated-candidate cost model as existing rules; no balance mutation.
        if 'input_json' in columns and r['status'] in SALE_STATUSES and d.get('zero_token_control_ok') is True:
            inp,_=obj(raw[1])
            if inp.get('inventory') and sale.get('status') in ('LOCAL_SWAP_EXECUTED','AMM_SALE_EXECUTED'):
                import exit_rules_v33
                r['accounting']=exit_rules_v33.accounting(c,dict(inp,trade_id=r['trade_id']),d)
    return dict(state='AVAILABLE',as_of=now,by_trade=groups,
      note='All attempts grouped by trade_id and result availability. latest is last finished attempt; latest_result excludes lifecycle/network failures; latest_sale retains separately classified local execution. Result/sale buckets preserve timing; late evidence never proves earlier execution. Ages are at report time. Accounting is an isolated synthetic-token scenario at entry FX, not cash, live execution or current liquidation value.')
