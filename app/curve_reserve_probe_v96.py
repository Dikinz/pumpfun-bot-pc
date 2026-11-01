"""Fresh negative-only Curve reserve gate. Never supplies a sale or cached bank."""
import time
import local_simulation as lab

def check(mint,amount,wallet):
    if type(amount)!=int or not 0<amount<2**64:raise lab.LabError('INVALID_AMOUNT')
    curve=lab.v.curve_address(mint);glob=lab.pda(lab.v.PUMP,b'global')
    snapshot=lab.read_accounts([mint,curve,glob,lab.RENT])
    accounts=snapshot['accounts']
    if any(accounts.get(x) is None for x in (mint,curve,glob,lab.RENT)):raise lab.LabError('MISSING_INITIAL_ACCOUNT')
    # Same canonical layout/mint/global/complete/quote checks as full capture.
    names=lab.addresses(mint,wallet,accounts[curve],accounts[mint],accounts[glob])
    evidence=dict(schema='CURVE_RESERVE_PROBE_V96',mint=mint,amount=str(amount),wallet=wallet,
        account_names=names,snapshot=snapshot,program_data={})
    import position_monitor as pm
    reserves=pm.reserve_evidence(evidence)
    decoded=lab.v.decode_curve(accounts[curve])
    # A ceil rounding boundary alone cannot skip the actual program simulation.
    lower=(amount*decoded['virtual_sol'])//(decoded['virtual_token']+amount)
    if min(reserves['real_sol_lamports'],reserves['spendable_lamports'])>=lower:return None
    if not 0<=time.time()-snapshot['received_ts']<=pm.FRESH_SECONDS:raise lab.LabError('STALE_RESERVE_PROBE')
    return dict(status='CURVE_RESERVE_PROBE_INSUFFICIENT',route='PUMP_CURVE',raw_amount=str(amount),
        snapshot_started_ts=snapshot['started_ts'],snapshot_received_ts=snapshot['received_ts'],slot=snapshot['slot'],
        reserves=reserves,reserve_probe_evidence_v96=evidence,simulation_skipped_v96=True,
        broadcast=False,paper_mutated=False,
        note='Fresh negative reserve evidence only; no sale, proceeds, bank reuse or position closure. Recovery requires full fresh capture and simulation.')
