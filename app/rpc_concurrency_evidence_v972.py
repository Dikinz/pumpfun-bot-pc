"""Offline test evidence, captured before pytest deletes its temporary control DB."""
import json,os,time
from pathlib import Path
import rpc_budget

def capture(root,errors,workers_alive,cleanup_alive):
    root=Path(root)
    result=dict(schema='RPC_CONCURRENCY_V972',ts=time.time(),errors=errors,
        workers_alive=workers_alive,cleanup_threads_alive=cleanup_alive)
    for name in ('control_failed.json','cleanup_v96.json','failure_recovery_v97.json','outcome_read_v991.json'):
        try:
            path=root/name
            if path.stat().st_size>131072:raise ValueError('DIAGNOSTIC_OVERSIZE')
            result[name]=json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:result[name]=None
        except (OSError,ValueError) as exc:result[name]=dict(read_error=rpc_budget.failure_details(exc))
    result['passed']=not errors and not workers_alive and not cleanup_alive and result['control_failed.json'] is None
    destination=os.environ.get('RPC_CONCURRENCY_EVIDENCE_V972')
    if destination:
        target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True)
        try:
            import rpc_evidence_v892
            result['control_export']=rpc_evidence_v892.export(root,target.with_suffix('.zip'))
        except Exception as exc:result['control_export_error']=rpc_budget.failure_details(exc)
        tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(result,indent=2),encoding='utf-8');tmp.replace(target)
    return result
