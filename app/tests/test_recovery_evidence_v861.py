"""Regression for v86's TimeoutExpired report losing all pytest output."""
import json,os,subprocess,sys,zipfile
from pathlib import Path
import pytest
import recovery_evidence as evidence,recovery_progress as progress,recovery_selftest as check


def test_timeout_retains_bytes_output_and_partial_evidence(tmp_path,monkeypatch):
    report={'passed':False}
    def timeout(*args,**kwargs):
        assert kwargs['timeout']==120
        (tmp_path/'progress.jsonl').write_text(json.dumps(dict(event='TEST_START',nodeid='tests/t.py::hung'))+'\n')
        (tmp_path/'scenario.json').write_text('{"partial":true}')
        (tmp_path/'partial.jsonl').write_text('{"saved":true}\n{"unfinished"')
        raise subprocess.TimeoutExpired(args[0],120,output=b'tests/t.py::hung \xd0\xa2\xd0\xb5\xd1\x81\xd1\x82',stderr=b'partial stderr')
    monkeypatch.setattr(subprocess,'run',timeout)
    with pytest.raises(subprocess.TimeoutExpired):evidence.run(['pytest'],cwd=tmp_path,env={},root=tmp_path,report=report)
    report.update(error_type='TimeoutExpired',error='120 seconds')
    evidence.preserve(tmp_path,report,tmp_path/'results')
    assert report['stdout'].endswith('Тест') and report['stderr']=='partial stderr'
    assert report['child_returncode'] is None and report['descendant_cleanup_verified_v86_1'] is False
    assert report['last_test_phase_v86_1']['nodeid']=='tests/t.py::hung'
    assert report['test_progress_is_partial_v86_1'] is True
    assert report['partial_artifacts_v86_1']['scenario.json']['partial']
    assert report['partial_artifacts_v86_1']['partial.jsonl']==[{'saved':True}]
    assert report['evidence_read_errors_v86_1'][0]['line']==2
    with zipfile.ZipFile(report['failure_evidence_zip_v86_1']) as z:
        assert z.testzip() is None and z.read('stdout.log').decode().endswith('Тест')
        assert {'progress.jsonl','scenario.json','partial.jsonl','MANIFEST.json'}<=set(z.namelist())


def test_actual_subprocess_timeout_preserves_flushed_output(tmp_path):
    report={}
    with pytest.raises(subprocess.TimeoutExpired):
        evidence.run([sys.executable,'-u','-c',"import time;print('ENTERED_TEST',flush=True);time.sleep(60)"],cwd=tmp_path,env=os.environ.copy(),root=tmp_path,report=report,timeout=1)
    assert 'ENTERED_TEST' in report['stdout'] and report['timeout_v86_1'] is True
    assert (tmp_path/'stdout.log').read_text() == report['stdout']


def test_failure_without_junit_still_writes_report(tmp_path,monkeypatch):
    import roadmap_progress
    live=tmp_path/'live/acceptance_v90.json';live.parent.mkdir();live.write_text('{"passed":true}')
    monkeypatch.setattr(roadmap_progress,'acceptance_path',lambda:live)
    monkeypatch.setattr(check,'REPORT',tmp_path/'results/recovery_check_v87.json')
    def timeout(*args,**kwargs):
        assert kwargs['timeout']==check.SUITE_TIMEOUT_S==600
        raise subprocess.TimeoutExpired(args[0],kwargs['timeout'],output=b'last test pending',stderr=b'')
    monkeypatch.setattr(subprocess,'run',timeout)
    assert check.main()==1
    report=json.loads(check.REPORT.read_text())
    assert report['passed'] is False and report['error_type']=='TimeoutExpired'
    assert report['stdout']=='last test pending' and Path(report['failure_evidence_zip_v86_1']).is_file()
    assert report['last_test_phase_v86_1'] is None
    assert live.read_text()=='{"passed":true}'
    assert json.loads((check.REPORT.parent/live.name).read_text())['passed'] is False


def test_progress_records_failure_and_teardown(tmp_path,monkeypatch):
    from types import SimpleNamespace as S
    path=tmp_path/'progress.jsonl';monkeypatch.setenv('RECOVERY_PROGRESS_PATH',str(path))
    progress.pytest_sessionstart(None);progress.pytest_runtest_logstart('test.py::case',None)
    for phase,outcome in [('setup','passed'),('call','failed'),('teardown','passed')]:
        progress.pytest_runtest_logreport(S(nodeid='test.py::case',when=phase,outcome=outcome,duration=.01))
    progress.pytest_sessionfinish(None,1)
    report={'passed':False};evidence.preserve(tmp_path,report,tmp_path/'out')
    assert report['last_test_phase_v86_1']['phase']=='teardown' and report['test_progress_is_partial_v86_1'] is False
    records=report['partial_artifacts_v86_1']['progress.jsonl']
    assert any(x.get('outcome')=='failed' for x in records) and all(x['pid']==os.getpid() for x in records)


def test_evidence_export_failure_does_not_mask_test_error(tmp_path):
    target=tmp_path/'not-a-directory';target.write_text('x')
    report=dict(passed=False,error_type='TimeoutExpired',error='test reason')
    evidence.preserve(tmp_path,report,target)
    assert report['error_type']=='TimeoutExpired' and report['error']=='test reason'
    assert report['evidence_export_error_v86_1']['error_type']=='FileExistsError'


def test_real_pytest_plugin_records_each_phase_before_and_after(tmp_path):
    (tmp_path/'test_probe.py').write_text('def test_probe():\n    assert True\n')
    target=tmp_path/'progress.jsonl'
    env=dict(os.environ,PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',RECOVERY_PROGRESS_PATH=str(target))
    result=subprocess.run([sys.executable,'-m','pytest','-q','-p','recovery_progress',str(tmp_path/'test_probe.py')],cwd=Path(evidence.__file__).parent,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    rows=[json.loads(x) for x in target.read_text().splitlines()]
    phases=[(x['event'],x.get('phase')) for x in rows if x['event'].startswith('TEST_PHASE')]
    assert phases==[(event,phase) for phase in ('setup','call','teardown') for event in ('TEST_PHASE_START','TEST_PHASE_END')]
    assert rows[-1]['event']=='SESSION_FINISH'
