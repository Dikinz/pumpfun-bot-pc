"""Read-only evidence capture of the RPC coordination directory."""
import hashlib,json,sqlite3,zipfile
from contextlib import closing
from pathlib import Path


def export(root,target):
    root=Path(root);target=Path(target)
    report=dict(schema='RPC_CONTROL_EVIDENCE_V892',files=[],errors=[],source_exists=root.exists())
    names=['control.db','control.db-wal','control.db-shm','control_failed.json','metadata.lock']
    names += [p.name for p in root.glob('rate_*.bin')]
    names += ['slot_'+str(i)+'.lock' for i in range(8)]
    names += [p.relative_to(root).as_posix() for p in (root/'outcomes').glob('*') if p.suffix in ('.json','.tmp')]
    names += [p.relative_to(root).as_posix() for p in (root/'exit_priority_v94').glob('*') if p.suffix in ('.json','.tmp')]
    def stamp(path):
        try:
            st=path.stat();return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
        except FileNotFoundError:return None
    identity_names=('control.db','control.db-wal')
    source_before={n:stamp(root/n) for n in identity_names}
    captured={}
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for name in names:
            p=root/name
            if not p.exists():continue
            try:
                if p.is_symlink() or p.stat().st_size>32*1024*1024:raise ValueError('UNSAFE_OR_OVERSIZE')
                before=p.stat();data=p.read_bytes();after=p.stat()
                z.writestr('raw/'+name,data)
                if name in ('control.db','control.db-wal'):captured[name]=data
                report['files'].append(dict(name=name,sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),stable=(before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns)))
            except (OSError,ValueError) as exc:report['errors'].append(dict(file=name,error_type=type(exc).__name__))
        source_after={n:stamp(root/n) for n in identity_names}
        report['source_db_wal_stable_v95']=source_before==source_after
        if source_before!=source_after:report['errors'].append(dict(file='control.db+wal',error_type='SOURCE_CHANGED_DURING_CAPTURE'))
        # Raw WAL/SHM stay in the archive; a SQLite backup is a separate view.
        if (root/'control.db').exists():
            import tempfile
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    p=Path(temporary)/'consistent.db'
                    # Backup the already captured DB/WAL, never open original SHM.
                    # A read-only SQLite reader can still change WAL read marks in
                    # source SHM while a persistent coordinator connection is open.
                    for name,data in captured.items():(Path(temporary)/name).write_bytes(data)
                    local=Path(temporary)/'control.db'
                    report['backup_scope_v95']='CAPTURED_DB_WAL_COPY'
                    with closing(sqlite3.connect(local.as_uri()+'?mode=ro',uri=True,timeout=.25)) as source,closing(sqlite3.connect(p)) as dest:
                        import time
                        deadline=time.monotonic()+2
                        def progress(*args):
                            if time.monotonic()>deadline:raise TimeoutError('RPC_EVIDENCE_BACKUP_DEADLINE')
                        source.backup(dest,pages=64,progress=progress,sleep=.01)
                        report['integrity']= [r[0] for r in dest.execute('PRAGMA integrity_check')]
                    z.write(p,'consistent.db')
            except (OSError,sqlite3.Error,TimeoutError) as exc:report['errors'].append(dict(file='consistent.db',error_type=type(exc).__name__))
        report['status']='PARTIAL' if report['errors'] or any(not f['stable'] for f in report['files']) else 'CAPTURED'
        z.writestr('MANIFEST.json',json.dumps(report,indent=2))
    return report
