"""Short, expiring admission leases for fresh exit simulations, never HTTP permits.

A child publishes once, before its first RPC, and removes only its own lease.
Killed children leave at most ten seconds of conservative background deferral.
The existing common rate, slots, cooldown and admission deadline still apply.
"""
import contextvars,json,math,os,time,uuid,sys
from pathlib import Path
from contextlib import contextmanager
IS_EXIT=contextvars.ContextVar('rpc_exit_priority_v94',default=False)
TTL=10.
LIMIT=16

@contextmanager
def exit_check(job):
    if not job.get('priority_exit'):
        yield;return
    import config
    directory=Path(config.RPC_CONTROL_DIR)/'exit_priority_v94';directory.mkdir(parents=True,exist_ok=True)
    identifier=uuid.uuid4().hex;path=directory/(identifier+'.json');temporary=path.with_suffix('.tmp')
    payload=dict(schema='RPC_EXIT_PRIORITY_V94',id=identifier,pid=os.getpid(),created_mono=time.monotonic(),created_wall=time.time(),ttl_s=TTL)
    token=None
    try:
        temporary.write_text(json.dumps(payload),encoding='utf-8');os.replace(temporary,path)
        token=IS_EXIT.set(True)
        yield
    finally:
        original=sys.exception()
        if token is not None:IS_EXIT.reset(token)
        for target in (path,temporary):
            try:target.unlink()
            except FileNotFoundError:pass
            except OSError as exc:
                if original is not None:original.add_note('RPC_EXIT_PRIORITY_RELEASE: '+type(exc).__name__)
                else:print('RPC_EXIT_PRIORITY_RELEASE_DEFERRED: '+type(exc).__name__+'; ttl_s=10',flush=True)


def pending(root):
    # Only published JSON is actionable. An atomic replacement is the publication point.
    directory=Path(root)/'exit_priority_v94'
    if not directory.exists():return 0
    active=0
    for i,path in enumerate(directory.glob('*')):
        # Bounded per-call scan, with fail-closed behavior if unexpected files accumulate.
        if i>=LIMIT:raise ValueError('RPC_EXIT_PRIORITY_BACKLOG')
        if path.suffix not in ('.json','.tmp'):raise ValueError('RPC_EXIT_PRIORITY_INVALID')
        try:
            if path.suffix=='.tmp':
                if time.time()-path.stat().st_mtime>TTL:path.unlink()
                continue
            if path.stat().st_size>1024:raise ValueError('RPC_EXIT_PRIORITY_INVALID')
            row=json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:continue # Owner finished after enumeration.
        if not isinstance(row,dict):raise ValueError('RPC_EXIT_PRIORITY_INVALID')
        if row.get('schema')!='RPC_EXIT_PRIORITY_V94' or row.get('id')!=path.stem or row.get('ttl_s')!=TTL:raise ValueError('RPC_EXIT_PRIORITY_INVALID')
        for field in ('created_mono','created_wall'):
            if type(row.get(field)) not in (int,float) or not math.isfinite(row[field]) or row[field]<0:raise ValueError('RPC_EXIT_PRIORITY_INVALID')
        age=time.monotonic()-row['created_mono'];wall_age=time.time()-row['created_wall']
        if age<0 or age>=TTL or wall_age>=TTL:
            try:path.unlink()
            except (FileNotFoundError,PermissionError):pass # Expired lease grants no priority even while another reader holds its file.
        else:active+=1
    return active
