"""Explicit PAPER-only priority-read injection; normal launches do no fault IO."""
import os,time,math,errno
START='PUMPFUN_TEST_PRIORITY_START_MONO_V101'
SPAN='PUMPFUN_TEST_PRIORITY_DURATION_V101'

def maybe_raise():
    raw=os.getenv(START)
    if raw is None:return
    import config
    start=float(raw);span=float(os.environ[SPAN])
    if config.EXECUTION_MODE!='paper' or not math.isfinite(start) or not math.isfinite(span) or start<=0 or not 0<span<=30:raise ValueError('RPC_FAULT_TEST_PLAN_INVALID')
    if start<=time.monotonic()<start+span:
        exc=PermissionError(errno.EACCES,'CONTROLLED_RPC_PRIORITY_READ_TEST');exc.winerror=5;exc.controlled_rpc_priority_fault_v101=True;raise exc
