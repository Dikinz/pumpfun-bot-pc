"""Offline evidence export. Never repairs or opens source databases in SQLite."""
import argparse,base64,hashlib,json,os,shutil,sqlite3,tempfile,time,uuid,zipfile
from pathlib import Path
from contextlib import closing
from sqlite_diagnostics import error

def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def encode(v):
    return {'base64':base64.b64encode(v).decode()} if isinstance(v,bytes) else v

def examine(path,folder,budget=30):
    folder.mkdir(parents=True);result=dict(database=path.name,tables=[],integrity=None,errors=[],recovered_database=False)
    deadline=time.monotonic()+budget
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as c:
            c.execute('PRAGMA query_only=ON');c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
            try:result['integrity']=[r[0] for r in c.execute('PRAGMA quick_check(20)')]
            except sqlite3.Error as e:result['errors'].append(dict(operation='quick_check',**error(e)))
            try:schema=c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            except sqlite3.Error as e:
                result['errors'].append(dict(operation='schema',**error(e)));schema=[]
            (folder/'schema.json').write_text(json.dumps(schema,ensure_ascii=False,indent=2),encoding='utf-8')
            for i,(name,sql) in enumerate(schema):
                info=dict(table=name,file=str(i)+'.jsonl',rows=0,status='SCAN_COMPLETE',completeness='Readable scan only; not proof of original row count')
                quoted='"'+name.replace('"','""')+'"'
                try:
                    with (folder/info['file']).open('w',encoding='utf-8') as f:
                        cur=c.execute('SELECT * FROM '+quoted+' NOT INDEXED');info['columns']=[x[0] for x in cur.description]
                        while True:
                            batch=cur.fetchmany(100)
                            if not batch:break
                            for row in batch:f.write(json.dumps([encode(v) for v in row],ensure_ascii=False,allow_nan=False)+'\n');info['rows']+=1
                except (sqlite3.Error,ValueError,OSError) as e:info.update(status='PARTIAL_OR_UNREADABLE',error=error(e))
                result['tables'].append(info)
            if result['integrity']==['ok']:
                try:
                    def progress(*args):
                        if time.monotonic()>deadline:raise TimeoutError()
                    with closing(sqlite3.connect(folder/'consistent.db')) as dst:
                        c.backup(dst,pages=256,progress=progress,sleep=.01)
                        result['consistent_backup_ok']=dst.execute('PRAGMA quick_check').fetchall()==[('ok',)]
                except (sqlite3.Error,TimeoutError) as e:result['errors'].append(dict(operation='backup',**error(e)))
    except sqlite3.Error as e:result['errors'].append(dict(operation='open',**error(e)))
    (folder/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');return result

def preserve(source,output,primary_name='clean_0762.db'):
    source=Path(source).resolve();output=Path(output).resolve()
    if not source.is_dir():raise ValueError('SOURCE_DIRECTORY_MISSING')
    if output==source:raise ValueError('OUTPUT_MUST_BE_OUTSIDE_SOURCE')
    output.mkdir(parents=True,exist_ok=False)
    manifest=dict(schema='SQLITE_PRESERVATION_V66',status='EVIDENCE_ONLY',source_name=source.name,created_ts=time.time(),snapshot_consistency='UNPROVEN_RAW_COPY; stop all writers before capture',files=[],databases=[],errors=[],note='No source repair, no resume, no balance certification. Originals retained; analysis uses separate working copies.')
    raw=output/'raw';raw.mkdir()
    # Allowlisted local diagnostics only, never .env, keys, or arbitrary folder contents.
    selected=[p for p in source.iterdir() if p.is_file() and p.name.startswith((Path(primary_name).stem,'clean_','program_cache_v57.db','time_check','monitor_selftest','target_amm','runtime_benchmark')) and (p.name.endswith(('.db','.db-wal','.db-shm','.json','.jsonl','.jsonl.1','.txt')))]
    for p in selected:
        try:
            before=sha(p);shutil.copyfile(p,raw/p.name);after=sha(p);copied=sha(raw/p.name)
            manifest['files'].append(dict(name=p.name,sha256=copied,source_before=before,source_after=after,stable=before==after==copied,bytes=(raw/p.name).stat().st_size))
        except OSError as e:manifest['errors'].append(dict(file=p.name,**error(e)))
    with tempfile.TemporaryDirectory() as td:
        work=Path(td)
        for p in raw.glob('*.db'):
            try:
                shutil.copyfile(p,work/p.name)
                wal=p.with_name(p.name+'-wal')
                if wal.exists():shutil.copyfile(wal,work/wal.name)
                # Rebuild SHM only in analysis copies; original SHM is retained byte-for-byte.
                manifest['databases'].append(examine(work/p.name,output/'readable'/p.name))
            except (OSError,sqlite3.Error) as e:manifest['errors'].append(dict(database=p.name,**error(e)))
    (output/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    target=output.with_suffix('.zip')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for p in output.rglob('*'):
            if p.is_file():z.write(p,p.relative_to(output))
    return target,manifest

def main(args=None):
    ap=argparse.ArgumentParser();ap.add_argument('source');ap.add_argument('--output');args=ap.parse_args(args)
    out=args.output or str(Path(__file__).resolve().parents[1]/'results'/('preservation_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]))
    target,m=preserve(args.source,out);print('PRESERVED EVIDENCE:',target);print('Databases:',len(m['databases']),'Not repaired; inspect MANIFEST.json.');return 0
if __name__=='__main__':raise SystemExit(main())
