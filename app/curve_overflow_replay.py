"""Pinned historical Curve bank. No RPC, strategy execution, or financial writes."""
import copy,hashlib,json,re,struct,base64,time
from pathlib import Path
FIXTURE=Path(__file__).resolve().parent/'research/curve_overflow_v83'
MODES=('original','quarter_amount','lamports_only','reserve_field_only','both')

def error_number(report):
    # Parse the numeric program error, never infer arithmetic operation from 6024 alone.
    match=re.search(r'InstructionErrorCustom\((\d+)\)',str(report.get('err') or ''))
    return int(match.group(1)) if match else None

def load_fixture(root=FIXTURE):
    import position_monitor as pm
    root=Path(root);source=json.loads((root/'source.json').read_text());raw=(root/'snapshot.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=source['snapshot_sha256']:raise RuntimeError('CURVE_REPLAY_FIXTURE_HASH_MISMATCH')
    bundle=pm.unpack_bundle(json.loads(raw),root)
    if (bundle['amount'],bundle['snapshot']['slot'])!=(source['amount'],source['snapshot_slot']):raise RuntimeError('CURVE_REPLAY_FIXTURE_IDENTITY_MISMATCH')
    return bundle,source

def variant(bundle,mode):
    import local_simulation as lab
    if mode not in MODES:raise ValueError('UNKNOWN_CONTROL')
    b=copy.deepcopy(bundle);account=b['snapshot']['accounts'][b['account_names']['bonding_curve']]
    if mode=='quarter_amount':b['amount']=str(int(b['amount'])//4)
    if mode in ('lamports_only','both'):account['lamports']+=30_000_000
    if mode in ('reserve_field_only','both'):
        data=bytearray(lab.raw(account));reserve=struct.unpack_from('<Q',data,32)[0]
        struct.pack_into('<Q',data,32,reserve+30_000_000);account['data']=[base64.b64encode(data).decode(),'base64']
    return b

def run_matrix(root=FIXTURE):
    from unittest.mock import patch
    import local_simulation as lab,position_monitor as pm
    bundle,source=load_fixture(root);fingerprint=hashlib.sha256(json.dumps(bundle,sort_keys=True).encode()).hexdigest()
    def forbidden(*args,**kwargs):raise RuntimeError('NETWORK_FORBIDDEN_IN_CURVE_REPLAY')
    rows=[]
    with patch('socket.socket',forbidden),patch('socket.create_connection',forbidden),patch.object(lab,'read_accounts',forbidden):
        for mode in MODES:
            b=variant(bundle,mode);start=time.monotonic()
            sale=lab.run_snapshot(b,volume_scenario='WALLET',persisted_volume=source['wallet_state']['volume'])
            rows.append(dict(mode=mode,status=sale['status'],program_error=error_number(sale),raw_error=sale['err'],
                amount=b['amount'],reserves=pm.reserve_evidence(b),program_sha256=sale['program_sha256'],
                compute_units=sale['compute_units'],elapsed_ms=(time.monotonic()-start)*1000,
                seller_net_lamports=sale.get('seller_net_sol_delta_lamports'),pool_sol_delta_lamports=sale.get('pool_sol_delta_lamports'),
                tokens_debited=sale.get('tokens_debited'),pool_tokens_credited=sale.get('pool_tokens_credited'),
                counterfactual=mode!='original',scope='HISTORICAL_DIAGNOSTIC_ONLY',broadcast=sale['broadcast'],paper_mutated=sale['paper_mutated']))
        zero=lab.run_snapshot(bundle,balance_override=0,volume_scenario='WALLET',persisted_volume=source['wallet_state']['volume'])
        repeat=[lab.run_snapshot(bundle,volume_scenario='WALLET',persisted_volume=source['wallet_state']['volume']) for _ in range(2)]
    expected={'original':'LOCAL_SWAP_FAILED','quarter_amount':'LOCAL_SWAP_EXECUTED','lamports_only':'LOCAL_SWAP_FAILED','reserve_field_only':'LOCAL_SWAP_EXECUTED','both':'LOCAL_SWAP_EXECUTED'}
    unchanged=fingerprint==hashlib.sha256(json.dumps(bundle,sort_keys=True).encode()).hexdigest()
    passed=(unchanged and all(r['status']==expected[r['mode']] and r['program_sha256']==source['program_sha256'] and not r['broadcast'] and not r['paper_mutated'] for r in rows)
        and all(r['program_error']==6024 for r in rows if r['mode'] in ('original','lamports_only'))
        and error_number(zero)==6023 and all(error_number(r)==6024 and r['program_sha256']==source['program_sha256'] for r in repeat))
    return dict(schema='CURVE_OVERFLOW_REPLAY_V83',passed=passed,source=source,rows=rows,
        original_repeated_errors=[rows[0]['program_error']]+[error_number(r) for r in repeat],zero_token_control=error_number(zero),
        original_bundle_unchanged=unchanged,network_used=False,paper_mutated=False,live_execution_proven=False,
        inference='For this saved bank, full-size 6024 depends on real reserve: lamports-only funding does not fix it; changing real reserve does. This does not identify the exact Rust operation or all causes of error 6024.',
        limitations=['Synthetic PAPER inventory; no actual BUY is replayed.','Partial and reserve-modified cases are diagnostic controls, never fills.','No partial-exit policy, slot release or trading threshold change.'])

if __name__=='__main__':
    result=run_matrix();target=Path(__file__).resolve().parent.parent/'results/curve_replay_check_v83.json';target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('HISTORICAL CURVE REPLAY: '+('PASS' if result['passed'] else 'FAILED'));print(target)
    raise SystemExit(0 if result['passed'] else 1)
