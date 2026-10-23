"""SQLite online backup gives a consistent snapshot including committed WAL."""
import sqlite3,zipfile,tempfile,json
from pathlib import Path
from contextlib import closing
from datetime import datetime,timezone

def export(source,folder):
 source=Path(source).resolve();folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
 if not source.is_file():raise FileNotFoundError(source)
 stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
 target=folder/('test_data_'+stamp+'.zip')
 with tempfile.TemporaryDirectory() as tmp:
  dst=Path(tmp)/source.name
  with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(dst)) as out:
   src.backup(out)
   result=[r[0] for r in out.execute('PRAGMA integrity_check')]
   if result!=['ok']:raise RuntimeError('Backup integrity failed: '+str(result[:3]))
  with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
   z.write(dst,dst.name);z.writestr('CHECK.json',json.dumps({'integrity':'ok','utc':stamp,'database':source.name}))
 return target
if __name__=='__main__':
 import config
 print(export(config.DB,'exports'))
