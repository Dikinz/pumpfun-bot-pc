"""Expected preflight costs and subsequent deterioration are different observations."""
import json
from decimal import Decimal
SCHEMA='''CREATE TABLE IF NOT EXISTS delta_baselines_v0734(trade_id TEXT PRIMARY KEY,check_id TEXT,available_ts REAL,slot INTEGER,reference_receipt TEXT,identity_json TEXT,details_json TEXT);'''

def expected_cost(c,tid):
    row=c.execute('''SELECT p.job_id,p.result_json,p.finished_ts,p.valid_until,o.fill_ts FROM preentry_v0729 p
      JOIN paper_orders_v0720 o ON o.order_id=p.order_id WHERE o.trade_id=? AND o.side='BUY'
      AND o.status='FILLED' AND p.status='USED' ''',(tid,)).fetchone()
    if not row or not row['finished_ts']<=row['fill_ts']<=row['valid_until']:return None
    r=__import__('json_cache_v1014').loads(row['result_json'] or '{}');budget=r.get('budget_lamports');delta=r.get('all_in_roundtrip_delta_lamports')
    if r.get('status')!='ROUNDTRIP_EXECUTED' or r.get('net_gate')!='PASS' or type(budget)!=int or budget<=0 or type(delta)!=int:return None
    loss=max(0,-delta)
    if loss*10000>budget*1500:return None
    return dict(job_id=row['job_id'],budget_lamports=budget,expected_loss_lamports=loss,
       expected_loss_bps=str(Decimal(loss)*10000/Decimal(budget)),
       token_rent_lamports=r.get('token_account_setup_rent_lamports'),volume_rent_lamports=r.get('buy_volume_rent_lamports'),
       buy_fee_lamports=r.get('buy_local_fee_lamports'),sell_fee_lamports=r.get('sell_local_fee_lamports'),
       note='Pre-entry immediate buy/sell simulation; not a measured future exit or comparable PAPER PNL.')

def record(c,job,r,status,available,config):
    import exit_rules_v33 as rules,price_exit_trial as trial
    tid=job['trade_id'];when=trial.timing(c,tid,available);expected=expected_cost(c,tid)
    base=c.execute('SELECT * FROM delta_baselines_v0734 WHERE trade_id=?',(tid,)).fetchone()
    account=rules.accounting(c,job,r) if r.get('sale',{}).get('status')=='LOCAL_SWAP_EXECUTED' else None
    fresh=(r.get('status')=='CHECKED' and isinstance(r.get('snapshot_received_ts'),(int,float)) and
      0<=available-r['snapshot_received_ts']<=5 and 0<=available-job['scheduled_ts']<=25)
    valid=fresh and status in ('SELLABLE','NET_BELOW_FLOOR','RESERVE_DROP') and r.get('zero_token_control_ok') is True and account is not None
    sale=r.get('sale',{});slot=r.get('slot');volume=sale.get('volume_account_rent_delta_lamports')
    identity=dict(raw_amount=sale.get('raw_amount'),wallet=sale.get('wallet'),fx=(account or {}).get('fx'),size=(account or {}).get('size_units'))
    metadata=(type(volume)==int and volume>=0 and type(slot)==int and bool(identity['raw_amount']) and bool(identity['wallet']))
    details=dict(config_sha256=config['sha256'],expected_entry_cost=expected,status=status,timing=when,accounting=account)
    if when!='BEFORE_CLOSE':decision=when
    elif expected is None:decision='MISSING_EXPECTED_ENTRY_COST'
    elif not valid:decision='UNUSABLE_'+status
    elif not metadata:decision='MISSING_COMPARABLE_METADATA'
    else:
        # Remove one-time account creation from the comparison, but NOT from reported realized PNL.
        normalized=Decimal(account['seller_net_lamports']+volume)*Decimal(account['fx'])/10**9
        ident=json.dumps(identity,sort_keys=True)
        details.update(normalized_receipt_units=str(normalized),volume_rent_removed_from_trigger_only=volume)
        if base is None:
            if not 0<=available-job['opened_ts']<=rules.RULES['delta_baseline_max_delay_s']:decision='BASELINE_WINDOW_MISSED'
            else:
                details['initial_model_pnl_units']=account['pnl_units']
                c.execute('INSERT INTO delta_baselines_v0734 VALUES(?,?,?,?,?,?,?)',(tid,job['id'],available,slot,str(normalized),ident,json.dumps(details)))
                decision='BASELINE_CAPTURED'
        elif base['identity_json']!=ident:decision='COMPARISON_IDENTITY_CHANGED'
        elif slot<=base['slot']:decision='NOT_NEWER_SNAPSHOT'
        else:
            deterioration=Decimal(base['reference_receipt'])-normalized
            threshold=Decimal(account['size_units'])*Decimal(rules.RULES['delta_loss_bps'])/10000
            details.update(baseline_check_id=base['check_id'],baseline_receipt_units=base['reference_receipt'],
               deterioration_units=str(deterioration),trigger_loss_units=str(threshold))
            if deterioration<threshold:decision='NO_TRIGGER'
            elif c.execute("SELECT 1 FROM rules_exits_v0733 WHERE branch='DELTA10' AND trade_id=?",(tid,)).fetchone():decision='ALREADY_SELECTED'
            else:
                c.execute('INSERT INTO rules_exits_v0733 VALUES(?,?,?,?,?,?,?,?)',('DELTA10',tid,job['id'],available,job['mint'],account['receipt_units'],account['pnl_units'],json.dumps(details)))
                decision='SELECTED'
    c.execute('INSERT OR IGNORE INTO rules_decisions_v0733 VALUES(?,?,?,?,?,?)',(job['id'],'DELTA10',tid,available,decision,json.dumps(details)))

def summary(c,now=None):
    import time
    from collections import Counter
    now=time.time() if now is None else now
    rows=[]
    for buy in c.execute("SELECT trade_id,mint,ts FROM trades WHERE side='BUY' ORDER BY ts"):
        row=dict(buy);tid=buy['trade_id'];row['expected']=expected_cost(c,tid)
        b=c.execute('SELECT * FROM delta_baselines_v0734 WHERE trade_id=?',(tid,)).fetchone()
        row['baseline']=dict(b) if b else None
        if b:row['baseline']['details']=json.loads(row['baseline'].pop('details_json'))
        checks=list(c.execute('SELECT started_ts,finished_ts,status FROM position_checks_v0730 WHERE trade_id=? ORDER BY started_ts',(tid,)))
        sell=c.execute("SELECT ts FROM trades WHERE trade_id=? AND side='SELL'",(tid,)).fetchone()
        decisions=list(c.execute("SELECT available_ts,decision FROM rules_decisions_v0733 WHERE branch='DELTA10' AND trade_id=? ORDER BY available_ts",(tid,)))
        first=checks[0] if checks else None
        row['start_delay_s']=first['started_ts']-buy['ts'] if first else None
        finished=[x for x in checks if x['finished_ts'] is not None]
        row['first_result_delay_s']=min(x['finished_ts'] for x in finished)-buy['ts'] if finished else None
        row['last_check_status']=checks[-1]['status'] if checks else None
        if b:reason='CAPTURED'
        elif sell and not any(x['finished_ts'] is not None and x['finished_ts']<sell['ts'] for x in checks):reason='CLOSED_BEFORE_RESULT'
        elif any(x['decision']=='BASELINE_WINDOW_MISSED' for x in decisions):reason='BASELINE_WINDOW_MISSED'
        elif row['expected'] is None:reason='MISSING_EXPECTED_ENTRY_COST'
        elif any(x['decision'].startswith('UNUSABLE_') or x['decision']=='MISSING_COMPARABLE_METADATA' for x in decisions):reason='NO_VALID_INITIAL_RESULT'
        elif not sell and 0<=now-buy['ts']<=10:reason='PENDING_BASELINE'
        elif not checks:reason='NO_CHECK_STARTED'
        else:reason='NO_RESULT_IN_WINDOW'
        row['coverage_reason']=reason
        row['comparable_checks']=sum(x['decision'] in ('NO_TRIGGER','SELECTED','ALREADY_SELECTED') for x in decisions)
        rows.append(row)
    captured=sum(r['baseline'] is not None for r in rows)
    return dict(rows=rows,entries=len(rows),expected_known=sum(r['expected'] is not None for r in rows),baseline_captured=captured,
        baseline_fraction=captured/len(rows) if rows else None,
        comparable_positions=sum(r['comparable_checks']>0 for r in rows),coverage_reasons=dict(Counter(r['coverage_reason'] for r in rows)))
