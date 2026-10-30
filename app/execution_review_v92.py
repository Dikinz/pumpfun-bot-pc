"""Read-only exit timing and matched-cost review; never replaces ledger proceeds."""
import csv,json,math,sqlite3
from pathlib import Path
from contextlib import closing
from collections import defaultdict


def finite(v):return type(v) in (int,float) and math.isfinite(v)


def timeline(signal,settled,row,result):
    scheduler=result.get('scheduler_timing_v65') or {}
    points=dict(signal=signal,scheduled=row.get('scheduled_ts'),worker=scheduler.get('worker_started_ts'),
                snapshot=result.get('snapshot_received_ts'),available=row.get('finished_ts'),settled=settled)
    stamps=list(points.values());valid=all(finite(x) for x in stamps) and stamps==sorted(stamps)
    parts={a+'_to_'+b+'_ms':(points[b]-points[a])*1000 for a,b in zip(points,list(points)[1:])} if valid else None
    rpc=result.get('rpc_timing') or {};stages=defaultdict(float)
    for stage in rpc.get('stages',[]):
        if finite(stage.get('elapsed_ms')):stages[stage['stage']]+=stage['elapsed_ms']
    requests=rpc.get('requests') or []
    waits=[r.get('rpc_admission_v89',{}).get('wait_ms') for r in requests]
    return dict(status='VALID' if valid else 'MISSING_OR_INVALID_TIMES',points=points,partition_ms=parts,
        signal_to_settle_ms=(settled-signal)*1000 if finite(signal) and finite(settled) and settled>=signal else None,
        worker_monotonic_ms=scheduler.get('worker_duration_ms'),child_monotonic_ms=rpc.get('child_elapsed_ms'),
        child_stage_totals_ms=dict(stages),stage_history_may_be_truncated=len(rpc.get('stages',[]))>=64,
        rpc_requests=len(requests),rpc_admission_wait_ms_sum=sum(v for v in waits if finite(v)),
        rpc_admission_measured_requests=sum(finite(v) for v in waits),
        note='Wall-clock partition is valid only for ordered timestamps. Child stages and RPC admission are nested observations; do not add them to wall/worker totals. HTTP headers include DNS/TCP/TLS/server wait.')


def matched_costs(review):
    pairs=review.get('pairs') or []
    eligible=[p for p in pairs if p.get('match_status')=='IDENTICAL_ENTRY_INVENTORY' and p.get('same_shared_sell_signal') is True
              and all(finite(p.get(k)) for k in ('primary_net','route_net','recorded_gross_difference','recorded_cost_difference'))]
    pnet=sum(p['primary_net'] for p in eligible);rnet=sum(p['route_net'] for p in eligible)
    gross=sum(p['recorded_gross_difference'] for p in eligible);cost=sum(p['recorded_cost_difference'] for p in eligible)
    return dict(pairs=len(eligible),excluded_pairs=len(pairs)-len(eligible),primary_net=pnet if eligible else None,route_net=rnet if eligible else None,
        net_difference=rnet-pnet if eligible else None,recorded_gross_difference=gross if eligible else None,recorded_extra_cost=cost if eligible else None,
        reconciled=math.isclose(rnet-pnet,gross-cost,abs_tol=1e-7) if eligible else None,
        common_wallet_net=None,causal_price_effect=None,
        note='Only closed pairs with identical entry inventory and shared sell signal. Recorded cost bases differ; this is descriptive accounting, not a common-wallet replay or causal price attribution.')


def build(review,route_db,monitor_db):
    rows=[]
    if Path(route_db).exists() and Path(monitor_db).exists():
        with closing(sqlite3.connect(Path(route_db).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as c,closing(sqlite3.connect(Path(monitor_db).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as m:
            c.row_factory=m.row_factory=sqlite3.Row;c.execute('BEGIN');m.execute('BEGIN')
            for settlement in c.execute('SELECT * FROM branch_amm_settlements ORDER BY settled_ts'):
                details=json.loads(settlement['details']);signal=(details.get('signal') or {}).get('signal_ts')
                selected=m.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(settlement['check_id'],)).fetchone()
                if selected is None:
                    rows.append(dict(trade_id=settlement['trade_id'],status='CHECK_EVIDENCE_MISSING'));continue
                selected=dict(selected);result=json.loads(selected['result_json']);attempts=[]
                for a in m.execute('''SELECT id,scheduled_ts,started_ts,finished_ts,status,
                    json_extract(result_json,'$.network_error.category') category,
                    json_extract(result_json,'$.network_error.reason') reason
                    FROM position_checks_v0730 WHERE trade_id=? AND scheduled_ts>=? AND scheduled_ts<=? ORDER BY scheduled_ts,id''',
                    (settlement['trade_id'],signal,selected['scheduled_ts'])):
                    attempts.append(dict(a))
                first=attempts[0] if attempts else None
                rows.append(dict(trade_id=settlement['trade_id'],route=details.get('route'),check_id=settlement['check_id'],
                    timing=timeline(signal,settlement['settled_ts'],selected,result),attempts=attempts,
                    first_check_after_signal_ms=(first['scheduled_ts']-signal)*1000 if first and finite(signal) else None,
                    first_to_selected_check_ms=(selected['scheduled_ts']-first['scheduled_ts'])*1000 if first else None,
                    status='RETAINED_EVIDENCE',broadcast=result.get('broadcast'),
                    sale_status=(result.get('sale') or {}).get('status')))
    return dict(schema='EXIT_EXECUTION_REVIEW_V92',read_only=True,route_exits=rows,matched_costs=matched_costs(review),
        recorded_role_totals=review.get('totals'),limits='No price/cost/eligibility adjustment. Open/unmatched positions excluded from paired PNL, preserved in original reports. No live or profitability proof.')


def write(report,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    (folder/'exit_execution_review_v92.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    fields=['trade_id','route','check_id','signal_to_settle_ms','first_check_after_signal_ms','first_to_selected_check_ms',
            'signal_to_scheduled_ms','scheduled_to_worker_ms','worker_to_snapshot_ms','snapshot_to_available_ms','available_to_settled_ms','attempts','rpc_admission_wait_ms_sum']
    with (folder/'exit_execution_timing_v92.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fields);writer.writeheader()
        for row in report['route_exits']:
            t=row.get('timing') or {};p=t.get('partition_ms') or {}
            flat=dict(trade_id=row['trade_id'],route=row.get('route'),check_id=row.get('check_id'),signal_to_settle_ms=t.get('signal_to_settle_ms'),
                first_check_after_signal_ms=row.get('first_check_after_signal_ms'),first_to_selected_check_ms=row.get('first_to_selected_check_ms'),
                attempts=len(row.get('attempts',[])),rpc_admission_wait_ms_sum=t.get('rpc_admission_wait_ms_sum'))
            flat.update(p);writer.writerow(flat)


def current(primary,review,folder):
    import route_branch,monitor_service
    route=route_branch.path(primary);report=build(review,route,monitor_service.paths(route)['db']);write(report,folder);return report
