"""Disposable stdlib-only process proving shared lock/rate behavior.
Per-process results and stderr survive in the recovery evidence; no user RPC,
wallet, environment dump or arbitrary command arguments are recorded.
"""
import sys,json,time,os,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rpc_budget import Budget
root=Path(sys.argv[1]);name=sys.argv[2];mode=sys.argv[3]
result=dict(schema='RPC_BUDGET_TEST_WORKER_V891',worker=name,pid=os.getpid(),phase='STARTING',status='RUNNING',expected_failure=mode=='controlled_failure')

def save():
    path=root/(name+'.result.json');tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(result),encoding='utf-8');tmp.replace(path)

def main():
    save();(root/(name+'.ready')).write_text('ready')
    deadline=time.monotonic()+6
    while not (root/'go').exists():
        if time.monotonic()>deadline:raise RuntimeError('barrier timeout')
        time.sleep(.005)
    result['phase']='ADMISSION';save()
    if mode=='controlled_failure':raise RuntimeError('CONTROLLED_WORKER_FAILURE_EXPECTED')
    budget=Budget(root/'shared',rate=float(sys.argv[4]),slots=int(sys.argv[5]),wait=3)
    with budget.request('https://rpc.invalid/path?api-key=PRIVATE') as permit:
        start=time.monotonic();result.update(phase='HELD',admission_wait_ms=permit.wait_ms);save()
        (root/(name+'.held')).write_text(str(start))
        time.sleep(.15 if mode=='normal' else 30)
        end=time.monotonic();result['phase']='RECORD_OUTCOME';save()
    (root/(name+'.json')).write_text(json.dumps(dict(start=start,end=end,pid=os.getpid())))
    result.update(phase='FINISHED',status='PASS');save()

try:main()
except BaseException as exc:
    chain=[];current=exc
    for _ in range(5):
        if current is None:break
        chain.append(dict(error_type=type(current).__name__,message=str(current)[:500],errno=getattr(current,'errno',None),winerror=getattr(current,'winerror',None),sqlite_errorcode=getattr(current,'sqlite_errorcode',None),frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(current.__traceback__)[-8:]]))
        current=current.__cause__ if current.__cause__ is not None else (None if current.__suppress_context__ else current.__context__)
    result.update(status='FAILED',error_chain=chain)
    try:save()
    except Exception:pass
    raise
