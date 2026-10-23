"""Read-only canonical SOL PumpSwap snapshot, isolated LiteSVM sell_v2.
No signing, broadcasting, PAPER mutation, or use of stream-supplied pool addresses.
"""
import json,struct,hashlib,time
from pathlib import Path
import local_simulation as lab
from solders.pubkey import Pubkey
from solders.account import Account
from solders.instruction import Instruction,AccountMeta
from solders.message import Message
from solders.transaction import Transaction
from solders.litesvm import LiteSVM
from solders.transaction_metadata import FailedTransactionMetadata
from solders.clock import Clock
from solders.rent import Rent
IDL_PATH=Path(__file__).parent/'research/pump_amm_idl_v36.json'
IDL=json.loads(IDL_PATH.read_text())
AMM='pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
TOKEN='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
SOL=lab.v.q.SOL
class AmmError(Exception):
    def __init__(self,code,details=None):super().__init__(code);self.details=details or {}

def canonical(mint):
    creator=lab.pda(lab.v.PUMP,b'pool-authority',bytes(lab.key(mint)))
    pool,bump=Pubkey.find_program_address([b'pool',b'\0\0',bytes(lab.key(creator)),bytes(lab.key(mint)),bytes(lab.key(SOL))],lab.key(AMM))
    return str(pool),creator,bump

def decode(account,name):
    if not account or account.get('owner')!=AMM or account.get('executable'):raise AmmError('AMM_INVALID_OWNER')
    raw=lab.raw(account);disc=bytes(next(x['discriminator'] for x in IDL['accounts'] if x['name']==name))
    if raw[:8]!=disc:raise AmmError('AMM_INVALID_DISCRIMINATOR')
    at=8;result={}
    fields=next(x for x in IDL['types'] if x['name']==name)['type']['fields']
    # Official append-only layout: only known trailing fields may be absent.
    optional={'Pool':{'is_mayhem_mode','is_cashback_coin','virtual_quote_reserves','creator_fee_bps','can_edit_creator_fee','is_holder_reward','protocol_fees','creator_fees'},'GlobalConfig':set()}[name]
    def read(typ,allow_missing=False):
        nonlocal at
        if isinstance(typ,dict):return [read(typ['array'][0],allow_missing) for _ in range(typ['array'][1])]
        size={'pubkey':32,'u8':1,'bool':1,'u16':2,'u64':8,'i128':16}[typ]
        if at==len(raw) and allow_missing:return lab.SYSTEM if typ=='pubkey' else 0
        if at+size>len(raw):raise AmmError('AMM_UNSUPPORTED_LAYOUT',dict(account_type=name,data_length=len(raw),offset=at))
        b=raw[at:at+size];at+=size
        if typ=='pubkey':return str(Pubkey.from_bytes(b))
        n=int.from_bytes(b,'little',signed=typ=='i128')
        if typ=='bool' and n not in (0,1):raise AmmError('AMM_INVALID_BOOLEAN')
        return n
    for f in fields:result[f['name']]=read(f['type'],f['name'] in optional)
    if any(raw[at:]):raise AmmError('AMM_UNKNOWN_EXTENSION',dict(account_type=name,data_length=len(raw),parsed=at))
    return result

def token_info(a,mint,owner,program):
    if not a or a.get('owner')!=program or a.get('executable'):raise AmmError('AMM_VAULT_PROGRAM_MISMATCH')
    b=lab.raw(a)
    if len(b)<165 or str(Pubkey.from_bytes(b[:32]))!=mint or str(Pubkey.from_bytes(b[32:64]))!=owner or b[108]!=1:raise AmmError('AMM_VAULT_IDENTITY_MISMATCH')
    if len(b)>165:
        if program==TOKEN or b[165]!=2:raise AmmError('AMM_VAULT_EXTENSIONS_UNSUPPORTED')
        at=166;seen=set()
        while at<len(b) and any(b[at:]):
            if at+4>len(b):raise AmmError('AMM_VAULT_EXTENSIONS_UNSUPPORTED')
            typ,size=struct.unpack_from('<HH',b,at);at+=4
            if typ!=7 or size!=0 or typ in seen:raise AmmError('AMM_VAULT_EXTENSIONS_UNSUPPORTED')
            seen.add(typ)
    amount=struct.unpack_from('<Q',b,64)[0]
    if mint==SOL:
        if struct.unpack_from('<I',b,109)[0]!=1:raise AmmError('AMM_WSOL_NOT_NATIVE')
        rent=struct.unpack_from('<Q',b,113)[0]
        if a['lamports']<rent+amount:raise AmmError('AMM_WSOL_BACKING_MISMATCH')
    return amount

def names_for(mint,wallet,a):
    pool,creator,bump=canonical(mint);pa=a.get(pool)
    if pa is None:raise AmmError('AMM_POOL_NOT_FOUND',dict(expected_pool=pool,base_mint=mint,quote_mint=SOL))
    p=decode(pa,'Pool')
    if (p['index'],p['creator'],p['base_mint'],p['quote_mint'],p['pool_bump'])!=(0,creator,mint,SOL,bump):raise AmmError('AMM_POOL_ADDRESS_MISMATCH',dict(expected_pool=pool,pool=p))
    if p['is_mayhem_mode'] or p['is_cashback_coin'] or p['is_holder_reward'] or p['creator_fee_bps'] or p['can_edit_creator_fee']:raise AmmError('AMM_POOL_FEATURE_UNSUPPORTED',p)
    for mintkey in (mint,SOL):
        ma=a.get(mintkey)
        if not ma:raise AmmError('AMM_MINT_NOT_FOUND')
        info=lab.v.decode_mint(ma,lab.raw(ma)[44],mintkey)
        if not info['supported']:raise AmmError('AMM_MINT_EXTENSIONS_UNSUPPORTED',info)
    token=a[mint]['owner']
    if a[SOL]['owner']!=TOKEN:raise AmmError('AMM_QUOTE_PROGRAM_MISMATCH')
    global_key=lab.pda(AMM,b'global_config');g=decode(a.get(global_key),'GlobalConfig')
    if g['disable_flags'] & 16:raise AmmError('AMM_SELL_DISABLED')
    bv=lab.ata(pool,mint,token);qv=lab.ata(pool,SOL,TOKEN)
    if p['pool_base_token_account']!=bv or p['pool_quote_token_account']!=qv:raise AmmError('AMM_VAULT_ADDRESS_MISMATCH')
    return dict(pool=pool,user=wallet,global_config=global_key,base_mint=mint,quote_mint=SOL,
      user_base_token_account=lab.ata(wallet,mint,token),user_quote_token_account=lab.ata(wallet,SOL,TOKEN),
      pool_base_token_account=bv,pool_quote_token_account=qv,base_token_program=token,quote_token_program=TOKEN,
      system_program=lab.SYSTEM,user_volume_accumulator=lab.pda(AMM,b'user_volume_accumulator',bytes(lab.key(wallet))),
      fee_config=lab.pda(lab.FEE,b'fee_config',bytes(lab.key(AMM))),
      buyback_fee_recipient=lab.ata(g['buyback_fee_recipients'][0],SOL,TOKEN),event_authority=lab.pda(AMM,b'__event_authority'),program=AMM),p,g

def reserves(bundle):
    a=bundle['snapshot']['accounts'];n,p,g=names_for(bundle['mint'],bundle['wallet'],a)
    if n!=bundle['account_names']:raise AmmError('AMM_BUNDLE_ADDRESS_MISMATCH')
    base=token_info(a[n['pool_base_token_account']],bundle['mint'],n['pool'],n['base_token_program'])
    quote=token_info(a[n['pool_quote_token_account']],SOL,n['pool'],TOKEN)
    effective=quote+p['virtual_quote_reserves']
    if effective<0:raise AmmError('AMM_NEGATIVE_EFFECTIVE_RESERVE')
    amount=int(bundle['amount']);gross=(amount*effective+base+amount-1)//(base+amount)
    return dict(pool=n['pool'],base_raw=str(base),quote_raw=str(quote),virtual_quote_raw=str(p['virtual_quote_reserves']),
      effective_quote_raw=str(effective),protocol_fees_raw=str(p['protocol_fees']),creator_fees_raw=str(p['creator_fees']),
      sell_gross_upper_raw=str(gross),status='AMM_RESERVES_AVAILABLE' if base>0 and quote>0 and effective>0 and gross<=quote else 'AMM_INSUFFICIENT_RESERVES')

def capture(mint,amount,wallet,evidence=None):
    if not 0<amount<2**64:raise AmmError('AMM_INVALID_AMOUNT')
    evidence=evidence if evidence is not None else {}
    pool,_,_=canonical(mint);g=lab.pda(AMM,b'global_config')
    import rpc_timing
    with rpc_timing.stage_scope('AMM_DISCOVERY'):
        first=lab.read_accounts([pool,mint,SOL,g]);evidence['discovery_snapshot']=first
    names,p,glob=names_for(mint,wallet,first['accounts'])
    programs=list(dict.fromkeys([AMM,names['base_token_program'],TOKEN]))
    import rpc_timing
    with rpc_timing.stage_scope('AMM_ACCOUNTS'):
        second=lab.read_accounts(list(dict.fromkeys(list(names.values())+programs+[lab.CLOCK,lab.RENT])),first['slot']);evidence['accounts_snapshot']=second
    progdata={pid:lab.program_data_address(second['accounts'][pid]) for pid in programs}
    import rpc_timing
    with rpc_timing.stage_scope('AMM_PROGRAM_BANK'):
        import program_cache
        final=program_cache.capture_bank(lab,list(second['accounts']),progdata,second['slot'])
    bundle=dict(schema='amm_sell_v1',mint=mint,amount=str(amount),minimum='1',wallet=wallet,account_names=names,program_data=progdata,snapshot=final,
      preparation_slots=[first['slot'],second['slot']],idl_sha256=hashlib.sha256(IDL_PATH.read_bytes()).hexdigest())
    evidence.clear();evidence['bundle']=bundle
    if names_for(mint,wallet,final['accounts'])[0]!=names:raise AmmError('AMM_ADDRESS_INPUTS_CHANGED_RETRY')
    a=final['accounts']
    for name in ('user','user_base_token_account','user_quote_token_account','user_volume_accumulator'):
        if a[names[name]] is not None:raise AmmError('AMM_TEST_ACCOUNT_ALREADY_EXISTS')
    if not a.get(names['fee_config']) or a[names['fee_config']]['owner']!=lab.FEE:raise AmmError('AMM_FEE_CONFIG_INVALID')
    if not a.get(names['buyback_fee_recipient']):raise AmmError('AMM_BUYBACK_ACCOUNT_MISSING')
    token_info(a[names['buyback_fee_recipient']],SOL,glob['buyback_fee_recipients'][0],TOKEN)
    reserves(bundle)
    return bundle

def load_vm(bundle):
    reserves(bundle);a=bundle['snapshot']['accounts'];n=bundle['account_names']
    if set(bundle['program_data'])!={AMM,n['base_token_program'],TOKEN}:raise AmmError('AMM_PROGRAM_SET_MISMATCH')
    vm=LiteSVM().with_sigverify(False).with_blockhash_check(False);hashes={}
    for pid,pd in bundle['program_data'].items():
        if lab.program_data_address(a[pid])!=pd or not a[pd] or a[pd]['owner']!=lab.UPGRADE:raise AmmError('AMM_PROGRAM_DATA_INVALID')
        b=lab.raw(a[pd])
        if len(b)<49 or struct.unpack_from('<I',b)[0]!=3 or b[45:49]!=b'\x7fELF':raise AmmError('AMM_PROGRAM_ELF_INVALID')
        vm.add_program(lab.key(pid),b[45:]);hashes[pid]=hashlib.sha256(b[45:]).hexdigest()
    excluded=set(bundle['program_data'])|set(bundle['program_data'].values())|{lab.SYSTEM,lab.CLOCK,lab.RENT}
    for addr,acc in a.items():
        if acc is not None and addr not in excluded:vm.set_account(lab.key(addr),lab.to_account(acc))
    vm.set_clock(Clock(*struct.unpack('<QqQQq',lab.raw(a[lab.CLOCK]))));vm.set_rent(Rent(*struct.unpack('<QdB',lab.raw(a[lab.RENT]))))
    return vm,hashes

def run_snapshot(bundle,zero=False):
    vm,hashes=load_vm(bundle);n=bundle['account_names'];wallet=bundle['wallet'];amount=int(bundle['amount'])
    seed_sol=1_000_000_000;vm.set_account(lab.key(wallet),Account(seed_sol,b'',lab.key(lab.SYSTEM)))
    base=lab.make_token_account(bundle['mint'],wallet,0 if zero else amount);base_rent=vm.minimum_balance_for_rent_exemption(len(base))
    vm.set_account(lab.key(n['user_base_token_account']),Account(base_rent,base,lab.key(n['base_token_program'])))
    quote=bytearray(lab.make_token_account(SOL,wallet,0));quote_rent=vm.minimum_balance_for_rent_exemption(len(quote))
    struct.pack_into('<IQ',quote,109,1,quote_rent)
    vm.set_account(lab.key(n['user_quote_token_account']),Account(quote_rent,bytes(quote),lab.key(TOKEN)))
    ins=next(x for x in IDL['instructions'] if x['name']=='sell_v2')
    ix=Instruction(lab.key(AMM),bytes(ins['discriminator'])+struct.pack('<QQ',amount,1),[AccountMeta(lab.key(n[x['name']]),x.get('signer',False),x.get('writable',False)) for x in ins['accounts']])
    tx=Transaction.new_unsigned(Message.new_with_blockhash([ix],lab.key(wallet),vm.latest_blockhash()))
    result=vm.simulate_transaction(tx);failed=isinstance(result,FailedTransactionMetadata);meta=result.meta()
    out=dict(status='AMM_SIMULATION_FAILED' if failed else 'AMM_SALE_EXECUTED',raw_amount=str(amount),wallet=wallet,
      err=str(result.err()) if failed else None,logs=meta.logs(),compute_units=meta.compute_units_consumed(),program_sha256=hashes,
      base_account_rent_lamports=base_rent,quote_account_rent_lamports=quote_rent,account_scenario='ISOLATED_FIRST_AMM_SELL',
      broadcast=False,paper_mutated=False,mainnet_execution_proven=False)
    if not failed:
        post={str(k):acc for k,acc in result.post_accounts()}
        def amount_at(addr):return struct.unpack_from('<Q',post[addr].data,64)[0]
        debit=(0 if zero else amount)-amount_at(n['user_base_token_account']);credit=amount_at(n['user_quote_token_account'])
        pool_credit=amount_at(n['pool_base_token_account'])-struct.unpack_from('<Q',lab.raw(bundle['snapshot']['accounts'][n['pool_base_token_account']]),64)[0]
        delta=post[wallet].lamports-seed_sol
        deltas={addr:acc.lamports-(vm.get_account(lab.key(addr)).lamports if vm.get_account(lab.key(addr)) else 0) for addr,acc in post.items()}
        out.update(tokens_debited=str(debit),pool_tokens_credited=str(pool_credit),quote_received_lamports=credit,wallet_sol_delta_lamports=delta,
          local_fee_lamports=-sum(deltas.values()),volume_rent_delta_lamports=deltas.get(n['user_volume_accumulator'],0),
          pool_rent_topup_lamports=deltas.get(n['pool'],0),net_after_setup_lamports=credit+delta-base_rent-quote_rent)
        if debit!=amount or pool_credit!=amount or credit<=0:out['status']='AMM_EFFECTS_MISMATCH'
    return out
