"""Read-only explanation of pending exits; silence is not proof of a dead mint."""
import json

def rows(c,now):
    meta=dict(c.execute('SELECT k,v FROM meta'));out=[]
    last=float(meta.get('trade_last_market_ts',0));age=max(0,now-last) if last else None
    for o in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING'"):
        d=c.execute('SELECT * FROM exit_diagnostics_v0722 WHERE order_id=?',(o['order_id'],)).fetchone();d=dict(d) if d else {}
        n=d.get('raw_count',0);rejected=d.get('rejected',0);other=d.get('other_pool',0)
        if n and other==n:state='MINT_EVENTS_OTHER_POOL'
        elif n and rejected+other>=n:state='MINT_EVENTS_REJECTED'
        elif n:state='MINT_EVENTS_OBSERVED_CHECK_ELIGIBILITY'
        elif age is None or age>15:state='MARKET_STREAM_NOT_FRESH'
        else:state='NO_MINT_EVENTS_CAUSE_UNPROVEN'
        out.append(dict(trade_id=o['trade_id'],mint=o['mint'],order_id=o['order_id'],signal_ts=o['signal_ts'],wait_s=max(0,now-o['signal_ts']),
          state=state,raw_events=n,rejected=rejected,other_pool=other,last_mint_event_ts=d.get('last_ts'),last_code=d.get('last_code'),
          market_age_s=age,market_state=meta.get('trade_stream_state'),stream_generation=meta.get('stream_generation'),
          provider=meta.get('trade_stream_provider'),
          note='Silence cannot distinguish an inactive mint from provider omission. No independent chain/subscription proof; last price is not executable proceeds.'))
    return out

def economics(review):
    rows=[]
    for r in review.get('rows',[]):
        s=r.get('settlement') or {}
        if r.get('role')!='route' or s.get('route')!='PUMP_CURVE':continue
        a=s.get('components') or {};entry=r.get('entry_cost');exit=r.get('exit_cost');size=r.get('size')
        if entry is None or exit is None or not size:continue
        total=entry+exit
        rows.append(dict(trade_id=r['trade_id'],mint=r['mint'],size=size,gross=r['gross'],net=r['net'],entry_cost=entry,exit_cost=exit,
          total_cost=total,break_even_gross=total,break_even_gross_pct=100*total/size,
          token_setup=a.get('token_account_setup'),volume_setup=a.get('volume_account_setup'),local_fee=a.get('local_transaction_fee'),
          residual=a.get('exit_fee_balance_residual'),signal_to_fill_ms=r.get('signal_to_fill_ms')))
    return dict(rows=rows,count=len(rows),mean_total_cost=sum(r['total_cost'] for r in rows)/len(rows) if rows else None,
      mean_break_even_gross_pct=sum(r['break_even_gross_pct'] for r in rows)/len(rows) if rows else None,
      note='Break-even is a retrospective identity at recorded costs, not a future target or entry filter. Pool debit already contains some pool fees; setup is retained, no persistent-wallet replay. Open positions excluded.')

def export(primary,folder,review):
    import sqlite3,monitor_service,paper_branch,route_branch
    from contextlib import closing
    from pathlib import Path
    from priority_exit import summary
    out={}
    for role,path in [('primary',Path(primary)),('amm',paper_branch.path(primary)),('route',route_branch.path(primary))]:
        if not path.exists():continue
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            c.row_factory=sqlite3.Row;c.execute('BEGIN');meta=dict(c.execute('SELECT k,v FROM meta'))
            at=float(meta.get('heartbeat_ts',0))
            out[role]=dict(waiting=rows(c,at))
        if monitor_service.paths(path)['db'].exists():
            with closing(monitor_service.connect(path,readonly=True)) as c:out[role]['priority']=summary(c)
    out['curve_economics']=economics(review)
    (Path(folder)/'exit_priority_v65.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
