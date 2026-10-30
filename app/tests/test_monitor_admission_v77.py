"""Monitor startup order and file failure regressions. No network or OS clock edits."""
import asyncio,json,os,sqlite3,threading,time
from pathlib import Path
import pytest
import recovery_gate as rg,monitor_service as ms,position_monitor as pm,config
from test_primary_integrity import fresh
from test_monitor_v31 import open_case,checked
from test_recovery_gate_v75 import Clock

@pytest.fixture(autouse=True)
def isolated_gate(monkeypatch):monkeypatch.setattr(rg,'ACTIVE',None)

def test_reader_installs_before_database_marker_and_state_exist(tmp_path):
    p=tmp_path/'not-yet-created.db'
    with rg.reader(p):
        assert isinstance(rg.ACTIVE,rg.Client)
        assert not p.exists()
        a=rg.admission();assert not a['allowed'] and a['reason']=='RECOVERY_STATE_MISSING'
        old=rg.stamp({});assert old['recovery_authorized_v75'] is False
        g=rg.Coordinator(p,'new-primary')
        assert rg.admission()['allowed'] and not rg.allowed(old)
        new=rg.stamp({});assert new['recovery_session_v77']=='new-primary'
        assert rg.allowed(new) and new['recovery_epoch_v75']==g.data['epoch']
    assert rg.ACTIVE is None

def test_pre_state_monitor_job_cannot_become_authorized_later(fresh):
    tid=open_case(fresh)
    with rg.reader(config.DB),ms.connect() as c:
        job=pm.next_job(c,time.time());assert job['recovery_authorized_v75'] is False
        g=rg.Coordinator(config.DB,'current-run')
        now=time.time();pm.finish(c,job,checked(now),now=now)
        row=c.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
        assert row['status']=='RECOVERY_INVALIDATED'
        assert json.loads(row['result_json'])['recovery_admission_v76']['reason']=='EPOCH_MISMATCH'
        pri=dict(trade_id=tid,order_id='offline-exit',signal_ts=now,detected_ts=now)
        new=pm.next_job(c,time.time(),priority=pri)
        assert new['recovery_session_v77']==g.session and new['recovery_authorized_v75'] is True
        now=time.time();r=checked(now);r['net_status']='NET_MEETS_FLOOR';pm.finish(c,new,r,now=now)
        assert c.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(new['id'],)).fetchone()[0]=='SELLABLE'

def test_actual_monitor_service_starts_before_primary_gate(fresh,monkeypatch):
    open_case(fresh);stop=threading.Event();observed={}
    async def worker(job,connection):
        assert isinstance(rg.ACTIVE,rg.Client)
        observed['old_job']=dict(job);observed['missing']=rg.admission()
        observed['published']=rg.Coordinator(config.DB,'late-primary').data['epoch']
        pm.finish(connection,job,checked(time.time()));stop.set()
    monkeypatch.setattr(pm,'run_job',worker)
    asyncio.run(asyncio.wait_for(ms.run(config.DB,0,stop_event=stop),3))
    assert observed['old_job']['recovery_authorized_v75'] is False
    assert observed['missing']['reason']=='RECOVERY_STATE_MISSING'
    with ms.connect(readonly=True) as c:
        row=c.execute('SELECT * FROM position_checks_v0730').fetchone();assert row['status']=='RECOVERY_INVALIDATED'
        start=c.execute("SELECT details_json FROM monitor_events_v0731 WHERE kind='START'").fetchone()
        assert json.loads(start[0])['recovery_admission_v77']['reason']=='RECOVERY_STATE_MISSING'
    state=ms.read_state();assert state['recovery_required_v77'] is True
    assert state['recovery_admission_v77']['reason']!='GATE_NOT_ACTIVE'
    if os.getenv('MONITOR_ADMISSION_CHECK_EVIDENCE'):
        Path(os.environ['MONITOR_ADMISSION_CHECK_EVIDENCE']).write_text(json.dumps(dict(
            schema='MONITOR_ADMISSION_CHECK_V77',actual_scheduler=True,gate_missing_at_start=True,
            client_installed=True,unknown_job_authorized=False,unknown_job_promoted_after_publish=False,
            unknown_result_status=row['status'],startup_reason=observed['missing']['reason'],
            observed_epoch_after_publish=observed['published'],financial_database='TEMPORARY_TEST_ONLY')),encoding='utf-8')

def test_unknown_read_blocks_sale_result_and_keeps_error_in_journal(fresh):
    open_case(fresh);g=rg.Coordinator(config.DB,'current-primary')
    with rg.reader(config.DB),ms.connect() as c:
        job=pm.next_job(c,time.time());assert job['recovery_authorized_v75'] is True
        rg.path(config.DB).unlink();now=time.time();pm.finish(c,job,checked(now),now=now)
        row=c.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
        a=json.loads(row['result_json'])['recovery_admission_v76']
        assert row['status']=='RECOVERY_INVALIDATED' and a['reason']=='RECOVERY_STATE_MISSING'
        assert a['read_error_v77']['error_type']=='FileNotFoundError'
        assert a['last_valid_state_v77']['epoch']==g.data['epoch']
        assert c.execute('SELECT count(*) FROM positions').fetchone()[0]==1

@pytest.mark.parametrize('failure,reason',[
    (FileNotFoundError(2,'missing'),'RECOVERY_STATE_MISSING'),
    (PermissionError(13,'denied'),'RECOVERY_STATE_PERMISSION_DENIED'),
    (OSError(5,'io'),'RECOVERY_STATE_IO_ERROR'),
])
def test_read_failure_keeps_exact_error_and_last_known_identity(tmp_path,monkeypatch,failure,reason):
    clock=Clock();g=rg.Coordinator(tmp_path/'p.db','primary',clock);client=rg.Client(g.database,clock)
    monkeypatch.setattr(rg,'ACTIVE',client);token=rg.stamp({})
    failure.winerror=5
    actual=Path.read_text
    def fail(p,*args,**kwargs):
        if p==rg.path(g.database):raise failure
        return actual(p,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',fail)
    a=rg.admission(token)
    assert a['allowed'] is False and a['reason']==reason
    assert a['read_error_v77']['error_type']==type(failure).__name__
    assert a['read_error_v77']['errno']==failure.errno and a['read_error_v77']['winerror']==5
    assert a['read_error_v77']['path']==str(rg.path(g.database))
    assert a['epoch'] is None and a['session'] is None
    assert a['last_valid_state_v77']['epoch']==g.data['epoch'] and a['last_valid_state_v77']['session']=='primary'
    monkeypatch.setattr(Path,'read_text',actual)
    assert rg.allowed(token) # a new successful read, not a cached authorization

@pytest.mark.parametrize('raw,reason',[
    ('{"schema":','RECOVERY_STATE_INVALID_JSON'),
    ('[]','RECOVERY_STATE_INVALID_SCHEMA'),
    ('null','RECOVERY_STATE_INVALID_SCHEMA'),
    ('{"schema":"wrong"}','RECOVERY_STATE_INVALID_SCHEMA'),
])
def test_invalid_json_or_schema_is_not_a_stale_pulse(tmp_path,monkeypatch,raw,reason):
    g=rg.Coordinator(tmp_path/'p.db','primary');client=rg.Client(g.database);monkeypatch.setattr(rg,'ACTIVE',client)
    rg.path(g.database).write_text(raw)
    a=rg.admission();assert not a['allowed'] and a['reason']==reason
    assert a['last_valid_state_v77']['session']=='primary'
    if reason=='RECOVERY_STATE_INVALID_JSON':assert a['read_error_v77']['line']==1

@pytest.mark.parametrize('field,value', [('epoch',None),('session',''),('state','UNKNOWN'),('pulse_mono',None),('pulse_mono',float('nan')),('pulse_mono',True)])
def test_invalid_required_field_fails_closed(tmp_path,monkeypatch,field,value):
    g=rg.Coordinator(tmp_path/'p.db','primary');s=dict(g.data);s[field]=value
    rg.path(g.database).write_text(json.dumps(s))
    monkeypatch.setattr(rg,'ACTIVE',rg.Client(g.database));a=rg.admission()
    assert a['reason']=='RECOVERY_STATE_INVALID_FIELDS' and not a['allowed']
    assert a['read_error_v77']['field']==field

def test_invalid_encoding_fails_closed(tmp_path):
    g=rg.Coordinator(tmp_path/'p.db','primary');rg.path(g.database).write_bytes(b'\xff')
    assert rg.Client(g.database).status()['reason']=='RECOVERY_STATE_INVALID_ENCODING'

def test_real_stale_pulse_keeps_session_epoch_and_measured_age(tmp_path):
    root=Clock();local=Clock();g=rg.Coordinator(tmp_path/'p.db','primary',root);client=rg.Client(g.database,local)
    for _ in range(4):local.step(1);s=client.status()
    assert s['reason']=='PRIMARY_GATE_PULSE_STALE' and s['pulse_age_s_v77']==4
    assert s['session']=='primary' and s['epoch']==g.data['epoch']
    assert s['observed_mono_v77']-s['pulse_mono_v77']==4
    assert not s.get('read_error_v77')

def test_future_pulse_has_separate_reason(tmp_path):
    root=Clock();local=Clock();g=rg.Coordinator(tmp_path/'p.db','primary',root);root.step(.5);g.save()
    assert rg.Client(g.database,local).status()['reason']=='PRIMARY_GATE_PULSE_IN_FUTURE'

def test_job_requires_current_session_as_well_as_epoch(tmp_path,monkeypatch):
    g=rg.Coordinator(tmp_path/'p.db','primary');monkeypatch.setattr(rg,'ACTIVE',g)
    job=rg.stamp({});job['recovery_session_v77']='another-session'
    assert rg.admission(job)['reason']=='SESSION_MISMATCH' and not rg.allowed(job)
