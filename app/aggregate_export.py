"""Read-only campaign aggregates. Stage observations are not a conversion cohort."""
import csv,json,sqlite3,time
from pathlib import Path
from contextlib import closing

def summary(c,role,now=None):
    c.row_factory=sqlite3.Row;now=time.time() if now is None else now
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    meta=dict(c.execute('SELECT k,v FROM meta'))
    stages=[];reasons=[]
    if 'candidate_first_v0742' in tables:
        stages=[dict(r) for r in c.execute('SELECT variant,stage,COUNT(*) count,COUNT(DISTINCT mint) unique_mints FROM candidate_first_v0742 GROUP BY variant,stage ORDER BY variant,stage')]
    if 'candidate_counts_v0742' in tables:
        reasons=[dict(r) for r in c.execute('SELECT variant,reason,n count FROM candidate_counts_v0742 ORDER BY variant,n DESC')]
    actual=[dict(r) for r in c.execute('SELECT stage,reason,COUNT(*) count,COUNT(DISTINCT mint) unique_mints FROM entry_debug GROUP BY stage,reason ORDER BY count DESC')]
    trades=[];known=[]
    for buy in c.execute("SELECT * FROM trades WHERE side='BUY' ORDER BY id"):
        sell=c.execute("SELECT * FROM trades WHERE trade_id=? AND side='SELL'",(buy['trade_id'],)).fetchone()
        fees=list(c.execute('SELECT side,total_cost FROM capital_ledger_v0721 WHERE trade_id=?',(buy['trade_id'],)))
        complete={r['side'] for r in fees}=={'BUY','SELL'}
        costs=sum(r['total_cost'] for r in fees)
        net=sell['pnl']-costs if sell and complete else None
        if net is not None:known.append(net)
        trades.append(dict(trade_id=buy['trade_id'],mint=buy['mint'],entry_ts=buy['ts'],exit_ts=sell['ts'] if sell else None,
          status='CLOSED' if sell else 'OPEN',reason=sell['reason'] if sell else None,size=buy['size'],
          gross=sell['pnl'] if sell else None,recorded_costs=costs,net=net,cost_coverage_complete=complete))
    wins=[x for x in known if x>0];losses=[x for x in known if x<0]
    avgwin=sum(wins)/len(wins) if wins else None;avgloss=sum(losses)/len(losses) if losses else None
    positions=[]
    for p in c.execute('SELECT trade_id,mint,size FROM positions'):
        order=c.execute("SELECT signal_ts,reason FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL' AND status='PENDING'",(p['trade_id'],)).fetchone()
        decision=c.execute('SELECT decision FROM route_exit_decisions_v59 WHERE trade_id=? ORDER BY available_ts DESC LIMIT 1',(p['trade_id'],)).fetchone() if 'route_exit_decisions_v59' in tables else None
        remainder=__import__('route_remainder_v96').state(order['signal_ts'] if order else None,now,decision[0] if decision else None) if role=='route' else {}
        positions.append(dict(p,**remainder,signal_ts=order['signal_ts'] if order else None,reason=order['reason'] if order else None,
          wait_s=max(0,now-order['signal_ts']) if order else None,exit_decision=decision[0] if decision else
          ('WAITING_VERIFIED_ROUTE_RESULT' if role=='route' else 'WAITING_FRESH_TICK_OR_AMM' if role=='amm' else 'WAITING_FRESH_TICK') if order else 'NO_EXIT_SIGNAL'))
    return dict(schema='AGGREGATES_V61',role=role,scope='WHOLE_CAMPAIGN',stages=stages,reasons=reasons,
      retained_entry_debug=actual,trades=trades,positions=positions,
      input=dict(persisted_gap=meta.get('branch_input_gap','0'),sequence=meta.get('branch_input_seq'),
        last_entry_reason=meta.get('last_entry_result_v0722'),session=meta.get('branch_session')),
      payoff=dict(closed_with_complete_costs=len(known),wins=len(wins),losses=len(losses),
        average_win=avgwin,average_loss=avgloss,payoff_ratio=avgwin/abs(avgloss) if avgwin is not None and avgloss else None,
        net=sum(known),missing_closed_costs=sum(t['status']=='CLOSED' and t['net'] is None for t in trades)),
      note='Stage counts are distinct observed mint/stage per variant, not nested conversion cohorts. Reason counts are repeated evaluations. entry_debug is retained history only, possibly pruned. Payoff is for executed PAPER trades; rejected candidates have no measured trade payoff. Costs differ by scenario. No claim about missed profitable entries.')

def export(primary,folder):
    import paper_branch,route_branch
    reports=[];folder=Path(folder)
    for role,path in [('primary',Path(primary)),('amm',paper_branch.path(primary)),('route',route_branch.path(primary))]:
        if not path.exists():continue
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            c.execute('BEGIN');report=summary(c,role);reports.append(report)
    (folder/'aggregates_v61.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    for key in ('stages','reasons','retained_entry_debug','trades','positions'):
        rows=[dict(role=r['role'],**v) for r in reports for v in r[key]]
        fields=list(dict.fromkeys(k for row in rows for k in row)) or ['role']
        with (folder/('aggregate_'+key+'.csv')).open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fields);writer.writeheader();writer.writerows(rows)
    return reports
