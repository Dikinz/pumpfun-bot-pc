"""Reserve evidence and unsigned RPC simulation. No transaction broadcasting.

Supported reserve scope: one full-size, direct, standard SOL Pump.fun curve.
Unknown routes/modes/layouts are explicitly unsupported, never silently passed.
"""
import base64,hashlib,json,struct,time
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
import config
import exit_quotes as q

PUMP='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
TOKEN_PROGRAMS={'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}
DISC=bytes([23,183,248,55,96,216,172,96])
RENT_CACHE={}
SCHEMA='''CREATE TABLE IF NOT EXISTS execution_verification_v0724(
 quote_id INTEGER PRIMARY KEY,order_id TEXT,ts REAL,reserve_status TEXT,simulation_status TEXT,details TEXT);
CREATE INDEX IF NOT EXISTS verification_order ON execution_verification_v0724(order_id,quote_id);'''

class VerificationError(Exception):
    def __init__(self,status,details=None):
        super().__init__(status);self.details=details or {}

def rpc(method,params,stage='RESERVES'):
    allowed={'getMultipleAccounts','getMinimumBalanceForRentExemption','getTokenAccountsByOwner','getBalance','simulateTransaction','getFeeForMessage'}
    if method not in allowed:raise VerificationError('RPC_METHOD_FORBIDDEN')
    data=q.request_json(config.QUOTE_RPC_URL,stage,{'jsonrpc':'2.0','id':1,'method':method,'params':params})
    if data.get('error') or 'result' not in data:raise VerificationError('RPC_ERROR')
    return data['result']

def curve_address(mint):
    return str(Pubkey.find_program_address([b'bonding-curve',bytes(Pubkey.from_string(mint))],Pubkey.from_string(PUMP))[0])

def decode_curve(account):
    evidence={'account_snapshot':account}
    def fail(status):raise VerificationError(status,evidence)
    if not isinstance(account,dict) or account.get('owner')!=PUMP or account.get('executable'):fail('WRONG_CURVE_OWNER')
    try:
        if account['data'][1]!='base64':raise ValueError()
        raw=base64.b64decode(account['data'][0],validate=True)
        evidence.update(data_length=len(raw),data_base64=account['data'][0])
        if len(raw)!=166 or raw[:8]!=DISC:fail('UNSUPPORTED_CURVE_LAYOUT')
        vtoken,vquote,rtoken,rquote,supply=struct.unpack_from('<5Q',raw,8)
        flags=dict(complete=raw[48],mayhem=raw[81],cashback=raw[82],creator_fee_editable=raw[123],holder_reward=raw[124],curve_depth=raw[141])
        evidence.update(virtual_token=vtoken,virtual_sol=vquote,real_token=rtoken,real_sol=rquote,
          lamports=q.integer(account['lamports']),creator_fee_held=struct.unpack_from('<Q',raw,125)[0],
          protocol_fee_held=struct.unpack_from('<Q',raw,133)[0],flags=flags,
          creator=str(Pubkey.from_bytes(raw[49:81])),quote_mint=str(Pubkey.from_bytes(raw[83:115])),
          creator_fee_bps=struct.unpack_from('<Q',raw,115)[0])
        if any(raw[n] not in (0,1) for n in (48,81,82,123,124)):raise ValueError()
        reasons=[]
        if raw[83:115]!=bytes(32):reasons.append('NON_SOL_QUOTE')
        if raw[48]:reasons.append('COMPLETE')
        reasons.extend(k.upper() for k in ('mayhem','cashback','holder_reward','curve_depth') if flags[k])
        evidence['unsupported_features']=reasons
        if 'NON_SOL_QUOTE' in reasons:fail('NON_SOL_CURVE')
        if 'COMPLETE' in reasons:fail('COMPLETE_CURVE_UNSUPPORTED')
        if reasons:fail('SPECIAL_CURVE_UNSUPPORTED')
        if not vtoken or not vquote or not supply:raise ValueError()
        return evidence
    except (KeyError,IndexError,ValueError,TypeError,struct.error):fail('INVALID_CURVE_DATA')

def timing_evidence(data,quote,slot,started,received):
    quoted_slot=data.get('contextSlot')
    if type(quoted_slot)!=int or quoted_slot<0:quoted_slot=None
    status='QUOTE_SLOT_UNKNOWN' if quoted_slot is None else 'SAME_REPORTED_SLOT' if quoted_slot==slot else 'DIFFERENT_SLOTS'
    age=started-quote['received_ts']
    return dict(status=status,quote_slot=quoted_slot,reserve_slot=slot,
      slot_delta=None if quoted_slot is None else slot-quoted_slot,
      quote_received_ts=quote['received_ts'],reserve_started_ts=started,reserve_received_ts=received,
      quote_age_at_reserve_ms=age*1000,rpc_elapsed_ms=(received-started)*1000,
      exact_state_match_proven=False,
      note='Even the same reported slot is not proof of the same intra-slot state; minContextSlot is a lower bound.')

def decode_mint(account,decimals,mint=None):
    """Strict Token-2022 mint TLV; only metadata extensions are supported.
    Source: solana-program/token-2022 interface/src/extension/mod.rs.
    Metadata never supplies executable code, prices or routing information.
    """
    if not account or account.get('owner') not in TOKEN_PROGRAMS or account.get('executable'):
        raise VerificationError('INVALID_MINT_OWNER')
    try:
        if account['data'][1]!='base64':raise ValueError()
        raw=base64.b64decode(account['data'][0],validate=True)
        if len(raw)<82 or raw[44]!=decimals or raw[45]!=1:raise VerificationError('MINT_PRECISION_MISMATCH')
        if struct.unpack_from('<I',raw,0)[0] not in (0,1) or struct.unpack_from('<I',raw,46)[0] not in (0,1):raise ValueError()
        ids=[];offset=166
        if len(raw)>82:
            if account['owner']!='TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb':raise ValueError()
            if len(raw)<166 or len(raw)==355 or any(raw[82:165]) or raw[165]!=1:raise ValueError()
            while offset<len(raw):
                if not any(raw[offset:]):break
                if offset+4>len(raw):raise ValueError()
                typ,size=struct.unpack_from('<HH',raw,offset);offset+=4
                if typ==0 or typ in ids or offset+size>len(raw):raise ValueError()
                value=raw[offset:offset+size];offset+=size;ids.append(typ)
                if typ==18 and size!=64:raise ValueError()
                if typ==19:
                    # Borsh TokenMetadata: authority, mint, 3 strings, Vec<(String,String)>.
                    if size<80:raise ValueError()
                    if mint is not None and value[32:64]!=bytes(Pubkey.from_string(mint)):raise ValueError()
                    at=64
                    def string():
                        nonlocal at
                        if at+4>size:raise ValueError()
                        n=struct.unpack_from('<I',value,at)[0];at+=4
                        if at+n>size:raise ValueError()
                        value[at:at+n].decode('utf-8');at+=n
                    for _ in range(3):string()
                    if at+4>size:raise ValueError()
                    n=struct.unpack_from('<I',value,at)[0];at+=4
                    if n>(size-at)//8:raise ValueError()
                    for _ in range(n):string();string()
                    if at!=size:raise ValueError()
        unsupported=[x for x in ids if x not in (18,19)]
        return dict(extension_types=ids,unsupported_extension_types=unsupported,
          extension_names=[{18:'MetadataPointer',19:'TokenMetadata',1:'TransferFeeConfig',14:'TransferHook',9:'NonTransferable'}.get(x,'Unsupported:'+str(x)) for x in ids],
          supported=not unsupported,data_base64=account['data'][0],owner=account['owner'])
    except (KeyError,IndexError,ValueError,TypeError,struct.error):
        raise VerificationError('INVALID_MINT_EXTENSION_DATA') from None

def reserve_check(job,inv,quote):
    started=time.time();mono=time.monotonic();data=quote['response'];route=data.get('routePlan')
    if not isinstance(route,list) or len(route)!=1:raise VerificationError('UNSUPPORTED_ROUTE')
    leg=route[0];swap=leg.get('swapInfo',{})
    if swap.get('label')!='Pump.fun' or swap.get('inputMint')!=job['mint'] or swap.get('outputMint')!=q.SOL:raise VerificationError('UNSUPPORTED_ROUTE')
    amount=q.integer(quote['raw_amount'])
    if q.integer(swap.get('inAmount'))!=amount:raise VerificationError('PARTIAL_ROUTE_UNSUPPORTED')
    address=curve_address(job['mint'])
    route_key=swap.get('ammKey')
    identity='CANONICAL_CURVE' if route_key==address else 'MINT_ALIAS_UNVERIFIED' if route_key==job['mint'] else 'ADDRESS_MISMATCH'
    if identity=='ADDRESS_MISMATCH':
        return dict(status='CURVE_ADDRESS_MISMATCH',curve=address,route_amm_key=route_key,route_identity=identity)
    opts={'encoding':'base64','commitment':'confirmed'}
    if type(data.get('contextSlot'))==int:opts['minContextSlot']=data['contextSlot']
    batch=rpc('getMultipleAccounts',[[address,job['mint']],opts])
    if not batch.get('value') or len(batch['value'])!=2:raise VerificationError('MISSING_ACCOUNTS')
    account,mint_account=batch['value']
    captured=time.time();slot=batch['context']['slot']
    timing=timing_evidence(data,quote,slot,started,captured)
    context=dict(curve=address,route_amm_key=route_key,route_identity=identity,slot=slot,
      started_ts=started,received_ts=captured,timing=timing,account_snapshot=account,mint_snapshot=mint_account)
    if account is None:return dict(status='CURVE_NOT_FOUND',**context)
    try:
        curve=decode_curve(account)
        mint_info=decode_mint(mint_account,inv['decimals'],job['mint'])
    except VerificationError as e:
        return dict(status=str(e),**{**context,**e.details})
    extensions=bool(mint_info['extension_types'])
    unsupported=not mint_info['supported']
    length=curve['data_length']
    if length not in RENT_CACHE:RENT_CACHE[length]=q.integer(rpc('getMinimumBalanceForRentExemption',[length,{'commitment':'confirmed'}]))
    rent=RENT_CACHE[length]
    spendable=max(0,curve['lamports']-rent-curve['creator_fee_held']-curve['protocol_fee_held'])
    # Ceiling is a conservative gross reserve debit before pool/service fees.
    gross=(amount*curve['virtual_sol']+curve['virtual_token']+amount-1)//(curve['virtual_token']+amount)
    quoted=q.integer(quote['out_lamports']);leg_out=q.integer(swap['outAmount'])
    required=max(gross,quoted,leg_out)
    status='MINT_EXTENSIONS_UNSUPPORTED' if unsupported else 'RESERVES_COVER_UPPER_BOUND'
    if min(curve['real_sol'],spendable)<(max(quoted,leg_out) if unsupported else required):status='INSUFFICIENT_REAL_RESERVES'
    elif not unsupported and (quoted>gross+2 or leg_out>gross+2):
        status='PRICE_DIFFERENCE_SAME_REPORTED_SLOT' if timing['status']=='SAME_REPORTED_SLOT' else 'PRICE_DIFFERENCE_TIME_UNVERIFIED'
    if status=='RESERVES_COVER_UPPER_BOUND' and identity!='CANONICAL_CURVE':status='ROUTE_IDENTITY_UNVERIFIED'
    if time.monotonic()-mono>5 or started-quote['received_ts']>5:status='RESERVE_CHECK_TOO_SLOW'
    return dict(status=status,timing=timing,mint_snapshot=mint_account,comparison=dict(route_minus_gross_lamports=str(leg_out-gross),quote_minus_gross_lamports=str(quoted-gross),route_exceeds_gross=leg_out>gross+2),curve=address,slot=slot,started_ts=started,received_ts=captured,
      route_amm_key=route_key,route_identity=identity,mint_evidence=mint_info,
      mint_extensions_present=extensions,rent_lamports=rent,spendable_lamports=str(spendable),required_lamports=str(required),
      gross_curve_upper_lamports=str(gross),quote_out_lamports=str(quoted),quote_route_out_lamports=str(leg_out),**curve)

def simulate(job,inv,reserves,quote):
    wallet=config.SIMULATION_PUBLIC_KEY
    if not wallet:return {'status':'NOT_CONFIGURED','note':'Only a public address is needed; PAPER inventory is not an on-chain balance.'}
    Pubkey.from_string(wallet)
    accounts=rpc('getTokenAccountsByOwner',[wallet,{'mint':job['mint']},{'encoding':'jsonParsed','commitment':'confirmed','minContextSlot':reserves['slot']}],'SIMULATION')
    balance=sum(q.integer(x['account']['data']['parsed']['info']['tokenAmount']['amount']) for x in accounts['value'])
    amount=q.integer(quote['raw_amount'])
    if balance<amount:return {'status':'INSUFFICIENT_WALLET_TOKENS','available_raw':str(balance),'required_raw':str(amount)}
    sol=rpc('getBalance',[wallet,{'commitment':'confirmed','minContextSlot':reserves['slot']}],'SIMULATION')['value']
    if sol<=0:return {'status':'NO_WALLET_SOL'}
    params={'inputMint':job['mint'],'outputMint':q.SOL,'amount':str(amount),'swapMode':'ExactIn','slippageBps':job['slippage_bps'],'taker':wallet}
    data=q.request_json(q.ENDPOINT+'?'+q.urllib.parse.urlencode(params),'BUILD',key=config.JUPITER_API_KEY)
    if not data.get('transaction') or data.get('errorCode'):
        return {'status':'BUILD_FAILED','builder_error_code':data.get('errorCode')}
    q.validate(data,job['mint'],q.SOL,amount)
    if q.integer(data.get('slippageBps'))!=job['slippage_bps']:raise VerificationError('BUILD_SLIPPAGE_MISMATCH')
    # The taker can change routing. Never reuse reserve evidence for a different route.
    def route_keys(x):
        return [(z.get('swapInfo',{}).get('ammKey'),z.get('swapInfo',{}).get('inputMint'),z.get('swapInfo',{}).get('outputMint'),z.get('swapInfo',{}).get('inAmount')) for z in x.get('routePlan',[])]
    if route_keys(data)!=route_keys(quote['response']):
        return {'status':'BUILD_ROUTE_CHANGED','quote':q.sanitize(data)}
    if q.integer(data['otherAmountThreshold'])<q.integer(quote['floor_lamports']):return {'status':'BUILD_PRICE_UNACCEPTABLE'}
    fresh_quote=dict(quote,response=data,out_lamports=data['outAmount'],received_ts=time.time())
    reserves=reserve_check(job,inv,fresh_quote)
    if reserves['status']!='RESERVES_COVER_UPPER_BOUND':return {'status':'BUILD_RESERVE_GATE','reserves':reserves}
    binary=base64.b64decode(data['transaction'],validate=True)
    tx=VersionedTransaction.from_bytes(binary)
    if str(tx.message.account_keys[0])!=wallet:raise VerificationError('UNEXPECTED_FEE_PAYER')
    if not tx.message.instructions:raise VerificationError('EMPTY_TRANSACTION')
    # Never sign; arbitrary provider bytes are submitted only to simulateTransaction.
    result=rpc('simulateTransaction',[data['transaction'],{'encoding':'base64','sigVerify':False,'replaceRecentBlockhash':True,'commitment':'confirmed','minContextSlot':reserves['slot'],'innerInstructions':False}],'SIMULATION')
    val=result['value']
    if 'err' not in val:raise VerificationError('INVALID_SIMULATION_RESPONSE')
    status='SIMULATION_OK' if val['err'] is None else 'SIMULATION_FAILED'
    fee=val.get('fee');fee_source='simulation' if fee is not None else None
    if fee is None:
        try:
            msg=base64.b64encode(to_bytes_versioned(tx.message)).decode()
            fee=rpc('getFeeForMessage',[msg,{'commitment':'confirmed'}],'FEES')['value']
            if fee is not None:fee_source='getFeeForMessage'
        except (q.ProbeError,VerificationError,KeyError):pass
    if fee is not None:fee=q.integer(fee)
    return dict(status=status,slot=result['context']['slot'],err=val['err'],units_consumed=val.get('unitsConsumed'),
      pre_balances=val.get('preBalances'),post_balances=val.get('postBalances'),
      pre_token_balances=val.get('preTokenBalances'),post_token_balances=val.get('postTokenBalances'),
      logs=(val.get('logs') or [])[-30:],network_fee_lamports=fee,fee_source=fee_source,
      transaction_sha256=hashlib.sha256(binary).hexdigest(),builder_cost_fields=q.sanitize(data),
      note='Simulation only, no broadcast. Network fee is not total cost; rent, separate tips and swap fees must not be silently treated as zero or counted twice.')

def verify(job,inv,quote):
    out={'ts':time.time(),'reserves':{'status':'NOT_CHECKED'},'simulation':{'status':'NOT_RUN'}}
    try:
        out['reserves']=reserve_check(job,inv,quote)
        if out['reserves']['status']!='RESERVES_COVER_UPPER_BOUND':out['simulation']={'status':'SKIPPED_RESERVE_GATE'}
        else:out['simulation']=simulate(job,inv,out['reserves'],quote)
    except q.ProbeError as e:
        target='reserves' if out['reserves']['status']=='NOT_CHECKED' else 'simulation'
        out[target]={'status':e.status,'stage':e.stage,'http_status':e.http_status}
    except VerificationError as e:
        target='reserves' if out['reserves']['status']=='NOT_CHECKED' else 'simulation';out[target]={'status':str(e),**e.details}
    except Exception:
        target='reserves' if out['reserves']['status']=='NOT_CHECKED' else 'simulation';out[target]={'status':'INVALID_VERIFICATION_DATA'}
    return out

def save(c,quote_id,order_id,details):
    c.execute('INSERT INTO execution_verification_v0724 VALUES(?,?,?,?,?,?)',(quote_id,order_id,details['ts'],details['reserves']['status'],details['simulation']['status'],json.dumps(details)))

def summary(c):
    counts=[dict(r) for r in c.execute('SELECT reserve_status,simulation_status,COUNT(*) n,COUNT(DISTINCT order_id) exits FROM execution_verification_v0724 GROUP BY reserve_status,simulation_status')]
    recent=[]
    for r in c.execute('SELECT v.*,q.mint FROM execution_verification_v0724 v JOIN quote_jobs_v0723 q USING(order_id) WHERE v.quote_id=(SELECT MAX(quote_id) FROM execution_verification_v0724 WHERE order_id=v.order_id) ORDER BY v.quote_id DESC LIMIT 30'):
        d=dict(r);d['details']=json.loads(d['details']);recent.append(d)
    return {'counts':counts,'latest':recent,'paper_quote_exit_enabled':False,
      'simulation_configured':bool(config.SIMULATION_PUBLIC_KEY),
      'simulation_requirement':'Public wallet address with actual token balance and SOL; PAPER holdings cannot be simulated as wallet funds.',
      'note':'Reserve coverage is necessary but not sufficient. Missing simulation/wallet/fees is not success. PAPER exits remain tick-based until verification results are reviewed.'}
