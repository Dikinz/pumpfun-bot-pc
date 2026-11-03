"""Regressions from the user's Windows run of 01.11.2026 (validation/run_20261101).

AN_REC_1/2/3 recovery interruption and progress, RUN-01 stall evidence, RUN-02
direct branch STOP, RUN-03/04 flight-recorder retention, RUN-08 abandoned
preentry label, ACC-1 small-sample quantile, TERM-1..5 dashboard display.
Round 2: CR1-01 build identity, CR1-03 Russian diagnostics, CR1-04 cached native
reader isolation, CR1-05 RPC_DEFERRED label and exit-signal lab coverage.
No trading threshold, position size, RPC limit or freshness rule is involved.
"""
import io,json,os,re,sqlite3,subprocess,sys,threading,time,zipfile
from contextlib import closing
from pathlib import Path
import pytest
import config
from test_primary_integrity import fresh  # noqa: F401  (fixture)

APP=Path(__file__).resolve().parents[1]
DASH=APP/'dashboard'


# ---------------------------------------------------------------- AN_REC_1 ---
def test_win314_recovery_interrupt_is_recorded_in_manifest_and_marker(tmp_path,monkeypatch,capsys):
    import roadmap_progress,recovery_selftest as check
    results=tmp_path/'results';results.mkdir()
    previous=dict(version=config.VERSION,passed=True,created_ts=1.0)
    (results/'recovery_check_v89.json').write_text(json.dumps(previous),encoding='utf-8')
    (results/'acceptance_v90.json').write_text(json.dumps(dict(previous,native_windows_verified=True)),encoding='utf-8')
    before={p.name:p.read_bytes() for p in results.iterdir()}
    monkeypatch.setattr(check,'REPORT',results/'recovery_check_v89.json')
    monkeypatch.setattr(roadmap_progress,'acceptance_path',lambda:results/'acceptance_v90.json')
    def interrupted(command,**kwargs):
        assert kwargs['timeout']==check.SUITE_TIMEOUT_S==600  # deadline unchanged
        root=Path(kwargs['env']['RECOVERY_PROGRESS_PATH']).parent
        (root/'progress.jsonl').write_text(json.dumps(dict(event='TEST_PHASE_START',nodeid='tests/t.py::slow',phase='setup'))+'\n')
        raise KeyboardInterrupt
    monkeypatch.setattr(subprocess,'run',interrupted)
    with pytest.raises(KeyboardInterrupt):check.main()
    out=capsys.readouterr().out
    assert 'Reason: INTERRUPTED (Ctrl+C)' in out and 'CONTROLLED PAPER RECOVERY: INTERRUPTED' in out
    # The previous (PASS) pair is neither overwritten nor deleted.
    assert {p.name:p.read_bytes() for p in results.iterdir() if p.name in before}==before
    marker=json.loads((results/check.LAST_ATTEMPT_NAME).read_text(encoding='utf-8'))
    assert marker['status']=='INTERRUPTED' and marker['report_written'] is False and marker['error_type']=='KeyboardInterrupt'
    zips=list(results.glob('recovery_failure_v89_*.zip'));assert len(zips)==1
    with zipfile.ZipFile(zips[0]) as z:manifest=json.loads(z.read('MANIFEST.json'))
    assert manifest['error_type']=='KeyboardInterrupt' and manifest['interrupted'] is True
    assert manifest['interruption']=='CONSOLE_CTRL_C_OR_USER_INTERRUPT' and manifest['child_returncode'] is None
    assert manifest['descendant_cleanup_verified'] is False and manifest['last_test_phase']['nodeid']=='tests/t.py::slow'
    assert isinstance(manifest['suite_elapsed_s'],float)
    assert marker['failure_evidence_zip']==str(zips[0])


def test_win314_recovery_completed_attempt_updates_marker(tmp_path,monkeypatch):
    import roadmap_progress,recovery_selftest as check
    results=tmp_path/'results'
    monkeypatch.setattr(check,'REPORT',results/'recovery_check_v89.json')
    monkeypatch.setattr(roadmap_progress,'acceptance_path',lambda:results/'acceptance_v90.json')
    def timeout(command,**kwargs):raise subprocess.TimeoutExpired(command,kwargs['timeout'],output=b'',stderr=b'')
    monkeypatch.setattr(subprocess,'run',timeout)
    assert check.main()==1
    marker=json.loads((results/check.LAST_ATTEMPT_NAME).read_text(encoding='utf-8'))
    assert marker['status']=='FAILED' and marker['report_written'] is True and marker['error_type']=='TimeoutExpired'


def test_win314_evidence_run_records_interrupt_fields(tmp_path,monkeypatch):
    import recovery_evidence as evidence
    def interrupted(*a,**k):raise KeyboardInterrupt
    monkeypatch.setattr(subprocess,'run',interrupted)
    report={}
    with pytest.raises(KeyboardInterrupt):
        evidence.run(['x'],cwd=tmp_path,env={},root=tmp_path,report=report,timeout=600,out=lambda:io.StringIO())
    assert report['interrupted_v1012'] is True and report['child_returncode'] is None
    assert report['descendant_cleanup_verified_v86_1'] is False and report['suite_timeout_s_v86_1']==600
    assert 'suite_elapsed_s_v86_1' in report


# ---------------------------------------------------------------- AN_REC_2 ---
def test_win314_recovery_prints_start_notice_and_heartbeat(tmp_path):
    import recovery_evidence as evidence
    lines=[json.dumps(dict(event='TEST_PHASE_END',nodeid='t.py::a',phase=p,outcome='passed')) for p in ('setup','call','teardown')]
    lines+=[json.dumps(dict(event='TEST_PHASE_END',nodeid='t.py::b',phase='teardown',outcome='passed')),'{"unfinished']
    (tmp_path/'progress.jsonl').write_text('\n'.join(lines),encoding='utf-8')
    buf=io.StringIO();report={}
    result=evidence.run([sys.executable,'-c','import time;time.sleep(1.5)'],cwd=tmp_path,env=os.environ.copy(),root=tmp_path,
                        report=report,timeout=600,heartbeat_s=.3,out=lambda:buf)
    text=buf.getvalue()
    assert result.returncode==0 and report['suite_timeout_s_v86_1']==600
    assert 'budget 600 s' in text and 'Do not press Ctrl+C' in text
    beats=[l for l in text.splitlines() if l.startswith('Recovery check running:')]
    assert beats and 'tests completed: 2' in beats[-1] and 'last: t.py::b' in beats[-1]
    assert text.isascii()  # cp866/cp1251 consoles cannot fail on progress output


def test_win314_recovery_heartbeat_default_interval_is_bounded():
    import recovery_evidence as evidence
    assert 15<=evidence.HEARTBEAT_S<=30


# ---------------------------------------------------------------- AN_REC_3 ---
def _pair(src,ts):
    v=dict(version=config.VERSION,passed=True,created_ts=ts)
    (src/'recovery_check_v89.json').write_text(json.dumps(v));(src/'acceptance_v90.json').write_text(json.dumps(dict(v,native_windows_verified=True)))


def test_win314_release_export_flags_interrupted_attempt_after_pass(tmp_path,capsys):
    import launcher
    src=tmp_path/'root'/'results';src.mkdir(parents=True);out=tmp_path/'out';out.mkdir()
    _pair(src,100.0)
    (src/'recovery_last_attempt_v89.json').write_text(json.dumps(dict(status='INTERRUPTED',created_ts=200.0,finished_ts=236.0,failure_evidence_zip='z.zip')))
    report=launcher.export_release_checks(out,tmp_path/'root')
    assert report['passed'] is True and report['native_windows_verified'] is True  # previous PASS not synthesized away
    assert report['last_attempt_interrupted_after_report_v1012'] is True and 'INTERRUPTED' in report['last_attempt_warning_v1012']
    assert (out/'recovery_last_attempt_v89.json').is_file() and 'WARNING' in capsys.readouterr().out
    saved=json.loads((out/'release_checks_v94.json').read_text(encoding='utf-8'))
    assert saved['last_attempt_v1012']['status']=='INTERRUPTED'


def test_win314_release_export_ignores_older_interruption(tmp_path):
    import launcher
    src=tmp_path/'root'/'results';src.mkdir(parents=True);out=tmp_path/'out';out.mkdir()
    _pair(src,300.0)
    (src/'recovery_last_attempt_v89.json').write_text(json.dumps(dict(status='INTERRUPTED',created_ts=200.0)))
    assert launcher.export_release_checks(out,tmp_path/'root')['last_attempt_interrupted_after_report_v1012'] is False


# ------------------------------------------------------------------ RUN-01 ---
def test_win314_stall_probe_saves_commit_stack_threads_and_wal_size(tmp_path):
    import runtime_control
    primary=tmp_path/'clean_x.db';primary.write_bytes(b'x'*10);(tmp_path/'clean_x.db-wal').write_bytes(b'w'*4096)
    made=threading.Event();release=threading.Event();box={}
    def commit():  # stands in for a COMMIT blocked in disk I/O
        made.set();release.wait(10)
    def primary_main():
        probe=runtime_control.Probe(str(primary));box['probe']=probe
        probe.stage='PROCESS_TRADE';probe.sql_v91=dict(operation='COMMIT',database='clean_x.db',started_mono=time.monotonic()-5)
        probe.pulse=time.monotonic()-5
        commit()
    t=threading.Thread(target=primary_main,name='primary-main');t.start()
    try:
        assert made.wait(5)
        state=box['probe'].inspect()
    finally:
        release.set();t.join(5)
    first=state['first_stall']
    assert first['sql_v91']['operation']=='COMMIT' and first['stage']=='PROCESS_TRADE'
    assert 'commit' in [f['function'] for f in first['frames']]
    assert {'clean_x.db':10,'clean_x.db-wal':4096}.items()<={f['name']:f.get('bytes') for f in first['files_v1012']}.items()
    assert any(x['name']==threading.current_thread().name for x in first['threads_v1012'])
    assert state['latest_stall_v1012']['stall_index']==1


def test_win314_launcher_stall_evidence_written_before_stop(tmp_path,monkeypatch):
    import stall_evidence_v1012 as se,runtime_control
    primary=tmp_path/'clean_y.db';primary.write_bytes(b'd'*20);(tmp_path/'clean_y.db-wal').write_bytes(b'w'*30)
    runtime_control.atomic_json(runtime_control.status_path(primary),dict(pid=1,state='RUNNING',loop_pulse_age_s=53.2,active_stage='PROCESS_TRADE',
        sql_v91=dict(operation='COMMIT',age_s=53.1),first_stall=dict(frames=[dict(function='commit')])))
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    try:data=se.capture(primary,dict(bot=child),dict(primary_heartbeat_age_s=18.4,alerts=['PRIMARY_HEARTBEAT_INVALID']))
    finally:child.kill();child.wait()
    saved=json.loads(se.path(primary).read_text(encoding='utf-8'))
    assert data['written']==se.path(primary).name
    sizes={f['name']:f.get('bytes') for f in saved['files']}
    assert sizes['clean_y.db']==20 and sizes['clean_y.db-wal']==30 and 'error_type' in saved['files'][2]  # no -shm
    assert saved['primary_runtime_v60']['sql_v91']['operation']=='COMMIT' and saved['root_cause_proven'] is False
    assert saved['bot_processes'][0]['pid']==child.pid and 'cpu_user_s' in saved['bot_processes'][0]
    assert saved['recorder']['primary_heartbeat_age_s']==18.4


def test_win314_stall_evidence_is_exported_and_preserved(tmp_path):
    import inspect,launcher,sqlite_preserve,stall_evidence_v1012 as se
    assert '*_stall_evidence_v1012.json' in inspect.getsource(launcher.legacy_results)
    src=tmp_path/'data';src.mkdir();se.capture(src/'clean_q.db')
    _,manifest=sqlite_preserve.preserve(src,tmp_path/'out',primary_name='clean_q.db')
    assert 'clean_q_stall_evidence_v1012.json' in {f['name'] for f in manifest['files']}


# ------------------------------------------------------------------ RUN-02 ---
class FakeChild:
    def __init__(self,log):self.pid=4242424;self.returncode=None;self.log=log
    def poll(self):return self.returncode
    def send_signal(self,sig):self.log.append('signal')
    def wait(self,timeout=None):
        if self.returncode is None:raise subprocess.TimeoutExpired('bot',timeout)
        return self.returncode


@pytest.fixture
def stalled_tree(tmp_path,monkeypatch):
    import launcher,psutil,late_spawn,branch_transport,route_branch
    primary=tmp_path/'clean_z.db';log=[]
    class P:
        def __init__(self,pid):self.pid=pid
        def children(self,recursive=False):return []
    monkeypatch.setattr(psutil,'Process',P)
    monkeypatch.setattr(late_spawn,'wait_before_force',lambda *a,**k:False)
    def kill(child):log.append('terminate_tree');child.returncode=1
    monkeypatch.setattr(branch_transport,'terminate_tree',kill)
    route_branch.write_lifecycle(primary,dict(session='s423',status='RUNNING',pid=1,ts=time.time()))
    return launcher,primary,log,FakeChild(log)


def test_win314_stalled_primary_branch_gets_direct_stop_and_acks(stalled_tree):
    launcher,primary,log,child=stalled_tree
    import runtime_control,route_branch
    stop_file=runtime_control.control_path(route_branch.path(primary),'s423')
    def route_worker():  # the ROUTE worker's own stop-file watcher acking STOP
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and not stop_file.exists():time.sleep(.02)
        log.append('route_stop_seen')
        route_branch.write_lifecycle(primary,dict(session='s423',status='STOPPED',reason='PARENT_STOP',pid=1,ts=time.time()))
    t=threading.Thread(target=route_worker);t.start()
    result=launcher.stop_child(child,graceful=True,primary=str(primary),primary_stalled=True);t.join(5)
    record=result['branch_direct_stop_v1012']
    assert record['classification']=='PRIMARY_STALLED_ROUTE_ACKED' and record['acks']['route_branch']['acked'] is True
    assert log.index('route_stop_seen')<log.index('signal')<log.index('terminate_tree')  # STOP before signal/kill
    assert result['forced'] is True


def test_win314_stalled_primary_unacked_branch_is_classified(stalled_tree,monkeypatch):
    launcher,primary,log,child=stalled_tree
    monkeypatch.setattr(launcher,'BRANCH_ACK_WAIT_S',.3)
    result=launcher.stop_child(child,graceful=True,primary=str(primary),primary_stalled=True)
    record=result['branch_direct_stop_v1012']
    assert record['classification']=='PRIMARY_STALLED_ROUTE_UNACKED' and record['requested']==[dict(engine='route_branch',session='s423')]


def test_win314_primary_stop_timeout_waits_for_branch_ack_before_kill(stalled_tree,monkeypatch):
    launcher,primary,log,child=stalled_tree
    monkeypatch.setattr(launcher,'BRANCH_ACK_WAIT_S',.3)
    result=launcher.stop_child(child,graceful=True,primary=str(primary))
    record=result['branch_direct_stop_v1012']
    assert record['primary_stalled'] is False and record['classification']=='PRIMARY_STOP_TIMEOUT_ROUTE_UNACKED'
    assert record['finished_ts']-record['requested_ts']>=.25 and log[-1]=='terminate_tree'


# ------------------------------------------------------------- RUN-03/04 ---
def test_win314_preserve_captures_every_rotated_flight_recorder_file(tmp_path):
    import sqlite_preserve,system_health
    src=tmp_path/'data';src.mkdir()
    names=['clean_x_system_events.jsonl']+['clean_x_system_events.jsonl.'+str(i) for i in range(1,system_health.BACKUPS+1)]
    for n in names:(src/n).write_text('{"kind":"T"}\n')
    _,manifest=sqlite_preserve.preserve(src,tmp_path/'out',primary_name='clean_x.db')
    assert {f['name'] for f in manifest['files']}>=set(names)


def test_win314_flight_recorder_retention_covers_planned_run(tmp_path,monkeypatch):
    import system_health as sh
    observed_bytes_per_s=2099358/1289  # user run: jsonl.2 held 2,099,358 B over 1289 s
    assert (sh.BACKUPS+1)*sh.MAX_LOG_BYTES>=2.5*3600*observed_bytes_per_s
    monkeypatch.setattr(sh,'MAX_LOG_BYTES',200)
    primary=tmp_path/'clean_r.db'
    for i in range(200):assert sh.event(primary,'TEST',n=i,pad='x'*50)
    files=sorted(tmp_path.glob('clean_r_system_events.jsonl*'))
    assert len(files)==sh.BACKUPS+1
    sh.export(primary,tmp_path)
    with zipfile.ZipFile(tmp_path/'system_flight_recorder.zip') as z:
        assert {f.name for f in files}<=set(z.namelist())


# ------------------------------------------------------------------ RUN-08 ---
def test_win314_abandoned_preentry_labelled_after_stall(tmp_path):
    import run_review_v100,system_health
    primary=tmp_path/'clean_p.db'
    with closing(sqlite3.connect(primary)) as c:
        c.execute('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT)')
        c.execute('CREATE TABLE preentry_v0729(status TEXT,started_ts REAL,finished_ts REAL,result_json TEXT)')
        start=time.time()-5000
        c.executemany('INSERT INTO meta VALUES(?,?)',[('runtime_started_ts_v41',str(start)),('runtime_updated_ts_v41',str(start+100)),('runtime_state_v41','RUNNING')])
        c.executemany('INSERT INTO preentry_v0729 VALUES(?,?,?,?)',[('RUNNING',start+90,None,None),('USED',start+10,start+12,None)])
        c.commit()
    system_health.event(primary,'STALL_CONTROLLED_STOP')
    with closing(run_review_v100.connect(primary)) as c:
        r=run_review_v100.unfinished_after_stop(c,primary)
    assert r['classification']=='ABANDONED_ON_STALL' and r['count']==1 and r['statuses']=={'RUNNING':1}
    with closing(sqlite3.connect(primary)) as c:  # stored status untouched (read-only report)
        assert c.execute("SELECT COUNT(*) FROM preentry_v0729 WHERE status='RUNNING'").fetchone()[0]==1
    report=run_review_v100.export(primary)
    assert report['preentry']['unfinished_after_stop_v1012']['classification']=='ABANDONED_ON_STALL'


# ------------------------------------------------------------------- ACC-1 ---
def test_win314_cost_quantile_nearest_rank_on_small_samples():
    import cost_evidence as ce
    assert ce.quantile([1,2,30],.9)==30            # was 2 (the median)
    assert ce.quantile([-0.85,1,5,6.5],.9)==6.5    # was the 75th percentile
    assert ce.quantile([1,2,3,4],.5)==2 and ce.quantile([5],.9)==5 and ce.quantile([],.9) is None
    assert ce.quantile(list(range(1,11)),.9)==9 and ce.quantile(list(range(1,101)),.95)==95
    values=[0.5,7.0,1.2]
    assert ce.quantile(values,.9)>=sum(values)/len(values)


# ------------------------------------------------------------------ TERM-1 ---
def test_win314_frozen_route_monitor_running_claim_shown_as_no_heartbeat(fresh):
    import monitor_service,campaign_view
    monitor_service.publish(dict(status='RUNNING',pid=16440,heartbeat_ts=time.time()-2176),config.DB)
    live=campaign_view.overview(config.DB)['primary']['live_v87']
    assert live['monitor_status']=='NO_HEARTBEAT' and live['monitor_status_raw_v1012']=='RUNNING'
    monitor_service.publish(dict(status='RUNNING',pid=16440,heartbeat_ts=time.time()),config.DB)
    assert campaign_view.overview(config.DB)['primary']['live_v87']['monitor_status']=='RUNNING'


# ---------------------------------------------------------------- TERM-2..5 ---
def _text(name):return (DASH/name).read_text(encoding='utf-8')


def test_win314_dashboard_rounds_cpu_offset_and_heartbeat_age():
    page,details,terminal=_text('page.html'),_text('details.html'),_text('terminal.html')
    assert "fix1(res87.total_cpu_pct)" in page and "(res87.total_cpu_pct??'—')" not in page
    assert "fix1(sr.total_cpu_pct)" in details and "Math.round(service.heartbeat_age_s)" in details
    assert "ref.offset_median_ms.toFixed(1)" in terminal and "cpu.toFixed(1)" in terminal
    assert "ref.offset_median_ms+' ± '" not in terminal


def test_win314_frozen_reports_label_snapshot_time():
    page,details,terminal=_text('page.html'),_text('details.html'),_text('terminal.html')
    assert "данные на <span class=m>${hms(A.as_of)}</span> <span class=warn>(заморожено)</span>" in terminal
    assert 'Отчёт заморожен: данные на момент' in page and 'prt.frozen===true' in page
    assert "NO_HEARTBEAT:'нет heartbeat (последний статус RUNNING)'" in page
    assert "на момент последнего heartbeat" in details


def test_win314_details_page_has_no_ukrainian_text():
    details=_text('details.html')
    assert not re.search('[ІіЇїЄєҐґ]',details)
    assert 'Все процессы' in details and 'Задержка цикла' in details and 'Свободный диск' in details



# ============================================================ round 2 (fix_r2) ===
# ------------------------------------------------------------------ CR1-01 ---
# v0.7.101.8: the previous build is now v0.7.101.7 (the user's 03–04.11 desktop PAPER night run).
PREVIOUS_BUILD=dict(version='v0.7.101.7',build_revision='RUN_REVIEW_V1017')


def test_win314_new_build_identity_is_bumped_and_consistent():
    # The user's PASS from 01.11.2026 belongs to v0.7.101.1/AGENT_REVIEW_V1011; the
    # changed code must carry a new identity everywhere the build is identified.
    import release_guard,network_check_v88,recovery_selftest
    manifest=json.loads((APP.parent/'BUILD_MANIFEST.json').read_text(encoding='utf-8'))
    assert config.VERSION==release_guard.VERSION==network_check_v88.VERSION==manifest['version']
    assert recovery_selftest.BUILD_REVISION==manifest['build_revision']
    assert config.VERSION!=PREVIOUS_BUILD['version'] and manifest['build_revision']!=PREVIOUS_BUILD['build_revision']
    for name in ('page.html','details.html'):
        assert '<title>'+config.VERSION+' ' in _text(name)


def test_win314_previous_build_pass_does_not_count_for_new_build(tmp_path,monkeypatch):
    # Exact shape of the user's results/acceptance_v90.json from the previous build.
    import launcher,roadmap_progress
    src=tmp_path/'root'/'results';src.mkdir(parents=True);out=tmp_path/'out';out.mkdir()
    old=dict(version=PREVIOUS_BUILD['version'],passed=True,created_ts=1793535974.63)
    (src/'recovery_check_v89.json').write_text(json.dumps(dict(old,build_revision=PREVIOUS_BUILD['build_revision'])),encoding='utf-8')
    (src/'acceptance_v90.json').write_text(json.dumps(dict(old,native_windows_verified=True)),encoding='utf-8')
    monkeypatch.setattr(roadmap_progress,'acceptance_path',lambda:src/'acceptance_v90.json')
    stages=[dict(id=i,title=str(i)) for i in range(1,5)]
    def native_gate():
        stage=roadmap_progress.build(stages,{},{},{},{})['stages'][0]
        return next(c['passed'] for c in stage['checkpoints'] if c['title']=='Нативные проверки Windows')
    assert roadmap_progress.acceptance()=={} and native_gate() is False
    report=launcher.export_release_checks(out,tmp_path/'root')
    assert report['pair_consistent'] is False and report['passed'] is False and report['native_windows_verified'] is False
    # Control: a PASS written by this build is still recognised (no weakening, no vacuous test).
    _pair(src,1793549999.0)
    assert roadmap_progress.acceptance()['version']==config.VERSION and native_gate() is True
    assert launcher.export_release_checks(out,tmp_path/'root')['native_windows_verified'] is True


# ------------------------------------------------------------------ CR1-03 ---
UKRAINIAN=re.compile('[ІіЇїЄєҐґ]')


def _strings(value):
    if isinstance(value,dict):
        for k,v in value.items():
            yield from _strings(k);yield from _strings(v)
    elif isinstance(value,(list,tuple)):
        for v in value:yield from _strings(v)
    elif isinstance(value,str):yield value


def test_win314_rendered_details_api_has_no_ukrainian_issue_text(fresh,monkeypatch):
    # /details shows system_diagnostics.issues[].detail from strategy_diagnostics.
    import main,strategy_diagnostics,system_health
    from dashboard.server import snap
    for i in range(3):
        assert main.buy('m%d'%i,1)[0];main.sell('m%d'%i,.5,'TEST')
    monkeypatch.setattr(system_health,'read_state',lambda primary:dict(status='RUNNING',heartbeat_ts=time.time()-60,alerts=['CPU_HIGH']))
    result=snap()
    issues=result['system_diagnostics']['issues'];codes={i['code'] for i in issues}
    assert {'SMALL_SAMPLE','RECORDER_HEARTBEAT_INVALID','CPU_HIGH'}<=codes
    assert next(i for i in issues if i['code']=='SMALL_SAMPLE')['detail'].startswith('Малая выборка: 3 закрытых сделок')
    leaked=[s for s in _strings(result) if UKRAINIAN.search(s)]
    assert not leaked,leaked[:5]
    # Every remaining branch of the issue builder, with a synthetic snapshot.
    snapshot=dict(capital=dict(status='ERROR',errors=[1]),positions=[{}],runtime=dict(state='UNKNOWN'),
        monitor=dict(health=dict(technical_errors=1),connection=dict(warning=True,gaps=dict(total_position_seconds=5)),service=dict(status='FAILED'),
                     signal_exit=dict(cohort=dict(matched_closed=dict(delta=-1.0)))))
    report=strategy_diagnostics.summary(fresh,snapshot,config.DB)
    assert {'CAPITAL_RECONCILIATION','MONITOR_ERROR_STREAK','MONITOR_COVERAGE_GAPS','OPEN_POSITIONS_WITHOUT_MONITOR','PRIMARY_HEARTBEAT_UNKNOWN','SIGNAL_EXIT_WORSE'}<={i['code'] for i in report['issues']}
    assert not [s for s in _strings(report) if UKRAINIAN.search(s)]


def test_win314_python_user_texts_have_no_ukrainian_letters():
    # Static guard for all Python modules and dashboard pages. The single allowed hit is
    # the w32tm output key of a Ukrainian Windows locale, parsed (not shown) by time_check.
    allowed={'час останньої успішної синхронізації'}
    hits=[]
    for path in sorted(list(APP.glob('*.py'))+list(DASH.glob('*.py'))+list(DASH.glob('*.html'))):
        text=path.read_text(encoding='utf-8')
        if path.suffix=='.html':
            if UKRAINIAN.search(text):hits.append((path.name,'html'))
            continue
        for single,double in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"",text):
            s=single or double
            if UKRAINIAN.search(s) and s not in allowed:hits.append((path.name,s[:80]))
    assert not hits,hits[:10]


# ------------------------------------------------------------------ CR1-04 ---
# Tests in definition order: the first leaves a faked ctypes.get_last_error cached in
# monitor_file_reader.bindings(), exactly as test_late_spawn_ownership_v85 did through
# fake_last_error; the next two prove the conftest isolation removed it.
def test_win314_native_reader_cache_populated_under_faked_last_error(monkeypatch):
    import ctypes,types,monitor_file_reader
    fake=types.SimpleNamespace(CreateFileW=types.SimpleNamespace(),ReadFile=types.SimpleNamespace(),CloseHandle=types.SimpleNamespace())
    monkeypatch.setattr(ctypes,'WinDLL',lambda *a,**kw:fake,raising=False)
    monkeypatch.setattr(ctypes,'get_last_error',lambda:0,raising=False)
    k,last_error,_=monitor_file_reader.bindings()
    assert k is fake and last_error()==0 and monitor_file_reader.bindings.cache_info().currsize==1


def test_win314_native_reader_cache_does_not_leak_into_next_test(tmp_path):
    import ctypes,monitor_file_reader
    assert monitor_file_reader.bindings.cache_info().currsize==0
    if os.name=='nt':
        k,last_error,_=monitor_file_reader.bindings()
        assert last_error is ctypes.get_last_error
        with pytest.raises(FileNotFoundError) as info:monitor_file_reader.read_windows(tmp_path/'missing_state.json')
        assert info.value.winerror==2


def test_win314_empty_install_monitor_not_started_after_fake_last_error_test(fresh):
    # The originally failing assertion (test_win314_tools), run after the leak above.
    import campaign_view
    live=campaign_view.overview(config.DB)['primary']['live_v87']
    assert live['monitor_status']=='NOT_STARTED' and live['state_read_error']['reason']=='MONITOR_STATE_MISSING'


# ------------------------------------------------------------------ CR1-05 ---
def test_win314_local_rpc_deferral_is_labelled_rpc_deferred_in_labs(tmp_path,monkeypatch):
    import rpc_budget,preentry,auto_simulation,exit_quotes,local_simulation as lab
    def deferred(*a,**kw):raise rpc_budget.Deferred('RATE',0.4,wait_ms=750)
    monkeypatch.setattr(lab,'capture',deferred)
    data=dict(size_units='3',fx_rate='100',mint='m',signal_price='1',buy_min_fraction='0.95',signal_ts=time.time(),max_loss_bps=1500,min_reserve_multiple=1)
    (tmp_path/'pre').mkdir();r=preentry.child(data,tmp_path/'pre')
    assert r['status']=='RPC_DEFERRED' and r['rpc_deferred_v1012']==dict(reason='RATE',retry_after_s=0.4,wait_ms=750)
    monkeypatch.setattr(exit_quotes,'resolve_amount',lambda inv:(6,'1000000'))
    (tmp_path/'sim').mkdir()
    r=auto_simulation.experiment(dict(signal_ts=time.time(),signal_price='1',max_loss_bps=1000,mint='x',inventory={}),tmp_path/'sim')
    assert r['status']=='RPC_DEFERRED' and r['rpc_deferred_v1012']['reason']=='RATE'
    # Other exceptions keep their previous labels.
    def broken(*a,**kw):raise TypeError('x')
    monkeypatch.setattr(lab,'capture',broken)
    (tmp_path/'sim2').mkdir()
    r=auto_simulation.experiment(dict(signal_ts=time.time(),signal_price='1',max_loss_bps=1000,mint='x',inventory={}),tmp_path/'sim2')
    assert r['status']=='REQUEST_OR_LAB_ERROR_TypeError' and 'rpc_deferred_v1012' not in r


def test_win314_rpc_deferred_preflight_keeps_technical_backoff(fresh):
    # Relabelling must not change shadow-preflight behaviour: still a technical failure with backoff.
    import shadow_preflight as sp
    def run(status):
        fresh.execute('UPDATE shadow_preflight_service_v0743 SET failure_streak=0,cooldown_until=0 WHERE id=1')
        j=dict(job_id='j-'+status,mint='m-'+status,created_ts=100.,started_ts=101.,input_json=json.dumps(dict(signal_ts=100.)))
        fresh.execute("INSERT INTO shadow_preflight_v0743(job_id,mint,created_ts,started_ts,input_json,status) VALUES(?,?,?,?,?,'RUNNING')",(j['job_id'],j['mint'],100.,101.,j['input_json']))
        sp.complete(fresh,j,{'status':status},now=102.)
        return tuple(fresh.execute('SELECT failure_streak,cooldown_until>0 FROM shadow_preflight_service_v0743 WHERE id=1').fetchone())
    assert run('RPC_DEFERRED')==run('REQUEST_OR_LAB_ERROR_Deferred')==(1,1)


def test_win314_run_review_reports_exit_signal_lab_coverage(fresh):
    import run_review_v100 as review,auto_simulation,exit_comparison
    fresh.executescript(auto_simulation.SCHEMA+exit_comparison.SCHEMA)
    for i in range(11):
        fresh.execute('INSERT INTO auto_sim_v0727(order_id,mint,signal_ts,status) VALUES(?,?,?,?)',('o%d'%i,'m',1.,'RPC_DEFERRED' if i<10 else 'REQUEST_OR_LAB_ERROR_Deferred'))
    fresh.commit()
    report=review.export(config.DB)['exit_signal_lab_v1012']
    assert report['jobs']==11 and report['rpc_deferred']==11 and report['local_swap_executed']==0
    assert report['classification']=='NO_COVERAGE' and report['coverage']==0
    fresh.execute("INSERT INTO exit_comparison_v0728(order_id,branch,mint,signal_ts,status,technical_status) VALUES('o1','FIRST','m',1.,'ACCEPTED_LOCAL','LOCAL_SWAP_EXECUTED')");fresh.commit()
    report=review.export(config.DB)['exit_signal_lab_v1012']
    assert report['classification']=='PARTIAL' and report['local_swap_executed']==1 and report['accepted_local']==1
