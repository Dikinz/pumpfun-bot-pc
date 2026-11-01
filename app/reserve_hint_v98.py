"""A routing hint for a NEW negative-only reserve read; never sale authority.

The latest job carries a reference to its validated negative source. This keeps
selection constant in memory/SQL work through arbitrarily many no-result jobs,
without a cached bank, a new writer, or a history scan. Revalidate the original
row on every dispatch, even after a monitor restart.
"""
import hashlib,json,math

SCHEMA='CURVE_RESERVE_DISPATCH_HINT_V98'
NO_RESULT={'WORKER_TIMEOUT','REQUEST_TIMEOUT','REQUEST_ERROR','REQUEST_INVALID_JSON',
    'CHILD_ERROR','WORKER_ERROR','WORKER_EXCEPTION','SUPERSEDED_BY_EXIT_SIGNAL',
    'INTERRUPTED_STOP','INTERRUPTED_RESTART'}

def identity(job):
    if not isinstance(job,dict) or job.get('route')!='PUMP_CURVE':return None
    if not isinstance(job.get('wallet_state'),dict) or not job['wallet_state'].get('wallet'):return None
    if not job.get('trade_id') or not job.get('mint'):return None
    opened=job.get('opened_ts')
    if type(opened) not in (int,float) or not math.isfinite(opened) or opened<=0:return None
    if not isinstance(job.get('inventory'),dict) and job.get('inventory') is not None:return None
    # Missing gate fields are used by isolated offline callers. A stamped job
    # with a closed/unknown gate cannot inherit the hint.
    if 'recovery_authorized_v75' in job and job['recovery_authorized_v75'] is not True:return None
    value={k:job.get(k) for k in ('trade_id','mint','route','opened_ts','inventory',
        'recovery_epoch_v75','recovery_session_v77','recovery_authorized_v75')}
    value['wallet']=job['wallet_state']['wallet']
    try:return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    except (ValueError,TypeError):return None

def decode(value):
    try:
        result=json.loads(value or '{}')
        return result if isinstance(result,dict) else {}
    except (ValueError,TypeError):return {}

def source_valid(row,job,result,signature):
    if row is None or row['status']!='INSUFFICIENT_RESERVES':return False
    if row['trade_id']!=job['trade_id'] or row['mint']!=job['mint']:return False
    original=decode(row['input_json'])
    if identity(original)!=signature:return False
    admission=result.get('recovery_admission_v76')
    if not isinstance(admission,dict) or admission.get('allowed') is not True:return False
    if admission.get('classified_status')!='INSUFFICIENT_RESERVES' or result.get('recovery_invalidated_v75'):return False
    for field,job_field in (('epoch','recovery_epoch_v75'),('session','recovery_session_v77')):
        if original.get(job_field) is not None and admission.get(field)!=original[job_field]:return False
    if result.get('status') not in ('CHECKED','CURVE_RESERVE_PROBE_INSUFFICIENT'):return False
    if result.get('route','PUMP_CURVE')!='PUMP_CURVE':return False
    reserves=result.get('reserves')
    if not isinstance(reserves,dict) or reserves.get('status')!='INSUFFICIENT_REAL_RESERVES':return False
    scheduled=original.get('scheduled_ts');started=row['started_ts'];finished=row['finished_ts'];snapshot=result.get('snapshot_received_ts')
    if not all(type(x) in (int,float) and math.isfinite(x) for x in (scheduled,started,finished,snapshot)):return False
    import position_monitor as pm
    # Old evidence need only have been valid when finished: its numbers never
    # authorize this new read or a sale. Current freshness stays in experiment.
    return (result.get('scheduled_ts')==scheduled and scheduled<=started<=snapshot<=finished and
        0<=finished-snapshot<=pm.FRESH_SECONDS and 0<=finished-scheduled<=pm.TIMEOUT)

def select(c,job,latest,result):
    signature=identity(job)
    if signature is None or latest is None:return None
    original=decode(latest['input_json'])
    if identity(original)!=signature:return None
    status=latest['status']
    if status=='INSUFFICIENT_RESERVES':
        source=latest
    else:
        network=result.get('network_error')
        local=(status=='DATA_REJECTED' and isinstance(network,dict) and network.get('category')=='LOCAL_RPC_ADMISSION')
        if status not in NO_RESULT and not local:return None
        hint=original.get('reserve_hint_v98')
        if not isinstance(hint,dict) or hint.get('schema')!=SCHEMA or hint.get('identity_sha256')!=signature:return None
        identifier=hint.get('source_check_id')
        if not isinstance(identifier,str) or len(identifier)!=32 or any(x not in '0123456789abcdef' for x in identifier):return None
        source=c.execute('SELECT id,trade_id,mint,status,input_json,result_json,started_ts,finished_ts FROM position_checks_v0730 WHERE id=?',(identifier,)).fetchone()
        if source is None or source['started_ts']>latest['started_ts']:return None
        result=decode(source['result_json'])
    if not source_valid(source,job,result,signature):return None
    return dict(schema=SCHEMA,source_check_id=source['id'],identity_sha256=signature,
        retained_after_status=status,mode='NEW_FRESH_NEGATIVE_READ_ONLY')
