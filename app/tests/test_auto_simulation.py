import asyncio,json,sqlite3,time,zipfile
from pathlib import Path
import pytest
import auto_simulation as a
import exit_quotes as q
from test_exit_quotes import prepared
from test_execution_model import env
from test_primary_integrity import fresh


def test_signal_freezes_inventory_exactly_once_and_no_capital_changes(prepared):
    c,j,inv=prepared
    row=dict(c.execute('select * from auto_sim_v0727').fetchone())
    assert json.loads(row['input_json'])['inventory']==inv
    before=[tuple(r) for r in c.execute('select * from capital_ledger_v0721')]
    c.execute("update quote_inventory_v0723 set token_units='999'")
    a.enqueue(c,j['order_id'],j['trade_id'],j['mint'],1,999)
    assert c.execute('select count(*) from auto_sim_v0727').fetchone()[0]==1
    assert c.execute('select input_json from auto_sim_v0727').fetchone()[0]==row['input_json']
    # Filled PAPER orders must still get their independent simulation.
    c.execute("update paper_orders_v0720 set status='FILLED',fill_ts=? where order_id=?",(j['signal_ts']+1,j['order_id']))
    assert a.claim(c,j['signal_ts']+.1)['order_id']==j['order_id']
    a.finish(c,j['order_id'],{'status':'LOCAL_SWAP_EXECUTED'})
    assert [tuple(r) for r in c.execute('select * from capital_ledger_v0721')]==before


def test_expiry_restart_and_timing(prepared):
    c,j,_=prepared
    assert a.claim(c,j['signal_ts']+61) is None
    assert c.execute('select status from auto_sim_v0727').fetchone()[0]=='QUEUE_EXPIRED'
    c.execute("update auto_sim_v0727 set status='RUNNING'");a.recover(c)
    assert c.execute('select status from auto_sim_v0727').fetchone()[0]=='INTERRUPTED_RESTART'
    assert a.timing(10,11,14,16)['timing_status']=='WITHIN_5S'
    assert a.timing(10,14,16,18)['timing_status']=='LATE_SNAPSHOT'
    assert a.timing(10,9,11,12)['timing_status']=='CLOCK_ANOMALY'


def test_real_vm_automatic_report_and_evidence(tmp_path,monkeypatch):
    import local_simulation as lab
    bundle=json.loads(Path('research/local_snapshot_v26.json').read_text())
    now=time.time();bundle['snapshot'].update(started_ts=now,received_ts=now)
    monkeypatch.setattr(q,'resolve_amount',lambda inv:(6,bundle['amount']))
    monkeypatch.setattr(lab,'capture',lambda mint,amount,minimum:dict(bundle,minimum=str(minimum)))
    report=a.experiment(dict(signal_ts=now-1,signal_price='0.000000001',max_loss_bps=1000,mint=bundle['mint'],inventory={}),tmp_path)
    assert report['status']=='LOCAL_SWAP_EXECUTED'
    assert report['zero_token_control_status']=='LOCAL_SWAP_FAILED'
    assert report['local_fee_from_balance_diff_lamports']==5000
    assert not report['paper_mutated'] and not report['broadcast']
    assert report['network_fee_lamports'] is None
    with zipfile.ZipFile(tmp_path/'evidence.zip') as z:assert 'snapshot.json' in z.namelist()


def test_unsupported_is_not_liquidity_failure(tmp_path,monkeypatch):
    import local_simulation as lab
    import execution_verification as v
    monkeypatch.setattr(q,'resolve_amount',lambda inv:(6,'1000000'))
    def fail(*args):raise v.VerificationError('SPECIAL_CURVE_UNSUPPORTED',{'feature':'mayhem'})
    monkeypatch.setattr(lab,'capture',fail)
    r=a.experiment(dict(signal_ts=time.time(),signal_price='1',max_loss_bps=1000,mint='x',inventory={}),tmp_path)
    assert r['status']=='SPECIAL_CURVE_UNSUPPORTED' and r['unsupported_details']=={'feature':'mayhem'}


@pytest.mark.parametrize('mode,expected',[('timeout','SIMULATION_TIMEOUT'),('cancel','INTERRUPTED_STOP'),('crash','CHILD_ERROR')])
def test_child_lifecycle(prepared,tmp_path,monkeypatch,mode,expected):
    c,j,_=prepared;job=a.claim(c,j['signal_ts']+.1)
    monkeypatch.setattr(a,'ROOT',tmp_path)
    class Process:
        pid=999999999
        returncode=None
        killed=False
        async def wait(self):
            if self.killed:return -9
            if mode=='timeout':raise asyncio.TimeoutError()
            if mode=='cancel':raise asyncio.CancelledError()
            self.returncode=1;return 1
        def kill(self):self.killed=True;self.returncode=-9
    proc=Process()
    async def spawn(*args,**kwargs):return proc
    monkeypatch.setattr(__import__('position_monitor'),'create_simulation_process',spawn)
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):asyncio.run(a.run_job(job))
    else:asyncio.run(a.run_job(job))
    assert c.execute('select status from auto_sim_v0727').fetchone()[0]==expected
    assert proc.killed==(mode!='crash')


def test_export_includes_evidence_and_comparison(prepared,tmp_path,monkeypatch):
    c,j,_=prepared
    monkeypatch.setattr(a,'ROOT',tmp_path/'evidence')
    folder=a.ROOT/j['order_id'];folder.mkdir(parents=True)
    with zipfile.ZipFile(folder/'evidence.zip','w') as z:z.writestr('result.json','{}')
    a.finish(c,j['order_id'],{'status':'LOCAL_SWAP_FAILED'},str(folder/'evidence.zip'))
    dbpath=c.execute('PRAGMA database_list').fetchone()[2]
    target=a.export_evidence(dbpath,tmp_path/'exports')
    with zipfile.ZipFile(target) as z:
        assert j['order_id']+'/evidence.zip' in z.namelist()
        assert json.loads(z.read('summary.json'))['rows'][0]['paper_status']=='PENDING'
        assert json.loads(z.read('missing_evidence.json'))==[]
