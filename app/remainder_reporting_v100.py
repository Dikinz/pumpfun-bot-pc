"""Read-only residual valuation and explicit parity exclusions; never ledger PNL."""
import json,math,sqlite3
from pathlib import Path
from contextlib import closing

def has(c,name):return c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(name,)).fetchone() is not None

def record_slot_exclusion(c,tid,now):
    if not has(c,'remainder_entry_exclusions_v100'):return
    role=c.execute("SELECT v FROM meta WHERE k='branch_role'").fetchone()
    if not role or role[0]!='ROUTE_PAPER':return
    held=[r[0] for r in c.execute("SELECT p.trade_id FROM positions p JOIN paper_orders_v0720 o ON p.trade_id=o.trade_id WHERE o.side='SELL' AND o.status='PENDING' AND o.signal_ts<=?",(now-120,))]
    if held:c.execute('INSERT OR IGNORE INTO remainder_entry_exclusions_v100 VALUES(?,?,?,?)',(tid,now,json.dumps(held),'LOCAL_SLOT_BLOCK_WITH_REMAINDER_PRESENT'))

def slot_exclusions(c):
    return dict(count=c.execute('SELECT COUNT(*) FROM remainder_entry_exclusions_v100').fetchone()[0] if has(c,'remainder_entry_exclusions_v100') else 0,
        meaning='Distinct committed PRIMARY entries rejected by the local slot gate while a retained remainder was present. Other later gates were not evaluated; not a missed-profit counter.')

def stress(c,monitor_path,now,capital):
    result=dict(schema='STRESSED_EQUITY_V100',cash_only=capital['net_cash'],all_entry_zero_receipt_pnl=capital['net_cash']-capital['settings']['start'],
        strict_closed_net=capital['closed_net'],open_principal=capital['open_principal'],reserve_capped_gross_upper=0.,equity_upper_before_exit_cost=None,complete=True,rows=[],
        note='Last reserve evidence is not an executable quote or a future-return interval. Exit costs/recovery unknown; no changes to cash, slots or realized PNL.')
    positions=list(c.execute('SELECT trade_id,mint,size FROM positions'))
    if not positions:result['equity_upper_before_exit_cost']=capital['net_cash'];return result
    try:
        with closing(sqlite3.connect(Path(monitor_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as monitor:
            monitor.row_factory=sqlite3.Row
            for position in positions:
                item=dict(trade_id=position['trade_id'],principal=position['size'],status='UNKNOWN',last_evidence_gross_cap=None,current_gross_cap=None)
                row=monitor.execute('SELECT * FROM position_checks_v0730 WHERE trade_id=? ORDER BY started_ts DESC LIMIT 1',(position['trade_id'],)).fetchone()
                if row:
                    import reserve_hint_v98 as hints
                    job=hints.decode(row['input_json']);data=hints.decode(row['result_json']);identity=hints.identity(job)
                    inventory=c.execute('SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(position['trade_id'],)).fetchone()
                    if identity and hints.source_valid(row,job,data,identity) and inventory and dict(inventory)==job.get('inventory') and row['mint']==position['mint']:
                        reserves=data['reserves'];fields=[reserves.get(k) for k in ('real_sol_lamports','spendable_lamports','sell_gross_upper_lamports')]
                        fx=float(inventory['sol_usdc'])
                        if all(type(v)==int and v>=0 for v in fields) and math.isfinite(fx) and fx>0:
                            cap=min(fields)*fx/1e9;age=now-data['snapshot_received_ts'];item.update(last_evidence_gross_cap=cap,evidence_age_s=age,status='STALE_RESERVE_CAP')
                            if 0<=age<=5:item.update(status='FRESH_RESERVE_CAP_NOT_QUOTE',current_gross_cap=cap);result['reserve_capped_gross_upper']+=cap
                if item['current_gross_cap'] is None:result['complete']=False
                result['rows'].append(item)
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):result['complete']=False
    if result['complete']:result['equity_upper_before_exit_cost']=capital['net_cash']+result['reserve_capped_gross_upper']
    return result
