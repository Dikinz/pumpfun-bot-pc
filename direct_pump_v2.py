"""Direct SOL-paired ordinary Pump v2 transactions; never a Jupiter swap."""
import json,struct,base64,time,uuid
from pathlib import Path
import local_simulation as lab


def global_fields(account):
    data=lab.raw(account);idl=json.loads(lab.IDL.read_text());at=8
    disc=bytes(next(a['discriminator'] for a in idl['accounts'] if a['name']=='Global'))
    if account['owner']!=lab.v.PUMP or data[:8]!=disc:raise ValueError('INVALID_GLOBAL')
    def read(t):
        nonlocal at
        if isinstance(t,dict):
            sub,n=t['array'];return [read(sub) for _ in range(n)]
        size={'pubkey':32,'u64':8,'u8':1,'bool':1}[t]
        b=data[at:at+size];at+=size
        if len(b)!=size:raise ValueError('SHORT_GLOBAL')
        return str(lab.Pubkey.from_bytes(b)) if t=='pubkey' else int.from_bytes(b,'little')
    return {f['name']:read(f['type']) for f in next(t for t in idl['types'] if t['name']=='Global')['type']['fields']}


def accounts(mint,wallet,curve,mint_account,glob):
    names=lab.addresses(mint,wallet,curve,mint_account,glob)
    info=lab.v.decode_curve(curve);g=global_fields(glob);pump=lab.v.PUMP;sol=names['quote_mint'];token=names['quote_token_program']
    fee=g['fee_recipient'];creator=lab.pda(pump,b'creator-vault',bytes(lab.key(info['creator'])))
    names.update(associated_token_program=lab.ATA,fee_recipient=fee,
        associated_quote_fee_recipient=lab.ata(fee,sol,token),
        associated_quote_buyback_fee_recipient=lab.ata(names['buyback_fee_recipient'],sol,token),
        creator_vault=creator,associated_creator_vault=lab.ata(creator,sol,token),
        sharing_config=lab.pda(lab.FEE,b'sharing-config',bytes(lab.key(mint))),
        global_volume_accumulator=lab.pda(pump,b'global_volume_accumulator'),
        associated_user_volume_accumulator=lab.ata(names['user_volume_accumulator'],sol,token),fee_program=lab.FEE)
    return names


def build(names,side,amount,limit,blockhash):
    from solders.instruction import Instruction,AccountMeta
    from solders.message import MessageV0
    from solders.hash import Hash
    from solders.compute_budget import set_compute_unit_limit,set_compute_unit_price
    if side not in ('BUY','SELL') or any(type(x)!=int or not 0<x<2**64 for x in (amount,limit)):raise ValueError('INVALID_AMOUNT')
    spec=next(i for i in json.loads(lab.IDL.read_text())['instructions'] if i['name']==('buy_v2' if side=='BUY' else 'sell_v2'))
    data=bytes(spec['discriminator'])+struct.pack('<QQ',amount,limit)+(b'\x00' if side=='BUY' else b'')
    ix=Instruction(lab.key(lab.v.PUMP),data,[AccountMeta(lab.key(names[a['name']]),a.get('signer',False),a.get('writable',False)) for a in spec['accounts']])
    instructions=[set_compute_unit_limit(200000),set_compute_unit_price(0)]
    if side=='BUY':
        instructions.append(Instruction(lab.key(lab.ATA),b'\x01',[
            AccountMeta(lab.key(names['user']),True,True),AccountMeta(lab.key(names['associated_base_user']),False,True),
            AccountMeta(lab.key(names['user']),False,False),AccountMeta(lab.key(names['base_mint']),False,False),
            AccountMeta(lab.key(lab.SYSTEM),False,False),AccountMeta(lab.key(names['base_token_program']),False,False)]))
    instructions.append(ix)
    return MessageV0.try_compile(lab.key(names['user']),instructions,[],Hash.from_string(blockhash))


def prepare(rpc,plan):
    # Full genesis hash is supplied in a local plan, never guessed from a prefix.
    if rpc.call('getGenesisHash',[])!=plan['expected_genesis']:raise ValueError('GENESIS_MISMATCH')
    mint=plan['mint'];wallet=plan['wallet'];curve=lab.v.curve_address(mint);glob=lab.pda(lab.v.PUMP,b'global')
    response=rpc.call('getMultipleAccounts',[[curve,mint,glob],{'encoding':'base64','commitment':'confirmed'}])
    a=response['value'];slot=response['context']['slot']
    if len(a)!=3 or not all(a):raise ValueError('ACCOUNTS_MISSING')
    names=accounts(mint,wallet,*a)
    volume=rpc.call('getAccountInfo',[names['user_volume_accumulator'],{'encoding':'base64','commitment':'confirmed','minContextSlot':slot}])
    if volume['context']['slot']<slot:raise ValueError('STALE_VOLUME_ACCOUNT')
    existing=volume['value'];rent=rpc.call('getMinimumBalanceForRentExemption',[137,{'commitment':'confirmed'}])
    if type(rent)!=int or rent<0:raise ValueError('INVALID_RENT')
    if existing and (existing['owner']!=lab.v.PUMP or len(lab.raw(existing))!=137):raise ValueError('UNSUPPORTED_VOLUME_ACCOUNT')
    topup=max(0,rent-(existing['lamports'] if existing else 0))
    if plan['fee_budget']<topup:raise ValueError('VOLUME_RENT_EXCEEDS_FEE_BUDGET')
    block=rpc.call('getLatestBlockhash',[{'commitment':'confirmed','minContextSlot':slot}])['value']
    message=build(names,plan['side'],plan['amount'],plan['limit'],block['blockhash'])
    return message,block,dict(instruction='buy_v2' if plan['side']=='BUY' else 'sell_v2',slot=slot,volume_rent_lamports=rent,volume_topup_lamports=topup,
        note='Volume rent only; ATA/creator/pool topups also consume fee budget. Signed preflight verifies total debit. Ordinary SOL curves only; unsupported and migrated curves rejected.')


def send(rpc,plan,signer,store,enabled=False,timeout=45):
    import live_execution as live
    if not enabled:raise live.ExecutionError('BROADCAST_DISABLED')
    if not 0<timeout<=120:raise ValueError('CONFIRMATION_TIMEOUT')
    adapter=live.Engine(store,rpc,enabled=True)
    # Daily equity must already be initialized explicitly by the operator.
    message,block,details=prepare(rpc,plan)
    ident='phase2:'+uuid.uuid4().hex
    adapter.reserve_online(ident,plan['wallet'],plan['mint'],plan['side'],plan['limit'] if plan['side']=='BUY' else 0,
        plan['amount'],plan['fee_budget'],plan['risk_limits'])
    signed=signer(message)
    row=adapter.submit(ident,signed,block['lastValidBlockHeight'],mode='rpc',skip_preflight=bool(plan.get('skip_preflight',False)))
    deadline=time.monotonic()+timeout
    while row['state'] in ('SUBMITTED','LANDED','SUBMITTING','UNKNOWN') and time.monotonic()<deadline:
        row=adapter.track(ident)
        if row['state'] in ('SUBMITTED','LANDED','SUBMITTING','UNKNOWN'):time.sleep(.5)
    return dict(id=ident,state=row['state'],signature=row.get('signature'),slot=row.get('slot'),preparation=details)


def main():
    import argparse,getpass,wallet_signer,live_execution as live
    from solders.message import to_bytes_versioned
    from solders.transaction import VersionedTransaction
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['prepare','send','track','begin-day']);ap.add_argument('--plan',required=True);ap.add_argument('--broadcast',action='store_true');ap.add_argument('--intent');args=ap.parse_args()
    plan=json.loads(Path(args.plan).read_text());rpc=live.Rpc(plan['rpc_url'])
    if args.command=='prepare':
        _,_,report=prepare(rpc,plan);print(json.dumps(report));return
    store=live.Store.for_wallet(plan['wallet'])
    if args.command=='begin-day':
        equity=plan['daily_start_equity_lamports']
        if type(equity)!=int or equity<=0:raise ValueError('POSITIVE_DAILY_EQUITY_REQUIRED')
        store.begin_day(time.strftime('%Y-%m-%d',time.gmtime()),equity);print('DAILY_EQUITY_INITIALIZED');return
    if args.command=='track':
        if not args.intent:raise ValueError('INTENT_REQUIRED')
        row=live.Engine(store,rpc).track(args.intent)
        print(json.dumps({k:row.get(k) for k in ('id','state','signature','slot')}));return
    if not args.broadcast:raise ValueError('EXPLICIT_BROADCAST_FLAG_REQUIRED')
    password=getpass.getpass('Vault password: ')
    def sign(message):
        raw=wallet_signer.sign_isolated(base64.b64encode(to_bytes_versioned(message)).decode(),plan['vault_path'],plan['signer_policy_path'],password)
        # sign_isolated returns a base64 signed transaction, same contract as adapter.
        return raw
    print(json.dumps(send(rpc,plan,sign,store,enabled=True)))
if __name__=='__main__':
    try:main()
    except Exception as exc:
        import re
        code=str(exc)
        print('PHASE2_FAILED: '+(code if re.fullmatch('[A-Z0-9_]{1,100}',code) else type(exc).__name__));raise SystemExit(1)
