import copy,json,time,sqlite3
from pathlib import Path
import pytest
import main,position_monitor as pm,reserve_hint_v98 as hint,recovery_gate,paper_capital
from test_primary_integrity import fresh
from test_reserve_probe_v96 import bank,install

def negative(job):
    return dict(status='CURVE_RESERVE_PROBE_INSUFFICIENT',scheduled_ts=job['scheduled_ts'],
        snapshot_received_ts=job['scheduled_ts'],reserves=dict(status='INSUFFICIENT_REAL_RESERVES',real_sol_lamports=1))

def seed(c,mint='mint'):
    assert main.buy(mint,1)[0]
    now=time.time();job=pm.next_job(c,now)
    assert job and not job['reserve_probe_v96']
    pm.finish(c,job,negative(job),now=now)
    return job

def local_error():
    return dict(status='DATA_REJECTED',network_error=dict(category='LOCAL_RPC_ADMISSION',chain=[dict(type='Deferred',reason='RATE')]))

def fail(c,job,result=None):
    pm.finish(c,job,local_error() if result is None else result,now=job['scheduled_ts']+1)

def test_hint98_local_admission_retains_source_and_does_not_change_capital(fresh):
    source=seed(fresh);before=paper_capital.summary(fresh)
    job=pm.next_job(fresh,source['scheduled_ts']+11)
    assert job['reserve_probe_v96']
    fail(fresh,job)
    following=pm.next_job(fresh,job['scheduled_ts']+11)
    assert following['reserve_probe_v96']
    assert following['reserve_hint_v98']['source_check_id']==source['id']
    assert following['reserve_hint_v98']['retained_after_status']=='DATA_REJECTED'
    assert following['id'] not in (source['id'],job['id'])
    assert paper_capital.summary(fresh)==before and not fresh.execute("SELECT 1 FROM trades WHERE side='SELL'").fetchone()

@pytest.mark.parametrize('status',['WORKER_TIMEOUT','REQUEST_ERROR','SUPERSEDED_BY_EXIT_SIGNAL','INTERRUPTED_RESTART'])
def test_hint98_no_result_keeps_negative_source(fresh,status):
    source=seed(fresh);job=pm.next_job(fresh,source['scheduled_ts']+11)
    fail(fresh,job,dict(status=status))
    following=pm.next_job(fresh,job['scheduled_ts']+11)
    assert following['reserve_probe_v96'] and following['reserve_hint_v98']['source_check_id']==source['id']

def test_hint98_technical_backoff_still_blocks_early_dispatch(fresh):
    source=seed(fresh);now=source['scheduled_ts']+11
    for _ in range(3):
        job=pm.next_job(fresh,now);assert job['reserve_probe_v96']
        fail(fresh,job,dict(status='WORKER_TIMEOUT'));now+=11
    assert pm.next_job(fresh,now) is None
    due=pm.next_due(fresh,source['trade_id'],source['opened_ts'])
    assert due==job['scheduled_ts']+1+30
    after=pm.next_job(fresh,due+.1)
    assert after['reserve_probe_v96'] and pm.CHECK_DEADLINE==10 and pm.FRESH_SECONDS==5

def test_hint98_chain_survives_long_history_without_scanning_results(fresh):
    source=seed(fresh);now=source['scheduled_ts']
    for _ in range(80):
        now+=311;job=pm.next_job(fresh,now)
        assert job['reserve_probe_v96'] and job['reserve_hint_v98']['source_check_id']==source['id']
        fail(fresh,job,dict(status='WORKER_TIMEOUT'))
    statements=[];fresh.set_trace_callback(statements.append)
    try:following=pm.next_job(fresh,now+311)
    finally:fresh.set_trace_callback(None)
    assert following['reserve_probe_v96']
    # The source lookup is one primary-key read, not 80 parsed historical results.
    source_reads=[s for s in statements if 'FROM position_checks_v0730 WHERE id=' in s]
    assert len(source_reads)==1

@pytest.mark.parametrize('field,value',[
    ('recovery_epoch_v75','new_epoch'),('recovery_session_v77','new_session'),
    ('recovery_authorized_v75',False),('mint','other_mint'),('trade_id','other_trade'),
    ('opened_ts',1.),('inventory',{'raw_amount':'999','decimals':6}),
    ('wallet_state',{'wallet':'other_wallet'}),('route','PUMP_AMM')])
def test_hint98_changed_context_cannot_reuse_source(fresh,field,value):
    source=seed(fresh);job=pm.next_job(fresh,source['scheduled_ts']+11);fail(fresh,job)
    row=fresh.execute('SELECT * FROM position_checks_v0730 WHERE id=?',(job['id'],)).fetchone()
    current=copy.deepcopy(job);current[field]=value
    assert hint.select(fresh,current,row,local_error()) is None

@pytest.mark.parametrize('status',['STALE_RESULT','NEGATIVE_CONTROL_FAILED','RECOVERY_INVALIDATED','CLEANUP_FAILED'])
def test_hint98_invalid_or_uncertain_result_breaks_chain(fresh,status):
    source=seed(fresh);job=pm.next_job(fresh,source['scheduled_ts']+11)
    fail(fresh,job,dict(status=status))
    following=pm.next_job(fresh,job['scheduled_ts']+11)
    assert not following['reserve_probe_v96'] and 'reserve_hint_v98' not in following

def test_hint98_full_positive_result_clears_old_hint(fresh):
    source=seed(fresh);job=pm.next_job(fresh,source['scheduled_ts']+11)
    result=dict(status='CHECKED',scheduled_ts=job['scheduled_ts'],snapshot_received_ts=job['scheduled_ts'],
        reserves=dict(status='RESERVES_COVER_GROSS',real_sol_lamports=100),sale=dict(status='LOCAL_SWAP_EXECUTED'),
        zero_token_control_ok=True,net_status='NET_MEETS_FLOOR')
    pm.finish(fresh,job,result,now=job['scheduled_ts'])
    following=pm.next_job(fresh,job['scheduled_ts']+11)
    assert not following['reserve_probe_v96']
    fail(fresh,following)
    assert not pm.next_job(fresh,following['scheduled_ts']+11)['reserve_probe_v96']

@pytest.mark.parametrize('change',['delete','wrong_identity','stale_source','denied_source'])
def test_hint98_missing_or_unvalidated_source_falls_back_to_full(fresh,change):
    source=seed(fresh);job=pm.next_job(fresh,source['scheduled_ts']+11);fail(fresh,job)
    if change=='delete':fresh.execute('DELETE FROM position_checks_v0730 WHERE id=?',(source['id'],))
    elif change=='wrong_identity':
        job['reserve_hint_v98']['identity_sha256']='0'*64
        fresh.execute('UPDATE position_checks_v0730 SET input_json=? WHERE id=?',(json.dumps(job),job['id']))
    else:
        row=fresh.execute('SELECT result_json FROM position_checks_v0730 WHERE id=?',(source['id'],)).fetchone();result=json.loads(row[0])
        if change=='stale_source':result['snapshot_received_ts']-=6
        else:result['recovery_admission_v76']['allowed']=False
        fresh.execute('UPDATE position_checks_v0730 SET result_json=? WHERE id=?',(json.dumps(result),source['id']))
    assert not pm.next_job(fresh,job['scheduled_ts']+11)['reserve_probe_v96']

def test_hint98_monitor_reopen_retains_reference_without_process_cache(fresh):
    source=seed(fresh);job=pm.next_job(fresh,source['scheduled_ts']+11);fail(fresh,job)
    path=fresh.execute('PRAGMA database_list').fetchone()[2]
    c=sqlite3.connect(path,isolation_level=None);c.row_factory=sqlite3.Row
    try:
        following=pm.next_job(c,job['scheduled_ts']+11)
        assert following['reserve_probe_v96'] and following['reserve_hint_v98']['source_check_id']==source['id']
    finally:c.close()

@pytest.mark.parametrize('field,value',[('epoch','next_epoch'),('session','next_session'),('state','RECOVERING')])
def test_hint98_scheduler_stamps_current_gate_before_selecting_hint(fresh,monkeypatch,field,value):
    class Gate:
        data=dict(state='READY',epoch='first_epoch',session='first_session',generation=0)
        def status(self):return self.data.copy()
    gate=Gate();monkeypatch.setattr(recovery_gate,'ACTIVE',gate)
    source=seed(fresh);job=pm.next_job(fresh,source['scheduled_ts']+11)
    assert job['reserve_probe_v96'];fail(fresh,job)
    gate.data[field]=value
    following=pm.next_job(fresh,job['scheduled_ts']+11)
    assert not following['reserve_probe_v96']
    assert 'reserve_hint_v98' not in following

def test_hint98_migration_dispatches_amm_without_curve_hint(fresh):
    source=seed(fresh);job=pm.next_job(fresh,source['scheduled_ts']+11)
    assert job['reserve_probe_v96']
    fail(fresh,job,dict(status='POOL_COMPLETED'))
    following=pm.next_job(fresh,job['scheduled_ts']+3)
    assert following['route']=='PUMP_AMM' and not following['reserve_probe_v96']

def test_hint98_retained_hint_performs_one_new_rpc_and_never_uses_old_bank(fresh,bank,monkeypatch,tmp_path):
    import local_simulation as lab,exit_quotes as q
    source=seed(fresh,bank['mint']);job=pm.next_job(fresh,source['scheduled_ts']+11);fail(fresh,job)
    following=pm.next_job(fresh,job['scheduled_ts']+11)
    bank['snapshot'].update(started_ts=following['scheduled_ts']+.1,received_ts=following['scheduled_ts']+.2)
    monkeypatch.setattr(time,'time',lambda:following['scheduled_ts']+.25)
    calls=install(bank,monkeypatch)
    monkeypatch.setattr(q,'resolve_amount',lambda inv:(6,bank['amount']))
    monkeypatch.setattr(lab,'capture',lambda *a,**k:pytest.fail('unnecessary full bank'))
    monkeypatch.setattr(lab,'run_snapshot',lambda *a,**k:pytest.fail('negative probe ran VM'))
    before=paper_capital.summary(fresh)
    result=pm.experiment(following,tmp_path)
    assert result['status']=='CURVE_RESERVE_PROBE_INSUFFICIENT' and len(calls)==1
    assert pm.classify(result,time.time())=='INSUFFICIENT_RESERVES'
    assert 'sale' not in result and not result['paper_mutated'] and not result['broadcast']
    assert paper_capital.summary(fresh)==before
