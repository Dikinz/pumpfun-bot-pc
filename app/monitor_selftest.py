"""Offline full child-process check on a recorded snapshot. No PAPER database."""
import asyncio,json,time,tempfile,zipfile
from pathlib import Path
import position_monitor as pm

async def check():
    fixture=Path(__file__).parent/'research/monitor_fixture.zip'
    with zipfile.ZipFile(fixture) as z:b=json.loads(z.read('snapshot.json'))
    job=dict(id='SELFTEST',trade_id='SELFTEST',mint=b['mint'],scheduled_ts=time.time(),entry_price='0.00000001',trial_loss_bps=1000,
      wallet_state={'wallet':b['wallet'],'volume':None},inventory={'token_units':'1','raw_amount':str(b['amount']),'decimals':6})
    with tempfile.TemporaryDirectory() as td:
        r=await pm.launch_check(job,Path(td),replay=fixture)
        passed=r.get('status')=='CHECKED' and r.get('zero_token_control_ok') is True and r.get('sale',{}).get('status')=='LOCAL_SWAP_EXECUTED'
        amm_fixture=Path(__file__).parent/'research/amm_fixture_v36.zip'
        with zipfile.ZipFile(amm_fixture) as z:ab=json.loads(z.read('snapshot.json'))
        ajob=dict(id='AMM_SELFTEST',route='PUMP_AMM',mint=ab['mint'],scheduled_ts=time.time(),wallet_state={'wallet':ab['wallet'],'volume':None},inventory={'token_units':'1','raw_amount':ab['amount'],'decimals':6})
        ar=await pm.launch_check(ajob,Path(td)/'amm',replay=amm_fixture)
        amm_passed=ar.get('status')=='AMM_SALE_EXECUTED' and ar.get('zero_token_control_ok') is True
        report=dict(passed=passed and amm_passed,amm=dict(passed=amm_passed,status=ar.get('status'),progress=ar.get('progress'),error=ar.get('child_log_tail')),status=r.get('status'),progress=r.get('progress'),elapsed_s=time.time()-job['scheduled_ts'],
          child_returncode=r.get('child_returncode'),error=r.get('child_log_tail'),note='Offline historical fixture; current RPC and Windows live execution are not proven.')
        target=Path(__file__).parent/'data/monitor_selftest.json';target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False));return report['passed']
if __name__=='__main__':raise SystemExit(0 if asyncio.run(check()) else 1)
