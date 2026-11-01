"""Explicit fresh canonical AMM RPC snapshot + isolated sale. No trade DB mutation."""
import asyncio,argparse,json,time,sqlite3,tempfile,zipfile,re
from pathlib import Path
from decimal import Decimal,localcontext
import config,position_monitor as pm,amm_exit_scenario as amm

def evaluate(job,result,started,finished,inventory,fx):
    row=dict(id=job['id'],trade_id=job['trade_id'],mint=job['mint'],started_ts=started,finished_ts=finished,status=result.get('status'))
    with sqlite3.connect(':memory:') as c:
        c.row_factory=sqlite3.Row;c.execute('CREATE TABLE capital_ledger_v0721(trade_id,side,notional,total_cost)')
        c.execute("INSERT INTO capital_ledger_v0721 VALUES(?,'BUY',3,0)",(job['trade_id'],))
        decision,account=amm.assess(c,row,job,result,job['scheduled_ts'],None,inventory)
    return dict(schema='TARGET_AMM_CHECK_V59',created_ts=time.time(),scope='CURRENT_RPC_ISOLATED_SIMULATION',
        passed=decision=='ELIGIBLE',decision=decision,status=result.get('status'),mint=job['mint'],raw_amount=inventory['raw_amount'],
        started_ts=started,finished_ts=finished,snapshot_received_ts=result.get('snapshot_received_ts'),pool=result.get('pool'),
        slot=result.get('slot'),accounting=account,fx=fx,broadcast=False,paper_mutated=False,mainnet_execution_proven=False,
        note='Synthetic token inventory and hypothetical entry notional 3 PAPER, entry cost 0. Only current canonical AMM isolated sell validation; not strategy PNL, branch slot release, historical replay or network landing.')

async def run(mint,raw,decimals):
    import exit_quotes as q,uuid
    from solders.pubkey import Pubkey
    Pubkey.from_string(mint)
    if not 0<int(raw)<2**64 or not 0<=decimals<=18:raise ValueError('INVALID_AMOUNT')
    await q.refresh_fx()
    if q.FX_STATUS!='READY':raise RuntimeError('FX_NOT_READY')
    with localcontext() as ctx:
        ctx.prec=60;units=str(Decimal(raw)/10**decimals)
    inv=dict(trade_id='TARGET_AMM',mint=mint,size_usdc='3',sol_usdc=q.FX['rate'],token_units=units,entry_sol_price='1',raw_amount=str(raw),decimals=decimals)
    scheduled=time.time();job=dict(id=uuid.uuid4().hex,trade_id=inv['trade_id'],mint=mint,route='PUMP_AMM',inventory=inv,scheduled_ts=scheduled,wallet_state=dict(wallet=config.SIMULATION_PUBLIC_KEY,volume=None))
    with tempfile.TemporaryDirectory() as td:
        result=await pm.launch_check(job,Path(td))
        report=evaluate(job,result,scheduled,time.time(),inv,dict(rate=q.FX['rate'],ts=q.FX['ts']))
        evidence=Path(__file__).parent/'data/target_amm_evidence_v59.zip';evidence.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(evidence,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('summary.json',json.dumps(report))
            for name in ('diagnostic.zip','evidence.zip'):
                f=Path(td)/name
                if f.exists():z.write(f,name)
            f=Path(td)/'evidence.zip'
            if f.exists():
                with zipfile.ZipFile(f) as source:
                    data=' '.join(source.read(n).decode(errors='replace') for n in source.namelist() if n.endswith('.json'))
                for digest in set(re.findall(r'"blob_sha256":\s*"([a-f0-9]{64})"',data)):
                    blob=pm.ROOT/'blobs'/(digest+'.zlib')
                    if blob.exists():z.write(blob,'blobs/'+blob.name)
        return report

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--mint');parser.add_argument('--raw-amount',type=int);parser.add_argument('--decimals',type=int)
    args=parser.parse_args()
    target=Path(__file__).parent/'data/target_amm_check_v59.json';target.parent.mkdir(exist_ok=True)
    status=None
    try:
        mint=args.mint or input('Canonical Pump AMM token mint: ').strip()
        raw=args.raw_amount if args.raw_amount is not None else int(input('Synthetic token amount in RAW integer units: '))
        decimals=args.decimals if args.decimals is not None else int(input('Token decimals: '))
    except EOFError:
        status='INPUT_UNAVAILABLE';print('\nAMM check needs interactive input or --mint/--raw-amount/--decimals.',flush=True)
    except ValueError:
        status='INPUT_INVALID';print('\nAMM check input must be whole numbers for amount and decimals.',flush=True)
    (target.parent/'target_amm_evidence_v59.zip').unlink(missing_ok=True)
    # Replace an old PASS even when this attempt fails. Never retain stale readiness.
    if status:report=dict(schema='TARGET_AMM_CHECK_V59',created_ts=time.time(),passed=False,scope='CURRENT_RPC_ISOLATED_SIMULATION',status=status,broadcast=False,paper_mutated=False,mainnet_execution_proven=False)
    else:
        try:report=asyncio.run(run(mint,raw,decimals))
        except Exception as exc:report=dict(schema='TARGET_AMM_CHECK_V59',created_ts=time.time(),passed=False,scope='CURRENT_RPC_ISOLATED_SIMULATION',status='CHECK_FAILED',error_type=type(exc).__name__,broadcast=False,paper_mutated=False,mainnet_execution_proven=False)
    tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(target)
    print(json.dumps(report,ensure_ascii=False));return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
