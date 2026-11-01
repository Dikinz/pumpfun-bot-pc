"""Normal two-hour PAPER launch; explicit ten-second read denial after an hour."""
import argparse,json,os,time,math
from pathlib import Path
import rpc_fault_v101 as fault
OUTPUT=Path(__file__).parent/'data/rpc_fault_paper_v101.json'

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--duration',type=float,default=7200);p.add_argument('--fault-at',type=float,default=3600);p.add_argument('--fault-for',type=float,default=10);args=p.parse_args(argv)
    if not all(math.isfinite(v) for v in (args.duration,args.fault_at,args.fault_for)):p.error('Finite timings required')
    if not 0<args.fault_at<args.duration or not 0<args.fault_for<=30 or args.fault_at+args.fault_for>=args.duration:p.error('Fault must be inside the run; duration > fault-at + fault-for')
    import config,launcher,rpc_budget
    if config.EXECUTION_MODE!='paper':raise RuntimeError('PAPER_ONLY')
    if rpc_budget.budget().blocked() and not rpc_budget.budget().recover():
        __import__('state_io').atomic_json(OUTPUT,dict(schema='PAPER_FAULT_TEST_V101',created_ts=time.time(),version=config.VERSION,status='START_BLOCKED',passed=False,reason='RPC_CONTROL_BLOCKED',scope='FAULT_INJECTION_AND_RPC_RESUME_ONLY'))
        raise RuntimeError('RPC_CONTROL_BLOCKED: run 14_RPC_STATUS and inspect evidence first')
    start=time.monotonic();wall=time.time();fault_start=start+args.fault_at
    report=dict(schema='PAPER_FAULT_TEST_V101',created_ts=wall,version=config.VERSION,status='SCHEDULED',duration_s=args.duration,fault_at_s=args.fault_at,fault_for_s=args.fault_for,clock_modified=False,scope='NORMAL_PAPER_WITH_SYNTHETIC_PRIORITY_READ_DENIAL',acceptance_scope='FAULT_INJECTION_AND_RPC_RESUME_ONLY',strategy_or_live_readiness_proven=False,fault_is_injected=True,timer_stop_requested=False,started_at_fault_end=None)
    report['counter_observations_v1011']={}
    def counter(label):
        snap=rpc_budget.snapshot();rows=snap.get('endpoints');state=snap.get('status')
        valid=state in ('READY','PENDING','CLEANUP_BACKLOG','PRIORITY_READ_BUSY','NOT_STARTED') and isinstance(rows,list)
        valid=valid and all(isinstance(e,dict) and type(e.get('started')) is int and e['started']>=0 for e in rows)
        value=sum(e['started'] for e in rows) if valid else None
        report['counter_observations_v1011'][label]=dict(status=state,valid=valid,value=value)
        return value
    report['started_before']=counter('BEFORE');__import__('state_io').atomic_json(OUTPUT,report)
    previous={k:os.environ.get(k) for k in (fault.START,fault.SPAN)}
    os.environ[fault.START]=str(fault_start);os.environ[fault.SPAN]=str(args.fault_for)
    def stop_condition():
        elapsed=time.monotonic()-start
        if elapsed>=args.fault_at+args.fault_for and report['started_at_fault_end'] is None:report['started_at_fault_end']=counter('AT_FAULT_END')
        if elapsed>=args.duration:report['timer_stop_requested']=True;return True
        return False
    print('PAPER FAULT TEST: duration',args.duration,'s; synthetic read denial at',args.fault_at,'s for',args.fault_for,'s. Normal RPC limits and STOP remain active.',flush=True)
    code=1
    try:code=launcher.start(stop_condition=stop_condition)
    finally:
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        report.update(status='FINISHED',exit_code=code,elapsed_s=time.monotonic()-start,started_after=counter('AFTER'),completed=report['timer_stop_requested'])
        try:
            note=json.loads((Path(config.RPC_CONTROL_DIR)/'priority_fault_observed_v101.json').read_text())
            report['controlled_fault_observed']=note.get('fault_start_mono_v101')==fault_start and any(x.get('injected_v101') is True for x in note.get('failure_chain',[]))
        except (OSError,ValueError):report['controlled_fault_observed']=False
        report['rpc_resumed_after_window']=report['started_at_fault_end'] is not None and report['started_after'] is not None and report['started_after']>report['started_at_fault_end']
        report['rpc_blocked_after']=rpc_budget.budget().blocked()
        report['passed']=code==0 and report['completed'] and report['controlled_fault_observed'] and report['rpc_resumed_after_window'] and not report['rpc_blocked_after']
        __import__('state_io').atomic_json(OUTPUT,report)
    print('PAPER FAULT TEST:', 'PASS' if report['passed'] else 'INCOMPLETE_OR_FAILED', 'Report:',OUTPUT,flush=True)
    return 0 if report['passed'] else code or 2
