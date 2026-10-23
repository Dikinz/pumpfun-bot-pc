"""Read-only local BUY -> commit post-accounts -> SELL experiment.
Only SOL and empty token-account rent are synthetic. Tokens come from BUY.
"""
import struct,time,json,re
import local_simulation as lab
from solders.account import Account
from solders.instruction import Instruction,AccountMeta
from solders.message import Message
from solders.transaction import Transaction
from solders.transaction_metadata import FailedTransactionMetadata


def reserve_evidence(bundle,amount=None):
    names=bundle['account_names'];a=bundle['snapshot']['accounts'];curve=lab.v.decode_curve(a[names['bonding_curve']])
    amount=int(bundle['amount'] if amount is None else amount)
    gross=amount*curve['virtual_sol']//(curve['virtual_token']+amount)
    return dict(real_sol_lamports=curve['real_sol'],virtual_sol_lamports=curve['virtual_sol'],
                sell_gross_upper_lamports=gross,raw_amount=str(amount),
                status='INSUFFICIENT_REAL_RESERVES' if curve['real_sol']<gross else 'RESERVES_COVER_GROSS')


def failure_classification(report,bundle=None):
    err=str(report.get('err',''));evidence=None
    if bundle:
        try:evidence=reserve_evidence(bundle,report.get('raw_amount'))
        except Exception:pass
    if report.get('status')=='LOCAL_SWAP_EXECUTED':category='EXECUTED'
    elif '6003' in err:category='PRICE_REJECTED'
    elif '6023' in err:category='INSUFFICIENT_SELLER_TOKENS'
    elif '6024' in err:category='PROGRAM_OVERFLOW_WITH_LOW_RESERVES' if evidence and evidence['status']=='INSUFFICIENT_REAL_RESERVES' else 'PROGRAM_OVERFLOW'
    else:category=report.get('status','UNCLASSIFIED')
    return dict(category=category,reserves=evidence,raw_error=report.get('err'),
                note='Reserve evidence is independent of VM error. Overflow operation is not inferred from code 6024 alone.')


def run_roundtrip(bundle,budget_lamports,min_tokens_out,max_loss_bps=1500,min_reserve_multiple=1):
    if not 0<budget_lamports<2**63 or not 0<min_tokens_out<2**64 or not 0<=max_loss_bps<10000:raise lab.LabError('INVALID_ROUNDTRIP_INPUT')
    vm,hashes=lab.load_vm(bundle);n=bundle['account_names'];wallet=bundle['wallet'];mint=bundle['mint']
    before_curve=lab.v.decode_curve(bundle['snapshot']['accounts'][n['bonding_curve']])
    report=dict(status='NOT_RUN',mint=mint,wallet=wallet,snapshot_slot=bundle['snapshot']['slot'],
      budget_lamports=budget_lamports,min_tokens_out=str(min_tokens_out),max_loss_bps=max_loss_bps,
      pre_real_sol_lamports=before_curve['real_sol'],required_pre_real_sol_lamports=budget_lamports*min_reserve_multiple,
      reserve_gate='PASS' if before_curve['real_sol']>=budget_lamports*min_reserve_multiple else 'INSUFFICIENT_PREENTRY_RESERVES',
      program_sha256=hashes,paper_mutated=False,broadcast=False,network_execution_proven=False,
      network_costs_known=False,inventory_model='TOKENS_FROM_LOCAL_BUY',
      limitations=['Immediate roundtrip, no intervening external trades; not a future exit guarantee.',
       'Local SOL funding only; network priority fees and landing not modeled.',
       'PAPER entries still use tick prices; roundtrip is a pre-entry gate, not replacement accounting.'])
    # Always run experiment even if the PRE-buy reserve rule rejects it; a BUY
    # can itself fund an empty pool. Gate and roundtrip result remain distinct.
    initial=budget_lamports+1_000_000_000
    vm.set_account(lab.key(wallet),Account(initial,b'',lab.key(lab.SYSTEM)))
    tok=lab.make_token_account(mint,wallet,0);token_rent=vm.minimum_balance_for_rent_exemption(len(tok))
    vm.set_account(lab.key(n['associated_base_user']),Account(token_rent,tok,lab.key(n['base_token_program'])))
    report['token_account_setup_rent_lamports']=token_rent
    idl=json.loads(lab.IDL.read_text())
    def invoke(name,data):
        ins=next(x for x in idl['instructions'] if x['name']==name)
        metas=[AccountMeta(lab.key(n[x['name']]),x.get('signer',False),x.get('writable',False)) for x in ins['accounts']]
        tx=Transaction.new_unsigned(Message.new_with_blockhash([Instruction(lab.key(lab.v.PUMP),bytes(ins['discriminator'])+data,metas)],lab.key(wallet),vm.latest_blockhash()))
        result=vm.simulate_transaction(tx);failed=isinstance(result,FailedTransactionMetadata)
        details=dict(status='FAILED' if failed else 'EXECUTED',err=str(result.err()) if failed else None,
                     logs=result.meta().logs(),compute_units=result.meta().compute_units_consumed())
        if failed:
            match=re.search(r'Custom\((\d+)\)',details['err'])
            code=int(match[1]) if match else None
            known=next((e for e in idl.get('errors',[]) if e['code']==code),{})
            details.update(error_code=code,error_name=known.get('name'),error_message=known.get('msg'))
        return result,details
    buy,br=invoke('buy_exact_quote_in_v3',struct.pack('<QQB',budget_lamports,min_tokens_out,0))
    report['buy']=br
    if br['status']!='EXECUTED':report['status']='BUY_SIMULATION_FAILED';return report
    post={str(k):acc for k,acc in buy.post_accounts()}
    pool=n['associated_base_bonding_curve'];user=n['associated_base_user']
    acquired=struct.unpack_from('<Q',post[user].data,64)[0]
    pool_before=struct.unpack_from('<Q',lab.raw(bundle['snapshot']['accounts'][pool]),64)[0]
    pool_after=struct.unpack_from('<Q',post[pool].data,64)[0]
    if acquired<=0 or pool_before-pool_after!=acquired:raise lab.LabError('BUY_TOKEN_CONSERVATION_FAILED')
    buy_deltas={addr:acc.lamports-(vm.get_account(lab.key(addr)).lamports if vm.get_account(lab.key(addr)) else 0) for addr,acc in post.items()}
    report.update(acquired_tokens_raw=str(acquired),buy_wallet_debit_lamports=initial-post[wallet].lamports,
                  buy_local_fee_lamports=-sum(buy_deltas.values()),buy_volume_rent_lamports=buy_deltas.get(n['user_volume_accumulator'],0))
    # Carry EVERY actual BUY post-account forward, including pool reserves,
    # fee accumulators, wallet balances and the tokens just purchased.
    for addr,acc in post.items():vm.set_account(lab.key(addr),acc)
    curve_after=vm.get_account(lab.key(n['bonding_curve']))
    report['post_buy_real_sol_lamports']=struct.unpack_from('<Q',curve_after.data,32)[0]
    buy_wallet=vm.get_account(lab.key(wallet)).lamports
    sell,sr=invoke('sell_v3',struct.pack('<QQ',acquired,1))
    report['sell']=sr;report['sell_minimum_lamports']=1
    if sr['status']!='EXECUTED':report['status']='POST_BUY_SELL_FAILED';return report
    sold={str(k):acc for k,acc in sell.post_accounts()}
    left=struct.unpack_from('<Q',sold[user].data,64)[0]
    returned=struct.unpack_from('<Q',sold[pool].data,64)[0]-pool_after
    if left!=0 or returned!=acquired:raise lab.LabError('SELL_TOKEN_CONSERVATION_FAILED')
    deltas={addr:acc.lamports-(vm.get_account(lab.key(addr)).lamports if vm.get_account(lab.key(addr)) else 0) for addr,acc in sold.items()}
    # Negative control at SAME post-buy state, with token balance zeroed only.
    funded_token=vm.get_account(lab.key(user));zero=bytearray(funded_token.data);struct.pack_into('<Q',zero,64,0)
    vm.set_account(lab.key(user),Account(funded_token.lamports,bytes(zero),funded_token.owner))
    _,control=invoke('sell_v3',struct.pack('<QQ',acquired,1));report['zero_token_control']=control
    if control['status']!='FAILED' or control.get('error_code')!=6023:report['status']='NEGATIVE_CONTROL_FAILED';return report
    wallet_delta=sold[wallet].lamports-initial
    all_in_delta=wallet_delta-token_rent
    maximum_loss=budget_lamports*max_loss_bps//10000
    report.update(status='ROUNDTRIP_EXECUTED',tokens_sold_raw=str(returned),tokens_remaining_raw=str(left),
      sell_net_receipt_lamports=sold[wallet].lamports-buy_wallet,sell_local_fee_lamports=-sum(deltas.values()),
      wallet_roundtrip_delta_lamports=wallet_delta,all_in_roundtrip_delta_lamports=all_in_delta,
      net_gate='PASS' if all_in_delta>=-maximum_loss else 'ROUNDTRIP_LOSS_TOO_HIGH',maximum_loss_lamports=maximum_loss,
      post_sell_real_sol_lamports=struct.unpack_from('<Q',sold[n['bonding_curve']].data,32)[0])
    return report
