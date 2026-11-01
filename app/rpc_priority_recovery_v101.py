"""Recovery for one audited pre-admission failure; preserve all control state.

Unknown/modified markers and changed DB/rate state require another audit. No
schema initialization, rate reset, deletion of the marker or HTTP is allowed.
"""
import hashlib,json,math,sqlite3,time
from pathlib import Path
from contextlib import closing
AUDIT=Path(__file__).parent/'research/priority_incident_v101.json'

def recover(b):
    import rpc_budget as rpc,rpc_priority
    held=[]
    try:
        if b._unpersisted_failure:return False
        known=json.loads(AUDIT.read_text(encoding='utf-8'));marker=b._marker_bytes()
        if hashlib.sha256(marker).hexdigest()!=known['marker_sha256']:return False
        original=json.loads(marker);ts=original.get('ts')
        if type(ts) not in (int,float) or not math.isfinite(ts) or time.time()-ts<b.RECOVER_AFTER_SECONDS:return False
        with b.control_lock():
            for i in range(b.slots):
                fd=rpc.lock_slot(b.root/('slot_'+str(i)+'.lock'))
                if fd is None:return False
                held.append(fd)
            st=b.path.stat()
            if b._control_identity is not None and b._control_identity!=(st.st_dev,st.st_ino):return False
            with closing(sqlite3.connect(b.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.025)) as c:
                c.row_factory=sqlite3.Row;c.execute('BEGIN')
                tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables!={'settings','endpoints','outcome_receipts'}:return False
                if [tuple(r) for r in c.execute('PRAGMA quick_check')]!=[('ok',)]:return False
                if tuple(c.execute('SELECT rate,slots FROM settings WHERE id=1').fetchone())!=(b.rate,b.slots):return False
                state={t:[dict(r) for r in c.execute('SELECT * FROM '+t+(' ORDER BY key' if t=='endpoints' else ' ORDER BY id'))] for t in ('settings','endpoints','outcome_receipts')}
                digest=hashlib.sha256(json.dumps(state,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if digest!=known['control_state_sha256']:return False
            if b._pending():return False # Unknown/pending outcomes are never skipped.
            for name,raw in known['rate_bytes_hex'].items():
                path=b.root/name
                if path.stat().st_size!=8 or path.read_bytes()!=bytes.fromhex(raw):return False
            rpc_priority.pending(b.root) # All current leases must be readable and valid.
            if b._marker_bytes()!=marker:return False
            receipt=dict(schema='RPC_FAILURE_RECOVERY_V97',marker_sha256=known['marker_sha256'],database_identity=[st.st_dev,st.st_ino],ts=time.time(),recovery_kind_v101='AUDITED_PRIORITY_READ',control_state_sha256=digest,original_failure_preserved=True,original_failure_v97=original)
            __import__('state_io').atomic_json(b.root/'failure_recovery_v97.json',receipt)
            return b._marker_bytes()==marker and b._recovery_matches(marker)
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,rpc.Unavailable):return False
    finally:
        for fd in held:rpc.unlock_slot(fd)
