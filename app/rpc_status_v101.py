"""Inspect/recover RPC control and optionally perform one read-only genesis RPC."""
import argparse,json,time
from pathlib import Path

def run(recover=False,probe=False):
    import rpc_budget as rpc,config
    b=rpc.budget();before=b.snapshot();was=b.blocked()
    fixed=bool(recover and was and b.recover())
    report=dict(schema='RPC_RESUME_STATUS_V101',created_ts=time.time(),read_only_rpc=True,was_blocked=was,recovery_applied=fixed,original_marker_preserved=True,before=before,probe=dict(status='NOT_REQUESTED'))
    if probe and b.blocked():report['probe']=dict(status='BLOCKED')
    elif probe:
        try:
            import exit_quotes,network_check_v88
            data=exit_quotes.request_json(config.QUOTE_RPC_URL,'RPC_RESUME_PROBE',dict(jsonrpc='2.0',id=1,method='getGenesisHash',params=[]))
            report['probe']=dict(status='PASS' if data.get('result')==network_check_v88.MAINNET_GENESIS else 'CHAIN_MISMATCH')
        except Exception as exc:report['probe']=dict(status='FAILED',failure_chain=rpc.failure_details(exc))
    report['after']=b.snapshot()
    report['started_before']=sum(x['started'] for x in before.get('endpoints',[]));report['started_after']=sum(x['started'] for x in report['after'].get('endpoints',[]))
    report['passed']=report['after'].get('status')=='READY' and (not probe or report['probe']['status']=='PASS' and report['started_after']>report['started_before'])
    return report

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--recover',action='store_true');p.add_argument('--probe',action='store_true');args=p.parse_args(argv)
    report=run(args.recover,args.probe);target=Path(__file__).parent/'data/rpc_resume_status_v101.json'
    __import__('state_io').atomic_json(target,report);print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['passed'] else 1
