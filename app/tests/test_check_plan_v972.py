import json,sys,zipfile
from pathlib import Path
import check_plan_v972 as plan

def test_checkplan972_streamed_integrity_rejects_same_size_same_mtime_change(tmp_path,monkeypatch):
    import hashlib,os,pytest,release_guard
    app=tmp_path/'app';app.mkdir();p=app/'large_fixture.bin'
    raw=b'x'*(2*1024*1024);p.write_bytes(raw);st=p.stat()
    (tmp_path/'BUILD_MANIFEST.json').write_text(json.dumps(dict(version=release_guard.VERSION,
        files={'app/large_fixture.bin':hashlib.sha256(raw).hexdigest()})))
    def unbounded_read(*a,**kw):raise AssertionError('whole-file read used')
    monkeypatch.setattr(Path,'read_bytes',unbounded_read)
    release_guard.verify(tmp_path)
    with p.open('r+b') as f:f.write(b'y')
    os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
    with pytest.raises(RuntimeError,match='FILE_CHANGED'):release_guard.verify(tmp_path)

def test_checkplan972_preserves_all_previous_files_without_rerunning_recovery():
    old=json.loads((Path(__file__).resolve().parents[2]/'validation/CHECK_PLAN_INPUT_v972.json').read_text())
    previous=set(old['previous_recovery_files'])|set(old['previous_technical_files'])
    planned=plan.RECOVERY_TEST_FILES+plan.technical_remainder()
    assert previous.issubset(planned)
    assert len(planned)==len(set(planned))
    assert len(set(old['previous_recovery_files'])&set(old['previous_technical_files']))==19
    assert plan.summary()['requested_unique_test_files']==plan.summary()['planned_unique_test_files']

def test_checkplan972_failed_recovery_never_runs_following_checks(monkeypatch):
    import launcher
    calls=[]
    def call(*args):calls.append(args);return 1
    monkeypatch.setattr(launcher,'call',call);monkeypatch.setattr(sys,'argv',['launcher.py','check'])
    assert launcher.main()==1 and calls==[('recovery_selftest.py',)]

def test_checkplan972_captures_original_cause_and_control_database(tmp_path,monkeypatch):
    import rpc_budget as rpc,rpc_concurrency_evidence_v972 as evidence
    b=rpc.Budget(tmp_path/'control',rate=1000)
    with b.request('https://rpc.invalid'):pass
    exc=PermissionError(13,'PRIVATE');exc.winerror=5
    b.block(exc);b.close()
    destination=tmp_path/'rpc_concurrency_v972.json'
    monkeypatch.setenv('RPC_CONCURRENCY_EVIDENCE_V972',str(destination))
    errors=[dict(worker='controlled',failure_chain=rpc.failure_details(exc))]
    r=evidence.capture(b.root,errors,0,0)
    assert not r['passed'] and r['control_failed.json']['winerror']==5
    assert r['errors'][0]['failure_chain'][0]['winerror']==5
    assert 'PRIVATE' not in json.dumps(r)
    with zipfile.ZipFile(destination.with_suffix('.zip')) as z:
        assert z.read('raw/control_failed.json')==b.failed.read_bytes()
        assert 'consistent.db' in z.namelist()

def test_checkplan972_failure_archive_retains_nested_concurrency_evidence(tmp_path):
    import recovery_evidence
    root=tmp_path/'evidence';root.mkdir();nested=root/'rpc_concurrency_v972.zip'
    with zipfile.ZipFile(nested,'w') as z:z.writestr('raw/control_failed.json','{"winerror":5}')
    raw=nested.read_bytes();report=dict(passed=False)
    recovery_evidence.preserve(root,report,tmp_path/'out')
    with zipfile.ZipFile(report['failure_evidence_zip_v86_1']) as z:
        assert z.read(nested.name)==raw
    assert nested.read_bytes()==raw
