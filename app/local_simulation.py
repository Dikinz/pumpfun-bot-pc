"""Isolated direct Pump sell experiment. Network is READ ONLY.

LiteSVM owns all synthetic balances. No signing, no sendTransaction, no write to
PAPER DB. This is a current snapshot experiment, not historical execution proof.
"""
import argparse,base64,hashlib,json,sqlite3,struct,time,urllib.request,urllib.error,secrets,zipfile,html
from pathlib import Path
from solders.pubkey import Pubkey
from solders.account import Account
from solders.instruction import Instruction,AccountMeta
from solders.message import Message
from solders.transaction import Transaction
from solders.litesvm import LiteSVM
from solders.transaction_metadata import FailedTransactionMetadata
from solders.clock import Clock
from solders.rent import Rent
import execution_verification as v
import config

SYSTEM='11111111111111111111111111111111'
ATA='ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
FEE='pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ'
UPGRADE='BPFLoaderUpgradeab1e11111111111111111111111'
CLOCK='SysvarC1ock11111111111111111111111111111111'
RENT='SysvarRent111111111111111111111111111111111'
IDL=Path(__file__).parent/'research/pump_idl_v26.json'

class LabError(Exception):pass

def key(x):return Pubkey.from_string(x)
def pda(program,*seeds):return str(Pubkey.find_program_address(list(seeds),key(program))[0])
def ata(owner,mint,program):return pda(ATA,bytes(key(owner)),bytes(key(program)),bytes(key(mint)))
def raw(a):
    if a['data'][1]!='base64':raise LabError('UNSUPPORTED_ACCOUNT_ENCODING')
    return base64.b64decode(a['data'][0],validate=True)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def read_accounts(addresses,min_slot=None,*,data_slice=None,commitment='confirmed'):
    # No configurable RPC method and no external transaction transmission.
    if not 0<len(addresses)<=100:raise LabError('ACCOUNT_BATCH_SIZE')
    opts={'encoding':'base64','commitment':commitment}
    if commitment not in ('confirmed','finalized'):raise LabError('INVALID_COMMITMENT')
    if data_slice is not None:
        if data_slice!=45:raise LabError('INVALID_PROGRAM_METADATA_SLICE')
        opts['dataSlice']={'offset':0,'length':45}
    if min_slot is not None:opts['minContextSlot']=min_slot
    payload={'jsonrpc':'2.0','id':1,'method':'getMultipleAccounts','params':[addresses,opts]}
    url=config.QUOTE_RPC_URL
    if not url.startswith('https://'):raise LabError('RPC_REQUIRES_HTTPS')
    start=time.time()
    req=urllib.request.Request(url,json.dumps(payload).encode(),{'Content-Type':'application/json'})
    import rpc_timing
    with __import__('rpc_budget').guard(url) as permit, rpc_timing.request('getMultipleAccounts',len(addresses)) as trace:
        trace.row['rpc_admission_v89']=permit.telemetry()
        import rpc_transport_v99 as transport
        if getattr(config,'RPC_KEEPALIVE',False) and transport.available(url):
            data=transport.post(url,req.data,trace,24*1024*1024)
        else:
            trace.row['transport_v99']='URLLIB'
            with trace.phase('OPEN_HEADERS'):
                response=urllib.request.build_opener(NoRedirect()).open(req,timeout=6)
            with response as f:
                with trace.phase('READ_BODY'):data=f.read(24*1024*1024+1)
        trace.row['response_bytes']=len(data)
        with trace.phase('DECODE_VALIDATE'):
            if len(data)>24*1024*1024:raise LabError('RPC_RESPONSE_TOO_LARGE')
            d=json.loads(data)
            if d.get('error'):
                code=d['error'].get('code')
                trace.row['rpc_error_code']=code if type(code)==int else None
                raise LabError('RPC_ERROR_'+str(d['error'].get('code','UNKNOWN')))
            r=d['result'];slot=r['context']['slot']
            if type(slot)!=int or slot<0 or len(r['value'])!=len(addresses) or (min_slot is not None and slot<min_slot):raise LabError('INVALID_RPC_SNAPSHOT')
            received=time.time() # Capture before admission bookkeeping; queue/IO cannot freshen a snapshot.

    return dict(slot=slot,started_ts=start,received_ts=received,accounts=dict(zip(addresses,r['value'])))

def addresses(mint,wallet,curve_account,mint_account,global_account):
    curve=v.decode_curve(curve_account)
    mr=raw(mint_account);info=v.decode_mint(mint_account,mr[44],mint)
    if not info['supported']:raise LabError('MINT_EXTENSIONS_UNSUPPORTED')
    glob=raw(global_account);idl=json.loads(IDL.read_text())
    disc=bytes(next(x['discriminator'] for x in idl['accounts'] if x['name']=='Global'))
    if global_account['owner']!=v.PUMP or glob[:8]!=disc or len(glob)<73:raise LabError('INVALID_GLOBAL')
    at=8;fields={}
    def read_type(typ):
        nonlocal at
        if isinstance(typ,dict) and 'array' in typ:
            sub,n=typ['array'];return [read_type(sub) for _ in range(n)]
        size={'pubkey':32,'u64':8,'u8':1,'bool':1}.get(typ)
        if size is None or at+size>len(glob):raise LabError('UNSUPPORTED_GLOBAL_LAYOUT')
        val=glob[at:at+size];at+=size
        if typ=='pubkey':return str(Pubkey.from_bytes(val))
        return int.from_bytes(val,'little')
    for f in next(t for t in idl['types'] if t['name']=='Global')['type']['fields']:
        fields[f['name']]=read_type(f['type'])
    token=mint_account['owner'];curve_key=v.curve_address(mint);sol=v.q.SOL;classic='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
    result=dict(global_=pda(v.PUMP,b'global'),base_mint=mint,quote_mint=sol,
      base_token_program=token,quote_token_program=classic,bonding_curve=curve_key,
      associated_base_bonding_curve=ata(curve_key,mint,token),associated_quote_bonding_curve=ata(curve_key,sol,classic),
      user=wallet,associated_base_user=ata(wallet,mint,token),associated_quote_user=ata(wallet,sol,classic),
      user_volume_accumulator=pda(v.PUMP,b'user_volume_accumulator',bytes(key(wallet))),
      fee_config=pda(FEE,b'fee_config',bytes(key(v.PUMP))),buyback_fee_recipient=fields['buyback_fee_recipients'][0],
      system_program=SYSTEM,event_authority=pda(v.PUMP,b'__event_authority'),program=v.PUMP)
    result['global']=result.pop('global_')
    return result

def program_data_address(account):
    if account is None or not account.get('executable') or account['owner']!=UPGRADE:raise LabError('UNSUPPORTED_PROGRAM_LOADER')
    b=raw(account)
    if len(b)!=36 or struct.unpack_from('<I',b)[0]!=2:raise LabError('INVALID_PROGRAM_ACCOUNT')
    return str(Pubkey.from_bytes(b[4:36]))

def capture(mint,amount,minimum,wallet=None):
    if not 0<amount<2**64 or not 0<minimum<2**64:raise LabError('INVALID_AMOUNT')
    test_key=Pubkey.from_bytes(secrets.token_bytes(32))
    while not test_key.is_on_curve():test_key=Pubkey.from_bytes(secrets.token_bytes(32))
    wallet=wallet or str(test_key);g=pda(v.PUMP,b'global');curve=v.curve_address(mint)
    import rpc_timing
    with rpc_timing.stage_scope('PUMP_DISCOVERY'):
        first=read_accounts([mint,curve,g]);a=first['accounts']
    if any(a[x] is None for x in (mint,curve,g)):raise LabError('MISSING_INITIAL_ACCOUNT')
    names=addresses(mint,wallet,a[curve],a[mint],a[g])
    programs=list(dict.fromkeys([v.PUMP,FEE,names['base_token_program'],names['quote_token_program']]))
    import rpc_timing
    with rpc_timing.stage_scope('PUMP_ACCOUNTS'):
        second=read_accounts(list(dict.fromkeys(list(names.values())+programs+[CLOCK,RENT])),first['slot'])
    progdata={pid:program_data_address(second['accounts'][pid]) for pid in programs}
    # Fresh trading bank plus rooted code verified against later live metadata.
    import rpc_timing
    with rpc_timing.stage_scope('PUMP_PROGRAM_BANK'):
        import program_cache
        final=program_cache.capture_bank(__import__(__name__),list(second['accounts']),progdata,second['slot'])
    a=final['accounts']
    if addresses(mint,wallet,a[curve],a[mint],a[g])!=names:raise LabError('ADDRESS_INPUTS_CHANGED_RETRY')
    for pid,pd in progdata.items():
        if program_data_address(a[pid])!=pd:raise LabError('PROGRAM_CHANGED_RETRY')
        if a[pd] is None or a[pd]['owner']!=UPGRADE:raise LabError('INVALID_PROGRAM_DATA')
        code=raw(a[pd])
        if len(code)<49 or struct.unpack_from('<I',code)[0]!=3:raise LabError('INVALID_PROGRAM_DATA')
        if code[45:49]!=b'\x7fELF':raise LabError('INVALID_PROGRAM_ELF')
        del code
    # Seller must have no mainnet funds/accounts. Local creation is explicit.
    if a[wallet] is not None or a[names['associated_base_user']] is not None:raise LabError('TEST_ADDRESS_ALREADY_EXISTS')
    return dict(schema='local_sell_v1',mint=mint,amount=str(amount),minimum=str(minimum),wallet=wallet,
      account_names=names,program_data=progdata,snapshot=final,preparation_slots=[first['slot'],second['slot']],
      source='current mainnet snapshot, synthetic seller; NOT historical replay',
      instruction='direct Pump sell_v3, not a Jupiter route',created_ts=time.time())

def to_account(a):return Account(a['lamports'],raw(a),key(a['owner']),a.get('executable',False),a.get('rentEpoch',0))

def make_token_account(mint,wallet,amount):
    b=bytearray(165);b[:32]=bytes(key(mint));b[32:64]=bytes(key(wallet));struct.pack_into('<Q',b,64,amount);b[108]=1
    return bytes(b)

def load_vm(bundle):
    names=bundle['account_names'];mint=bundle['mint'];wallet=bundle['wallet']
    a=bundle['snapshot']['accounts']
    if addresses(mint,wallet,a[names['bonding_curve']],a[mint],a[names['global']])!=names:raise LabError('BUNDLE_ADDRESS_MISMATCH')
    if set(bundle['program_data'])!={v.PUMP,FEE,names['base_token_program'],names['quote_token_program']}:raise LabError('PROGRAM_SET_MISMATCH')
    vm=LiteSVM().with_sigverify(False).with_blockhash_check(False)
    # Exact cloned binaries for Pump, fee program and token program; no substitute mock program.
    hashes={}
    for pid,pd in bundle['program_data'].items():
        code=raw(a[pd])
        if program_data_address(a[pid])!=pd or a[pd]['owner']!=UPGRADE or struct.unpack_from('<I',code)[0]!=3:raise LabError('INVALID_PROGRAM_DATA')
        binary=code[45:];del code
        if binary[:4]!=b'\x7fELF':raise LabError('INVALID_PROGRAM_ELF')
        vm.add_program(key(pid),binary);hashes[pid]=hashlib.sha256(binary).hexdigest()
    excluded=set(bundle['program_data'])|set(bundle['program_data'].values())|{SYSTEM,CLOCK,RENT}
    for addr,acc in a.items():
        if acc is not None and addr not in excluded:vm.set_account(key(addr),to_account(acc))
    if a[CLOCK] is None or a[RENT] is None:raise LabError('MISSING_SYSVARS')
    cb=raw(a[CLOCK]);rb=raw(a[RENT])
    vm.set_clock(Clock(*struct.unpack('<QqQQq',cb)))
    rent=Rent(*struct.unpack('<QdB',rb));vm.set_rent(rent)
    return vm,hashes

def run_snapshot(bundle,balance_override=None,volume_scenario="FIRST",persisted_volume=None):
    if volume_scenario not in ("FIRST","REPEAT","WALLET"):raise LabError("INVALID_VOLUME_SCENARIO")
    if bundle.get('schema')!='local_sell_v1':raise LabError('UNSUPPORTED_BUNDLE')
    names=bundle['account_names'];mint=bundle['mint'];wallet=bundle['wallet'];amount=int(bundle['amount']);minimum=int(bundle['minimum'])
    a=bundle['snapshot']['accounts'];slot=bundle['snapshot']['slot']
    if addresses(mint,wallet,a[names['bonding_curve']],a[mint],a[names['global']])!=names:raise LabError('BUNDLE_ADDRESS_MISMATCH')
    expected_programs={v.PUMP,FEE,names['base_token_program'],names['quote_token_program']}
    if set(bundle['program_data'])!=expected_programs:raise LabError('PROGRAM_SET_MISMATCH')
    if not 0<amount<2**64 or not 0<minimum<2**64:raise LabError('INVALID_AMOUNT')
    vm,hashes=load_vm(bundle)
    seller_sol=1_000_000_000;synthetic=amount if balance_override is None else balance_override
    vm.set_account(key(wallet),Account(seller_sol,b'',key(SYSTEM)))
    token_data=make_token_account(mint,wallet,synthetic)
    token_rent=vm.minimum_balance_for_rent_exemption(len(token_data))
    vm.set_account(key(names['associated_base_user']),Account(token_rent,token_data,key(names['base_token_program'])))
    # Both scenarios use the SAME wallet and SAME pool snapshot. REPEAT changes
    # only the existence of the wallet's volume account, initialized by real code.
    if a.get(names['user_volume_accumulator']) is not None:raise LabError('TEST_VOLUME_ACCOUNT_ALREADY_EXISTS')
    setup=None
    if volume_scenario=='REPEAT':
        init=next(x for x in json.loads(IDL.read_text())['instructions'] if x['name']=='init_user_volume_accumulator')
        init_names=dict(names,payer=wallet)
        init_metas=[AccountMeta(key(init_names[x['name']]),x.get('signer',False),x.get('writable',False)) for x in init['accounts']]
        init_ix=Instruction(key(v.PUMP),bytes(init['discriminator']),init_metas)
        init_tx=Transaction.new_unsigned(Message.new_with_blockhash([init_ix],key(wallet),vm.latest_blockhash()))
        created=vm.simulate_transaction(init_tx)
        if isinstance(created,FailedTransactionMetadata):raise LabError('VOLUME_INITIALIZATION_FAILED')
        init_post={str(k):acc for k,acc in created.post_accounts()}
        volume=init_post.get(names['user_volume_accumulator'])
        if volume is None or str(volume.owner)!=v.PUMP:raise LabError('INVALID_INITIALIZED_VOLUME_ACCOUNT')
        deltas={addr:acc.lamports-(vm.get_account(key(addr)).lamports if vm.get_account(key(addr)) else 0) for addr,acc in init_post.items()}
        setup=dict(status='LOCAL_INITIALIZATION_OK',rent_lamports=volume.lamports,
                   local_fee_lamports=-sum(deltas.values()),logs=created.meta().logs(),
                   account_data_base64=base64.b64encode(volume.data).decode(),
                   note='Already funded before this sell; not charged again. Synthetic scenario, no wallet history replay.')
        # Copy ONLY initialized volume account. Pool and seller stay at the same
        # baseline so this is a controlled cost comparison, not a second market trade.
        vm.set_account(key(names['user_volume_accumulator']),volume)
    if volume_scenario=='WALLET' and persisted_volume:
        if persisted_volume['address']!=names['user_volume_accumulator'] or persisted_volume['owner']!=v.PUMP:raise LabError('PERSISTED_VOLUME_MISMATCH')
        vm.set_account(key(names['user_volume_accumulator']),to_account(persisted_volume))
    ins=next(x for x in json.loads(IDL.read_text())['instructions'] if x['name']=='sell_v3')
    metas=[AccountMeta(key(names[x['name']]),x.get('signer',False),x.get('writable',False)) for x in ins['accounts']]
    ix=Instruction(key(v.PUMP),bytes(ins['discriminator'])+struct.pack('<QQ',amount,minimum),metas)
    tx=Transaction.new_unsigned(Message.new_with_blockhash([ix],key(wallet),vm.latest_blockhash()))
    result=vm.simulate_transaction(tx)
    failed=isinstance(result,FailedTransactionMetadata);meta=result.meta()
    report=dict(status='LOCAL_SWAP_FAILED' if failed else 'LOCAL_SWAP_EXECUTED',snapshot_slot=slot,
      mint=mint,raw_amount=str(amount),minimum_lamports=str(minimum),synthetic_token_balance=str(synthetic),
      synthetic_sol_lamports=seller_sol,synthetic_token_rent_lamports=token_rent,
      volume_scenario=volume_scenario,wallet=wallet,volume_setup=setup,
      program_sha256=hashes,logs=meta.logs(),compute_units=meta.compute_units_consumed(),
      err=str(result.err()) if failed else None,network_fee_lamports=None,
      paper_mutated=False,broadcast=False,mainnet_execution_proven=False,
      limitations=['Direct Pump route, not Jupiter route.','Synthetic tokens injected; mint supply not adjusted.',
        'LiteSVM runtime features may differ from mainnet.','No priority fee, landing latency or failed-network-transaction estimate.',
        'Cloned accounts are from current snapshot; not the past PAPER exit.'])
    if not failed:
        post={str(k):acc for k,acc in result.post_accounts()}
        seller=post.get(wallet);token=post.get(names['associated_base_user'])
        if seller is None or token is None:raise LabError('MISSING_POST_BALANCES')
        sold=synthetic-struct.unpack_from('<Q',token.data,64)[0]
        pool_token=names['associated_base_bonding_curve']
        received=struct.unpack_from('<Q',post[pool_token].data,64)[0]-struct.unpack_from('<Q',raw(a[pool_token]),64)[0]
        deltas={addr:acc.lamports-(vm.get_account(key(addr)).lamports if vm.get_account(key(addr)) else 0) for addr,acc in post.items()}
        report['local_fee_from_balance_diff_lamports']=-sum(deltas.values())
        report['lamport_deltas']=deltas
        report['pool_tokens_credited']=str(received)
        volume=post.get(names['user_volume_accumulator'])
        if volume:
            report['post_volume_account']=dict(address=names['user_volume_accumulator'],lamports=volume.lamports,owner=str(volume.owner),data=[base64.b64encode(volume.data).decode(),'base64'],executable=False,rentEpoch=0)
        report['volume_account_rent_delta_lamports']=deltas.get(names['user_volume_accumulator'])
        report.update(tokens_debited=str(sold),seller_net_sol_delta_lamports=seller.lamports-seller_sol,
          pool_sol_delta_lamports=post[names['bonding_curve']].lamports-a[names['bonding_curve']]['lamports'])
        if sold!=amount or received!=amount:report['status']='LOCAL_EFFECTS_MISMATCH'
    return report

def latest_case(db):
    with sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True) as c:
        row=c.execute("SELECT j.mint,q.raw_amount,q.floor_lamports FROM execution_verification_v0724 v JOIN exit_quotes_v0723 q ON q.id=v.quote_id JOIN quote_jobs_v0723 j ON j.order_id=v.order_id WHERE v.reserve_status='RESERVES_COVER_UPPER_BOUND' ORDER BY v.quote_id DESC LIMIT 1").fetchone()
    if row is None:raise LabError('NO_SUPPORTED_EXIT_IN_DB')
    return row[0],int(row[1]),max(1,int(row[2]))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',default=config.DB)
    parser.add_argument('--mint');parser.add_argument('--amount',type=int);parser.add_argument('--minimum',type=int,default=1)
    parser.add_argument('--replay');parser.add_argument('--out',default='simulation_exports')
    args=parser.parse_args();out=Path(args.out)/(time.strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3));out.mkdir(parents=True,exist_ok=False)
    report={'status':'NOT_STARTED','broadcast':False,'paper_mutated':False}
    try:
        if args.replay:bundle=json.loads(Path(args.replay).read_text())
        else:
            case=(args.mint,args.amount,args.minimum) if args.mint else latest_case(args.db)
            print('Read-only snapshot of',case[0],flush=True);bundle=capture(*case)
        (out/'snapshot.json').write_text(json.dumps(bundle))
        report=run_snapshot(bundle)
        # Same snapshot with no seller tokens is a negative control.
        negative=run_snapshot(bundle,balance_override=0)
        report['zero_token_control_status']=negative['status'];report['zero_token_control_error']=negative['err']
        capacity_bundle=dict(bundle,minimum='1')
        capacity=run_snapshot(capacity_bundle)
        capacity['experiment']='Capacity only, minimum=1 lamport. Does not override original price-floor result.'
        capacity['original_minimum_lamports']=bundle['minimum']
        capacity_negative=run_snapshot(capacity_bundle,balance_override=0)
        capacity['zero_token_control_status']=capacity_negative['status']
        if capacity['status']=='LOCAL_SWAP_EXECUTED' and capacity_negative['status']!='LOCAL_SWAP_FAILED':capacity['status']='NEGATIVE_CONTROL_FAILED'
        report['capacity_diagnostic_status']=capacity['status']
        report['capacity_diagnostic_net_sol_delta_lamports']=capacity.get('seller_net_sol_delta_lamports')
        (out/'capacity_result.json').write_text(json.dumps(capacity,indent=2))
        (out/'capacity_zero_token_control.json').write_text(json.dumps(capacity_negative,indent=2))
        if report['status']=='LOCAL_SWAP_EXECUTED' and negative['status']!='LOCAL_SWAP_FAILED':report['status']='NEGATIVE_CONTROL_FAILED'
        (out/'zero_token_control.json').write_text(json.dumps(negative,indent=2))
    except Exception as e:
        # Avoid leaking RPC URL credentials in network exception strings.
        report={'status':str(e) if isinstance(e,(LabError,v.VerificationError)) else 'LAB_ERROR_'+type(e).__name__,
          'broadcast':False,'paper_mutated':False,'mainnet_execution_proven':False}
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    explanation='Локальная симуляция прямой продажи. Основной результат сохраняет исходный ценовой порог. capacity_result — отдельная диагностика с минимумом 1 lamport. Это не подтверждение исполнения в сети и не результат торговой стратегии.'
    (out/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>Локальная симуляция</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}</style><h1>'+html.escape(report['status'])+'</h1><p>'+explanation+'</p><p>Слот: '+str(report.get('snapshot_slot','—'))+'</p><p>Диагностика возможности продажи: '+html.escape(report.get('capacity_diagnostic_status','не выполнена'))+'</p><details><summary>Результат и логи</summary><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre></details>',encoding='utf-8')
    with zipfile.ZipFile(str(out)+'.zip','w',zipfile.ZIP_DEFLATED) as z:
        for f in out.iterdir():z.write(f,f.name)
    print(report['status']);print('Results:',out);print('Send archive:',str(out)+'.zip')
    return 0 if report['status']=='LOCAL_SWAP_EXECUTED' else 1

if __name__=='__main__':raise SystemExit(main())
