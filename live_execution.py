"""Opt-in execution adapter; never called by the PAPER strategy.

Submission is a durable intent, not a fill. Ambiguous/expired submissions stay
reserved. Only finalized, reconciled network evidence releases the wallet.
"""
import base64, hashlib, json, math, os, sqlite3, time, urllib.request
from contextlib import closing
from pathlib import Path
from decimal import Decimal, ROUND_CEILING

class ExecutionError(RuntimeError):pass
class RpcError(ExecutionError):
    def __init__(self,code,category):self.code=code;self.category=category;super().__init__(category)

def category(error):
    # Diagnostic labels only: never retry by matching an arbitrary substring.
    text=json.dumps(error).lower()
    for needle,label in [('blockhashnotfound','BLOCKHASH'),('blockhash not found','BLOCKHASH'),
                         ('computationalbudgetexceeded','COMPUTE_LIMIT'),('slippage','SLIPPAGE'),
                         ('insufficientfunds','INSUFFICIENT_FUNDS')]:
        if needle in text:return label
    return 'RPC_OR_PROGRAM_ERROR'

class Rpc:
    def __init__(self,url,timeout=6):
        if not url.startswith('https://'):raise ExecutionError('HTTPS_RPC_REQUIRED')
        self.url=url;self.timeout=timeout
    def call(self,method,params):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*a,**k):return None
        req=urllib.request.Request(self.url,json.dumps(dict(jsonrpc='2.0',id=1,method=method,params=params)).encode(),{'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=self.timeout) as response:
                raw=response.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:raise ExecutionError('RPC_RESPONSE_TOO_LARGE')
            data=json.loads(raw)
        except ExecutionError:raise
        except Exception:raise ExecutionError('RPC_TRANSPORT_OR_JSON_ERROR') from None
        if data.get('error'):raise RpcError(data['error'].get('code'),category(data['error']))
        if 'result' not in data:raise ExecutionError('RPC_RESULT_MISSING')
        return data['result']

def tip_lamports(floor,max_tip):
    # tip_floor is REST, not a getTipFloor JSON-RPC method. Units are SOL.
    try:value=Decimal(str(floor[0]['landed_tips_50th_percentile']))
    except Exception:raise ExecutionError('TIP_FLOOR_INVALID') from None
    if not value.is_finite() or value<0:raise ExecutionError('TIP_FLOOR_INVALID')
    tip=max(1000,int((value*10**9).to_integral_value(rounding=ROUND_CEILING)))
    if tip>max_tip:raise ExecutionError('TIP_ABOVE_CAP')
    return tip

class Jito:
    def __init__(self,url='https://mainnet.block-engine.jito.wtf/api/v1/bundles'):
        self.rpc=Rpc(url);self.last_request=0.
    def call(self,method,params):
        time.sleep(max(0.,1.05-(time.monotonic()-self.last_request)))
        self.last_request=time.monotonic()
        return self.rpc.call(method,params)
    def tip_accounts(self):return self.call('getTipAccounts',[])
    def tip_floor(self):
        req=urllib.request.Request('https://bundles.jito.wtf/api/v1/bundles/tip_floor')
        try:
            with urllib.request.urlopen(req,timeout=6) as r:return json.loads(r.read(65536))
        except Exception:raise ExecutionError('TIP_FLOOR_UNAVAILABLE') from None
    def send(self,transactions):
        if not 1<=len(transactions)<=5:raise ExecutionError('INVALID_BUNDLE_SIZE')
        return self.call('sendBundle',[transactions,{'encoding':'base64'}])
    def status(self,bundle_id):return self.call('getBundleStatuses',[[bundle_id]])

ACTIVE=('RESERVED','SIGNED','SUBMITTING','SUBMITTED','UNKNOWN','LANDED','EXPIRED_UNRESOLVED','FINALIZED_RECONCILE_REQUIRED')
SCHEMA='''CREATE TABLE IF NOT EXISTS intents(
 id TEXT PRIMARY KEY,wallet TEXT,mint TEXT,side TEXT,state TEXT,budget INTEGER,token_amount TEXT,
 fee_budget INTEGER,signature TEXT,last_height INTEGER,bundle_id TEXT,slot INTEGER,
 created_ts REAL,updated_ts REAL,result_json TEXT,day TEXT);
 CREATE UNIQUE INDEX IF NOT EXISTS live_one_unresolved_wallet ON intents(wallet)
 WHERE state IN ('RESERVED','SIGNED','SUBMITTING','SUBMITTED','UNKNOWN','LANDED','EXPIRED_UNRESOLVED','FINALIZED_RECONCILE_REQUIRED');
 CREATE TABLE IF NOT EXISTS risk(day TEXT PRIMARY KEY,start_equity INTEGER,realized INTEGER DEFAULT 0,
 failure_streak INTEGER DEFAULT 0,halt INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS holdings(wallet TEXT,mint TEXT,amount TEXT,cost INTEGER,PRIMARY KEY(wallet,mint));
 CREATE TABLE IF NOT EXISTS live_events(id INTEGER PRIMARY KEY,ts REAL,intent_id TEXT,kind TEXT,details TEXT);'''

class Store:
    @staticmethod
    def wallet_path(wallet):
        import wallet_signer
        from solders.pubkey import Pubkey
        Pubkey.from_string(wallet)
        return wallet_signer.PRIVATE/'execution_journal'/(wallet+'.db')
    @classmethod
    def for_wallet(cls,wallet):return cls(cls.wallet_path(wallet))
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as c:c.executescript(SCHEMA)
        try:os.chmod(self.path,0o600)
        except OSError:pass
    def connect(self):
        c=sqlite3.connect(self.path,timeout=1,isolation_level=None);c.row_factory=sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL')
        # sqlite connection context manager does not close; callers use closing.
        return c
    def event(self,c,ident,kind,details=None):
        c.execute('INSERT INTO live_events(ts,intent_id,kind,details) VALUES(?,?,?,?)',(time.time(),ident,kind,json.dumps(details or {})))
    def get(self,ident):
        with closing(self.connect()) as c:
            r=c.execute('SELECT * FROM intents WHERE id=?',(ident,)).fetchone()
            return dict(r) if r else None
    def update(self,ident,state,**data):
        allowed={'signature','last_height','bundle_id','slot','result_json'}
        if not set(data)<=allowed:raise ExecutionError('INVALID_JOURNAL_FIELD')
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                c.execute('UPDATE intents SET state=?,updated_ts=?'+''.join(','+k+'=?' for k in data)+' WHERE id=?',[state,time.time(),*data.values(),ident])
                self.event(c,ident,state);c.commit()
            except BaseException:c.rollback();raise
    def reserve(self,ident,wallet,mint,side,budget,token_amount,fee_budget,balance,available_tokens,limits):
        if any(type(x)!=int or x<0 for x in (budget,token_amount,fee_budget,balance,available_tokens)):raise ExecutionError('INTEGER_AMOUNTS_REQUIRED')
        if limits.get('day')!=time.strftime('%Y-%m-%d',time.gmtime()):raise ExecutionError('CURRENT_UTC_RISK_DAY_REQUIRED')
        if not 1<=limits['daily_loss_bps']<=10000 or limits['max_failures']<1:raise ExecutionError('INVALID_RISK_LIMITS')
        if fee_budget>limits.get('max_fee_lamports',100000):raise ExecutionError('FEE_BUDGET_CAP')
        if side not in ('BUY','SELL') or min(budget,fee_budget,token_amount)<0:raise ExecutionError('INVALID_INTENT')
        if side=='BUY' and (budget<=0 or budget>limits['max_order_lamports']):raise ExecutionError('ORDER_CAP')
        if side=='SELL' and (token_amount<=0 or token_amount>available_tokens):raise ExecutionError('TOKEN_BALANCE')
        if balance < (budget if side=='BUY' else 0)+fee_budget:raise ExecutionError('SOL_BALANCE')
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                if c.execute('SELECT 1 FROM intents WHERE id=?',(ident,)).fetchone():raise ExecutionError('DUPLICATE_INTENT')
                r=c.execute('SELECT * FROM risk WHERE day=?',(limits['day'],)).fetchone()
                if not r or r['start_equity']<=0:raise ExecutionError('DAILY_EQUITY_NOT_INITIALIZED')
                if r['halt']:raise ExecutionError('KILL_SWITCH')
                if side=='BUY' and (r['failure_streak']>=limits['max_failures'] or
                    r['realized']<=-math.ceil(r['start_equity']*limits['daily_loss_bps']/10000)):
                    raise ExecutionError('CIRCUIT_BREAKER')
                if side=='BUY' and c.execute('SELECT COUNT(*) FROM holdings WHERE wallet=?',(wallet,)).fetchone()[0]>=limits.get('max_positions',2):raise ExecutionError('POSITION_CAP')
                now=time.time()
                c.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (ident,wallet,mint,side,'RESERVED',budget,str(token_amount),fee_budget,None,None,None,None,now,now,None,limits['day']))
                self.event(c,ident,'RESERVED');c.commit()
            except sqlite3.IntegrityError:c.rollback();raise ExecutionError('WALLET_HAS_UNRESOLVED_INTENT') from None
            except BaseException:c.rollback();raise
    def begin_day(self,day,equity):
        if type(equity)!=int or equity<=0:raise ExecutionError('INVALID_DAILY_EQUITY')
        with closing(self.connect()) as c:
            c.execute('INSERT OR IGNORE INTO risk(day,start_equity) VALUES(?,?)',(day,equity))
    def kill(self,day):
        with closing(self.connect()) as c:
            c.execute('UPDATE risk SET halt=1 WHERE day=?',(day,))
    def finalize(self,ident,effect):
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                row=c.execute('SELECT * FROM intents WHERE id=?',(ident,)).fetchone()
                if row['state'] in ('FINALIZED_SUCCESS','FINALIZED_FAILED'):c.rollback();return
                if row['state']!='FINALIZED_RECONCILE_REQUIRED':raise ExecutionError('NOT_RECONCILED')
                holding=c.execute('SELECT * FROM holdings WHERE wallet=? AND mint=?',(row['wallet'],row['mint'])).fetchone()
                delta=effect['wallet_delta_lamports'];tokens=int(effect['token_delta_raw']);pnl=0
                failed=effect['status']=='FINALIZED_FAILED'
                if failed:
                    if delta!=-effect['fee_lamports'] or tokens!=0:raise ExecutionError('FAILED_TX_BALANCE_MISMATCH')
                    pnl=delta
                elif row['side']=='BUY':
                    if not -row['budget']-row['fee_budget']<=delta<0 or tokens<=0:raise ExecutionError('BUY_BALANCE_OUTSIDE_BUDGET')
                    amount=int(holding['amount']) if holding else 0;cost=holding['cost'] if holding else 0
                    c.execute('INSERT INTO holdings VALUES(?,?,?,?) ON CONFLICT(wallet,mint) DO UPDATE SET amount=excluded.amount,cost=excluded.cost',
                              (row['wallet'],row['mint'],str(amount+tokens),cost-delta))
                else:
                    sold=-tokens
                    if not holding or not 0<sold<=int(holding['amount']):raise ExecutionError('SELL_COST_BASIS_UNKNOWN')
                    allocated=holding['cost']*sold//int(holding['amount']);pnl=delta-allocated
                    remain=int(holding['amount'])-sold
                    if remain:c.execute('UPDATE holdings SET amount=?,cost=? WHERE wallet=? AND mint=?',(str(remain),holding['cost']-allocated,row['wallet'],row['mint']))
                    else:c.execute('DELETE FROM holdings WHERE wallet=? AND mint=?',(row['wallet'],row['mint']))
                effect=dict(effect,realized_pnl_lamports=pnl)
                c.execute('UPDATE risk SET realized=realized+?,failure_streak=CASE WHEN ? THEN failure_streak+1 ELSE 0 END WHERE day=?',(pnl,int(failed),row['day']))
                c.execute('UPDATE intents SET state=?,result_json=?,updated_ts=? WHERE id=?',(effect['status'],json.dumps(effect),time.time(),ident))
                self.event(c,ident,effect['status'],dict(realized_pnl_lamports=pnl));c.commit()
            except BaseException:c.rollback();raise
    def assert_not_halted(self,ident):
        with closing(self.connect()) as c:
            row=c.execute('SELECT r.halt FROM intents i JOIN risk r ON r.day=i.day WHERE i.id=?',(ident,)).fetchone()
            if row is None or row['halt']:raise ExecutionError('KILL_SWITCH')
    def summary(self):
        with closing(self.connect()) as c:
            return dict(states=dict(c.execute('SELECT state,COUNT(*) FROM intents GROUP BY state')),
              risk=[dict(r) for r in c.execute('SELECT * FROM risk ORDER BY day DESC LIMIT 7')],
              recent=[dict(r) for r in c.execute('SELECT id,mint,side,state,signature,slot,bundle_id FROM intents ORDER BY created_ts DESC LIMIT 50')])

def inspect_signed(encoded,wallet):
    from solders.transaction import VersionedTransaction
    try:
        raw=base64.b64decode(encoded,validate=True);tx=VersionedTransaction.from_bytes(raw)
        tx.sanitize();tx.verify_and_hash_message()
    except Exception:raise ExecutionError('INVALID_SIGNED_TRANSACTION') from None
    if len(raw)>1232 or str(tx.message.account_keys[0])!=wallet:raise ExecutionError('WRONG_FEE_PAYER_OR_TX_SIZE')
    if tx.message.header.num_required_signatures!=1:raise ExecutionError('SINGLE_SIGNER_REQUIRED')
    return tx,str(tx.signatures[0])

class Engine:
    def __init__(self,store,rpc,enabled=False,jito=None,kill_file=None):
        self.store=store;self.rpc=rpc;self.enabled=enabled;self.jito=jito;self.kill_file=kill_file or (__import__('wallet_signer').PRIVATE/'KILL_SWITCH')
    def _enabled(self):
        if not self.enabled:raise ExecutionError('LIVE_DISABLED')
        if self.kill_file and Path(self.kill_file).exists():raise ExecutionError('KILL_SWITCH_FILE')
    def reserve_online(self,ident,wallet,mint,side,budget,token_amount,fee_budget,limits):
        self._enabled()
        self._canonical(wallet)
        balance=self.rpc.call('getBalance',[wallet,{'commitment':'confirmed'}])
        slot=balance['context']['slot']
        response=self.rpc.call('getTokenAccountsByOwner',[wallet,{'mint':mint},{'encoding':'jsonParsed','commitment':'confirmed','minContextSlot':slot}])
        if response['context']['slot']<slot:raise ExecutionError('STALE_TOKEN_BALANCE')
        total=0
        for row in response['value']:
            info=row['account']['data']['parsed']['info']
            if info['owner']!=wallet or info['mint']!=mint:raise ExecutionError('TOKEN_ACCOUNT_MISMATCH')
            total+=int(info['tokenAmount']['amount'])
        with closing(self.store.connect()) as c:
            holding=c.execute('SELECT amount FROM holdings WHERE wallet=? AND mint=?',(wallet,mint)).fetchone()
        if total!=(int(holding[0]) if holding else 0):raise ExecutionError('ONCHAIN_INVENTORY_NOT_RECONCILED')
        self.store.reserve(ident,wallet,mint,side,budget,token_amount,fee_budget,balance['value'],total,limits)
        return self.store.get(ident)
    def execute_with_rebuild(self,intent_prefix,wallet,mint,side,budget,token_amount,fee_budget,limits,builder,max_attempts=2,**transport):
        # builder must refresh its quote on each call. Never widen slippage or
        # the approved amount here; never rebuild an ambiguous sent intent.
        if not 1<=max_attempts<=3:raise ExecutionError('REBUILD_LIMIT')
        reason=None
        for attempt in range(max_attempts):
            ident=intent_prefix+':'+str(attempt)
            self.reserve_online(ident,wallet,mint,side,budget,token_amount,fee_budget,limits)
            block=self.rpc.call('getLatestBlockhash',[{'commitment':'confirmed'}])['value']
            try:
                signed=builder(block,reason)
                result=self.submit(ident,signed,block['lastValidBlockHeight'],**transport)
            except ExecutionError as exc:
                if self.store.get(ident)['state']!='RESERVED':raise
                self.store.update(ident,'BUILD_REJECTED',result_json=json.dumps({'category':str(exc)}))
                if str(exc)!='BLOCKHASH_TOO_OLD_REBUILD_REQUIRED':raise
                reason='BLOCKHASH';continue
            if result['state']!='PREFLIGHT_REJECTED':return result
            reason=json.loads(result['result_json']).get('category')
            if reason not in ('BLOCKHASH','COMPUTE_LIMIT','SLIPPAGE'):return result
        return self.store.get(ident)
    def _canonical(self,wallet):
        if self.store.path.resolve()!=Store.wallet_path(wallet).resolve():raise ExecutionError('CANONICAL_WALLET_JOURNAL_REQUIRED')
    def submit(self,ident,signed,last_height,mode='rpc',skip_preflight=False,tip_account=None,max_tip=0):
        self._enabled();row=self.store.get(ident)
        if not row or row['state']!='RESERVED':raise ExecutionError('INTENT_NOT_RESERVED')
        self._canonical(row['wallet'])
        self.store.assert_not_halted(ident)
        if mode not in ('rpc','jito'):raise ExecutionError('INVALID_TRANSPORT')
        if type(last_height)!=int or last_height<=0:raise ExecutionError('INVALID_LAST_VALID_HEIGHT')
        tx,signature=inspect_signed(signed,row['wallet'])
        from wallet_signer import validate_message
        validate_message(tx.message,row['wallet'],dict(enabled=True,allowed_mints=[row['mint']],
          max_order_lamports=row['budget'],max_sell_tokens=row['token_amount'],max_compute_units=200000,
          max_priority_lamports=row['fee_budget'],max_tip_lamports=max_tip,tip_accounts=[tip_account] if tip_account else []))
        # A signed mainnet transaction must be simulated with signature checking.
        from solders.message import to_bytes_versioned
        message64=base64.b64encode(to_bytes_versioned(tx.message)).decode()
        fee=self.rpc.call('getFeeForMessage',[message64,{'commitment':'confirmed'}]).get('value')
        if type(fee)!=int or fee>row['fee_budget']:raise ExecutionError('NETWORK_FEE_ABOVE_BUDGET')
        before=self.rpc.call('getBalance',[row['wallet'],{'commitment':'confirmed'}])['value']
        sim=self.rpc.call('simulateTransaction',[signed,{'encoding':'base64','sigVerify':True,'commitment':'confirmed','replaceRecentBlockhash':False,
            'accounts':{'encoding':'base64','addresses':[row['wallet']]}}])
        value=sim.get('value') or {}
        if 'err' not in value:raise ExecutionError('SIMULATION_INCOMPLETE')
        if value['err'] is not None:
            self.store.update(ident,'PREFLIGHT_REJECTED',result_json=json.dumps({'category':category(value['err'])}))
            return self.store.get(ident)
        accounts=value.get('accounts') or []
        if len(accounts)!=1 or not accounts[0] or type(accounts[0].get('lamports'))!=int:raise ExecutionError('SIMULATED_WALLET_BALANCE_MISSING')
        max_debit=(row['budget'] if row['side']=='BUY' else 0)+row['fee_budget']
        if before-accounts[0]['lamports']>max_debit:
            self.store.update(ident,'PREFLIGHT_REJECTED',result_json=json.dumps({'category':'SIMULATED_SPEND_ABOVE_BUDGET'}))
            return self.store.get(ident)
        height=self.rpc.call('getBlockHeight',[{'commitment':'confirmed'}])
        if height>=last_height-2:raise ExecutionError('BLOCKHASH_TOO_OLD_REBUILD_REQUIRED')
        balance=self.rpc.call('getBalance',[row['wallet'],{'commitment':'confirmed'}])['value']
        required=(row['budget'] if row['side']=='BUY' else 0)+row['fee_budget']
        if balance<required:
            raise ExecutionError('BALANCE_CHANGED_BEFORE_SEND')
        if mode=='jito':
            if not self.jito or not tip_account or tip_account not in self.jito.tip_accounts():raise ExecutionError('JITO_TIP_ACCOUNT_INVALID')
            # Require tip in SAME transaction as the swap. It cannot land alone.
            verify_tip(tx,row['wallet'],tip_account,max_tip)
        self.store.update(ident,'SIGNED',signature=signature,last_height=last_height)
        # Commit before network IO. A crash from this point is ambiguous.
        self.store.update(ident,'SUBMITTING')
        try:
            self._enabled()
            self.store.assert_not_halted(ident)
            if mode=='jito':
                bundle=self.jito.send([signed]);self.store.update(ident,'SUBMITTED',bundle_id=bundle)
            else:
                answer=self.rpc.call('sendTransaction',[signed,{'encoding':'base64','skipPreflight':bool(skip_preflight),'maxRetries':0,'preflightCommitment':'confirmed'}])
                if answer!=signature:raise ExecutionError('SIGNATURE_MISMATCH')
                self.store.update(ident,'SUBMITTED')
        except Exception:
            self.store.update(ident,'UNKNOWN')
        return self.store.get(ident)
    def track(self,ident):
        row=self.store.get(ident)
        if row and row['state'] in ('FINALIZED_SUCCESS','FINALIZED_FAILED'):return row
        if not row or not row['signature']:raise ExecutionError('SIGNATURE_NOT_RECORDED')
        status=self.rpc.call('getSignatureStatuses',[[row['signature']],{'searchTransactionHistory':True}])['value'][0]
        if status is not None:
            slot=status['slot']
            if status.get('confirmationStatus')!='finalized':
                self.store.update(ident,'LANDED',slot=slot);return self.store.get(ident)
            tx=self.rpc.call('getTransaction',[row['signature'],{'encoding':'json','commitment':'finalized','maxSupportedTransactionVersion':0}])
            if tx and tx.get('slot')!=slot:raise ExecutionError('FINALIZED_SLOT_MISMATCH')
            result=reconcile(tx,row)
            # Keep successful fills quarantined until strategy cost basis and
            # position inventory have been reconciled. A signature isn't a fill.
            self.store.update(ident,'FINALIZED_RECONCILE_REQUIRED',slot=slot,result_json=json.dumps(result))
            self.store.finalize(ident,result)
            return self.store.get(ident)
        height=self.rpc.call('getBlockHeight',[{'commitment':'finalized'}])
        if row['last_height'] is not None and height>row['last_height']:
            self.store.update(ident,'EXPIRED_UNRESOLVED')
        return self.store.get(ident)

def verify_tip(tx,wallet,account,cap):
    import struct
    keys=tx.message.account_keys;total=0
    for ix in tx.message.instructions:
        if str(keys[ix.program_id_index])=='11111111111111111111111111111111' and len(ix.data)==12 and struct.unpack('<I',bytes(ix.data[:4]))[0]==2:
            if len(ix.accounts)==2 and str(keys[ix.accounts[0]])==wallet and str(keys[ix.accounts[1]])==account:
                total+=struct.unpack('<Q',bytes(ix.data[4:]))[0]
    if not 1000<=total<=cap:raise ExecutionError('JITO_TIP_MISSING_OR_ABOVE_CAP')

def reconcile(tx,intent):
    if not tx or not isinstance(tx.get('meta'),dict):raise ExecutionError('FINALIZED_METADATA_MISSING')
    if tx['transaction']['signatures'][0]!=intent['signature']:raise ExecutionError('FINALIZED_SIGNATURE_MISMATCH')
    meta=tx['meta'];keys=tx['transaction']['message']['accountKeys']
    keys=[k.get('pubkey') if isinstance(k,dict) else k for k in keys]
    if not keys or keys[0]!=intent['wallet']:raise ExecutionError('FINALIZED_PAYER_MISMATCH')
    delta=meta['postBalances'][0]-meta['preBalances'][0]
    def tokens(which):return sum(int(x['uiTokenAmount']['amount']) for x in meta.get(which,[]) if x.get('owner')==intent['wallet'] and x.get('mint')==intent['mint'])
    acquired=tokens('postTokenBalances')-tokens('preTokenBalances')
    if meta.get('err') is None:
        if intent['side']=='BUY' and acquired<=0:raise ExecutionError('BUY_WITHOUT_TOKEN_RECEIPT')
        if intent['side']=='SELL' and acquired!=-int(intent['token_amount']):raise ExecutionError('SELL_TOKEN_DELTA_MISMATCH')
    return dict(err_category=category(meta['err']) if meta.get('err') else None,fee_lamports=meta['fee'],
      wallet_delta_lamports=delta,token_delta_raw=str(acquired),slot=tx['slot'],
      status='FINALIZED_FAILED' if meta.get('err') is not None else 'FINALIZED_SUCCESS',
      pnl=None,note='Wallet deltas include fees/rent/transfers; they are not strategy PNL without a reconciled cost basis.')
