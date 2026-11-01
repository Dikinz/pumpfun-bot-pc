"""Read-only diagnostics, cohort definitions and explicit missing evidence."""
import collections,json,math,sqlite3,time
from pathlib import Path
from contextlib import closing

def connect(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5);c.row_factory=sqlite3.Row;return c

def decode(value):
    try:r=json.loads(value or '{}');return r if isinstance(r,dict) else {}
    except (ValueError,TypeError):return {}

def preentry(c):
    counts=collections.Counter();failures=collections.Counter();buckets=[]
    bounds=((0,1.5,'<1.5'),(1.5,2.5,'1.5-2.5'),(2.5,4,'2.5-4'),(4,float('inf'),'>=4'))
    groups=[collections.Counter() for b in bounds]
    for row in c.execute('SELECT status,started_ts,finished_ts,result_json FROM preentry_v0729'):
        counts[row['status']]+=1
        if row['status']=='BUY_SIMULATION_FAILED':
            buy=decode(row['result_json']).get('buy') or {};failures[(str(buy.get('error_code','UNKNOWN')),str(buy.get('error_name','UNKNOWN')))]+=1
        a,b=row['started_ts'],row['finished_ts']
        if all(type(v) in (int,float) and math.isfinite(v) for v in (a,b)) and b>=a:
            for i,(lo,hi,label) in enumerate(bounds):
                if lo<=b-a<hi:groups[i][row['status']]+=1;break
    for (lo,hi,label),g in zip(bounds,groups):
        n=g['USED']+g['APPROVAL_EXPIRED'];buckets.append(dict(bin=label,all_jobs=sum(g.values()),used=g['USED'],expired=g['APPROVAL_EXPIRED'],approved_denominator=n,expiry_rate=g['APPROVAL_EXPIRED']/n if n else None,statuses=dict(g)))
    return dict(statuses=dict(counts),expiry_by_wall_runtime=buckets,buy_failure_reasons=[dict(error_code=k[0],error_name=k[1],count=v) for k,v in sorted(failures.items())],
        denominator='USED + APPROVAL_EXPIRED only; unsupported and failed jobs are not approved. Runtime excludes queue. Association is not causal proof.')

DEFERRED_LAB_STATUSES=('RPC_DEFERRED','REQUEST_OR_LAB_ERROR_Deferred') # second: label before v0.7.101.2

def exit_signal_lab(c):
    """Read-only coverage of the exit-signal simulation lab (TERM-6/CR1-05).

    A local RPC deferral is counted separately from request/lab failures; RPC limits are unchanged."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='auto_sim_v0727'").fetchone():
        return dict(available=False,jobs=0,classification='NO_JOBS')
    statuses=collections.Counter(r[0] for r in c.execute('SELECT status FROM auto_sim_v0727'))
    finished=sum(v for k,v in statuses.items() if k not in ('PENDING','RUNNING'))
    executed=accepted=0
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='exit_comparison_v0728'").fetchone():
        executed=c.execute("SELECT COUNT(*) FROM exit_comparison_v0728 WHERE branch='FIRST' AND technical_status='LOCAL_SWAP_EXECUTED'").fetchone()[0]
        accepted=c.execute("SELECT COUNT(*) FROM exit_comparison_v0728 WHERE branch='FIRST' AND status='ACCEPTED_LOCAL'").fetchone()[0]
    deferred=sum(statuses[k] for k in DEFERRED_LAB_STATUSES)
    label='NO_JOBS' if not finished else 'NO_COVERAGE' if not executed else 'PARTIAL' if executed<finished else 'COVERED'
    return dict(available=True,jobs=sum(statuses.values()),finished=finished,statuses=dict(statuses),rpc_deferred=deferred,
        local_swap_executed=executed,accepted_local=accepted,coverage=executed/finished if finished else None,classification=label,
        note='FIRST branch per exit signal. RPC_DEFERRED = local RPC budget deferral, not a request or lab failure. Diagnostics only; no trading rule depends on it.')

UNFINISHED=('PENDING','RUNNING','READY')

def unfinished_after_stop(c,primary,now=None):
    """Read-only label for preentry jobs left open by a run that is no longer live.

    The DB is not changed: the next start still marks them INTERRUPTED_RESTART."""
    import runtime_status,system_health
    runtime=runtime_status.summary(c,now)
    counts={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM preentry_v0729 WHERE status IN (?,?,?) GROUP BY status',UNFINISHED)}
    if runtime['state'] in ('RUNNING','STARTING','NOT_STARTED') or not counts:
        return dict(count=0,statuses={},classification=None,runtime_state=runtime['state'])
    started=runtime.get('started_ts') or 0
    stalled=any(e.get('kind')=='STALL_CONTROLLED_STOP' and isinstance(e.get('ts'),(int,float)) and e['ts']>=started for e in system_health.history(primary,limit=2000))
    label='ABANDONED_ON_STALL' if stalled else 'ABANDONED_ON_STOP' if runtime['state']=='STOPPED' else 'ABANDONED_NO_HEARTBEAT'
    return dict(count=sum(counts.values()),statuses=counts,classification=label,runtime_state=runtime['state'],
        note='Frozen-report label only; the stored status stays as written and becomes INTERRUPTED_RESTART on the next start.')

def fill_timeline(primary,route,monitor):
    rows=[]
    for p in route.execute('SELECT trade_id,mint FROM positions'):
        fill=primary.execute("SELECT ts,pnl FROM trades WHERE trade_id=? AND side='SELL'",(p['trade_id'],)).fetchone()
        sig=primary.execute("SELECT signal_ts FROM paper_orders_v0720 WHERE trade_id=? AND side='SELL' ORDER BY signal_ts LIMIT 1",(p['trade_id'],)).fetchone()
        if not fill or not sig:continue
        snapshots=[]
        for r in monitor.execute('SELECT started_ts,result_json FROM position_checks_v0730 WHERE trade_id=? AND started_ts>=? ORDER BY started_ts',(p['trade_id'],sig[0])):
            data=decode(r['result_json']);res=data.get('reserves') or {};ts=data.get('snapshot_received_ts')
            if type(ts) in (int,float) and math.isfinite(ts) and type(res.get('real_sol_lamports'))==int:
                snapshots.append(dict(started_ts=r['started_ts'],snapshot_received_ts=ts,real_sol_lamports=res['real_sol_lamports'],slot=data.get('snapshot_slot',data.get('slot'))))
        first=snapshots[0] if snapshots else None;before=[s for s in snapshots if s['snapshot_received_ts']<=fill['ts']];after=[s for s in snapshots if s['snapshot_received_ts']>fill['ts']]
        rows.append(dict(trade_id=p['trade_id'],mint=p['mint'],signal_ts=sig[0],primary_fill_ts=fill['ts'],primary_gross=fill['pnl'],first_post_signal_snapshot=first,
            fill_minus_first_snapshot_s=fill['ts']-first['snapshot_received_ts'] if first else None,last_snapshot_before_fill=before[-1] if before else None,first_snapshot_after_fill=after[0] if after else None,
            limitation='Snapshots are points in time; no proof of continuous reserves or executable PRIMARY fill. Tick-to-fill price path requires observed ticks and account proofs.'))
    return rows

def export(primary):
    import route_branch,monitor_service,paper_capital
    report=dict(schema='RUN_REVIEW_V100',created_ts=time.time(),read_only=True,errors=[])
    try:
        with closing(connect(primary)) as p:
            p.execute('BEGIN');report['preentry']=preentry(p)
            try:report['preentry']['unfinished_after_stop_v1012']=unfinished_after_stop(p,primary)
            except (sqlite3.Error,ValueError,TypeError) as exc:report['preentry']['unfinished_after_stop_v1012']=dict(error_type=type(exc).__name__)
            try:report['exit_signal_lab_v1012']=exit_signal_lab(p)
            except (sqlite3.Error,ValueError,TypeError) as exc:report['exit_signal_lab_v1012']=dict(error_type=type(exc).__name__)
            report['discovery_gap_evidence']=dict(available=False,lost_candidates=None,note='Unobserved provider events cannot be counted locally.')
            if p.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_gaps_v100'").fetchone():
                report['discovery_gap_evidence'].update(available=True,gaps=[dict(r) for r in p.execute('SELECT * FROM discovery_gaps_v100 ORDER BY started_ts')])
            target=route_branch.path(primary);monitor=monitor_service.paths(target)['db']
            if target.exists():
                with closing(connect(target)) as r:
                    r.execute('BEGIN');pc=paper_capital.summary(p);rc=paper_capital.summary(r)
                    report['all_entries_cohort']=dict(primary_closed_net=pc['closed_net'],route_closed_net=rc['closed_net'],route_open_principal=rc['open_principal'],route_all_entry_zero_receipt_pnl=rc['net_cash']-rc['settings']['start'],note='Zero-receipt scenario includes open principal and paid entry cost; not a future-result interval. Matched closed-only rows exclude unsuccessful unresolved exits.')
                    if Path(monitor).exists():
                        with closing(connect(monitor)) as m:report['unresolved_primary_fill_timeline']=fill_timeline(p,r,m)
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError) as exc:report['errors'].append(dict(type=type(exc).__name__,message=str(exc)[:500]))
    return report
