"""Diagnostic replica of test_real_process_metadata_contention_does_not_discard_429 (read-only to product)."""
import sys,subprocess,threading,queue,time,tempfile,urllib.error,json
from pathlib import Path
APP=Path(sys.argv[1]);sys.path.insert(0,str(APP))
import rpc_budget as rpc
def once(tmp):
    b=rpc.Budget(tmp,rate=1000,slots=2,wait=.01)
    permit=b.request('https://rpc.invalid');permit.__enter__()
    script="import sys,rpc_budget as r\nb=r.Budget(sys.argv[1],rate=1000,slots=2)\nwith b.control_lock():\n print('held',flush=True)\n sys.stdin.readline()\n"
    p=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-u','-c',script,str(tmp)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=APP)
    q=queue.Queue();threading.Thread(target=lambda:q.put(p.stdout.readline()),daemon=True).start()
    assert q.get(timeout=10).strip()=='held'
    err=urllib.error.HTTPError('https://SECRET',429,'SECRET',{'Retry-After':'10'},None)
    permit.__exit__(type(err),err,None)
    try:
        with b.request('https://rpc.invalid'):pass
    except rpc.Deferred:pass
    p.stdin.write('release\n');p.stdin.flush();p.wait(timeout=10)
    try:
        with b.request('https://rpc.invalid'):pass
    except rpc.Deferred as e:reason=e.rpc_budget_reason
    t0=time.monotonic();first=len(b._pending())
    while b._pending() and time.monotonic()-t0<5:time.sleep(.01)
    gone=time.monotonic()-t0
    row=b.snapshot()['endpoints'][0]
    return dict(reason=reason,pending_at_check=first,cleared_after_s=round(gone,3),still_pending=len(b._pending()),
        server_429=row['server_429'],gc_alive=bool(b._gc_thread and b._gc_thread.is_alive()),
        unlink_error=getattr(b._local,'last_unlink_error',None))
def burn(stop):
    while not stop.is_set():pass
if __name__=='__main__':
    n=int(sys.argv[2]);load=int(sys.argv[3])
    procs=[subprocess.Popen([sys.executable,'-c','while True: pass']) for _ in range(load)]
    try:
        res=[]
        for i in range(n):
            with tempfile.TemporaryDirectory(dir=sys.argv[4],ignore_cleanup_errors=True) as d:res.append(once(Path(d)))
        bad=[r for r in res if r['pending_at_check']]
        print(json.dumps(dict(runs=n,load=load,pending_at_check=len(bad),max_clear_s=max(r['cleared_after_s'] for r in res),still_pending_after_5s=sum(r['still_pending'] for r in res),samples=bad[:5]),default=str))
    finally:
        for p in procs:p.kill()
