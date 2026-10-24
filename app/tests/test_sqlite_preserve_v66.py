import sqlite3,json,hashlib,zipfile
from pathlib import Path
import pytest
import sqlite_preserve as sp,sqlite_diagnostics as diag

def test_bad_primary_does_not_hide_healthy_db_and_never_changes_source(tmp_path):
    src=tmp_path/'data';src.mkdir();(src/'clean_0762.db').write_bytes(b'bad sqlite')
    with sqlite3.connect(src/'clean_0762_route_paper.db') as c:
        c.execute('create table trades(id integer primary key,value blob)');c.execute("insert into trades values(1,?)",(b'abc',))
    (src/'.env').write_text('SECRET');(src/'wallet.json').write_text('SECRET')
    before={p.name:p.read_bytes() for p in src.iterdir()}
    z,m=sp.preserve(src,tmp_path/'out')
    assert before=={p.name:p.read_bytes() for p in src.iterdir()}
    assert len(m['databases'])==2
    bad=next(x for x in m['databases'] if x['database']=='clean_0762.db')
    assert any(e.get('sqlite_errorcode')==26 for e in bad['errors'])
    healthy=next(x for x in m['databases'] if 'route' in x['database'])
    assert healthy['consistent_backup_ok'] and healthy['tables'][0]['rows']==1
    with zipfile.ZipFile(z) as a:
        assert 'raw/clean_0762.db' in a.namelist()
        assert not any('wallet' in n or '.env' in n for n in a.namelist())

def test_output_inside_source_rejected(tmp_path):
    with pytest.raises(ValueError):sp.preserve(tmp_path,tmp_path)

def test_connection_errors_are_logged_without_parameters(tmp_path,monkeypatch):
    import database as db,config
    monkeypatch.setattr(config,'DB',str(tmp_path/'clean_test.db'))
    c=sqlite3.connect(config.DB,factory=db.Connection)
    c.execute('create table t(x unique)');c.execute('insert into t values(?)',('SENSITIVE_VALUE',))
    with pytest.raises(sqlite3.IntegrityError):c.execute('insert into t values(?)',('SENSITIVE_VALUE',))
    c.close();text=(tmp_path/'clean_test.db.sqlite_diagnostics.jsonl').read_text();r=json.loads(text)
    assert r['sqlite_errorname']=='SQLITE_CONSTRAINT_UNIQUE' and r['operation']=='SQL_INSERT'
    assert 'SENSITIVE_VALUE' not in text

def test_unreadable_schema_still_retains_raw_bytes(tmp_path):
    src=tmp_path/'data';src.mkdir();p=src/'clean_broken.db';p.write_bytes(b'SQLite format 3\0'+b'\0'*500)
    z,m=sp.preserve(src,tmp_path/'out')
    with zipfile.ZipFile(z) as a:assert a.read('raw/clean_broken.db')==p.read_bytes()
    assert not m['databases'][0]['recovered_database']
