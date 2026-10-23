"""Explicit Phase 3 lab: one swap+tip transaction per Jito bundle.

Never wired to PAPER. Bundle acceptance, landing, and finalized accounting are
separate evidence. Two slots trigger observation, never an ambiguous resend.
"""
import base64,json,time,uuid,hashlib,secrets
from contextlib import closing
from datetime import datetime,timezone
from decimal import Decimal,ROUND_CEILING
from pathlib import Path
import live_execution as live
import direct_pump_v2 as direct

SCHEMA='''CREATE TABLE IF NOT EXISTS jito_attempts(
 intent_id TEXT PRIMARY KEY,group_id TEXT,attempt INTEGER,prepared_slot INTEGER,
 tip INTEGER,priority_micro INTEGER,prepared_ts REAL,message_hash TEXT,
 observation_json TEXT,plan_hash TEXT);'''
FINAL=('FINALIZED_SUCCESS','FINALIZED_FAILED','JITO_NOT_FORWARDED','PREFLIGHT_REJECTED','BUILD_REJECTED')

def amount(x,label,minimum=0,maximum=2**64-1):
    if type(x)!=int or not minimum<=x<=maximum:raise live.ExecutionError(label)
    return x

def choose_tip(floor,cap,percentile=75,now=None,max_age=30,minimum=1000):
    amount(cap,'TIP_CAP_INVALID',1000);amount(minimum,'TIP_MINIMUM_INVALID',1000)
    if percentile not in (75,95):raise live.ExecutionError('TIP_PERCENTILE_INVALID')
    try:
        row=floor[0];stamp=datetime.fromisoformat(row['time'].replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError()
        age=(time.time() if now is None else now)-stamp.timestamp()
        if not 0<=age<=max_age:raise live.ExecutionError('TIP_FLOOR_STALE')
        value=Decimal(str(row['landed_tips_'+str(percentile)+'th_percentile']))
        if not value.is_finite() or value<0:raise ValueError()
        tip=max(minimum,int((value*10**9).to_integral_value(rounding=ROUND_CEILING)))
    except live.ExecutionError:raise
    except Exception:raise live.ExecutionError('TIP_FLOOR_INVALID') from None
    if tip>cap:raise live.ExecutionError('TIP_ABOVE_CAP')
    return tip

class Jito(live.Jito):
    def __init__(self,url='https://mainnet.block-engine.jito.wtf/api/v1/bundles'):
        super().__init__(url);self.accounts_cache=None;self.accounts_at=0.
    def tip_accounts(self):
        if self.accounts_cache is None or time.monotonic()-self.accounts_at>60:
            from solders.pubkey import Pubkey
            rows=super().tip_accounts()
            if not isinstance(rows,list) or not rows or len(rows)>64:raise live.ExecutionError('TIP_ACCOUNTS_INVALID')
            try:
                for row in rows:Pubkey.from_string(row)
            except Exception:raise live.ExecutionError('TIP_ACCOUNTS_INVALID') from None
            self.accounts_cache=list(set(rows));self.accounts_at=time.monotonic()
        return self.accounts_cache
    def inflight(self,ident):return self.call('getInflightBundleStatuses',[[ident]])
    def send(self,transactions):
        answer=super().send(transactions)
        if not isinstance(answer,str) or len(answer)!=64 or any(c not in '0123456789abcdef' for c in answer):raise live.ExecutionError('BUNDLE_ID_INVALID')
        return answer

def compose(message,account,tip,priority_micro):
    """Keep the pinned direct Pump instruction and append conditional tip."""
    from solders.instruction import Instruction,AccountMeta
    from solders.message import MessageV0
    from solders.compute_budget import set_compute_unit_price
    from solders.system_program import transfer,TransferParams
    from solders.pubkey import Pubkey
    amount(tip,'TIP_INVALID',1000);amount(priority_micro,'PRIORITY_INVALID')
    if message.address_table_lookups:raise live.ExecutionError('LOOKUP_TABLES_NOT_SUPPORTED')
    keys=message.account_keys;h=message.header;instructions=[]
    def meta(i):
        signed=i<h.num_required_signatures
        writable=i<h.num_required_signatures-h.num_readonly_signed_accounts if signed else i<len(keys)-h.num_readonly_unsigned_accounts
        return AccountMeta(keys[i],signed,writable)
    for ix in message.instructions:
        if str(keys[ix.program_id_index])=='ComputeBudget111111111111111111111111111111' and bytes(ix.data)[:1]==b'\x03':
            instructions.append(set_compute_unit_price(priority_micro))
        else:instructions.append(Instruction(keys[ix.program_id_index],bytes(ix.data),[meta(i) for i in ix.accounts]))
    instructions.append(transfer(TransferParams(from_pubkey=keys[0],to_pubkey=Pubkey.from_string(account),lamports=tip)))
    return MessageV0.try_compile(keys[0],instructions,[],message.recent_blockhash)

def initialize(store):
    with closing(store.connect()) as c:c.executescript(SCHEMA)

def record(store,ident,group,attempt,slot,tip,priority,message,plan):
    from solders.message import to_bytes_versioned
    digest=hashlib.sha256(to_bytes_versioned(message)).hexdigest()
    planhash=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    with closing(store.connect()) as c:
        c.execute('INSERT INTO jito_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,group,attempt,slot,tip,priority,time.time(),digest,'{}',planhash))

def observation(store,ident):
    initialize(store)
    with closing(store.connect()) as c:
        row=c.execute('SELECT * FROM jito_attempts WHERE intent_id=?',(ident,)).fetchone()
        return dict(row) if row else None

def one_value(response,bundle):
    if response is None:return None
    if not isinstance(response,dict) or not isinstance(response.get('value'),list) or len(response['value'])!=1:raise live.ExecutionError('BUNDLE_STATUS_INVALID')
    value=response['value'][0]
    if value is not None and (not isinstance(value,dict) or value.get('bundle_id')!=bundle):raise live.ExecutionError('BUNDLE_STATUS_ID_MISMATCH')
    return value

def track(store,rpc,jito,ident):
    """Read both bundle APIs and chain. Evidence failures keep wallet reserved."""
    row=store.get(ident);attempt=observation(store,ident)
    if not row or not attempt:raise live.ExecutionError('JITO_INTENT_REQUIRED')
    if row['state'] in FINAL:return report(store,ident)
    if not row['signature']:
        # An interrupted preparation remains reserved; never query a null signature.
        return report(store,ident)
    evidence=dict(checked_ts=time.time(),bundle_state='UNKNOWN',inflight_state='UNKNOWN',rebuild_allowed=False)
    status=None;inflight=None;bundle=row.get('bundle_id')
    # Bundle status is required to claim bundle landing, not to account for an
    # independently observed finalized signature (e.g. an uncled rebroadcast).
    try:
        if bundle:
            status=one_value(jito.status(bundle),bundle)
            if status:
                if status.get('transactions')!=[row['signature']] or type(status.get('slot'))!=int or status.get('confirmation_status',status.get('confirmationStatus')) not in ('processed','confirmed','finalized'):raise live.ExecutionError('BUNDLE_CONTENT_MISMATCH')
                evidence.update(bundle_state='LANDED',bundle_slot=status['slot'],slots_from_preparation_to_landing=status['slot']-attempt['prepared_slot'],bundle_confirmation=status.get('confirmation_status',status.get('confirmationStatus')),bundle_err=status.get('err'))
            inflight=one_value(jito.inflight(bundle),bundle)
            if inflight:evidence['inflight_state']=inflight.get('status','UNKNOWN')
    except Exception as exc:
        evidence['bundle_query_error']=type(exc).__name__
        # Invalid evidence must never be used to release/resend an intent.
        status=None;inflight=None
    slot=rpc.call('getSlot',[{'commitment':'confirmed'}]);amount(slot,'SLOT_INVALID')
    evidence.update(observed_slot=slot,slots_elapsed=max(0,slot-attempt['prepared_slot']),two_slot_deadline_reached=slot-attempt['prepared_slot']>=2)
    sig=rpc.call('getSignatureStatuses',[[row['signature']],{'searchTransactionHistory':True}])['value'][0]
    if status and sig and sig.get('slot')!=status['slot']:
        evidence['bundle_state']='SLOT_CONFLICT';inflight=None
    # A timeout/null/Invalid/expired hash is never proof of non-execution.
    # Only Jito's explicit all-regions "not forwarded" rejection is retryable.
    rejected=(bundle and not status and inflight and inflight.get('status')=='Failed' and sig is None and 'bundle_query_error' not in evidence and evidence['two_slot_deadline_reached'])
    if rejected:
        with closing(store.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT state,day FROM intents WHERE id=?',(ident,)).fetchone()
            if current['state'] not in FINAL:
                c.execute("UPDATE intents SET state='JITO_NOT_FORWARDED',updated_ts=? WHERE id=?",(time.time(),ident))
                c.execute('UPDATE risk SET failure_streak=failure_streak+1 WHERE day=?',(current['day'],))
                store.event(c,ident,'JITO_NOT_FORWARDED',dict(bundle_id=bundle))
            c.commit()
        evidence['rebuild_allowed']=True
    else:
        live.Engine(store,rpc).track(ident)
    with closing(store.connect()) as c:c.execute('UPDATE jito_attempts SET observation_json=? WHERE intent_id=?',(json.dumps(evidence),ident))
    return report(store,ident)

def report(store,ident):
    row=store.get(ident);a=observation(store,ident)
    return dict(id=ident,state=row['state'],signature=row['signature'],bundle_id=row['bundle_id'],slot=row['slot'],
        tip_lamports=a['tip'],priority_micro_lamports=a['priority_micro'],attempt=a['attempt'],group_id=a['group_id'],
        landing=json.loads(a['observation_json']),
        paid_tip_lamports=a['tip'] if row['state']=='FINALIZED_SUCCESS' else 0 if row['state'] in ('FINALIZED_FAILED','JITO_NOT_FORWARDED','PREFLIGHT_REJECTED') else None,
        finalized_accounting=json.loads(row['result_json']) if row['state'] in ('FINALIZED_SUCCESS','FINALIZED_FAILED') and row['result_json'] else None,
        note='Accepted bundle is not a fill. Wallet accounting uses finalized chain metadata. UNKNOWN keeps the wallet reserved.')

def send(rpc,plan,signer,store,enabled=False,jito=None,timeout=20):
    if not enabled:raise live.ExecutionError('BROADCAST_DISABLED')
    if not 0<timeout<=120:raise live.ExecutionError('CONFIRMATION_TIMEOUT')
    jito=jito or Jito(plan.get('jito_url','https://mainnet.block-engine.jito.wtf/api/v1/bundles'))
    settings=plan['jito'];cap=amount(settings['max_tip_lamports'],'TIP_CAP_INVALID',1000)
    attempts=amount(settings.get('max_attempts',2),'REBUILD_LIMIT',1,3)
    priority=amount(settings.get('priority_micro_lamports',0),'PRIORITY_INVALID')
    bump=amount(settings.get('tip_bump_bps',2500),'TIP_BUMP_INVALID',1,10000)
    percentile=settings.get('tip_percentile',75)
    initialize(store);group=uuid.uuid4().hex;previous_tip=0;previous_hash=None
    adapter=live.Engine(store,rpc,enabled=True,jito=jito)
    deadline=time.monotonic()+timeout
    for attempt in range(attempts):
        floor=jito.tip_floor()
        tip=choose_tip(floor,cap,percentile,minimum=max(1000,(previous_tip*(10000+bump)+9999)//10000))
        account=secrets.choice(jito.tip_accounts())
        message,block,details=direct.prepare(rpc,plan) # refreshed accounts + blockhash
        if previous_hash==block['blockhash']:raise live.ExecutionError('FRESH_BLOCKHASH_REQUIRED')
        message=compose(message,account,tip,priority)
        ident='phase3:'+group+':'+str(attempt)
        adapter.reserve_online(ident,plan['wallet'],plan['mint'],plan['side'],plan['limit'] if plan['side']=='BUY' else 0,
            plan['amount'],plan['fee_budget'],plan['risk_limits'])
        try:
            signed=signer(message)
            tx,_=live.inspect_signed(signed,plan['wallet'])
            from solders.message import to_bytes_versioned
            if to_bytes_versioned(tx.message)!=to_bytes_versioned(message):raise live.ExecutionError('SIGNED_MESSAGE_MISMATCH')
            # Signing delay must not turn stale market tip data into fresh data.
            choose_tip(floor,cap,percentile)
            slot=rpc.call('getSlot',[{'commitment':'confirmed'}]);amount(slot,'SLOT_INVALID')
            record(store,ident,group,attempt,slot,tip,priority,message,plan)
            row=adapter.submit(ident,signed,block['lastValidBlockHeight'],mode='jito',tip_account=account,max_tip=cap)
        except Exception:
            if store.get(ident)['state']=='RESERVED':store.update(ident,'BUILD_REJECTED')
            raise
        if row['state']=='PREFLIGHT_REJECTED':return report(store,ident)
        while time.monotonic()<deadline:
            result=track(store,rpc,jito,ident)
            if result['state'] in FINAL:break
            time.sleep(.25) # Jito's shared call limiter controls actual API rate.
        else:return report(store,ident)
        if result['state']!='JITO_NOT_FORWARDED' or attempt+1==attempts or time.monotonic()>=deadline:return result
        previous_tip=tip;previous_hash=block['blockhash']
    return report(store,ident)

def summary(store):
    initialize(store)
    with closing(store.connect()) as c:result=summary_connection(c)
    return dict(result,wallet_journal=store.summary())

def summary_connection(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jito_attempts'").fetchone():return dict(attempts=0,accepted_ids=0,observed_bundle_landings=0,inclusion_rate=None,recent=[])
    rows=[dict(r) for r in c.execute('SELECT a.*,i.state,i.bundle_id FROM jito_attempts a JOIN intents i ON i.id=a.intent_id ORDER BY a.prepared_ts')]
    submitted=[r for r in rows if r['bundle_id'] or r['state'] in ('UNKNOWN','SUBMITTING')]
    landed=[r for r in rows if json.loads(r['observation_json']).get('bundle_state')=='LANDED']
    return dict(attempts=len(rows),accepted_ids=sum(bool(r['bundle_id']) for r in rows),observed_bundle_landings=len(landed),
        inclusion_rate=len(landed)/len(submitted) if submitted else None,
        denominator='Accepted bundle IDs plus ambiguous submissions; observations may be incomplete.',
        recent=[dict(intent_id=r['intent_id'],state=r['state'],tip=r['tip'],bundle_id=r['bundle_id'],landing=json.loads(r['observation_json'])) for r in rows[-50:]])

def main():
    import argparse,getpass,wallet_signer
    from solders.message import to_bytes_versioned
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=('prepare','send','track','summary','begin-day'))
    ap.add_argument('--plan',required=True);ap.add_argument('--broadcast',action='store_true');ap.add_argument('--intent');args=ap.parse_args()
    plan=json.loads(Path(args.plan).read_text());rpc=live.Rpc(plan['rpc_url']);jito=Jito(plan.get('jito_url','https://mainnet.block-engine.jito.wtf/api/v1/bundles'))
    if args.command=='prepare':
        message,block,details=direct.prepare(rpc,plan)
        tip=choose_tip(jito.tip_floor(),plan['jito']['max_tip_lamports'],plan['jito'].get('tip_percentile',75));account=secrets.choice(jito.tip_accounts())
        message=compose(message,account,tip,plan['jito'].get('priority_micro_lamports',0))
        print(json.dumps(dict(details,tip_lamports=tip,tip_account=account,available_tip_accounts=jito.tip_accounts(),priority_lamports=(200000*plan['jito'].get('priority_micro_lamports',0)+999999)//1000000,broadcast=False)));return
    store=live.Store.for_wallet(plan['wallet'])
    if args.command=='begin-day':store.begin_day(plan['risk_limits']['day'],plan['daily_start_equity_lamports']);print('DAILY_EQUITY_INITIALIZED');return
    if args.command=='summary':print(json.dumps(summary(store)));return
    if args.command=='track':
        if not args.intent:raise live.ExecutionError('INTENT_REQUIRED')
        print(json.dumps(track(store,rpc,jito,args.intent)));return
    if not args.broadcast:raise live.ExecutionError('EXPLICIT_BROADCAST_FLAG_REQUIRED')
    password=getpass.getpass('Vault password: ')
    def sign(message):return wallet_signer.sign_isolated(base64.b64encode(to_bytes_versioned(message)).decode(),plan['vault_path'],plan['signer_policy_path'],password)
    print(json.dumps(send(rpc,plan,sign,store,enabled=True,jito=jito)))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        import re
        code=str(exc);print('PHASE3_FAILED: '+(code if re.fullmatch('[A-Z0-9_]{1,100}',code) else type(exc).__name__));raise SystemExit(1)
