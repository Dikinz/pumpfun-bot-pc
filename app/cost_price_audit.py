"""Read-only balance decomposition and temporal pool/TICK comparison.

Pool debit is an observed balance component, NOT a fee-free spot quote.
Post-close evidence is diagnostic only and never feeds selection.
"""
import json, math
from decimal import Decimal
from collections import Counter
import paper_capital

def number(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)

def local_components(row, inventory, entry_cost):
    out=dict(check_id=row['id'],started_ts=row.get('started_ts'),available_ts=row.get('finished_ts'),
             status=row['status'],accounting_status='UNAVAILABLE')
    try:
        r=json.loads(row.get('result_json') or '{}');job=json.loads(row.get('input_json') or '{}')
        sale=r.get('sale') or {};inv=job.get('inventory') or {}
        out.update(snapshot_ts=r.get('snapshot_received_ts'),snapshot_slot=sale.get('snapshot_slot'),
                   snapshot_started_ts=r.get('snapshot_started_ts'),
                   raw_amount=sale.get('raw_amount'))
        amm=(r.get('status')=='AMM_SALE_EXECUTED' and sale.get('status')=='AMM_SALE_EXECUTED' and r.get('route')=='PUMP_AMM' and job.get('route')=='PUMP_AMM')
        curve=r.get('status')=='CHECKED' and sale.get('status')=='LOCAL_SWAP_EXECUTED'
        if not (amm or curve) or r.get('zero_token_control_ok') is not True:return out
        if not inventory or inv.get('mint')!=row['mint'] or (job.get('mint')!=row['mint'] if amm else sale.get('mint')!=row['mint']):
            out['accounting_status']='INVENTORY_MISMATCH';return out
        for k in ('size_usdc','sol_usdc','token_units','entry_sol_price'):
            a,b=Decimal(str(inv[k])),Decimal(str(inventory[k]))
            if not a.is_finite() or a<=0 or a!=b:raise ValueError('inventory')
        raw=str(inventory['raw_amount'])
        if raw!=str(sale['raw_amount']) or raw!=str(sale['tokens_debited']) or int(raw)<=0:raise ValueError('amount')
        topup=0
        if amm:
            if (str(sale.get('pool_tokens_credited'))!=raw or sale.get('err') is not None or
                any(x.get('broadcast') is not False or x.get('paper_mutated') is not False for x in (r,sale))):raise ValueError('isolated effects')
            fields=['quote_received_lamports','wallet_sol_delta_lamports','base_account_rent_lamports','quote_account_rent_lamports',
                    'local_fee_lamports','volume_rent_delta_lamports','pool_rent_topup_lamports','net_after_setup_lamports']
            if any(type(sale.get(k))!=int for k in fields):
                out['accounting_status']='MISSING_COST_COMPONENT';return out
            gross,wallet,base,quote,fee,volume,topup,after=[sale[k] for k in fields]
            token=base+quote;net=gross+wallet
            if min(base,quote,fee,volume,topup)<0 or after!=net-token:raise ValueError('AMM balance decomposition')
        else:
            fields=['seller_net_sol_delta_lamports','pool_sol_delta_lamports',
                'local_fee_from_balance_diff_lamports','synthetic_token_rent_lamports',
                'volume_account_rent_delta_lamports']
            if any(type(sale.get(k))!=int for k in fields):
                out['accounting_status']='MISSING_COST_COMPONENT';return out
            net,pool,fee,token,volume=[sale[k] for k in fields];gross=-pool
        residual=gross-net-fee-volume
        if gross<=0 or min(fee,token,volume,residual)<0:raise ValueError('balance decomposition')
        if residual<topup:raise ValueError('unreconciled topup')
        fx=Decimal(str(inv['sol_usdc']));size=float(inv['size_usdc'])
        cv=lambda n:float(Decimal(n)*fx/10**9)
        receipt=cv(net-token)
        trading=cv(gross)-size
        pnl=receipt-size-entry_cost
        recomposed=trading-entry_cost-cv(residual)-cv(fee)-cv(token)-cv(volume)
        out.update(accounting_status='RECONCILED' if math.isclose(pnl,recomposed,abs_tol=1e-9) else 'MISMATCH',
            pool_debit_units=cv(gross),pool_debit_lamports=gross,
            trading_component=trading,entry_cost=entry_cost,
            exit_fee_balance_residual=cv(residual),local_transaction_fee=cv(fee),
            token_account_setup=cv(token),volume_account_setup=cv(volume),
            gross_basis='SELLER_QUOTE_RECEIPT_BEFORE_ACCOUNT_SETUP' if amm else 'POOL_SOL_DEBIT',
            pool_rent_topup_subset_of_residual=cv(topup),
            receipt_units=receipt,pnl_units=pnl,pnl_without_setup=pnl+cv(token+volume),
            fx=float(fx),amount_matches=True,
            pool_debit_price_sol=float(Decimal(gross)/10**9/Decimal(inv['token_units'])),
            unknown_costs=['fees_retained_inside_pool','priority_and_landing','failed_network_transactions'],
            note=('AMM gross is seller quote receipt, not a fee-free pool debit. ' if amm else '')+'Pool debit minus size is not a fee-free market return. Residual groups balance differences, including reported pool topup; individual protocol fees not inferred. Net already includes local fee and volume setup; token setup subtracted once. Setup-excluded result is sensitivity only, not a REPEAT-wallet replay.')
    except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError):
        out['accounting_status']='INVALID_OR_INCOMPLETE_PAYLOAD'
    return out

def nearest(checks, tick_ts):
    valid=[x for x in checks if number(x.get('available_ts'))]
    pick=lambda rows:min(rows,key=lambda x:(abs(x['available_ts']-tick_ts),x['available_ts'],x['check_id'])) if rows else None
    def stamp(x):
        if x is None:return None
        delta=x['available_ts']-tick_ts
        snap=x.get('snapshot_ts')
        return dict(x,delta_to_tick_s=delta,timing='AFTER_TICK_DIAGNOSTIC_ONLY' if delta>0 else 'AVAILABLE_BY_TICK',
                    snapshot_delta_to_tick_s=snap-tick_ts if number(snap) else None,
                    within_5s=abs(delta)<=5,
                    snapshot_age_at_result_s=x['available_ts']-snap if number(snap) else None)
    sales=[x for x in valid if x.get('accounting_status')=='RECONCILED']
    return dict(nearest_attempt=stamp(pick(valid)),nearest_sale=stamp(pick(sales)),
                sale_before=stamp(pick([x for x in sales if x['available_ts']<=tick_ts])),
                sale_after=stamp(pick([x for x in sales if x['available_ts']>tick_ts])))

def summary(c, signal_report=None):
    settings=paper_capital.settings(c)
    inventory={r['trade_id']:dict(r) for r in c.execute('SELECT * FROM quote_inventory_v0723')}
    ledgers={}
    for r in c.execute('SELECT * FROM capital_ledger_v0721'):ledgers.setdefault(r['trade_id'],{})[r['side']]=dict(r)
    checks={};by_id={}
    for r in c.execute('SELECT * FROM position_checks_v0730 ORDER BY finished_ts,id'):
        row=dict(r);ledger=ledgers.get(row['trade_id'],{}).get('BUY')
        a=local_components(row,inventory.get(row['trade_id']),ledger['total_cost']) if ledger else dict(check_id=row['id'],available_ts=row['finished_ts'],status=row['status'],accounting_status='MISSING_ENTRY_COST')
        checks.setdefault(row['trade_id'],[]).append(a);by_id[row['id']]=a
    selected={x['trade_id']:x for x in (signal_report or {}).get('selected',[])}
    rows=[]
    for sell in c.execute("SELECT * FROM trades WHERE side='SELL' ORDER BY ts"):
        tid=sell['trade_id'];ledger=ledgers.get(tid,{});b=ledger.get('BUY');s=ledger.get('SELL')
        item=dict(trade_id=tid,mint=sell['mint'],tick_ts=sell['ts'],tick_price_sol=sell['price'],
                  reason=sell['reason'],paper=None,pool=nearest(checks.get(tid,[]),sell['ts']),selected_signal_exit=None)
        order=c.execute("SELECT fill_provider_ts,fill_signature,fill_ts FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL' AND status='FILLED' ORDER BY fill_ts LIMIT 1",(tid,)).fetchone()
        item['tick_event']=dict(order) if order else None
        if b and s:
            item['paper']=dict(trading_component=s['notional']-b['notional'],entry_cost=b['total_cost'],
                exit_pool_fee=s['pool_fee'],exit_route_fee=s['route_fee'],exit_fixed_cost=s['fixed_cost'],
                account_setup=None,account_setup_status='NOT_MODELED',
                receipt_units=s['cash_delta'],pnl_units=sell['pnl']-b['total_cost']-s['total_cost'])
            choice=selected.get(tid)
            if choice:
                a=by_id.get(choice['check_id']);item['selected_signal_exit']=a
                if a and a.get('accounting_status')=='RECONCILED' and a.get('gross_basis')=='POOL_SOL_DEBIT':
                    # Replace all exit costs with the same explicit PAPER scenario.
                    # Start from pool debit, never charge again on seller net.
                    rate=s['pool_rate']+settings['route_rate'];fixed=settings['fixed_per_fill']
                    normalized=a['pool_debit_units']*(1-rate)-fixed-b['notional']-b['total_cost']
                    item['common_exit_cost_scenario']=dict(pool_rate=s['pool_rate'],route_rate=settings['route_rate'],fixed=fixed,
                        paper_pnl=item['paper']['pnl_units'],local_pnl=normalized,
                        delta=normalized-item['paper']['pnl_units'],setup_excluded_both=True,
                        note='Diagnostic same-cost scenario, not wallet proceeds. Uses PAPER tick exit fee rate on both price bases. Pool debit may retain embedded fees; time, impact and source differences remain.')
                elif a and a.get('gross_basis')=='SELLER_QUOTE_RECEIPT_BEFORE_ACCOUNT_SETUP':
                    item['common_exit_cost_status']='UNKNOWN_AMM_GROSS_IS_ALREADY_SELLER_QUOTE_RECEIPT'
        rows.append(item)
    paired=[r['common_exit_cost_scenario'] for r in rows if 'common_exit_cost_scenario' in r]
    return dict(version='COST_PRICE_AUDIT_V1',rows=rows,closed_count=len(rows),
        selected_checks=[by_id[x['check_id']] for x in selected.values() if x['check_id'] in by_id],
        accounting_counts=dict(Counter(a['accounting_status'] for a in by_id.values())),
        common_cost_selected_closed=dict(count=len(paired),delta=sum(x['delta'] for x in paired) if paired else None),
        note='Read-only accounting. Nearest means result availability, with snapshot timestamp and signed gap shown separately. Post-close checks are diagnostic only. Missing setup cost is unknown, not zero. No change to PAPER, signal selection, RPC rate or thresholds.')
