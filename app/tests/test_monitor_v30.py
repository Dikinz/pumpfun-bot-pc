import json,time,copy,sqlite3
from pathlib import Path
import pytest
import main,database as db,position_monitor as pm,wallet_costs as wc,local_simulation as lab,paper_capital as cap
from test_primary_integrity import fresh

@pytest.fixture
def bundle():
    b=json.loads(Path('research/local_snapshot_v26.json').read_text());b['minimum']='1';return b

def checked(now,**changes):
    r=dict(status='CHECKED',scheduled_ts=now-2,snapshot_received_ts=now-1,reserves={'real_sol_lamports':100,'status':'RESERVES_COVER_GROSS'},sale={'status':'LOCAL_SWAP_EXECUTED'},zero_token_control_ok=True,net_status='NET_MEETS_FLOOR')
    r.update(changes);return r

def test_timer_checks_without_feed_and_preserves_first_detection(fresh):
    assert main.buy('mintA',1)[0]
    before=cap.summary(fresh);now=time.time();job=pm.next_job(fresh,now)
    assert job and pm.next_job(fresh,now+1) is None
    pm.finish(fresh,job,checked(now),now=now)
    job=pm.next_job(fresh,now+11)
    pm.finish(fresh,job,checked(now+11,net_status='NET_BELOW_FLOOR'),now=now+11)
    job2=pm.next_job(fresh,now+22)
    pm.finish(fresh,job2,checked(now+22,net_status='NET_BELOW_FLOOR'),now=now+22)
    h=dict(fresh.execute('SELECT * FROM position_health_v0730').fetchone())
    assert h['first_problem_ts']==now+11 and h['last_good_ts']==now
    assert cap.summary(fresh)==before
    assert fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==1

def test_fairness_stale_results_and_restart(fresh):
    main.buy('mintA',1);main.buy('mintB',1)
    now=time.time();a=pm.next_job(fresh,now);b=pm.next_job(fresh,now)
    assert a['trade_id']!=b['trade_id']
    pm.finish(fresh,a,checked(now,snapshot_received_ts=now-6),now=now)
    assert fresh.execute('SELECT first_problem_ts FROM position_health_v0730').fetchone()[0] is None
    pm.recover(fresh)
    assert fresh.execute('SELECT status FROM position_checks_v0730 WHERE id=?',(b['id'],)).fetchone()[0]=='INTERRUPTED_RESTART'
    pm.finish(fresh,b,checked(now),now=now)
    assert fresh.execute('SELECT COUNT(*) FROM position_health_v0730').fetchone()[0]==1
    assert pm.classify(checked(now,reserves={'status':'INSUFFICIENT_REAL_RESERVES'},zero_token_control_ok=False),now)=='INSUFFICIENT_RESERVES'
    assert pm.classify(checked(now,zero_token_control_ok=False),now)=='NEGATIVE_CONTROL_FAILED'

def test_same_wallet_reuses_real_volume_bytes_and_charges_once(fresh,bundle):
    wallet=bundle['wallet'];fresh.execute("INSERT INTO wallet_state_v0730 VALUES('wallet',?)",(wallet,))
    r=lab.run_snapshot(bundle,volume_scenario='WALLET');r['zero_token_control_ok']=True
    assert r['status']=='LOCAL_SWAP_EXECUTED'
    wc.record(fresh,'a',bundle['mint'],r)
    saved=wc.state(fresh)
    second=lab.run_snapshot(bundle,volume_scenario='WALLET',persisted_volume=saved['volume'])
    assert second['status']=='LOCAL_SWAP_EXECUTED' and second['volume_account_rent_delta_lamports']==0
    assert second['seller_net_sol_delta_lamports']-r['seller_net_sol_delta_lamports']==r['volume_account_rent_delta_lamports']
    control=lab.run_snapshot(bundle,balance_override=0,volume_scenario='WALLET',persisted_volume=saved['volume'])
    assert '6023' in control['err']
    second['zero_token_control_ok']=True
    wc.record(fresh,'b',bundle['mint'],second);wc.record(fresh,'b',bundle['mint'],second)
    report=wc.summary(fresh)
    assert len(report['rows'])==2 and report['token_setup_lamports']==r['synthetic_token_rent_lamports']
    assert report['volume_setup_lamports']==r['volume_account_rent_delta_lamports'] and report['local_fees_lamports']==10000
    previous=wc.state(fresh)
    wc.record(fresh,'bad','other',{'status':'LOCAL_SWAP_FAILED'})
    assert wc.state(fresh)==previous
    assert wc.summary(fresh)['rows'][0]['net_receipt'] is None

def test_evidence_dedup_roundtrip_hash_and_simulation(bundle,tmp_path):
    packed=pm.pack_bundle(bundle,tmp_path)
    assert packed!=bundle
    restored=pm.unpack_bundle(packed,tmp_path)
    assert restored==bundle
    assert lab.run_snapshot(restored)['status']=='LOCAL_SWAP_EXECUTED'
    count=len(list((tmp_path/'blobs').glob('*')))
    pm.pack_bundle(bundle,tmp_path)
    assert len(list((tmp_path/'blobs').glob('*')))==count
    f=next((tmp_path/'blobs').glob('*'));f.write_bytes(b'corrupt')
    with pytest.raises(Exception):pm.unpack_bundle(packed,tmp_path)

def test_compact_api_pending_and_failed_cases_are_included(fresh):
    from dashboard.server import snap
    main.buy('mintA',1)
    wc.ensure(fresh)
    import monitor_service as ms
    monitor=ms.connect()
    now=time.time();job=pm.next_job(monitor,now);pm.finish(monitor,job,{'status':'REQUEST_TIMEOUT'},now=now)
    result=snap();monitor.close()
    assert result['capital']['open_principal']==3 and result['monitor']['counts']['REQUEST_TIMEOUT']==1
    assert len(result['positions'])==1 and result['monitor']['positions'][0]['health']['first_problem_ts'] is None
    assert result['monitor']['positions'][0]['health']['first_error_ts']==now
    json.dumps(result,allow_nan=False)

def test_complete_monitor_experiment_records_replayable_evidence(bundle,tmp_path,monkeypatch):
    import exit_quotes as q,zipfile
    now=time.time();bundle['snapshot'].update(started_ts=now,received_ts=now)
    monkeypatch.setattr(q,'resolve_amount',lambda inv:(6,bundle['amount']))
    monkeypatch.setattr(lab,'capture',lambda *args,**kwargs:bundle)
    monkeypatch.setattr(pm,'ROOT',tmp_path)
    data=dict(scheduled_ts=now,entry_price='0.000000001',mint=bundle['mint'],inventory={},wallet_state={'wallet':bundle['wallet'],'volume':None})
    r=pm.experiment(data,tmp_path)
    assert pm.classify(r,time.time())=='SELLABLE'
    assert not r['broadcast'] and not r['paper_mutated']
    with zipfile.ZipFile(tmp_path/'evidence.zip') as z:
        decoded=pm.unpack_bundle(json.loads(z.read('snapshot.json')),tmp_path)
        assert decoded==bundle
    # Errors are not disguised as lack of reserves.
    import execution_verification as v
    def fail(*a,**kw):raise v.VerificationError('SPECIAL_CURVE_UNSUPPORTED',{'feature':'holder_reward'})
    monkeypatch.setattr(lab,'capture',fail)
    r=pm.experiment(data,tmp_path)
    assert r['status']=='UNSUPPORTED_POOL' and r['unsupported_details']['feature']=='holder_reward'

def test_auto_exit_persists_same_wallet_after_real_vm(prepared,tmp_path,monkeypatch,bundle):
    import auto_simulation as a,exit_quotes as q
    c,j,inv=prepared
    c.execute("INSERT INTO wallet_state_v0730 VALUES('wallet',?)",(bundle['wallet'],))
    now=time.time();bundle['snapshot'].update(started_ts=now,received_ts=now)
    monkeypatch.setattr(q,'resolve_amount',lambda inv:(6,bundle['amount']))
    monkeypatch.setattr(lab,'capture',lambda mint,amount,minimum,**kw:dict(bundle,minimum=str(minimum)))
    r=a.experiment(dict(signal_ts=now-1,signal_price='0.000000001',max_loss_bps=1000,mint=bundle['mint'],inventory={},wallet_state=wc.state(c)),tmp_path)
    before=cap.summary(c)
    a.finish(c,j['order_id'],r)
    assert wc.state(c)['volume'] and len(wc.summary(c)['rows'])==1
    assert cap.summary(c)==before

from test_exit_quotes import prepared
from test_execution_model import env

@pytest.mark.parametrize('mode,expected',[('timeout','REQUEST_TIMEOUT'),('cancel','INTERRUPTED_STOP'),('crash','CHILD_ERROR')])
def test_monitor_child_cleanup(fresh,tmp_path,monkeypatch,mode,expected):
    import asyncio
    main.buy('mintA',1);job=pm.next_job(fresh,time.time());monkeypatch.setattr(pm,'ROOT',tmp_path)
    class Process:
        returncode=None
        killed=False
        async def wait(self):
            if self.killed:return -9
            if mode=='timeout':raise asyncio.TimeoutError()
            if mode=='cancel':raise asyncio.CancelledError()
            self.returncode=1;return 1
        def kill(self):self.killed=True;self.returncode=-9
    proc=Process()
    async def create(*a,**kw):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):asyncio.run(pm.run_job(job))
    else:asyncio.run(pm.run_job(job))
    assert fresh.execute('SELECT status FROM position_checks_v0730').fetchone()[0]==expected
    assert proc.returncode is not None

def test_result_package_has_dashboard_database_and_all_evidence(fresh,tmp_path,monkeypatch):
    import launcher,zipfile
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    assert launcher.results()==0
    target=next((tmp_path/'results').glob('*.zip'))
    with zipfile.ZipFile(target) as z:
        names=z.namelist()
        assert 'dashboard.html' in names and 'summary.json' in names
        assert all(any(n.startswith(prefix) for n in names) for prefix in ['test_data_','auto_simulations_','preentry_','position_monitor_'])
        assert not any('.env' in n for n in names)
        assert 'window.REPORT_DATA=' in z.read('dashboard.html').decode()

def test_reserves_check_actual_lamports_not_only_curve_field(bundle):
    good=pm.reserve_evidence(bundle)
    assert good['status']=='RESERVES_COVER_GROSS'
    low=copy.deepcopy(bundle);low['snapshot']['accounts'][low['account_names']['bonding_curve']]['lamports']=0
    r=pm.reserve_evidence(low)
    assert r['real_sol_lamports']==good['real_sol_lamports'] and r['spendable_lamports']==0
    assert r['status']=='INSUFFICIENT_REAL_RESERVES'
