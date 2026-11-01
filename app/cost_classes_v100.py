"""Cost taxonomy and wallet-lifecycle assumptions, separate from strict PNL."""
def classify(token,volume,fee):
    return [dict(article='token_account_storage',amount=token,classes=['REFUNDABLE'],scope='PER_WALLET_MINT_TOKEN_PROGRAM',condition='Only after empty-account close with correct authority and supported extensions; actual refund not proven'),
      dict(article='volume_accumulator_storage',amount=volume,classes=['ONE_TIME_PER_WALLET'],scope='PER_WALLET_PROGRAM_WHILE_ACCOUNT_RETAINED',condition='Recreation/closure/other program may require funding again'),
      dict(article='local_transaction_fee',amount=fee,classes=['RECURRING'],scope='PER_EXECUTED_LOCAL_TRANSACTION',condition='Local VM fee only; network landing/priority costs not proven')]

def wallet_model(settlements,strict_closed_net=None):
    # Sequence in one synthetic persistent wallet, without treating account
    # creations as free. Retain volume deposit once per program. Token storage
    # refund is an explicit assumed scenario AFTER a complete close, never cash.
    observed=0.;volume_by_program={};token_total=0.;volume_total=0.;eligible=True
    for row in settlements:
        details=__import__('json').loads(row['details']) if isinstance(row.get('details'),str) else row.get('details',{})
        a=details.get('accounting') or {};route=details.get('route','UNKNOWN');fx=a.get('fx')
        try:
            fx=float(fx);token=int(a['token_rent_lamports'])*fx/1e9;volume=int(a['volume_rent_lamports'])*fx/1e9
            if token<0 or volume<0 or fx<=0 or not __import__('math').isfinite(token+volume+fx):raise ValueError()
        except (ValueError,TypeError,KeyError):eligible=False;continue
        token_total+=token;volume_total+=volume
        if route not in volume_by_program:volume_by_program[route]=volume
    retained=sum(volume_by_program.values())
    return dict(schema='PERSISTENT_WALLET_SENSITIVITY_V100',coverage_complete=eligible,token_setup=token_total,volume_setup=volume_total,retained_first_volume_by_program=retained,
        retain_token_accounts_cost_adjustment=volume_total-retained if eligible else None,
        assume_empty_ata_close_cost_adjustment=volume_total-retained+token_total if eligible else None,
        strict_closed_net=strict_closed_net,
        retain_token_accounts_model_net=strict_closed_net+volume_total-retained if eligible and strict_closed_net is not None else None,
        assumed_ata_close_model_net=strict_closed_net+volume_total-retained+token_total if eligible and strict_closed_net is not None else None,
        actual_refunds_proven=False,common_wallet_costs_proven=False,
        assumptions=['Same entry/exit economics and FX as isolated candidates','One volume account retained per wallet/program','Optimistic variant assumes valid empty token-account closure','Extra close transaction/priority/landing costs excluded'],
        note='Adjustments for a modeled line only. Never applied to ledger, collateral or realized PNL; not a validated persistent-wallet replay.')
