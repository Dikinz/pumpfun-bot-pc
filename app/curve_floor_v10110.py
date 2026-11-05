"""v0.7.101.10 (user's choice 05.11.2026): a drained curve is closed at the price a real position would get.

A PAPER position is not in the curve. When every other holder sells, the curve's real SOL falls below our sale
and the monitor reports INSUFFICIENT_REAL_RESERVES, so v0.7.101.7/8 wrote the whole entry off (receipt 0).
A real position would be in the curve: a standard pump.fun curve's state depends only on the tokens outstanding,
so with our T tokens added back the curve is at (vt-T, k/(vt-T)) and selling T returns exactly vs*T/(vt-T) lamports
before fees (vs, vt: current virtual reserves). The monitor computes that value from the same fresh snapshot
(position_monitor.reserve_evidence, paper_inventory_gross_lamports).

04-05.11.2026: all 8 write-offs (3 PRIMARY, 5 ROUTE) booked -3.05 each, the curve model gives -0.26...-1.04;
two of the ROUTE positions were sold by PRIMARY on ticks of the same drained curves at -0.264 and -1.036,
the model gives -0.262 and -1.037. ROUTE ran out of PAPER cash on the write-offs and stopped trading.

This is a model, not a simulated sale: it is labelled PAPER_INVENTORY_CURVE_FLOOR, never counted as a verified
route, and costs are the PRIMARY ledger costs (pool 1.25% + route rate + fixed per fill). Special or completed
curves never reach it (execution_verification.decode_curve rejects them); old results without the fields keep
the v0.7.101.7 write-off."""
import math
from decimal import Decimal,localcontext
ROUTE='PAPER_INVENTORY_CURVE_FLOOR'
COST_MODEL='PAPER_INVENTORY_FLOOR_V10110'
REASON='CURVE_FLOOR_EXIT'
POOL_RATE=Decimal('0.0125')
EVIDENCE=dict(source='local_curve_reserves',price_basis='PAPER_INVENTORY_CURVE_MODEL_NO_SALE',clock_status='LOCAL_RESERVE_EVIDENCE')
FIELDS=('paper_inventory_gross_lamports','sell_gross_upper_lamports','virtual_sol_lamports','virtual_token_raw','real_token_raw','amount_raw')

def gross_lamports(virtual_sol,virtual_token,real_token,amount):
    """Sale of `amount` tokens on the curve with our own buy included; None when the curve could not have sold them."""
    if not all(type(x)==int for x in (virtual_sol,virtual_token,real_token,amount)):return None
    if virtual_sol<=0 or not 0<amount<=real_token<virtual_token:return None
    return amount*virtual_sol//(virtual_token-amount)

def account(c,row,job,result):
    """Ledger accounting for one fresh drained check, or None (then the old write-off rule applies)."""
    import paper_capital
    inv=job.get('inventory') or {};reserves=result.get('reserves') or {}
    if job.get('trade_id')!=row['trade_id'] or inv.get('trade_id')!=row['trade_id'] or inv.get('mint')!=row['mint']:return None
    if job.get('route')!='PUMP_CURVE' or result.get('route','PUMP_CURVE')!='PUMP_CURVE':return None
    if reserves.get('status')!='INSUFFICIENT_REAL_RESERVES':return None
    # The reserve flag uses a ceil: at the 1-lamport boundary a full simulation can still execute. That is not a
    # drained curve, so never model it (the check keeps the old handling).
    if (result.get('sale') or {}).get('status')=='LOCAL_SWAP_EXECUTED':return None
    values=[reserves.get(k) for k in FIELDS]
    if not all(type(x)==int for x in values):return None
    gross,upper,vs,vt,rt,amount=values
    if gross!=gross_lamports(vs,vt,rt,amount) or not 0<upper<=gross:return None
    # The monitor sized the sale from this inventory; any other amount is someone else's position.
    if inv.get('raw_amount') is not None and str(inv['raw_amount'])!=str(amount):return None
    entry=c.execute("SELECT notional,total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(row['trade_id'],)).fetchone()
    if not entry:return None
    s=paper_capital.settings(c)
    with localcontext() as ctx:
        ctx.prec=60
        size=Decimal(str(inv['size_usdc']));fx=Decimal(str(inv['sol_usdc']));cost=Decimal(str(entry[1]))
        route_rate=Decimal(str(s['route_rate']));fixed=Decimal(str(s['fixed_per_fill']))
        if not all(v.is_finite() for v in (size,fx,cost,route_rate,fixed)) or size<=0 or fx<=0 or size!=Decimal(str(entry[0])):return None
        value=Decimal(gross)*fx/10**9
        receipt=max(Decimal(0),value-value*(POOL_RATE+route_rate)-fixed);pnl=receipt-size-cost
    if not all(math.isfinite(float(x)) for x in (receipt,pnl)):return None
    return dict(cost_model=COST_MODEL,receipt_units=str(receipt),pnl_units=str(pnl),strict_receipt_units=str(receipt),strict_pnl_units=str(pnl),
        size_units=str(size),entry_cost_units=str(cost),fx=str(fx),quote_received_lamports=gross,
        paper_inventory_gross_lamports=gross,sell_gross_upper_lamports=upper,real_sol_lamports=reserves.get('real_sol_lamports'),
        spendable_lamports=reserves.get('spendable_lamports'),virtual_sol_lamports=vs,virtual_token_raw=vt,real_token_raw=rt,amount_raw=amount,
        pool_rate=str(POOL_RATE),route_rate=str(route_rate),fixed_per_fill=str(fixed),
        note='Drained curve: PAPER tokens are not in the curve, a real position would be. Sale value with our buy included '
            '(vs*T/(vt-T)) minus PRIMARY ledger costs. Modelled, not simulated; not a verified route.')

def settle(main,c,row,job,result,signal,scenario):
    """Close at the model value on a fresh drained check after the exit signal.
    None: the model does not apply (the caller keeps the write-off rule); True/False: settled or position changed."""
    import time,paper_branch as common,route_branch
    try:a=account(c,row,job,result)
    except (KeyError,TypeError,ValueError,ArithmeticError):a=None
    # Freshness rechecked at the mutation boundary, as for verified sales.
    if not a or route_branch.unusable(row,job,result,signal['signal_ts'],time.time()):return None
    return common.settle(main,row,a,row['id'],row['finished_ts'],dict(accounting=a,signal=signal,route=ROUTE,scenario=scenario),
        ROUTE,reason=REASON,rate_source=ROUTE,evidence=EVIDENCE)
