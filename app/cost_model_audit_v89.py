"""Describe existing cost bases; never recalculate a fill or common-wallet PNL."""
def audit(review):
    totals=review.get('totals') or {};rows={}
    for role in ('primary','amm','route'):
        t=totals.get(role) or {}
        rows[role]=dict(observed_net=t.get('net'),closed=t.get('closed'),accounted=t.get('accounted'),setup_coverage=t.get('setup_coverage','UNKNOWN'),token_setup=t.get('token_setup'),volume_setup=t.get('volume_setup'),local_fee=t.get('local_fee'),balance_residual=t.get('balance_residual'),setup_excluded_sensitivity_net=t.get('setup_excluded_sensitivity_net'),cost_basis='ISOLATED_SELL_WITH_ACCOUNT_SETUP' if role=='route' else 'TICK_FEE_MODEL_WITHOUT_ACCOUNT_SETUP')
    return dict(schema='COST_MODEL_AUDIT_V89',read_only=True,rows=rows,common_wallet_costs_proven=False,common_cost_net=None,note='Observed PNL keeps all recorded costs. Primary/AMM tick fees and ROUTE isolated sell account setup are different bases. Setup-excluded sensitivity is not realized profit, a rent refund or a persistent-wallet replay.')
