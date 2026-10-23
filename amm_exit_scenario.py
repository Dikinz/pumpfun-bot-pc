"""Read-only AMM exit after a PAPER sell signal, fixed entries, no reinvestment."""
import json,hashlib
from decimal import Decimal,InvalidOperation,localcontext
from collections import Counter
import signal_exit,cohort_report
POLICY=dict(version='AMM_SIGNAL_EXIT_V1',max_snapshot_age_s=5,max_check_delay_s=25,
            require_post_signal_start=True,fallback='TICK',reinvestment=False,
            cost_model='PAPER_ENTRY_PLUS_ISOLATED_FIRST_AMM_SELL')

def integer(x):return type(x)==int

def assess(c,row,job,result,signal,closed,inventory):
    f=signal_exit.finite;end=row.get('finished_ts');start=row.get('started_ts');snap=result.get('snapshot_received_ts')
    if not f(end) or not f(start):return 'UNFINISHED',None
    if closed is not None and end>=closed:return 'AFTER_OR_AT_TICK_CLOSE',None
    if start<signal:return 'STARTED_BEFORE_SIGNAL',None
    if not f(snap) or not start<=snap<=end or snap<signal or end-snap>5:return 'SNAPSHOT_NOT_TIMELY',None
    scheduled=job.get('scheduled_ts')
    if not f(scheduled) or not 0<=end-scheduled<=25 or scheduled>start:return 'CHECK_NOT_TIMELY',None
    if job.get('route')!='PUMP_AMM' or result.get('route')!='PUMP_AMM':return 'OTHER_ROUTE',None
    sale=result.get('sale') or {};reserves=result.get('reserves') or {}
    if not isinstance(sale,dict) or not isinstance(reserves,dict):return 'INVALID_PAYLOAD',None
    if any(x!='AMM_SALE_EXECUTED' for x in (row.get('status'),result.get('status'),sale.get('status'))):return 'NO_VERIFIED_SALE',None
    if result.get('zero_token_control_ok') is not True or sale.get('err') is not None:return 'NO_VERIFIED_SALE',None
    if result.get('address_status')!='CANONICAL_POOL_VERIFIED' or reserves.get('status')!='AMM_RESERVES_AVAILABLE' or not result.get('pool') or result['pool']!=reserves.get('pool'):return 'POOL_NOT_CONFIRMED',None
    if result.get('broadcast') is not False or result.get('paper_mutated') is not False or sale.get('broadcast') is not False:return 'NOT_ISOLATED_SIMULATION',None
    inv=job.get('inventory') or {}
    if not isinstance(inv,dict) or not isinstance(inventory,dict):return 'INVENTORY_MISMATCH',None
    try:
        if not inventory or job.get('trade_id')!=row['trade_id'] or job.get('mint')!=row['mint']:return 'INVENTORY_MISMATCH',None
        if any(inv.get(k)!=inventory.get(k) for k in ('trade_id','mint')) or inv.get('trade_id')!=row['trade_id'] or inv.get('mint')!=row['mint']:return 'INVENTORY_MISMATCH',None
        for k in ('size_usdc','sol_usdc','token_units','entry_sol_price'):
            a,b=Decimal(str(inv[k])),Decimal(str(inventory[k]))
            if not a.is_finite() or a<=0 or a!=b:return 'INVENTORY_MISMATCH',None
        raw=int(result['raw_amount'])
        if not 0<raw<2**64 or any(str(sale.get(k))!=str(raw) for k in ('raw_amount','tokens_debited','pool_tokens_credited')):return 'AMOUNT_MISMATCH',None
        decimals=result.get('amount_decimals',inventory.get('decimals'))
        if decimals is not None:
            if not integer(decimals) or not 0<=decimals<=18:return 'AMOUNT_MISMATCH',None
            with localcontext() as ctx:
                ctx.prec=60
                if int(Decimal(inv['token_units'])*10**decimals)!=raw:return 'AMOUNT_MISMATCH',None
        if inventory.get('raw_amount') is not None and int(inventory['raw_amount'])!=raw:return 'AMOUNT_MISMATCH',None
        keys=('quote_received_lamports','wallet_sol_delta_lamports','base_account_rent_lamports','quote_account_rent_lamports','net_after_setup_lamports','local_fee_lamports','volume_rent_delta_lamports','pool_rent_topup_lamports')
        if not all(integer(sale.get(k)) for k in keys):return 'MISSING_ACCOUNTING',None
        if sale.get('account_scenario')!='ISOLATED_FIRST_AMM_SELL':return 'UNKNOWN_ACCOUNT_SCENARIO',None
        credit=sale['quote_received_lamports'];delta=sale['wallet_sol_delta_lamports'];base=sale['base_account_rent_lamports'];quote=sale['quote_account_rent_lamports'];net=sale['net_after_setup_lamports']
        fee=sale['local_fee_lamports'];volume=sale['volume_rent_delta_lamports'];topup=sale['pool_rent_topup_lamports']
        if credit<=0 or min(base,quote,fee,volume,topup)<0 or net!=credit+delta-base-quote or delta!=-(fee+volume+topup):return 'ACCOUNTING_MISMATCH',None
        entry=c.execute("SELECT notional,total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(row['trade_id'],)).fetchone()
        if entry is None:return 'MISSING_ACCOUNTING',None
        with localcontext() as ctx:
            ctx.prec=60;size=Decimal(inv['size_usdc']);fx=Decimal(inv['sol_usdc']);cost=Decimal(str(entry['total_cost']))
            if not cost.is_finite() or cost<0 or size!=Decimal(str(entry['notional'])):return 'ACCOUNTING_MISMATCH',None
            receipt=Decimal(net)*fx/10**9;pnl=receipt-size-cost
        return 'ELIGIBLE',dict(receipt_units=str(receipt),pnl_units=str(pnl),size_units=str(size),entry_cost_units=str(cost),fx=str(fx),
            net_after_setup_lamports=net,quote_received_lamports=credit,wallet_sol_delta_lamports=delta,
            base_rent_lamports=base,quote_rent_lamports=quote,local_fee_lamports=fee,volume_rent_lamports=volume,pool_rent_topup_lamports=topup,
            amount_validation='DECIMALS_AND_EFFECTS' if decimals is not None else 'LEGACY_SAVED_RESOLVER_AND_EFFECTS',
            note='First isolated AMM sale: base and quote setup subtracted once; fee, volume rent and pool topup already in wallet delta. Entry FX fixed. Unknown priority/landing/failed-send costs; no wallet lifecycle replay.')
    except (KeyError,ValueError,TypeError,InvalidOperation,OverflowError):return 'INVALID_PAYLOAD',None

def summary(c):
    signals={}
    for r in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL' ORDER BY signal_ts,order_id"):signals.setdefault(r['trade_id'],dict(r))
    closes={r['trade_id']:r['ts'] for r in c.execute("SELECT trade_id,ts FROM trades WHERE side='SELL'")}
    inventories={r['trade_id']:dict(r) for r in c.execute('SELECT * FROM quote_inventory_v0723')}
    selected={};counts=Counter();recent=[]
    for raw in c.execute('SELECT * FROM position_checks_v0730 ORDER BY finished_ts,id'):
        row=dict(raw);tid=row['trade_id'];sig=signals.get(tid)
        if not sig:continue
        try:
            job=json.loads(row['input_json'] or '{}');result=json.loads(row['result_json'] or '{}')
            if not isinstance(job,dict) or not isinstance(result,dict):raise ValueError()
            decision,account=assess(c,row,job,result,sig['signal_ts'],closes.get(tid),inventories.get(tid))
            if decision=='ELIGIBLE':
                if tid in selected:decision='ALREADY_SELECTED'
                else:
                    decision='SELECTED';selected[tid]=dict(trade_id=tid,mint=row['mint'],check_id=row['id'],available_ts=row['finished_ts'],
                        signal_ts=sig['signal_ts'],reason=sig['reason'],snapshot_ts=result['snapshot_received_ts'],raw_amount=result['raw_amount'],
                        receipt_units=account['receipt_units'],pnl_units=account['pnl_units'],accounting=account)
        except (ValueError,TypeError,KeyError):decision='INVALID_PAYLOAD'
        counts[decision]+=1;recent.append(dict(check_id=row['id'],trade_id=tid,decision=decision,available_ts=row['finished_ts']))
    cohort=cohort_report.summary(c,selected=selected)
    cohort['note']='Первый своевременный AMM-результат после сигнала PAPER, иначе TICK. Те же входы, без реинвестирования. Сценарий не освобождает деньги основного PAPER.'
    return dict(policy=POLICY,policy_sha256=hashlib.sha256(json.dumps(POLICY,sort_keys=True).encode()).hexdigest(),
        cohort=cohort,selected=list(selected.values()),decisions=dict(counts),recent=recent[-100:],
        note='Исторический пересчёт и последующее наблюдение по фиксированному правилу; не независимый тест и не исполнение в сети. Счета созданы заново для каждого кандидата.')
