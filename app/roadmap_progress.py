"""Evidence-based checklist progress, never a probability of profitable/live trading."""
import json
from pathlib import Path
import config


def acceptance_path():return Path(__file__).resolve().parent.parent/'results/acceptance_v90.json'

def write_acceptance(report, report_path):
    names=('priority_windows_verified_v101','overview_windows_verified_v1001','review_windows_verified_v100','transport_thread_windows_verified_v992','rpc_outcome_read_windows_verified_v991','monitor_publication_windows_verified_v99','rpc_transport_models_windows_verified_v99','native_job_verified_v78','monitor_shutdown_windows_verified_v81','late_spawn_windows_verified_v82','late_spawn_ownership_windows_verified_v85','native_state_io_windows_verified_v87','native_cleanup_owner_windows_verified_v93','candidate_rpc_windows_verified_v94','rpc_coordination_windows_verified_v95','rpc_cleanup_windows_verified_v96','rpc_transient_windows_verified_v97','saved_result_handles_windows_verified_v971','rpc_receipt_windows_verified_v972','reserve_hint_windows_verified_v98')
    value=dict(version=report.get('version'),passed=report.get('passed') is True,
               native_windows_verified=all(report.get(n) is True for n in names),created_ts=report.get('created_ts'))
    # Follow the actual report, including disposable failure-injection tests.
    path=Path(report_path).parent/acceptance_path().name
    __import__('runtime_control').atomic_json(path,value)


def acceptance():
    try:
        p=acceptance_path()
        if p.stat().st_size>4096:return {}
        data=json.loads(p.read_text(encoding='utf-8'))
        return data if data.get('version')==config.VERSION else {}
    except (OSError,ValueError,AttributeError):return {}


AMM_NOT_NEEDED_MIN_CLOSED=100

def amm_exit(route,counts):
    """v0.7.101.9, path B chosen by the user 04.11.2026: a verified AMM exit, or proof the strategy never needs one —
    at least 100 ROUTE closes in this campaign and no exit check ever took the AMM route (no position migrated).
    02–04.11.2026: 178 PRIMARY and 148 ROUTE positions, held up to 2 min, never migrated. Returns the basis or None."""
    if counts.get('PUMP_AMM',0)>0:return 'VERIFIED'
    routes=route.get('exit_routes_v1019')
    if not isinstance(routes,dict) or route.get('unknown_route_exits'):return None
    if route.get('new_closed',0)>=AMM_NOT_NEEDED_MIN_CLOSED and set(routes)<={'PUMP_CURVE'} and not route.get('positions'):return 'NOT_NEEDED_NO_MIGRATION'
    return None

def build(stages,primary,amm,route,rpc,accepted=None):
    a=amm or {};r=route or {};accepted=acceptance() if accepted is None else accepted
    counts=r.get('verified_routes') or {}
    gates=[('Нативные проверки Windows',accepted.get('version')==config.VERSION and accepted.get('passed') is True and accepted.get('native_windows_verified') is True),
           ('RPC работает под общим лимитом',rpc.get('status')=='READY' and sum(e.get('started',0) for e in rpc.get('endpoints',[]))>0),
           ('Проверенный Curve-выход',counts.get('PUMP_CURVE',0)>0),
           ('AMM-выход (или не нужен)',amm_exit(r,counts)),
           ('Все входы ROUTE закрыты без разрыва',r.get('entries',0)>0 and r.get('closed')==r.get('entries') and not r.get('positions') and not r.get('input_gap') and r.get('runtime',{}).get('state')!='FAILED' and not r.get('live_v87',{}).get('ownership_pending') and not (r.get('runtime_perf',{}).get('diagnostic_writer_v92') or {}).get('diagnostic_gap_v94'))]
    closed=max(0,primary.get('new_closed',a.get('new_closed',0)));collection=min(100,closed)
    percentages=[sum(bool(v) for _,v in gates)*20,round(collection*.5),0,0]
    rows=[]
    for index,stage in enumerate(stages):
        row=dict(stage,percent=percentages[index],estimate_kind='EVIDENCE_CHECKLIST')
        if index==0:
            row['checkpoints']=[dict(title=t,passed=bool(v),**({'basis':v} if isinstance(v,str) else {})) for t,v in gates]
            row['progress']=f"Подтверждено {sum(bool(v) for _,v in gates)}/{len(gates)} контрольных пунктов"
            row.pop('estimate_percent',None)
        elif index==1:
            row['progress']=f'{closed}/100 новых закрытий PRIMARY; набор выборки даёт 50% этапа, разбор результатов ещё не подтверждён'
        else:row['checkpoints']=[]
        rows.append(row)
    return dict(collection_basis_v100='PRIMARY_NEW_CLOSED' if 'new_closed' in primary else 'LEGACY_AMM_NEW_CLOSED',percent=round(sum(percentages)/4,1),stages=rows,
                basis='Четыре этапа имеют равный вес. Это доля контрольных пунктов, не вероятность прибыли и не разрешение live. Этап 2: 50% за выборку, 50% после отдельного разбора.')
