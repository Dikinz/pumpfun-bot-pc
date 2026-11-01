"""Small JSON-only review ZIPs; raw database groups are independent archives."""
import datetime,hashlib,json,math,re,zipfile,zlib,io
from pathlib import Path
MAX_REPORT_BYTES=16*1024*1024
DEFAULT_VOLUME_BYTES=8*1024*1024

def stamp(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<value<253402300799:return None
    return datetime.datetime.fromtimestamp(value,datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')

def report_time(data):
    if not isinstance(data,dict):return None,None
    for field in ('created_ts','generated_ts','captured_ts','ts','heartbeat_ts','updated_ts'):
        if stamp(data.get(field)):return field,data[field]
    return None,None

def export(folder,max_volume_bytes=DEFAULT_VOLUME_BYTES):
    folder=Path(folder);items=[];errors=[]
    for path in sorted(folder.glob('*.json')):
        if path.name.startswith('review_bundle'):continue
        try:
            if path.stat().st_size>MAX_REPORT_BYTES:raise ValueError('REPORT_OVERSIZE')
            raw=path.read_bytes();data=json.loads(raw);field,ts=report_time(data)
            name=path.stem+('__'+stamp(ts) if ts is not None else '')+'.json'
            items.append((dict(source_name=path.name,name=name,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),timestamp_field=field,report_ts=ts),raw))
        except (OSError,ValueError,TypeError) as exc:errors.append(dict(name=path.name,error_type=type(exc).__name__))
    def archive(batch):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
            for meta,raw in batch:z.writestr(meta['name'],raw)
            z.writestr('REVIEW_MANIFEST.json',json.dumps(dict(schema='REVIEW_BUNDLE_V100',files=[meta for meta,raw in batch]),indent=2))
        return buf.getvalue()
    batches=[];current=[]
    for item in items:
        if current and len(archive(current+[item]))>max_volume_bytes:batches.append(current);current=[]
        current.append(item)
    if current:batches.append(current)
    result=dict(schema='REVIEW_BUNDLE_V100',files=[meta for meta,raw in items],volumes=[],errors=errors,note='Only JSON reports. Filenames use the report timestamp, not export time. Missing timestamps are explicit; original report bytes preserved.')
    for i,batch in enumerate(batches,1):
        path=folder/('review_bundle_%03d.zip'%i)
        path.write_bytes(archive(batch))
        result['volumes'].append(dict(name=path.name,bytes=path.stat().st_size,sha256=sha(path),oversize_single_report=len(batch)==1 and path.stat().st_size>max_volume_bytes))
    (folder/'review_bundle_manifest.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result

def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def database_volumes(preserved,destination):
    # Read the archive only. Never open original DB/WAL/SHM in SQLite or modify
    # the preservation ZIP. Each volume contains one DB group and its manifest.
    import shutil
    preserved=Path(preserved);destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    result=dict(schema='RAW_DATABASE_VOLUMES_V100',source_name=preserved.name,source_sha256=sha(preserved),volumes=[])
    with zipfile.ZipFile(preserved) as source:
        manifest=json.loads(source.read('MANIFEST.json'))
        for index,db in enumerate(manifest['databases'],1):
            name=db['database'];selected=[n for n in source.namelist() if n.startswith('readable/'+name+'/') or n=='raw/'+name or n.startswith('raw/'+name+'-') or n.startswith('raw/'+name+'.')]
            path=destination/('raw_db_%02d.zip'%index);files=[]
            with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as out:
                for n in selected:
                    digest=hashlib.sha256();count=0
                    with source.open(n) as src,out.open(n,'w',force_zip64=True) as dst:
                        while True:
                            block=src.read(1024*1024)
                            if not block:break
                            dst.write(block);digest.update(block);count+=len(block)
                    files.append(dict(name=n,bytes=count,sha256=digest.hexdigest()))
                out.writestr('VOLUME_MANIFEST.json',json.dumps(dict(schema=result['schema'],database=name,source_archive_sha256=result['source_sha256'],files=files),indent=2))
            result['volumes'].append(dict(name=path.name,database=name,bytes=path.stat().st_size,sha256=sha(path)))
    (destination/'raw_database_volumes_manifest.json').write_text(json.dumps(result,indent=2),encoding='utf-8');return result
