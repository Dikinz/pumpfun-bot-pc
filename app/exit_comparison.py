"""Independent exit ledger: same entries, three branches, no trading mutations.

Receipts use the frozen ENTRY SOL/USDC reference rate for both comparison and
PAPER units. This is not an FX-realized PnL or a capital-reallocation backtest.
"""
import json,time
from decimal import Decimal,localcontext
from collections import Counter
MAX_DELAY=5.0
SCHEMA='''CREATE TABLE IF NOT EXISTS exit_comparison_v0728(
 order_id TEXT,branch TEXT,trade_id TEXT,mint TEXT,signal_ts REAL,observed_ts REAL,
 status TEXT,technical_status TEXT,net_status TEXT,receipt_units TEXT,pnl_units TEXT,
 details_json TEXT,PRIMARY KEY(order_id,branch));'''

def evaluate(result,signal_ts,available_ts):
    """Keep technical success and net-floor test orthogonal; no float lamports."""
    technical=result.get('status','NOT_RUN')
    net='NOT_EVALUATED';margin=None
    if technical=='LOCAL_SWAP_EXECUTED':
        receipt=result.get('seller_net_sol_delta_lamports');floor=result.get('minimum_lamports')
        if receipt is not None and floor is not None:
            margin=int(receipt)-int(floor)
            net='NET_MEETS_FLOOR' if margin>=0 else 'NET_BELOW_FLOOR'
    if technical!='LOCAL_SWAP_EXECUTED':status=technical
    elif result.get('zero_token_control_status')!='LOCAL_SWAP_FAILED':status='CONTROL_NOT_CONFIRMED'
    elif net!='NET_MEETS_FLOOR':status=net
    elif result.get('timing_status')!='WITHIN_5S':status='SNAPSHOT_NOT_TIMELY'
    elif available_ts-signal_ts<0:status='CLOCK_ANOMALY'
    elif available_ts-signal_ts>MAX_DELAY:status='LATE_RESULT'
    else:status='ACCEPTED_LOCAL'
    return dict(technical_status=technical,net_status=net,net_margin_lamports=margin,
                comparison_status=status,available_delay_s=available_ts-signal_ts,
                unknown_network_costs=True)

def put(c,oid,branch,data,status,technical,net,observed,receipt,pnl,details):
    c.execute('''INSERT OR IGNORE INTO exit_comparison_v0728 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
      (oid,branch,data.get('trade_id'),data['mint'],data['signal_ts'],observed,status,technical,net,
       str(receipt) if receipt is not None else None,str(pnl) if pnl is not None else None,json.dumps(details)))

def source(c,oid):
    row=c.execute('SELECT input_json FROM auto_sim_v0727 WHERE order_id=?',(oid,)).fetchone()
    return json.loads(row[0]) if row else None

def record_simulation(c,oid,report,available_ts):
    data=source(c,oid)
    if data is None:return
    inv=data.get('inventory') or {};entry=data.get('entry_cost_units')
    for branch in ('FIRST','REPEAT'):
        r=report.get('scenarios',{}).get(branch,{'status':report.get('status','NOT_RUN')})
        assessment=evaluate(r,data['signal_ts'],available_ts)
        receipt=pnl=None
        if r.get('status')=='LOCAL_SWAP_EXECUTED' and r.get('seller_net_sol_delta_lamports') is not None and inv.get('sol_usdc') and inv.get('size_usdc') and entry is not None:
            with localcontext() as ctx:
                ctx.prec=60
                receipt=Decimal(r['seller_net_sol_delta_lamports'])/Decimal(10)**9*Decimal(inv['sol_usdc'])
                pnl=receipt-Decimal(inv['size_usdc'])-Decimal(entry)
        elif assessment['comparison_status']=='ACCEPTED_LOCAL':assessment['comparison_status']='MISSING_ENTRY_ACCOUNTING'
        details=dict(assessment,failure_classification=r.get('failure_classification'),volume_scenario=branch,raw_amount=r.get('raw_amount'),
                     minimum_lamports=r.get('minimum_lamports'),seller_net_sol_delta_lamports=r.get('seller_net_sol_delta_lamports'),
                     local_fee_lamports=r.get('local_fee_from_balance_diff_lamports'),
                     volume_rent_lamports=r.get('volume_account_rent_delta_lamports'),
                     volume_setup=r.get('volume_setup'),fx_reference=inv.get('sol_usdc'),
                     entry_cost_units=entry,snapshot_slot=r.get('snapshot_slot'),
                     snapshot_delay_s=r.get('snapshot_received_delay_s'),wallet=r.get('wallet'),
                     note='FIRST and REPEAT are alternative account states on the same pool snapshot. Never add branches together. Missing/late/rejected cases are not zero-profit closes.')
        put(c,oid,branch,data,assessment['comparison_status'],assessment['technical_status'],assessment['net_status'],available_ts,receipt,pnl,details)

def record_tick(c,oid):
    data=source(c,oid)
    if data is None:return
    order=c.execute('SELECT * FROM paper_orders_v0720 WHERE order_id=?',(oid,)).fetchone()
    if not order or order['side']!='SELL' or order['status']!='FILLED':return
    ledger=c.execute("SELECT * FROM capital_ledger_v0721 WHERE trade_id=? AND side='SELL'",(order['trade_id'],)).fetchone()
    if not ledger:return
    entry=data.get('entry_cost_units');inv=data.get('inventory') or {}
    receipt=Decimal(str(ledger['cash_delta']));pnl=None
    if entry is not None and inv.get('size_usdc'):
        pnl=receipt-Decimal(inv['size_usdc'])-Decimal(entry)
    details=dict(wait_s=order['fill_ts']-data['signal_ts'],fill_price=order['fill_price'],
                 model_exit_cost_units=ledger['total_cost'],entry_cost_units=entry,
                 note='Actual baseline PAPER ledger, modeled fees; not an on-chain fill.')
    put(c,oid,'TICK',data,'TICK_FILLED','TICK_PROXY','NOT_APPLICABLE',order['fill_ts'],receipt,pnl,details)

def summary(c,limit=100):
    rows=[dict(r) for r in c.execute('SELECT * FROM exit_comparison_v0728 ORDER BY signal_ts DESC,branch')]
    by={(r['order_id'],r['branch']):r for r in rows}
    total=c.execute('SELECT COUNT(*) FROM auto_sim_v0727').fetchone()[0]
    aggregates=[]
    for branch in ('FIRST','REPEAT'):
        group=[r for r in rows if r['branch']==branch];accepted=[r for r in group if r['status']=='ACCEPTED_LOCAL']
        pairs=[(r,by[(r['order_id'],'TICK')]) for r in accepted if (r['order_id'],'TICK') in by and by[(r['order_id'],'TICK')]['pnl_units'] is not None]
        sim=sum((Decimal(r['pnl_units']) for r,t in pairs),Decimal(0))
        tick=sum((Decimal(t['pnl_units']) for r,t in pairs),Decimal(0))
        aggregates.append(dict(branch=branch,total_signals=total,recorded=len(group),unrecorded=total-len(group),
          statuses=dict(Counter(r['status'] for r in group)),technical=dict(Counter(r['technical_status'] for r in group)),
          net=dict(Counter(r['net_status'] for r in group)),accepted=len(accepted),paired=len(pairs),
          paired_sim_pnl_units=str(sim) if pairs else None,paired_tick_pnl_units=str(tick) if pairs else None,
          paired_difference_units=str(sim-tick) if pairs else None,
          sim_earlier_than_tick=sum(r['observed_ts']<t['observed_ts'] for r,t in pairs)))
    ids=[oid for (oid,branch),r in by.items() if branch=='FIRST' and r['status']=='ACCEPTED_LOCAL'
         and by.get((oid,'REPEAT'),{}).get('status')=='ACCEPTED_LOCAL'
         and by.get((oid,'TICK'),{}).get('pnl_units') is not None]
    common_rows=[]
    for branch in ('FIRST','REPEAT','TICK'):
        subset=[by[(oid,branch)] for oid in ids]
        total_pnl=sum((Decimal(r['pnl_units']) for r in subset),Decimal(0)) if subset else None
        tick_pnl=sum((Decimal(by[(oid,'TICK')]['pnl_units']) for oid in ids),Decimal(0)) if ids else None
        common_rows.append(dict(branch=branch,count=len(ids),pnl_units=str(total_pnl) if total_pnl is not None else None,
          difference_vs_tick_units=str(total_pnl-tick_pnl) if total_pnl is not None else None))
    common=dict(order_ids=ids,count=len(ids),total_signals=total,excluded=total-len(ids),rows=common_rows)
    display=[]
    jobs=list(c.execute('SELECT order_id,mint,signal_ts,status FROM auto_sim_v0727 ORDER BY signal_ts DESC LIMIT ?',(limit,)))
    for oid,mint,signal,jobstatus in jobs:
        branches={}
        for branch in ('FIRST','REPEAT','TICK'):
            r=by.get((oid,branch))
            if r:
                r=dict(r);r['details']=json.loads(r.pop('details_json'));branches[branch]=r
        display.append(dict(order_id=oid,mint=mint,signal_ts=signal,job_status=jobstatus,branches=branches))
    return dict(common_cohort=common,aggregates=aggregates,rows=display,max_available_delay_s=MAX_DELAY,
                note='Paired matched exits only; no compounding or freed-capital replay. Frozen entry FX, modeled entry costs, unknown network costs. FIRST and REPEAT are separate alternatives.')
