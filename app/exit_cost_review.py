"""Read-only review of observed exits, not a new execution or wallet model."""
import csv,json,math,sqlite3,zipfile,io,tempfile,time,hashlib
from pathlib import Path
from contextlib import closing
from decimal import Decimal,InvalidOperation,localcontext
import exit_timeline,cost_price_audit

def finite(x):return type(x) in (int,float) and math.isfinite(x)
def equal(a,b):
    try:
        a,b=Decimal(str(a)),Decimal(str(b));return a.is_finite() and b.is_finite() and a==b
    except (InvalidOperation,TypeError,ValueError):return False
def decoded(text):
    try:
        d=json.loads(text or '{}');return d if isinstance(d,dict) else {}
    except (ValueError,TypeError):return {}
def table(c,name):return bool(c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone())
def one(c,sql,params=()):return dict(c.execute(sql,params).fetchone() or {})
def evidence(value):
    value=value if isinstance(value,dict) else {}
    return {k:value.get(k) for k in ('source','price_basis','signature','provider_ts','received_ts','provider_lag_ms','pool_fee_rate','pool','quote_mint','clock_status')}
def event_sample(event):
    return dict(wall_ts=event.get('wall_ts'),price=event.get('price'),side=event.get('side'),
      validation=event.get('validation'),eligibility=event.get('eligibility'),evidence=evidence(event.get('evidence')))

def checks(c,monitor):
    """Resolve RAW only from this frozen inventory and this check's decimals."""
    result={}
    if monitor is None or not table(monitor,'position_checks_v0730'):return result
    inventories={r['trade_id']:dict(r) for r in c.execute('SELECT * FROM quote_inventory_v0723')}
    fees={r['trade_id']:r['total_cost'] for r in c.execute("SELECT * FROM capital_ledger_v0721 WHERE side='BUY'")}
    for row in monitor.execute('SELECT * FROM position_checks_v0730 ORDER BY finished_ts,id'):
        row=dict(row);r=decoded(row.get('result_json'));j=decoded(row.get('input_json'));sale=r.get('sale') or {}
        inv=dict(inventories.get(row['trade_id']) or {});dec=r.get('amount_decimals')
        if inv and inv.get('raw_amount') is None and type(dec)==int and 0<=dec<=18:
            try:
                with localcontext() as ctx:
                    ctx.prec=80
                    inv.update(raw_amount=str(int(Decimal(inv['token_units'])*10**dec)),decimals=dec)
            except (InvalidOperation,KeyError,ValueError,TypeError):pass
        component=cost_price_audit.local_components(row,inv,fees[row['trade_id']]) if row['trade_id'] in fees else dict(accounting_status='MISSING_ENTRY_COST',check_id=row['id'])
        identity=(j.get('trade_id')==row['trade_id'] and j.get('mint')==row['mint'] and
          r.get('broadcast') is False and r.get('paper_mutated') is False and sale.get('broadcast') is False and
          sale.get('paper_mutated') is False and sale.get('err') is None and
          str(sale.get('tokens_debited'))==str(sale.get('pool_tokens_credited')))
        if component.get('accounting_status')=='RECONCILED' and not identity:component['accounting_status']='IDENTITY_OR_ISOLATION_MISMATCH'
        component.update(scheduled_ts=j.get('scheduled_ts'),started_ts=row.get('started_ts'),available_ts=row.get('finished_ts'),
          snapshot_ts=r.get('snapshot_received_ts'),status=row['status'],route=j.get('route'),mint=row['mint'],
          raw_amount=sale.get('raw_amount'),inventory_matches=component.get('amount_matches',False),
          inventory_token_units=inv.get('token_units'),entry_fx=inv.get('sol_usdc'))
        stamps=[component.get(k) for k in ('scheduled_ts','started_ts','snapshot_ts','available_ts')]
        component['timing_status']='VALID' if all(finite(x) for x in stamps) and stamps==sorted(stamps) else 'INVALID_OR_MISSING'
        result.setdefault(row['trade_id'],[]).append(component)
    return result

def nearest(rows,stamp):
    if not finite(stamp):return dict(status='MISSING_TARGET_TIME',before=None,after=None)
    valid=[r for r in rows if finite(r.get('available_ts')) and r.get('accounting_status')=='RECONCILED' and r.get('timing_status')=='VALID']
    def pick(candidates):
        if not candidates:return None
        r=min(candidates,key=lambda r:(abs(r['available_ts']-stamp),r['check_id']))
        age=stamp-r['snapshot_ts'] if finite(r.get('snapshot_ts')) else None
        return dict(r,delta_to_target_s=r['available_ts']-stamp,snapshot_age_at_target_s=age,
          available_by_target=r['available_ts']<=stamp,fresh_reference_at_target=r['available_ts']<=stamp and age is not None and 0<=age<=5,
          timing='AVAILABLE_BY_TARGET' if r['available_ts']<=stamp else 'AFTER_TARGET_DIAGNOSTIC_ONLY')
    return dict(status='AVAILABLE' if valid else 'NO_RECONCILED_RESULT',
      before=pick([r for r in valid if r['available_ts']<=stamp]),after=pick([r for r in valid if r['available_ts']>stamp]))

def scenario(c,role,monitor=None):
    c.row_factory=sqlite3.Row
    if monitor is not None:monitor.row_factory=sqlite3.Row
    checkmap=checks(c,monitor);rows=[]
    for buy in c.execute("SELECT * FROM trades WHERE side='BUY' ORDER BY id"):
        tid=buy['trade_id'];sell=one(c,"SELECT * FROM trades WHERE trade_id=? AND side='SELL'",(tid,))
        if not sell:continue
        order=one(c,"SELECT * FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL' ORDER BY rowid DESC LIMIT 1",(tid,))
        entry_order=one(c,"SELECT fill_signature FROM paper_orders_v0720 WHERE trade_id=? AND side='BUY' AND status='FILLED'",(tid,))
        audit=one(c,'SELECT data_json FROM exit_audit_v45 WHERE trade_id=?',(tid,))
        payload=decoded(audit.get('data_json'));signal=payload.get('order') or order
        costs=[dict(r) for r in c.execute('SELECT * FROM capital_ledger_v0721 WHERE trade_id=?',(tid,))]
        fees={r['side']:r for r in costs};complete=set(fees)=={'BUY','SELL'}
        inv=one(c,'SELECT token_units,sol_usdc,entry_sol_price,size_usdc FROM quote_inventory_v0723 WHERE trade_id=?',(tid,))
        attribution=exit_timeline.attribution(dict(buy),sell,signal,costs)
        events=payload.get('events_before_fill') or (payload.get('checkpoint') or {}).get('events') or []
        eligible=[e for e in events if e.get('eligibility')=='ELIGIBLE' and finite(e.get('wall_ts')) and finite(signal.get('signal_ts')) and e['wall_ts']>=signal['signal_ts']]
        first_eligible=min(eligible,key=lambda e:e['wall_ts']) if eligible else None
        monotonic_ms=payload.get('signal_to_fill_monotonic_ms')
        row=dict(role=role,trade_id=tid,mint=buy['mint'],entry_ts=buy['ts'],entry_price=buy['price'],size=buy['size'],
          entry_signature=entry_order.get('fill_signature'),inventory=inv,signal_ts=signal.get('signal_ts'),signal_price=signal.get('signal_price'),
          fill_ts=sell['ts'],fill_price=sell['price'],reason=sell['reason'],gross=sell['pnl'],
          entry_cost=fees.get('BUY',{}).get('total_cost'),exit_cost=fees.get('SELL',{}).get('total_cost'),
          net=sell['pnl']-sum(r['total_cost'] for r in costs) if complete else None,
          attribution=attribution,signal_to_fill_ms=monotonic_ms if finite(monotonic_ms) else
          (sell['ts']-signal['signal_ts'])*1000 if finite(signal.get('signal_ts')) else None,
          elapsed_basis='MONOTONIC_SAME_PROCESS' if finite(monotonic_ms) else 'WALL_CLOCK',
          fill_provider_lag_ms=order.get('provider_lag_ms'),fill_price_age_ms=order.get('price_age_ms'),
          retained_events=len(events),retained_eligibility_counts=dict(__import__('collections').Counter(e.get('eligibility','NOT_RECORDED') for e in events)),
          audit_events_seen=payload.get('events_seen'),audit_events_dropped=payload.get('events_dropped'),
          audit_process_restarted=(payload.get('checkpoint') or {}).get('process_restarted'),
          signal_evidence=evidence(payload.get('signal_evidence')),fill_evidence=evidence(payload.get('fill_evidence')),
          first_retained_eligible_event=event_sample(first_eligible) if first_eligible else None,
          retained_event_samples=[event_sample(e) for e in events[:10]+events[10:][-10:]],
          nearest_signal=nearest(checkmap.get(tid,[]),signal.get('signal_ts')),nearest_fill=nearest(checkmap.get(tid,[]),sell['ts']),
          setup_status='NOT_MODELED_UNKNOWN' if role=='primary' else 'NO_VERIFIED_SETTLEMENT',settlement=None)
        if table(c,'comparison_lots_v64'):
            ref=one(c,'SELECT lot_hash FROM comparison_lots_v64 WHERE trade_id=?',(tid,))
            receipt=one(c,'SELECT lot_hash,signal_status FROM comparison_receipts_v64 WHERE trade_id=?',(tid,))
            row.update(shared_lot_hash=ref.get('lot_hash') or receipt.get('lot_hash'),shared_signal_id=signal.get('order_id'),
              shared_signal_status='SOURCE' if role=='primary' else receipt.get('signal_status'))
        if table(c,'branch_amm_settlements'):
            settlement=one(c,'SELECT * FROM branch_amm_settlements WHERE trade_id=?',(tid,))
            if settlement:
                details=decoded(settlement.get('details'));account=details.get('accounting') or {}
                component=next((x for x in checkmap.get(tid,[]) if x['check_id']==settlement['check_id']),None)
                row['settlement']=dict(check_id=settlement['check_id'],route=details.get('route'),available_ts=settlement['available_ts'],
                  settled_ts=settlement['settled_ts'],delivery_ms=(settlement['settled_ts']-settlement['available_ts'])*1000,
                  signal_to_result_ms=(settlement['available_ts']-signal['signal_ts'])*1000 if finite(signal.get('signal_ts')) else None,
                  components=component,raw_amount=component.get('raw_amount') if component else None)
                if component and finite(signal.get('signal_ts')):
                    ts=[signal['signal_ts'],component.get('scheduled_ts'),component.get('started_ts'),component.get('available_ts'),settlement['settled_ts']]
                    if all(finite(x) for x in ts) and ts==sorted(ts):
                        row['settlement']['timing_ms']=dict(zip(('signal_to_scheduled_check','recorded_schedule_gap','simulation_worker','delivery'),[(b-a)*1000 for a,b in zip(ts,ts[1:])]))
                        row['settlement']['timing_note']='Recorded scheduled/start stamps can coincide; zero recorded gap is not proof of zero scheduler queue. Signal-to-check interval includes monitor cadence and unmeasured waiting.'
                if component and component.get('accounting_status')=='RECONCILED' and row['net'] is not None:
                    # v0.7.101.6 ROUTE wallet model: the ledger differs from the strict isolated sale by the booked wallet adjustment.
                    wallet=account.get('cost_model')
                    try:adjust=float(account['receipt_units'])-float(account['strict_receipt_units']) if wallet else 0.
                    except (KeyError,TypeError,ValueError):adjust=None
                    matches=adjust is not None and all(math.isclose(a,b,abs_tol=1e-8,rel_tol=1e-9) for a,b in
                      [(row['net'],component['pnl_units']+adjust),(settlement['receipt'],component['receipt_units']+adjust),
                       (settlement['gross'],component['pool_debit_units']),
                       (settlement['cost'],row['exit_cost'])])
                    row['setup_status']=('RECONCILED_WALLET_MODEL' if wallet else 'RECONCILED_ISOLATED') if matches else 'LEDGER_MISMATCH'
                    if matches and wallet:row.update(cost_model=wallet,wallet_adjustment=adjust,strict_net=component['pnl_units'])
                    if matches:
                        row['setup_excluded_sensitivity_net']=component['pnl_units']+component['token_account_setup']+component['volume_account_setup']
                        row['sensitivity_note']='NOT_REALIZED_PNL_OR_WALLET_REPLAY; refund/reuse not proven; original ledger unchanged'
                elif account.get('cost_model')=='ROUTE_WRITE_OFF_V1017' and row['net'] is not None:
                    # v0.7.101.7: drained curve, no sale to reconcile; the ledger must book exactly the lost entry.
                    try:matches=math.isclose(row['net'],float(account['pnl_units']),abs_tol=1e-8) and settlement['receipt']==0
                    except (KeyError,TypeError,ValueError):matches=False
                    row['setup_status']='WRITTEN_OFF_DRAINED_CURVE' if matches else 'LEDGER_MISMATCH'
                    if matches:row.update(cost_model=account['cost_model'],strict_net=row['net'],setup_excluded_sensitivity_net=row['net'])
        rows.append(row)
    return rows

def pairs(primary,route):
    """Mint alone cannot establish an identical position or token quantity."""
    out=[]
    for r in route:
        candidates=[p for p in primary if p['mint']==r['mint'] and (not r.get('shared_lot_hash') or p['trade_id']==r['trade_id'])]
        if len(candidates)!=1:
            out.append(dict(mint=r['mint'],match_status='NO_UNIQUE_PRIMARY_ENTRY'));continue
        p=candidates[0];flags=[]
        if p.get('shared_lot_hash') or r.get('shared_lot_hash'):
            if not p.get('shared_lot_hash') or p.get('shared_lot_hash')!=r.get('shared_lot_hash'):flags.append('SHARED_LOT_HASH_MISMATCH')
        if not p.get('entry_signature') or p['entry_signature']!=r.get('entry_signature'):flags.append('ENTRY_EVENT_DIFFERS_OR_UNKNOWN')
        for name,a,b in [('ENTRY_PRICE',p['entry_price'],r['entry_price']),('SIZE',p['size'],r['size']),
          ('TOKEN_AMOUNT',p['inventory'].get('token_units'),r['inventory'].get('token_units')),
          ('ENTRY_FX',p['inventory'].get('sol_usdc'),r['inventory'].get('sol_usdc'))]:
            if not equal(a,b):flags.append(name+'_DIFFERS_OR_UNKNOWN')
        known=p['net'] is not None and r['net'] is not None
        out.append(dict(mint=r['mint'],primary_trade_id=p['trade_id'],route_trade_id=r['trade_id'],
          match_status='IDENTICAL_ENTRY_INVENTORY' if not flags else 'DESCRIPTIVE_MATCH_ONLY',mismatch_flags=flags,
          primary_net=p['net'],route_net=r['net'],net_difference=r['net']-p['net'] if known else None,
          recorded_gross_difference=r['gross']-p['gross'],
          recorded_cost_difference=r['entry_cost']+r['exit_cost']-p['entry_cost']-p['exit_cost'] if known else None,
          cost_basis='PRIMARY_MODELED_FEES_AND_UNKNOWN_SETUP_VS_ISOLATED_BALANCE_DELTAS',
          causal_price_effect=None,alternative_wallet_net=None))
        out[-1]['same_shared_sell_signal']=bool(p.get('shared_signal_id') and p.get('shared_signal_id')==r.get('shared_signal_id') and r.get('shared_signal_status')=='SHARED_SIGNAL')
    return out

def build(paths,source=None):
    from contextlib import ExitStack
    rows=[]
    with ExitStack() as stack:
        for role,(database,monitor) in paths.items():
            c=stack.enter_context(closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)));c.execute('BEGIN')
            m=stack.enter_context(closing(sqlite3.connect(Path(monitor).resolve().as_uri()+'?mode=ro',uri=True))) if monitor and Path(monitor).exists() else None
            if m:m.execute('BEGIN')
            rows+=scenario(c,role,m)
    totals={}
    for role in paths:
        subset=[r for r in rows if r['role']==role];sold=[r for r in subset if r['setup_status'] in ('RECONCILED_ISOLATED','RECONCILED_WALLET_MODEL')]
        reconciled=sold+[r for r in subset if r['setup_status']=='WRITTEN_OFF_DRAINED_CURVE']  # write-offs: no sale, no exit setup
        complete=all(r['net'] is not None for r in subset)
        totals[role]=dict(closed=len(subset),accounted=sum(r['net'] is not None for r in subset),net=sum(r['net'] for r in subset) if complete else None,
          before_signal=sum(r['attribution'].get('before_signal') or 0 for r in subset),
          after_signal=sum(r['attribution'].get('after_signal') or 0 for r in subset),
          attribution_missing=sum(r['attribution'].get('net') is None for r in subset),
          setup_reconciled=len(reconciled),written_off_v1017=len(reconciled)-len(sold),setup_coverage='COMPLETE' if subset and len(reconciled)==len(subset) else 'PARTIAL' if reconciled else 'UNKNOWN',
          token_setup=sum(r['settlement']['components']['token_account_setup'] for r in sold) if sold else None,
          volume_setup=sum(r['settlement']['components']['volume_account_setup'] for r in sold) if sold else None,
          local_fee=sum(r['settlement']['components']['local_transaction_fee'] for r in sold) if sold else None,
          balance_residual=sum(r['settlement']['components']['exit_fee_balance_residual'] for r in sold) if sold else None,
          setup_excluded_sensitivity_net=sum(r['setup_excluded_sensitivity_net'] for r in reconciled) if len(reconciled)==len(subset) and subset else None)
    return dict(schema='EXIT_COST_REVIEW_V63',source=source,scope='WHOLE_SAVED_CAMPAIGN',read_only=True,rows=rows,totals=totals,
      pairs=pairs([r for r in rows if r['role']=='primary'],[r for r in rows if r['role']=='route']),
      limitations=['Signal price is not an executable quote; attribution describes observed prices, not cause',
       'Pool debit is not a fee-free market price; protocol fees retained in the pool can remain unknown',
       'Setup-excluded sensitivity is not realized profit, a rent refund, or a persistent-wallet replay',
       'Nearest results after signal/fill are marked diagnostic only; freshness reference uses unchanged 5 s bound',
       'No linear token-amount rescaling and no invented common-wallet cost when inventory differs',
       'Priority/landing/failed-network costs and wallet rent reuse/refunds are unknown; no Jito/live proof'])

def write(report,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    (folder/'exit_cost_review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    flat=[]
    for r in report['rows']:
        a=r['attribution'];component=(r.get('settlement') or {}).get('components') or {}
        before=r['nearest_fill'].get('before') or {};after=r['nearest_fill'].get('after') or {}
        flat.append(dict(role=r['role'],mint=r['mint'],trade_id=r['trade_id'],reason=r['reason'],signal_ts=r['signal_ts'],fill_ts=r['fill_ts'],
          entry_price_sol=r['entry_price'],signal_price_sol=r['signal_price'],fill_price_sol=r['fill_price'],
          signal_to_fill_ms=r['signal_to_fill_ms'],gross=r['gross'],net=r['net'],entry_cost=r['entry_cost'],exit_cost=r['exit_cost'],
          before_signal=a.get('before_signal'),after_signal=a.get('after_signal'),token_setup=component.get('token_account_setup'),
          volume_setup=component.get('volume_account_setup'),local_fee=component.get('local_transaction_fee'),
          residual=component.get('exit_fee_balance_residual'),setup_status=r['setup_status'],
          pool_price_before_sol=before.get('pool_debit_price_sol'),pool_before_delta_s=before.get('delta_to_target_s'),pool_before_fresh=before.get('fresh_reference_at_target'),
          pool_price_after_sol=after.get('pool_debit_price_sol'),pool_after_delta_s=after.get('delta_to_target_s'),
          setup_excluded_sensitivity_net=r.get('setup_excluded_sensitivity_net')))
    for name,data in [('exit_cost_trades.csv',flat),('exit_cost_pairs.csv',report['pairs'])]:
        fields=list(dict.fromkeys(k for row in data for k in row)) or ['status']
        with (folder/name).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fields);w.writeheader();w.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()} for row in data)

def current(primary,folder):
    import paper_branch,route_branch,monitor_service
    paths={role:(p,monitor_service.paths(p)['db']) for role,p in [('primary',Path(primary)),('amm',paper_branch.path(primary)),('route',route_branch.path(primary))] if p.exists()}
    report=build(paths,'STOPPED_LOCAL_CAMPAIGN');write(report,folder);return report

def archive(source,folder):
    """Extract only database bytes to temporary scratch, never execute archive code."""
    source=Path(source);digest=hashlib.sha256()
    with source.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    with tempfile.TemporaryDirectory() as tmp,zipfile.ZipFile(source) as z:
        paths={};root=Path(tmp)
        for role,prefix in [('primary','test_data_'),('amm','parallel_paper.zip'),('route','route_paper.zip')]:
            candidates=[n for n in z.namelist() if n.startswith(prefix) and n.endswith('.zip')]
            if len(candidates)!=1:continue
            if z.getinfo(candidates[0]).file_size>128*1024*1024:raise ValueError('NESTED_ARCHIVE_TOO_LARGE')
            with zipfile.ZipFile(io.BytesIO(z.read(candidates[0]))) as inner:
                files=[n for n in inner.namelist() if n.endswith('.db')]
                paths[role]=[None,None]
                for n in files:
                    if inner.getinfo(n).file_size>256*1024*1024:raise ValueError('DATABASE_TOO_LARGE')
                    dest=root/(role+'_'+Path(n).name);dest.write_bytes(inner.read(n));paths[role][int(n.endswith('_monitor.db'))]=dest
        primary_monitor=[n for n in z.namelist() if n.startswith('position_monitor_') and n.endswith('.zip')]
        if 'primary' in paths and len(primary_monitor)==1:
            if z.getinfo(primary_monitor[0]).file_size>128*1024*1024:raise ValueError('MONITOR_ARCHIVE_TOO_LARGE')
            with zipfile.ZipFile(io.BytesIO(z.read(primary_monitor[0]))) as inner:
                for n in inner.namelist():
                    if n.endswith('_monitor.db'):
                        if inner.getinfo(n).file_size>256*1024*1024:raise ValueError('DATABASE_TOO_LARGE')
                        dest=root/'primary_monitor.db';dest.write_bytes(inner.read(n));paths['primary'][1]=dest
        paths={k:v for k,v in paths.items() if v[0]}
        if 'primary' not in paths:raise ValueError('PRIMARY_DATABASE_NOT_FOUND')
        report=build(paths,dict(filename=source.name,sha256=digest.hexdigest()))
    write(report,folder);return report

def main(args=None):
    import sys
    args=sys.argv[1:] if args is None else args
    selected=(args[0] if args else input('Saved 05_RESULTS ZIP path: ')).strip().strip('"')
    if not selected:
        print('Review cancelled: no ZIP selected.');return 0
    source=Path(selected)
    if not source.is_file():
        print('Select an existing 05_RESULTS ZIP file, not a folder.');return 1
    folder=Path(__file__).resolve().parents[1]/'reviews'/time.strftime('%Y%m%d_%H%M%S')
    folder.mkdir(parents=True,exist_ok=False)
    report=archive(source,folder)
    print('READ-ONLY REVIEW:',folder)
    for role,totals in report['totals'].items():print(role,json.dumps(totals,ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
