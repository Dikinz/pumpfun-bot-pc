"""Prospectively frozen, separate NET10 and RESERVE50 shadow exits."""
import hashlib,json,time
from decimal import Decimal,localcontext
RULES={'version':'v0.7.44','net_loss_bps':1000,'reserve_drop_bps':5000,
       'reserve_reference':'USED_PREFLIGHT_BEFORE_ENTRY','interval_s':10,'max_result_age_s':5,'max_total_delay_s':25,
       'cost_model':'PAPER_ENTRY_PLUS_LOCAL_EXIT_AND_TOKEN_RENT','fallback':'TICK','reinvestment':False,'entry_expected_loss_bps':1500,'delta_loss_bps':1000,'delta_baseline_max_delay_s':10,'delta_reference':'FIRST_FRESH_POST_ENTRY_RECEIPT_EXCLUDING_SETUP_RENT'}
BRANCHES=('NET10','RESERVE50','DELTA10')
SCHEMA='''CREATE TABLE IF NOT EXISTS rules_config_v0733(id INTEGER PRIMARY KEY CHECK(id=1),created_ts REAL,sha256 TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS rules_decisions_v0733(check_id TEXT,branch TEXT,trade_id TEXT,available_ts REAL,decision TEXT,details_json TEXT,PRIMARY KEY(check_id,branch));
CREATE TABLE IF NOT EXISTS rules_exits_v0733(branch TEXT,trade_id TEXT,check_id TEXT,available_ts REAL,mint TEXT,receipt_units TEXT,pnl_units TEXT,details_json TEXT,PRIMARY KEY(branch,trade_id));'''

import entry_cost_delta
SCHEMA+=entry_cost_delta.SCHEMA

def freeze(c):
    import preentry
    if preentry.MAX_LOSS_BPS!=RULES['entry_expected_loss_bps']:raise RuntimeError('ENTRY_BUDGET_MISMATCH')
    payload=json.dumps(RULES,sort_keys=True,separators=(',',':'));digest=hashlib.sha256(payload.encode()).hexdigest()
    c.execute('INSERT OR IGNORE INTO rules_config_v0733 VALUES(1,?,?,?)',(time.time(),digest,payload))
    row=c.execute('SELECT * FROM rules_config_v0733 WHERE id=1').fetchone()
    if row['sha256']!=digest or row['payload']!=payload:raise RuntimeError('RULES_CHANGED_START_NEW_TEST_DATABASE')
    return dict(row)

def parameters(c):
    row=c.execute('SELECT * FROM rules_config_v0733 WHERE id=1').fetchone()
    return dict(row) if row else None

def baseline(c,tid):
    row=c.execute('''SELECT p.job_id,p.result_json,p.finished_ts,p.valid_until,o.fill_ts
        FROM preentry_v0729 p JOIN paper_orders_v0720 o ON p.order_id=o.order_id
        WHERE o.trade_id=? AND o.side='BUY' AND o.status='FILLED' AND p.status='USED'
        ORDER BY p.finished_ts LIMIT 1''',(tid,)).fetchone()
    if not row or not row['finished_ts']<=row['fill_ts']<=row['valid_until']:return None
    result=json.loads(row['result_json'] or '{}');reserve=result.get('pre_real_sol_lamports')
    if type(reserve)!=int or reserve<=0 or result.get('reserve_gate')!='PASS':return None
    return dict(job_id=row['job_id'],real_sol_lamports=reserve,snapshot_ts=result.get('snapshot_received_ts'))

def accounting(c,job,r):
    sale=r.get('sale',{});net=sale.get('seller_net_sol_delta_lamports');rent=sale.get('synthetic_token_rent_lamports')
    if type(net)!=int or type(rent)!=int or rent<0:return None
    inv=job.get('inventory') or {};entry=c.execute("SELECT notional,total_cost FROM capital_ledger_v0721 WHERE trade_id=? AND side='BUY'",(job['trade_id'],)).fetchone()
    if not entry or not inv.get('sol_usdc') or not inv.get('size_usdc'):return None
    with localcontext() as ctx:
        ctx.prec=60
        size=Decimal(str(inv['size_usdc']));fx=Decimal(str(inv['sol_usdc']));cost=Decimal(str(entry['total_cost']))
        if not all(v.is_finite() for v in (size,fx,cost)) or size<=0 or fx<=0 or size!=Decimal(str(entry['notional'])):return None
        receipt=Decimal(net-rent)*fx/10**9;pnl=receipt-size-cost
    return dict(receipt_units=str(receipt),pnl_units=str(pnl),size_units=str(size),entry_cost_units=str(cost),fx=str(fx),
        seller_net_lamports=net,token_rent_lamports=rent,local_fee_lamports=sale.get('local_fee_from_balance_diff_lamports'),
        volume_rent_lamports=sale.get('volume_account_rent_delta_lamports'),
        note='Local fee and volume rent already in seller net; token rent subtracted once per isolated candidate. Priority/landing costs unknown; no full alternative wallet lifecycle.')

def record(c,job,r,status,available):
    import price_exit_trial as trial
    frozen=freeze(c);tid=job['trade_id'];when=trial.timing(c,tid,available)
    reference=baseline(c,tid);current=r.get('reserves',{}).get('real_sol_lamports')
    fresh=(r.get('status')=='CHECKED' and isinstance(r.get('snapshot_received_ts'),(int,float))
           and 0<=available-r['snapshot_received_ts']<=RULES['max_result_age_s']
           and 0<=available-job['scheduled_ts']<=RULES['max_total_delay_s'])
    executable=(fresh and status in ('SELLABLE','NET_BELOW_FLOOR','RESERVE_DROP')
                and r.get('sale',{}).get('status')=='LOCAL_SWAP_EXECUTED' and r.get('zero_token_control_ok') is True)
    account=accounting(c,job,r) if executable else None
    reserve_trigger=(reference is not None and type(current)==int and current>=0 and
                     current*10000<=reference['real_sol_lamports']*(10000-RULES['reserve_drop_bps']))
    for branch in ('NET10','RESERVE50'):
        details=dict(config_sha256=frozen['sha256'],status=status,timing=when,reference=reference,current_reserve_lamports=current,accounting=account)
        if when!='BEFORE_CLOSE':decision=when
        elif not fresh:decision='UNUSABLE_'+status
        elif branch=='RESERVE50' and reference is None:decision='MISSING_RESERVE_BASELINE'
        elif branch=='RESERVE50' and not reserve_trigger:decision='NO_TRIGGER'
        elif not executable:decision='TRIGGER_UNEXECUTABLE' if branch=='RESERVE50' else 'NET_UNAVAILABLE_'+status
        elif account is None:decision='MISSING_ACCOUNTING'
        else:
            triggered=reserve_trigger if branch=='RESERVE50' else Decimal(account['pnl_units'])<=-Decimal(account['size_units'])*Decimal(RULES['net_loss_bps'])/10000
            if not triggered:decision='NO_TRIGGER'
            elif c.execute('SELECT 1 FROM rules_exits_v0733 WHERE branch=? AND trade_id=?',(branch,tid)).fetchone():decision='ALREADY_SELECTED'
            else:
                c.execute('INSERT INTO rules_exits_v0733 VALUES(?,?,?,?,?,?,?,?)',
                    (branch,tid,job['id'],available,job['mint'],account['receipt_units'],account['pnl_units'],json.dumps(details)))
                decision='SELECTED'
        c.execute('INSERT OR IGNORE INTO rules_decisions_v0733 VALUES(?,?,?,?,?,?)',(job['id'],branch,tid,available,decision,json.dumps(details)))

    entry_cost_delta.record(c,job,r,status,available,frozen)

def summary(c):
    import cohort_report,price_exit_trial
    reports={}
    for branch in BRANCHES:
        selected={r['trade_id']:dict(r) for r in c.execute('SELECT * FROM rules_exits_v0733 WHERE branch=?',(branch,))}
        counts=dict(c.execute('SELECT decision,COUNT(*) FROM rules_decisions_v0733 WHERE branch=? GROUP BY decision',(branch,)))
        cohort=cohort_report.summary(c,selected=selected)
        for row in cohort['rows']:
            first=c.execute("SELECT MIN(available_ts) FROM rules_decisions_v0733 WHERE branch=? AND trade_id=? AND decision IN ('SELECTED','TRIGGER_UNEXECUTABLE')",(branch,row['trade_id'])).fetchone()[0]
            row['first_trigger_ts']=first
        late=sum(price_exit_trial.timing(c,r['trade_id'],r['available_ts'])=='AFTER_CLOSE' for r in selected.values())
        reports[branch]=dict(cohort,decisions=counts,late_excluded=late,
            recent=[dict(r) for r in c.execute('SELECT check_id,trade_id,available_ts,decision,details_json FROM rules_decisions_v0733 WHERE branch=? ORDER BY available_ts DESC LIMIT 100',(branch,))])
    return dict(config=parameters(c),branches=reports,entry_costs=entry_cost_delta.summary(c),
        note='DELTA10 и два контрольных правила, одинаковые входы. При отсутствии подтверждённого выхода используется PAPER TICK. Это смешанная модель исполнения, не доказательство возможности продать в сети.')
