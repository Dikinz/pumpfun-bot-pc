import io,json,sqlite3,zipfile
from pathlib import Path
import pytest
import monitor_artifacts as ma,monitor_service as ms,position_monitor as pm,paper_branch as branch,config
from test_primary_integrity import fresh

def artifacts(root,cid,identity=None,evidence=True):
    d=root/cid;d.mkdir(parents=True,exist_ok=True);identity=identity or cid
    data=dict(check_id=identity,schema='CHILD_DIAGNOSTICS_V73')
    (d/'child_lifecycle_v73.json').write_text(json.dumps(data))
    with zipfile.ZipFile(d/'diagnostic.zip','w') as z:z.writestr('diagnostic.json',json.dumps(dict(child_diagnostics_v73=data)))
    if evidence:
        with zipfile.ZipFile(d/'evidence.zip','w') as z:z.writestr('input.json',json.dumps(dict(id=identity)))

def archive(tmp_path,ids=('check1',),root=None,status='SELLABLE',db_ids=None):
    root=root or tmp_path/'files';root.mkdir(exist_ok=True)
    database=tmp_path/'sample_monitor.db'
    with sqlite3.connect(database) as c:
        c.execute('create table position_checks_v0730(id text,status text)')
        c.executemany('insert into position_checks_v0730 values(?,?)',[(cid,status) for cid in (db_ids if db_ids is not None else ids)])
    target=tmp_path/'export.zip';checks=[dict(id=cid,status=status) for cid in ids]
    with zipfile.ZipFile(target,'w') as z:
        z.writestr('position_checks_v0730.json',json.dumps(checks));z.write(database,database.name)
        errors=ma.add(z,checks,root)
    return target,ma.finalize(target,errors)

def test_four_checks_each_has_diagnostic_lifecycle_and_optional_evidence(tmp_path):
    root=tmp_path/'files'
    for i in range(4):artifacts(root,'check'+str(i),evidence=i!=3)
    target,r=archive(tmp_path,tuple('check'+str(i) for i in range(4)),root)
    assert r['status']=='PASS' and r['database_checks_match'] and r['check_count']==4
    assert r['archived_counts']=={'evidence.zip':3,'diagnostic.zip':4,'child_lifecycle_v73.json':4}
    with zipfile.ZipFile(target) as z:assert json.loads(z.read(ma.MANIFEST))==r

def test_absent_files_are_partial_not_reconstructed(tmp_path,capsys):
    target,r=archive(tmp_path)
    assert r['status']=='PARTIAL' and r['incomplete_check_ids']==['check1']
    with zipfile.ZipFile(target) as z:assert 'check1/diagnostic.zip' not in z.namelist()
    assert 'MONITOR EXPORT PARTIAL' in capsys.readouterr().out

def test_wrong_identity_not_counted_as_complete(tmp_path):
    root=tmp_path/'files';artifacts(root,'check1','foreign')
    _,r=archive(tmp_path,root=root);assert r['status']=='PARTIAL'
    assert set(r['checks'][0]['errors'])=={'CHECK_ID_MISMATCH_diagnostic.zip','CHECK_ID_MISMATCH_child_lifecycle_v73.json','CHECK_ID_MISMATCH_evidence.zip'}

def test_database_backup_mismatch_is_explicit(tmp_path):
    root=tmp_path/'files';artifacts(root,'check1');_,r=archive(tmp_path,root=root,db_ids=['check1','missing'])
    assert r['status']=='PARTIAL' and not r['database_checks_match']

def test_empty_and_running_checks_are_not_pass(tmp_path):
    a=tmp_path/'a';a.mkdir();_,r=archive(a,ids=());assert r['status']=='NO_CHECKS'
    b=tmp_path/'b';b.mkdir();root=b/'files';artifacts(root,'check1');_,r=archive(b,root=root,status='RUNNING')
    assert r['status']=='PARTIAL' and 'CHECK_STILL_RUNNING' in r['checks'][0]['errors']

def test_corrupt_zip_and_bad_id_do_not_discard_other_data(tmp_path):
    root=tmp_path/'files';artifacts(root,'check1');(root/'check1'/'diagnostic.zip').write_bytes(b'bad zip')
    _,r=archive(tmp_path,ids=('check1','../outside'),root=root)
    assert r['status']=='PARTIAL' and any(x['error']=='INVALID_CHECK_ID' for x in r['errors'])

def test_real_amm_outer_export_contains_verified_monitor_bundle(fresh,tmp_path,monkeypatch):
    target=branch.seed(config.DB);root=tmp_path/'files';monkeypatch.setattr(pm,'ROOT',root)
    ids=['check'+str(i) for i in range(4)]
    with ms.connect(target) as c:
        for cid in ids:
            c.execute("insert into position_checks_v0730(id,trade_id,mint,scheduled_ts,started_ts,finished_ts,status,input_json,result_json) values(?,?,?,1,1,2,'SELLABLE','{}','{}')",(cid,'trade','mint'))
            artifacts(root,cid)
    out=tmp_path/'report';out.mkdir();branch.export(config.DB,out)
    with zipfile.ZipFile(out/'parallel_paper.zip') as z:
        summary=json.loads(z.read('amm_monitor_export_v74.json'));assert summary['status']=='PASS' and summary['check_count']==4
        with zipfile.ZipFile(io.BytesIO(z.read('amm_monitor_evidence.zip'))) as monitor:
            assert json.loads(monitor.read(ma.MANIFEST))==summary
            for cid in ids:
                assert cid+'/diagnostic.zip' in monitor.namelist() and cid+'/child_lifecycle_v73.json' in monitor.namelist()


def test_evidence_referenced_by_database_must_not_silently_disappear(tmp_path):
    root=tmp_path/'files';artifacts(root,'check1',evidence=False)
    target,_=archive(tmp_path,root=root)
    # Rebuild list with its original evidence pointer, simulating a removed file.
    with zipfile.ZipFile(target) as src:members={n:src.read(n) for n in src.namelist() if n!=ma.MANIFEST}
    members['position_checks_v0730.json']=json.dumps([dict(id='check1',status='SELLABLE',archive=r'C:\old\check1\evidence.zip')]).encode()
    with zipfile.ZipFile(target,'w') as dst:
        for name,data in members.items():dst.writestr(name,data)
    r=ma.finalize(target);assert r['status']=='PARTIAL' and 'EXPECTED_EVIDENCE_MISSING' in r['checks'][0]['errors']


def test_verification_closes_backup_connection_before_return(tmp_path,monkeypatch):
    root=tmp_path/'files';artifacts(root,'check1');target,_=archive(tmp_path,root=root)
    real=ma.sqlite3.connect;opened=[]
    class Connection:
        def __init__(self,*args,**kw):self.c=real(*args,**kw);self.closed=False;opened.append(self)
        def execute(self,*args):return self.c.execute(*args)
        def close(self):self.c.close();self.closed=True
    monkeypatch.setattr(ma.sqlite3,'connect',Connection)
    assert ma.verify(target)['status']=='PASS'
    assert opened and all(c.closed for c in opened)
