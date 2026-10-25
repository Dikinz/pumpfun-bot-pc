"""Export-time verification of checks against actual ZIP members; no runtime writes."""
import io,json,re,sqlite3,tempfile,zipfile
from pathlib import Path
from contextlib import closing
MANIFEST='artifact_manifest_v74.json'
FILES=('evidence.zip','diagnostic.zip','child_lifecycle_v73.json')

def add(z,checks,root):
    root=Path(root).resolve();missing=[];errors=[]
    for r in checks:
        cid=r['id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',cid):
            errors.append(dict(check_id=cid,error='INVALID_CHECK_ID'));continue
        have=[]
        for name in FILES:
            f=root/cid/name
            try:
                if f.is_file() and f.resolve().is_relative_to(root):z.write(f,cid+'/'+name);have.append(name)
            except OSError as e:errors.append(dict(check_id=cid,file=name,error=type(e).__name__,errno=e.errno))
        if not any(n in have for n in ('evidence.zip','diagnostic.zip')):missing.append(cid)
    z.writestr('missing_evidence.json',json.dumps(missing))
    return errors

def read_json(z,name):
    if z.getinfo(name).file_size>2*1024*1024:raise ValueError('JSON_TOO_LARGE')
    return json.loads(z.read(name))

def verify(target,copy_errors=()):
    """Read back ZIP and its SQLite backup, rather than trusting source existence."""
    rows=[];errors=list(copy_errors);db_match=False
    with zipfile.ZipFile(target) as z:
        names=z.namelist();checks=json.loads(z.read('position_checks_v0730.json')) if 'position_checks_v0730.json' in names else []
        if 'position_checks_v0730.json' not in names:errors.append(dict(error='CHECK_LIST_MISSING'))
        ids=[r['id'] for r in checks]
        if len(ids)!=len(set(ids)):errors.append(dict(error='DUPLICATE_CHECK_ID'))
        for r in checks:
            cid=r['id'];entry=dict(check_id=cid,check_status=r['status'],files={},errors=[])
            for name in FILES:entry['files'][name]=names.count(cid+'/'+name)==1
            for name in ('diagnostic.zip','child_lifecycle_v73.json'):
                path=cid+'/'+name
                if names.count(path)!=1:entry['errors'].append('MISSING_OR_DUPLICATE_'+name);continue
                try:
                    if name.endswith('.json'):d=read_json(z,path)
                    else:
                        with zipfile.ZipFile(io.BytesIO(z.read(path))) as child:d=read_json(child,'diagnostic.json').get('child_diagnostics_v73',{})
                    if d.get('check_id')!=cid:entry['errors'].append('CHECK_ID_MISMATCH_'+name)
                except (OSError,ValueError,KeyError,zipfile.BadZipFile) as e:entry['errors'].append('INVALID_'+name+':'+type(e).__name__)
            if entry['files']['evidence.zip']:
                try:
                    with zipfile.ZipFile(io.BytesIO(z.read(cid+'/evidence.zip'))) as child:identity=read_json(child,'input.json').get('id')
                    if identity!=cid:entry['errors'].append('CHECK_ID_MISMATCH_evidence.zip')
                except (OSError,ValueError,KeyError,zipfile.BadZipFile) as e:entry['errors'].append('INVALID_evidence.zip:'+type(e).__name__)
            expected=str(r.get('archive') or '').replace('\\','/').rsplit('/',1)[-1]
            if expected=='evidence.zip' and not entry['files']['evidence.zip']:entry['errors'].append('EXPECTED_EVIDENCE_MISSING')
            if r['status']=='RUNNING':entry['errors'].append('CHECK_STILL_RUNNING')
            rows.append(entry)
        dbs=[n for n in names if n.endswith('_monitor.db') and '/' not in n]
        if len(dbs)==1:
            try:
                with tempfile.TemporaryDirectory() as td:
                    dest=Path(td)/'monitor.db'
                    with z.open(dbs[0]) as src,dest.open('wb') as dst:
                        __import__('shutil').copyfileobj(src,dst)
                    with closing(sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True)) as c:
                        actual=dict(c.execute('SELECT id,status FROM position_checks_v0730'))
                    db_match=actual=={r['id']:r['status'] for r in checks}
            except (OSError,sqlite3.Error) as e:errors.append(dict(error='BACKUP_READ_ERROR',error_type=type(e).__name__))
        if not db_match:errors.append(dict(error='BACKUP_CHECK_LIST_MISMATCH'))
        extras=sorted({n.split('/')[0] for n in names if n.endswith(tuple('/'+f for f in FILES))}-set(ids))
        if extras:errors.append(dict(error='UNEXPECTED_CHECK_ARTIFACTS',check_ids=extras))
    incomplete=[r['check_id'] for r in rows if r['errors']]
    status='PARTIAL' if incomplete or errors else 'PASS' if rows else 'NO_CHECKS'
    return dict(schema='MONITOR_ARTIFACTS_V74',status=status,check_count=len(rows),database_checks_match=db_match,
        archived_counts={f:sum(r['files'][f] for r in rows) for f in FILES},incomplete_check_ids=incomplete,errors=errors,checks=rows,
        note='Checks and file identities verified against ZIP and its database backup. No missing artifacts reconstructed; legacy checks without v73 diagnostics remain partial. Not proof of successful simulation or trading.')

def finalize(target,copy_errors=()):
    try:result=verify(target,copy_errors)
    except (OSError,ValueError,KeyError,sqlite3.Error,zipfile.BadZipFile) as e:
        result=dict(schema='MONITOR_ARTIFACTS_V74',status='PARTIAL',errors=[dict(error='VERIFICATION_FAILED',error_type=type(e).__name__)],note='Existing archive retained; completeness unverified.')
    with zipfile.ZipFile(target,'a',zipfile.ZIP_STORED) as z:z.writestr(MANIFEST,json.dumps(result,ensure_ascii=False))
    if result['status']=='PARTIAL':print('MONITOR EXPORT PARTIAL:',Path(target).name,'inspect',MANIFEST,flush=True)
    return result
