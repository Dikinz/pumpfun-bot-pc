"""Disposable, offline PAPER recovery scenario; never operates the running bot."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
from xml.etree import ElementTree as ET
APP=Path(__file__).resolve().parent
REPORT=APP.parent/'results/recovery_check_v76.json'

def main():
    import config
    report=dict(schema='RECOVERY_SELFTEST_V76',version=config.VERSION,created_ts=time.time(),
        passed=False,scope='OFFLINE_DISPOSABLE_PAPER_CLOCK_INJECTION',os_clock_modified=False,
        windows_suspend_resume_proven=False,live_rpc_proven=False,
        note='Time reports and the market are injected test data. Test DBs are temporary; no existing trading DB is opened. The running bot is not paused or controlled.')
    with tempfile.TemporaryDirectory(prefix='pump_recovery_v76_') as td:
        root=Path(td);xml=root/'tests.xml';evidence=root/'scenario.json'
        env=dict(os.environ,DATABASE_PATH=str(root/'unused.db'),EXECUTION_MODE='paper',
            PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',RECOVERY_CHECK_EVIDENCE=str(evidence))
        for key in ('PYTEST_ADDOPTS','PYTHONPATH','PYTHONHOME'):env.pop(key,None)
        command=[sys.executable,'-m','pytest','-q','-rA','--basetemp',str(root/'cases'),
            '--junitxml',str(xml),'tests/test_recovery_classification_v76.py','tests/test_recovery_gate_v75.py']
        try:
            result=subprocess.run(command,cwd=APP,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
            report.update(child_returncode=result.returncode,stdout=result.stdout,stderr=result.stderr)
            suites=ET.parse(xml).getroot()
            cases=suites.findall('.//testcase')
            report['tests']=[dict(name=c.get('name'),outcome='FAILED' if c.find('failure') is not None or c.find('error') is not None else 'SKIPPED' if c.find('skipped') is not None else 'PASSED') for c in cases]
            report['scenario']=json.loads(evidence.read_text(encoding='utf-8'))
            report['passed']=result.returncode==0 and bool(cases) and all(c['outcome']=='PASSED' for c in report['tests']) and report['scenario'].get('financial_integrity')=='OK'
        except Exception as exc:
            report.update(error_type=type(exc).__name__,error=str(exc))
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    tmp=REPORT.with_suffix('.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(REPORT)
    print('CONTROLLED PAPER RECOVERY: '+('PASS' if report['passed'] else 'FAILED'))
    print('Offline clock injection; no Windows suspension, clock modification or live RPC.')
    print('Report:',REPORT)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
