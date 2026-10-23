"""Deterministic read-only shadow exit from persisted monitor evidence.

No orders, network calls, wallet or PAPER mutations. Selection uses availability,
never best price. Historical replay is diagnostic, not prospective validation.
"""
import json, math, hashlib
from decimal import Decimal, InvalidOperation
from collections import Counter
import exit_rules_v33, cohort_report

POLICY = dict(version='SIGNAL_EXIT_V1', max_snapshot_age_s=5, max_check_delay_s=25,
              require_post_signal_start=True, fallback='TICK', reinvestment=False,
              cost_model='PAPER_ENTRY_PLUS_LOCAL_EXIT_AND_TOKEN_RENT')

def finite(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)

def eligibility(row, job, result, signal, closed, inventory):
    end=row.get('finished_ts');start=row.get('started_ts');snap=result.get('snapshot_received_ts')
    if not finite(end):return 'UNFINISHED'
    if closed is not None and end>=closed:return 'AFTER_OR_AT_TICK_CLOSE'
    if not finite(start) or start<signal:return 'STARTED_BEFORE_SIGNAL'
    if not finite(snap) or snap<signal or not 0<=end-snap<=POLICY['max_snapshot_age_s']:return 'SNAPSHOT_NOT_TIMELY'
    scheduled=job.get('scheduled_ts')
    if not finite(scheduled) or not 0<=end-scheduled<=POLICY['max_check_delay_s'] or end<start:return 'CHECK_NOT_TIMELY'
    if job.get('route','PUMP_CURVE')!='PUMP_CURVE':return 'ROUTE_NOT_SUPPORTED_IN_V1'
    if row.get('status') not in ('SELLABLE','NET_BELOW_FLOOR','RESERVE_DROP'):return 'NO_VERIFIED_SALE'
    sale=result.get('sale') or {}
    if not isinstance(sale,dict) or not isinstance(result.get('reserves'),dict):return 'INVALID_PAYLOAD'
    if result.get('status')!='CHECKED' or sale.get('status')!='LOCAL_SWAP_EXECUTED' or result.get('zero_token_control_ok') is not True:return 'NO_VERIFIED_SALE'
    if result['reserves'].get('status')!='RESERVES_COVER_GROSS':return 'RESERVES_NOT_CONFIRMED'
    inv=job.get('inventory') or {}
    try:
        if not inventory or sale.get('mint')!=row['mint'] or inv.get('mint')!=row['mint'] or inv.get('trade_id')!=row['trade_id']:return 'INVENTORY_MISMATCH'
        for key in ('size_usdc','sol_usdc','token_units','entry_sol_price'):
            a,b=Decimal(str(inv[key])),Decimal(str(inventory[key]))
            if not a.is_finite() or a<=0 or a!=b:return 'INVENTORY_MISMATCH'
        raw=inventory.get('raw_amount')
        if raw is None:return 'INVENTORY_UNVERIFIED'
        if int(raw)<=0 or str(sale.get('raw_amount'))!=str(raw):return 'AMOUNT_MISMATCH'
    except (KeyError,TypeError,ValueError,InvalidOperation):return 'INVENTORY_UNVERIFIED'
    return 'ELIGIBLE'

def summary(c):
    signals={}
    for r in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL' ORDER BY signal_ts,order_id"):
        signals.setdefault(r['trade_id'],dict(r))
    closes={r['trade_id']:r['ts'] for r in c.execute("SELECT trade_id,ts FROM trades WHERE side='SELL'")}
    inventory={r['trade_id']:dict(r) for r in c.execute('SELECT * FROM quote_inventory_v0723')}
    selected={};decisions=[];counts=Counter()
    for raw in c.execute('SELECT * FROM position_checks_v0730 ORDER BY finished_ts,id'):
        row=dict(raw);tid=row['trade_id'];signal=signals.get(tid)
        if not signal:continue
        account=None
        try:
            job=json.loads(row.get('input_json') or '{}');result=json.loads(row.get('result_json') or '{}')
            if not isinstance(job,dict) or not isinstance(result,dict):raise ValueError('object required')
            decision=eligibility(row,job,result,signal['signal_ts'],closes.get(tid),inventory.get(tid))
            if decision=='ELIGIBLE':
                account=exit_rules_v33.accounting(c,dict(job,trade_id=tid),result)
                if account is None:decision='MISSING_ACCOUNTING'
                elif tid in selected:decision='ALREADY_SELECTED'
                else:
                    decision='SELECTED'
                    selected[tid]=dict(trade_id=tid,mint=row['mint'],check_id=row['id'],available_ts=row['finished_ts'],
                        signal_ts=signal['signal_ts'],reason=signal['reason'],delay_s=row['finished_ts']-signal['signal_ts'],
                        snapshot_ts=result['snapshot_received_ts'],raw_amount=result['sale']['raw_amount'],
                        receipt_units=account['receipt_units'],pnl_units=account['pnl_units'],accounting=account)
        except (ValueError,TypeError,KeyError,InvalidOperation):decision='INVALID_PAYLOAD'
        counts[decision]+=1
        decisions.append(dict(check_id=row['id'],trade_id=tid,available_ts=row.get('finished_ts'),decision=decision))
    cohort=cohort_report.summary(c,selected=selected)
    cohort['note']='Одинаковые входы; первый своевременный результат проверки, начатой после сигнала PAPER, иначе TICK. Без реинвестирования. Изолированные расходы симуляции отличаются от PAPER; это не исполнение в сети.'
    return dict(policy=POLICY,policy_sha256=hashlib.sha256(json.dumps(POLICY,sort_keys=True).encode()).hexdigest(),cohort=cohort,selected=list(selected.values()),decisions=dict(counts),
                recent=decisions[-100:],signals=len(signals),
                note='Расчёт по сохранённым проверкам, без новых RPC и изменения PAPER. Продажа ниже порога допустима в этой ветке: сигнал уже дал PAPER. Исторический пересчёт не является независимым тестом.')
