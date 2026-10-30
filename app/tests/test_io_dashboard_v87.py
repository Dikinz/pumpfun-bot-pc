import errno,json,os,time,threading,sqlite3,zipfile
from pathlib import Path
from types import SimpleNamespace as S
import pytest
import state_io as io,late_spawn as ls,config,main,paper_capital as cap,system_health as sh,monitor_service as ms,campaign_view as view
from test_primary_integrity import fresh
from dashboard.cache import LatestJSON

def denied():
    exc=PermissionError(errno.EACCES,'controlled target held');exc.winerror=5;return exc

def evidence(kind,**values):
    p=os.getenv('IO_CHECK_EVIDENCE_V87')
    if p:
        with open(p,'a') as f:f.write(json.dumps(dict(kind=kind,**values))+'\n')


def test_io87_unchanged_empty_registry_does_not_rewrite_or_die(tmp_path,monkeypatch):
    reg=ls.Registry(tmp_path/'paper.db','s');reg.save(force=True)
    before=ls.path(reg.primary).read_bytes();original=Path.replace
    def blocked(p,target):
        if Path(target)==ls.path(reg.primary):raise denied()
        return original(p,target)
    monkeypatch.setattr(Path,'replace',blocked)
    for _ in range(200):reg.last_save=0;assert reg.poll()==[]
    assert reg.writes_v87==1 and reg.skipped_writes_v87==200 and not reg.pending
    assert ls.path(reg.primary).read_bytes()==before
    evidence('UNCHANGED_EMPTY_REGISTRY',writes=1,avoided=200)


def test_io87_registry_session_change_and_missing_file_are_published(tmp_path):
    reg=ls.Registry(tmp_path/'paper.db','s');reg.save(force=True)
    reg.session='next';reg.last_save=0;reg.save()
    assert json.loads(ls.path(reg.primary).read_text())['session']=='next'
    ls.path(reg.primary).unlink();reg.last_save=0;reg.save()
    assert reg.writes_v87==3 and ls.path(reg.primary).exists()


def test_io87_transient_replace_retries_preserve_payload_and_log(tmp_path,monkeypatch):
    target=tmp_path/'state.json';target.write_text('{"old":true}')
    original=Path.replace;calls=[];pauses=[]
    def replace(p,dst):
        if Path(dst)==target:
            calls.append(p)
            if len(calls)<3:raise denied()
        return original(p,dst)
    monkeypatch.setattr(Path,'replace',replace);monkeypatch.setattr(io,'_pause',pauses.append)
    result=io.atomic_json(target,dict(pending=True,session='s'))
    assert result['attempts']==3 and result['recovered']
    assert json.loads(target.read_text())==dict(pending=True,session='s')
    assert len(set(calls))==1 and pauses==list(io.DELAYS[:2]) and not list(tmp_path.glob('*.tmp'))
    row=json.loads(io.log_path(target).read_text());assert row['status']=='RECOVERED' and row['failures'][0]['winerror']==5
    evidence('TRANSIENT_REPLACE',attempts=3,winerror=5)


@pytest.mark.parametrize('code',[5,32,33])
def test_io87_persistent_target_denial_keeps_previous_state_and_raises(tmp_path,monkeypatch,code):
    target=tmp_path/'state.json';target.write_bytes(b'{"pending":true}')
    original=Path.replace
    def replace(p,dst):
        if Path(dst)==target:
            exc=denied();exc.winerror=code;raise exc
        return original(p,dst)
    monkeypatch.setattr(Path,'replace',replace);monkeypatch.setattr(io,'_pause',lambda _:None)
    with pytest.raises(PermissionError) as caught:io.atomic_json(target,dict(pending=False))
    assert target.read_bytes()==b'{"pending":true}'
    row=caught.value.state_write_v87
    assert row['stage']=='REPLACE' and row['attempts']==4 and row['winerror']==code
    assert json.loads(io.log_path(target).read_text())['status']=='FAILED'
    assert not list(tmp_path.glob('*.tmp'))


def test_io87_nontransient_disk_error_is_not_retried(tmp_path,monkeypatch):
    target=tmp_path/'state.json';pauses=[]
    monkeypatch.setattr(Path,'replace',lambda *a:(_ for _ in ()).throw(OSError(errno.ENOSPC,'disk full')))
    monkeypatch.setattr(io,'_pause',pauses.append)
    with pytest.raises(OSError) as caught:io.atomic_json(target,dict(pending=True))
    assert caught.value.state_write_v87['attempts']==1 and not pauses and not target.exists()


def test_io87_financial_position_not_mutated_by_failed_state_publication(fresh,monkeypatch):
    assert main.buy('held',1)[0];tid=main.P['held']['trade_id'];cap.record(fresh,tid,'BUY',3,{'pool_fee_rate':.0125})
    before=cap.summary(fresh);target=ms.paths()['state'];original=Path.replace
    def replace(p,dst):
        if Path(dst)==target:raise denied()
        return original(p,dst)
    monkeypatch.setattr(Path,'replace',replace);monkeypatch.setattr(io,'_pause',lambda _:None)
    with pytest.raises(PermissionError):ms.publish(dict(status='RUNNING'))
    after=cap.summary(fresh)
    for k in ('available','reserved','net_cash','open_principal','total_cost'):assert before[k]==after[k]
    assert fresh.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    out=ms.export(config.DB,Path(config.DB).parent/'export')
    with zipfile.ZipFile(out) as z:assert io.log_path(target).name in z.namelist()


def test_io87_failure_registering_owner_does_not_discard_in_memory_owner(tmp_path,monkeypatch):
    reg=ls.Registry(tmp_path/'paper.db','s');reg.save(force=True)
    owner=S(pid=None,created_ts=None,done=threading.Event(),thread=S(is_alive=lambda:True),trace=S(data={'native_calls_v82':[dict(api='CreateProcessW',state='ENTERED',call_id=1)]},event=lambda *a,**k:None))
    monkeypatch.setattr(ls,'write',lambda *a:(_ for _ in ()).throw(denied()))
    with pytest.raises(PermissionError):reg.register(owner,tmp_path,dict(id='job',trade_id='trade'))
    assert reg.pending and 'job' in reg.owners and not reg.history


def test_io87_recorder_uses_time_after_read_and_still_rejects_future_pulse(fresh,monkeypatch):
    now=[100.];monkeypatch.setattr(sh.time,'monotonic',lambda:now[0]);rec=sh.Recorder(config.DB);now[0]=200.
    def read(*a):
        # A newer heartbeat appears while the sampler reads the snapshot.
        now[0]=200.03
        return dict(status='RUNNING',monotonic_ts=200.02,heartbeat_ts=time.time(),monitor_identity_v86=dict(pid=1,created_ts=1.,session='s'))
    monkeypatch.setattr(ms,'read_state',read)
    result=rec.sample({},force=True)
    assert result['monitor_status']=='RUNNING' and result['monitor_supervision_v86']['age_s']==pytest.approx(.01)
    now[0]=210.
    monkeypatch.setattr(ms,'read_state',lambda *a:dict(status='RUNNING',monotonic_ts=211.,heartbeat_ts=time.time(),monitor_identity_v86=dict(pid=1,created_ts=1.,session='s')))
    assert rec.sample({},force=True)['monitor_status']=='MONITOR_HEARTBEAT_INVALID'
    evidence('READ_INTERVAL_RACE',false_alert_prevented=True,future_still_rejected=True)


def test_io87_ui_response_cache_is_single_bounded_and_never_returns_stale_on_failure():
    now=[0.];cache=LatestJSON(ttl=2,clock=lambda:now[0]);calls=[]
    def build():calls.append(1);return {'generated_ts':len(calls),'available':30}
    first=cache.get('db-a',build)
    assert all(cache.get('db-a',build)==first for _ in range(100)) and len(calls)==1
    now[0]=3.;assert cache.get('db-a',build)!=first
    assert json.loads(cache.get('db-b',build))['generated_ts']==3
    now[0]=6.
    with pytest.raises(OSError):cache.get('db-b',lambda:(_ for _ in ()).throw(OSError('unreadable')))
    assert cache.builds==3


def seed_closed(c,n):
    for i in range(n):
        tid='synthetic-'+str(i)
        for side,price,pnl in [('BUY',1.,0.),('SELL',1.1,.3)]:
            c.execute('INSERT INTO trades(trade_id,size,ts,mint,side,price,pnl,reason,hold) VALUES(?,?,?,?,?,?,?,?,?)',(tid,3,1000+i,tid,side,price,pnl,'TEST',1))
            cap.record(c,tid,side,3 if side=='BUY' else 3.3,{'pool_fee_rate':.01})
    c.execute("UPDATE meta SET v=? WHERE k='cash'",(str(30+.3*n),))


def test_io87_long_history_accounting_and_front_page_stay_exact_and_bounded(fresh):
    import clean_campaign
    clean_campaign.ensure(fresh,config.DB);seed_closed(fresh,600)
    queries=[];fresh.set_trace_callback(queries.append)
    r=view.branch(fresh,config.DB,'primary',time.time())
    fresh.set_trace_callback(None)
    assert r['closed']==600 and r['entries']==600 and len(r['trades'])==12
    assert r['live_v87']['active_checks'] is None and r['live_v87']['ownership_pending'] is None
    assert r['costs']==pytest.approx(.08875*600) and r['net']==pytest.approx(.21125*600)
    assert r['capital']['status']=='OK' and cap.summary(fresh)['closed_net']==pytest.approx(r['net'])
    assert len(queries)<80,len(queries)
    fresh.execute("DELETE FROM capital_ledger_v0721 WHERE trade_id='synthetic-599' AND side='SELL'")
    r=cap.summary(fresh);assert r['status']=='ERROR' and 'Ledger/trade coverage mismatch' in r['errors']


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows target-denial and release required')
def test_io87_native_windows_control_state_replace_recovers_after_release(tmp_path,monkeypatch):
    import ctypes as C,monitor_file_reader
    target=tmp_path/'state.json';target.write_text('{"pending":true}')
    k,_,_=monitor_file_reader.bindings()
    handle=k.CreateFileW(str(target),0x80000000,1,None,3,0x80,None)
    assert handle not in (None,C.c_void_p(-1).value)
    closed=[False]
    def release(_):
        if not closed[0]:assert k.CloseHandle(handle);closed[0]=True
        else:time.sleep(_)
    monkeypatch.setattr(io,'_pause',release)
    try:result=io.atomic_json(target,dict(pending=False))
    finally:
        if not closed[0]:assert k.CloseHandle(handle)
    assert result['recovered'] and 2<=result['attempts']<=4 and json.loads(target.read_text())['pending'] is False
    row=json.loads(io.log_path(target).read_text());assert row['failures'][0]['winerror'] in (5,32,33)
    evidence('NATIVE_WINDOWS_RENAME_RECOVERED',first_error=row['failures'][0],attempts=result['attempts'])
