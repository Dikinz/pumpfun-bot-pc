"""Immutable first below-floor local exit; isolated comparison, never a portfolio."""
import json,time
from decimal import Decimal,localcontext
SCHEMA='''CREATE TABLE IF NOT EXISTS monitor_timing_v0731(check_id TEXT PRIMARY KEY,trade_id TEXT,available_ts REAL,state_at_record TEXT,primary_heartbeat_age REAL);
CREATE TABLE IF NOT EXISTS price_trial_decisions_v0731(check_id TEXT PRIMARY KEY,trade_id TEXT,available_ts REAL,decision TEXT);
CREATE TABLE IF NOT EXISTS price_trial_exits_v0731(trade_id TEXT PRIMARY KEY,check_id TEXT UNIQUE,available_ts REAL,mint TEXT,receipt_units TEXT,pnl_units TEXT,details_json TEXT);'''

import exit_rules_v33
SCHEMA+=exit_rules_v33.SCHEMA

def timing(c,tid,available):
    closed=c.execute("SELECT ts FROM trades WHERE trade_id=? AND side='SELL'",(tid,)).fetchone()
    if closed:return 'AFTER_CLOSE' if closed[0]<=available else 'BEFORE_CLOSE'
    return 'BEFORE_CLOSE' if c.execute('SELECT 1 FROM positions WHERE trade_id=?',(tid,)).fetchone() else 'POSITION_UNKNOWN'

def record(c,job,r,status,available):
    tid=job['trade_id'];state=timing(c,tid,available)
    hb=c.execute("SELECT v FROM meta WHERE k='heartbeat_ts'").fetchone()
    age=max(0,available-float(hb[0])) if hb else None
    c.execute('INSERT OR IGNORE INTO monitor_timing_v0731 VALUES(?,?,?,?,?)',(job['id'],tid,available,state,age))
    if state!='BEFORE_CLOSE':decision=state
    elif status not in ('SELLABLE','NET_BELOW_FLOOR','RESERVE_DROP'):decision='UNUSABLE_'+status
    elif status!='NET_BELOW_FLOOR':decision='NO_PRICE_TRIGGER'
    elif not r.get('zero_token_control_ok') or r.get('sale',{}).get('status')!='LOCAL_SWAP_EXECUTED':decision='UNCONFIRMED_SALE'
    elif c.execute('SELECT 1 FROM price_trial_exits_v0731 WHERE trade_id=?',(tid,)).fetchone():decision='ALREADY_SELECTED'
    elif type(r.get('sale',{}).get('seller_net_sol_delta_lamports')) is not int or type(r.get('sale',{}).get('synthetic_token_rent_lamports')) is not int:decision='MISSING_SIMULATION_ACCOUNTING'
    else:
        inv=job.get('inventory') or {};entry=c.execute("SELECT total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(tid,)).fetchone()
        if not inv.get('sol_usdc') or not inv.get('size_usdc') or entry is None:decision='MISSING_ENTRY_ACCOUNTING'
        else:
            sale=r['sale'];rent=int(sale['synthetic_token_rent_lamports']);net=int(sale['seller_net_sol_delta_lamports'])
            with localcontext() as ctx:
                ctx.prec=60
                fx=Decimal(str(inv['sol_usdc']));receipt=Decimal(net-rent)/10**9*fx
                pnl=receipt-Decimal(str(inv['size_usdc']))-Decimal(str(entry[0]))
            details=dict(rule='FIRST_FRESH_NET_BELOW_ENTRY_FLOOR',threshold_lamports=r['minimum_net_lamports'],loss_bps=job.get('trial_loss_bps'),
                snapshot_received_ts=r.get('snapshot_received_ts'),net_before_token_setup_lamports=net,token_setup_lamports=rent,
                fx=str(fx),entry_notional=str(inv['size_usdc']),modeled_entry_cost=str(entry[0]),
                primary_heartbeat_age=age,model='ISOLATED_EXIT_WITH_TOKEN_SETUP',
                note='Synthetic tokens; volume state from control wallet. Token rent charged once for this isolated candidate. No counterfactual portfolio, no real buy/landing/priority fees. PAPER reference has different exit costs.')
            c.execute('INSERT INTO price_trial_exits_v0731 VALUES(?,?,?,?,?,?,?)',(tid,job['id'],available,job['mint'],str(receipt),str(pnl),json.dumps(details)))
            decision='SELECTED'
    c.execute('INSERT OR IGNORE INTO price_trial_decisions_v0731 VALUES(?,?,?,?)',(job['id'],tid,available,decision))
    return state

def summary(c):
    counts=dict(c.execute('SELECT decision,COUNT(*) FROM price_trial_decisions_v0731 GROUP BY decision'))
    rows=[];sim=Decimal(0);paper=Decimal(0);paired=0;late=0
    for row in c.execute('SELECT * FROM price_trial_exits_v0731 ORDER BY available_ts DESC'):
        r=dict(row);r['details']=json.loads(r.pop('details_json'));r['timing']=timing(c,r['trade_id'],r['available_ts'])
        closed=c.execute("SELECT ts,pnl FROM trades WHERE trade_id=? AND side='SELL'",(r['trade_id'],)).fetchone()
        costs=c.execute('SELECT SUM(total_cost) FROM capital_ledger_v0721 WHERE trade_id=?',(r['trade_id'],)).fetchone()[0]
        r['paper_pnl']=float(closed['pnl'])-float(costs) if closed else None
        r['seconds_before_paper_close']=closed['ts']-r['available_ts'] if closed else None
        if r['timing']=='AFTER_CLOSE':late+=1
        elif closed:
            paired+=1;sim+=Decimal(r['pnl_units']);paper+=Decimal(str(r['paper_pnl']))
        rows.append(r)
    return dict(decisions=counts,rows=rows,paired=paired,late_excluded=late,
        local_pnl_units=str(sim) if paired else None,paper_pnl_units=str(paper) if paired else None,
        note='Первый свежий успешный локальный выход ниже порога от входа. Только совпавшие позиции; поздние исключены. Расходы локального выхода и PAPER различаются. Это отдельные случаи, не прибыль портфеля; деньги для новых входов не освобождаются.')
