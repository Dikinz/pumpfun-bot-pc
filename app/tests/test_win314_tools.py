"""Windows operator tool regressions (PROD-2, PROD-6, PROD-7, PROD-8, PROD-9, VR1-CRLF-1)."""
import builtins,json,os,subprocess,sys,time,zipfile
from pathlib import Path
import pytest
import config,campaign_view
from test_primary_integrity import fresh

APP=Path(__file__).resolve().parents[1]


def test_win314_perf_reference_benchmarks_decode_without_utf8_locale():
    # PROD-2: bootstrap runs Python with -I, so PYTHONUTF8 is ignored; on a cp1251
    # Windows host the locale codec cannot decode the UTF-8 benchmark sources.
    env=dict(os.environ,PYTHONCOERCECLOCALE='0',LC_ALL='C',LANG='C')
    for key in ('PYTHONUTF8','PYTHONIOENCODING','PYTHONPATH','PYTHONHOME'):env.pop(key,None)
    code=("import sys;sys.path.insert(0,sys.argv[1]);import perf_check_v87 as p\n"
          "for n in ('campaign_view','paper_capital','late_spawn'):p.reference(n)\n"
          "print('OK',sys.flags.utf8_mode)")
    r=subprocess.run([sys.executable,'-X','utf8=0','-c',code,str(APP)],capture_output=True,text=True,encoding='utf-8',errors='replace',env=env,timeout=60,cwd=APP)
    assert r.returncode==0 and r.stdout.strip()=='OK 0',r.stderr[-2000:]


def test_win314_results_failure_reports_operator_reason(tmp_path,monkeypatch,capsys):
    # PROD-6: a fresh install printed only 'REPORT FAILED'; the reason was lost.
    import launcher,rpc_budget as rpc
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    monkeypatch.setattr(config,'DB',str(tmp_path/'data/missing.db'))
    monkeypatch.setattr(config,'RPC_CONTROL_DIR',tmp_path/'rpc')
    assert launcher.results()==1
    out=capsys.readouterr().out
    assert 'REPORT FAILED: No test database. Start the bot first.' in out
    folder=next((tmp_path/'results').glob('preservation_*/'))
    report=json.loads((folder/'REPORT_ERROR.json').read_text(encoding='utf-8'))
    assert report['error_type']=='RuntimeError' and report['message']=='No test database. Start the bot first.'


def test_win314_results_failure_never_exports_arbitrary_exception_text(tmp_path,monkeypatch,capsys):
    import launcher
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    monkeypatch.setattr(config,'DB',str(tmp_path/'data/missing.db'))
    monkeypatch.setattr(config,'RPC_CONTROL_DIR',tmp_path/'rpc')
    def leak():raise RuntimeError('https://SECRET.invalid/?api-key=PRIVATE')
    monkeypatch.setattr(launcher,'legacy_results',leak)
    assert launcher.results()==1
    folder=next((tmp_path/'results').glob('preservation_*/'))
    assert 'SECRET' not in capsys.readouterr().out and 'SECRET' not in (folder/'REPORT_ERROR.json').read_text(encoding='utf-8')


def test_win314_empty_installation_dashboard_has_no_epoch_age_or_monitor_alarm(fresh):
    # PROD-7/PROD-8: meta default '0' was a truthy string (age ~1.79e9 s), and a
    # never-written monitor state file was shown as STATE_READ_ERROR.
    primary=campaign_view.overview(config.DB)['primary']
    live=primary['live_v87']
    assert live['market_data_age_s'] is None and primary['stream']['age_s'] is None
    assert live['monitor_status']=='NOT_STARTED'
    assert live['state_read_error']['reason']=='MONITOR_STATE_MISSING' # raw diagnostic preserved


def test_win314_started_installation_keeps_missing_monitor_state_as_error(fresh):
    now=time.time()
    for k,v in (('runtime_started_ts_v41',now-60),('runtime_updated_ts_v41',now),('runtime_state_v41','RUNNING'),('trade_last_market_ts',now-5)):
        fresh.execute('INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)',(k,str(v)))
    fresh.commit()
    primary=campaign_view.overview(config.DB)['primary'];live=primary['live_v87']
    assert live['monitor_status']=='STATE_READ_ERROR'
    assert 4<=live['market_data_age_s']<60 and 4<=primary['stream']['age_s']<60


@pytest.mark.parametrize('answers',[[],['So11111111111111111111111111111111111111112','not-a-number']])
def test_win314_amm_check_without_interactive_input_fails_cleanly(tmp_path,monkeypatch,capsys,answers):
    # PROD-9: 06_AMM_CHECK with stdin=NUL crashed with an EOFError traceback.
    import target_amm_check as target
    monkeypatch.setattr(target,'__file__',str(tmp_path/'target_amm_check.py'))
    monkeypatch.setattr(sys,'argv',['target_amm_check.py'])
    queue=list(answers)
    def fake_input(prompt=''):
        if not queue:raise EOFError
        return queue.pop(0)
    monkeypatch.setattr(builtins,'input',fake_input)
    async def no_rpc(*a):pytest.fail('RPC check started without complete input')
    monkeypatch.setattr(target,'run',no_rpc)
    (tmp_path/'data').mkdir();(tmp_path/'data/target_amm_check_v59.json').write_text(json.dumps(dict(passed=True)))
    assert target.main()==1
    report=json.loads((tmp_path/'data/target_amm_check_v59.json').read_text(encoding='utf-8'))
    assert report['passed'] is False and report['status']==('INPUT_UNAVAILABLE' if not answers else 'INPUT_INVALID')
    assert report['broadcast'] is False and report['paper_mutated'] is False
    assert 'Traceback' not in capsys.readouterr().err


@pytest.mark.parametrize('eol',[b'\r\n',b'\n'],ids=['crlf','lf'])
def test_win314_exported_dashboard_keeps_template_line_endings(fresh,tmp_path,monkeypatch,eol):
    # VR1-CRLF-1: a CRLF (autocrlf) template was decoded and written in text mode,
    # so Windows turned every CRLF into CR CR LF in dashboard.html.
    import launcher,dashboard.server as server
    page=eol.join([b'<html>',b'<script>var a=1;</script>',b'</html>',b''])
    details=eol.join([b'<html>',b'<a href="/">back</a>',b'<script>var b=2;</script>',b'</html>',b''])
    monkeypatch.setattr(server,'PAGE',page);monkeypatch.setattr(server,'DETAILS',details)
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    assert launcher.results()==0
    folder=next((tmp_path/'results').glob('*/review_bundle_manifest.json')).parent
    for name,template in (('dashboard.html',page),('dashboard_details.html',details)):
        raw=(folder/name).read_bytes()
        assert b'\r\r' not in raw
        assert raw.count(eol)==template.count(eol) and raw.endswith(b'</html>'+eol)
        if eol==b'\n':assert b'\r' not in raw
        assert b'window.REPORT_DATA=' in raw
    assert b'href="dashboard.html"' in (folder/'dashboard_details.html').read_bytes()
