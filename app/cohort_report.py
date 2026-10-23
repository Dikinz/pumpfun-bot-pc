"""All fixed PAPER entries, no reinvestment; local exit override with TICK fallback."""
import json,time
import price_exit_trial as trial
import paper_capital as capital

def health(c,now=None):
    now=time.time() if now is None else now
    rows=list(c.execute('SELECT status,finished_ts,result_json FROM position_checks_v0730 ORDER BY finished_ts DESC'))
    import position_monitor as pm
    valid=pm.GOOD|{'AMM_SALE_EXECUTED','AMM_INSUFFICIENT_RESERVES','AMM_SIMULATION_FAILED'}
    done=[r for r in rows if r['status']!='RUNNING'];streak=0
    for r in done:
        if r['status']=='INTERRUPTED_STOP':continue
        if r['status'] not in pm.TECHNICAL:break
        streak+=1
    good=next((r for r in done if r['status'] in valid),None)
    age=max(0,now-good['finished_ts']) if good else None
    positions=list(c.execute('SELECT trade_id,opened_ts FROM positions'))
    waiting=sum(pm.migrated(c,p['trade_id']) for p in positions)
    overdue=any(now>pm.next_due(c,p['trade_id'],p['opened_ts'])+pm.TIMEOUT for p in positions)
    import connection_audit
    position_streak=max((connection_audit.retry(c,p['trade_id'])['streak'] for p in positions),default=0)
    streak=max(streak,position_streak)
    state='IDLE' if not positions else 'ERROR' if streak>=3 else 'STALE' if overdue else 'MIGRATION_LAB' if waiting else 'NO_RESULT' if not good else 'OK'
    return dict(completed=len(done),useful=sum(r['status'] in valid for r in done),errors_in_row=streak,
        technical_errors=sum(r['status'] in pm.TECHNICAL for r in done),migration_positions=waiting,
        last_useful_age_s=age,state=state,last_diagnostic=json.loads(done[0]['result_json'] or '{}') if done else {})

def summary(c,selected=None):
    base=capital.summary(c)
    if selected is None:selected={r['trade_id']:dict(r) for r in c.execute('SELECT * FROM price_trial_exits_v0731')}
    incomplete=0
    matched=[];released=[]
    rows=[];trial_cash=base['settings']['start'];trial_pnl=0.;paper_pnl=0.;open_principal=0.;chosen=0
    for b in c.execute("SELECT * FROM trades WHERE side='BUY' ORDER BY ts"):
        tid=b['trade_id'];sell=c.execute("SELECT * FROM trades WHERE trade_id=? AND side='SELL'",(tid,)).fetchone()
        ledger={r['side']:dict(r) for r in c.execute('SELECT * FROM capital_ledger_v0721 WHERE trade_id=?',(tid,))}
        if 'BUY' not in ledger or (sell and 'SELL' not in ledger):
            incomplete+=1;rows.append(dict(trade_id=tid,mint=b['mint'],status='MISSING_ACCOUNTING',paper_net=None,trial_net=None,checks=0));continue
        buy_cost=ledger['BUY']['total_cost'];trial_cash-=b['size']+buy_cost
        candidate=selected.get(tid)
        if candidate and trial.timing(c,tid,candidate['available_ts'])=='AFTER_CLOSE':candidate=None
        paper_net=sell['pnl']-sum(r['total_cost'] for r in ledger.values()) if sell else None
        if paper_net is not None:paper_pnl+=paper_net
        if candidate:
            receipt=float(candidate['receipt_units']);net=receipt-b['size']-buy_cost;chosen+=1;status='LOCAL_EXIT'
        elif sell:
            receipt=ledger['SELL']['cash_delta'];net=paper_net;status='TICK_FALLBACK'
        else:
            receipt=0.;net=None;open_principal+=b['size'];status='OPEN_UNRESOLVED'
        if sell:matched.append(dict(trade_id=tid,paper_net=paper_net,trial_net=net))
        elif candidate:released.append(dict(trade_id=tid,mint=b['mint'],paper_principal=b['size'],receipt_units=receipt,trial_net=net,available_ts=candidate['available_ts']))
        trial_cash+=receipt
        if net is not None:trial_pnl+=net
        checks=c.execute('SELECT COUNT(*) FROM position_checks_v0730 WHERE trade_id=?',(tid,)).fetchone()[0]
        rows.append(dict(trade_id=tid,mint=b['mint'],status=status,paper_net=paper_net,trial_net=net,checks=checks,local_exit_ts=candidate['available_ts'] if candidate else None,seconds_before_paper_exit=sell['ts']-candidate['available_ts'] if candidate and sell else None))
    unresolved=sum(r['status']=='OPEN_UNRESOLVED' for r in rows)
    return dict(matched_closed=dict(count=len(matched),paper_pnl=sum(x['paper_net'] for x in matched) if not incomplete else None,trial_pnl=sum(x['trial_net'] for x in matched) if not incomplete else None,delta=sum(x['trial_net']-x['paper_net'] for x in matched) if not incomplete else None),
        released_while_paper_open=dict(count=len(released),paper_principal=sum(x['paper_principal'] for x in released),receipts=sum(x['receipt_units'] for x in released) if not incomplete else None,realized_pnl=sum(x['trial_net'] for x in released) if not incomplete else None,rows=released),
        cash_difference=trial_cash-base['available'] if not incomplete else None,accounting_missing=incomplete,entries=len(rows),paper_closed=sum(r['paper_net'] is not None for r in rows),paper_pnl=paper_pnl if not incomplete else None,
        paper_available=base['available'],paper_open_principal=base['open_principal'],paper_open=len(rows)-sum(r['paper_net'] is not None for r in rows),
        trial_realized_pnl=trial_pnl if not incomplete else None,trial_cash_fixed_entries=trial_cash if not incomplete else None,trial_open_principal=open_principal,
        trial_unresolved=unresolved,trial_unresolved_fraction=unresolved/len(rows) if rows else None,
        local_exits=chosen,tick_fallback=sum(r['status']=='TICK_FALLBACK' for r in rows),rows=rows,
        note='Одинаковые фактические входы. Первый подтверждённый выход ниже порога, иначе обычный TICK. Незакрытые остаются незакрытыми. Деньги сценария не используются для новых входов. Модели расходов выхода различаются; это не чистый эффект времени выхода и не реальная доходность.')
