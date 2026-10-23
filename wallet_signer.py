"""Encrypted vault + short-lived signer subprocess. This is process isolation,
not an OS sandbox against a compromised parent/administrator. Never export vaults.
"""
import base64,json,os,struct,subprocess,sys
from pathlib import Path
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey

PRIVATE=Path.home()/'PumpfunBotPrivate'
PUMP='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
TOKEN_PROGRAMS={'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}
ATA='ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
COMPUTE='ComputeBudget111111111111111111111111111111'

class SigningError(RuntimeError):pass

def derive(password,salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt,length=32,n=2**15,r=8,p=1).derive(password.encode())

def create_vault(path,keypair,password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(password)<12:raise SigningError('PASSWORD_TOO_SHORT')
    p=Path(path);created=not p.parent.exists();p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    from wallet_manager import restrict_access
    if created:restrict_access(p.parent)
    salt=os.urandom(16);nonce=os.urandom(12);public=str(keypair.pubkey())
    ciphertext=AESGCM(derive(password,salt)).encrypt(nonce,bytes(keypair),public.encode())
    data=dict(schema='PUMPFUN_ENCRYPTED_WALLET_V1',public_key=public,
              salt=base64.b64encode(salt).decode(),nonce=base64.b64encode(nonce).decode(),ciphertext=base64.b64encode(ciphertext).decode())
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(data,f)
    restrict_access(p)
    return public

def unlock(path,password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        from wallet_manager import assert_private
        assert_private(path)
        raw=Path(path).read_bytes()
        if len(raw)>4096:raise ValueError()
        d=json.loads(raw)
        if d['schema']!='PUMPFUN_ENCRYPTED_WALLET_V1':raise ValueError()
        dec=lambda k:base64.b64decode(d[k],validate=True)
        salt=dec('salt');nonce=dec('nonce')
        if len(salt)!=16 or len(nonce)!=12:raise ValueError()
        secret=AESGCM(derive(password,salt)).decrypt(nonce,dec('ciphertext'),d['public_key'].encode())
        kp=Keypair.from_bytes(secret)
        if str(kp.pubkey())!=d['public_key']:raise ValueError()
        return kp
    except Exception:raise SigningError('WALLET_UNLOCK_FAILED') from None

def validate_message(message,public,policy):
    if not policy.get('enabled'):raise SigningError('SIGNER_DISABLED')
    if message.header.num_required_signatures!=1 or str(message.account_keys[0])!=public:raise SigningError('WRONG_SIGNER')
    if getattr(message,'address_table_lookups',[]):raise SigningError('LOOKUP_TABLES_NOT_SUPPORTED_IN_SIGNER_LAB')
    keys=message.account_keys;swap=None;tip_total=0;cu_limit=None;cu_price=None
    idl=json.loads((Path(__file__).parent/'research/pump_public_idl.json').read_text()) if (Path(__file__).parent/'research/pump_public_idl.json').exists() else None
    # Use the same pinned IDL as local tests, never an IDL supplied by a request.
    if idl is None:
        import local_simulation
        idl=json.loads(local_simulation.IDL.read_text())
    for ix in message.instructions:
        program=str(keys[ix.program_id_index]);data=bytes(ix.data);accounts=[str(keys[i]) for i in ix.accounts]
        if program==PUMP:
            if swap is not None:raise SigningError('MULTIPLE_SWAPS')
            definitions={bytes(x['discriminator']):x for x in idl['instructions'] if x['name'] in ('buy_exact_quote_in_v3','sell_v3','buy_v2','sell_v2')}
            spec=definitions.get(data[:8])
            if spec is None or len(accounts)!=len(spec['accounts']):raise SigningError('UNSUPPORTED_PUMP_INSTRUCTION')
            named=dict(zip([a['name'] for a in spec['accounts']],accounts))
            if named['user']!=public or named['base_mint'] not in policy.get('allowed_mints',[]):raise SigningError('MINT_OR_USER_NOT_ALLOWED')
            import local_simulation as lab
            mint=named['base_mint'];token=named['base_token_program']
            if token not in TOKEN_PROGRAMS:raise SigningError('TOKEN_PROGRAM_NOT_ALLOWED')
            if named['bonding_curve']!=lab.v.curve_address(mint) or named['associated_base_user']!=lab.ata(public,mint,token):raise SigningError('DERIVED_ACCOUNTS_MISMATCH')
            if named['quote_mint']!='So11111111111111111111111111111111111111112' or named['user_volume_accumulator']!=lab.pda(PUMP,b'user_volume_accumulator',bytes(lab.key(public))):raise SigningError('QUOTE_OR_VOLUME_MISMATCH')
            if spec['name']=='buy_v2':
                if len(data)!=25:raise SigningError('BUY_LAYOUT')
                amount,maximum,flag=struct.unpack('<QQB',data[8:])
                if amount<=0 or not 0<maximum<=policy['max_order_lamports'] or flag!=0:raise SigningError('BUY_LIMIT')
            elif spec['name']=='buy_exact_quote_in_v3':
                if len(data)!=25:raise SigningError('BUY_LAYOUT')
                spend,minimum,flag=struct.unpack('<QQB',data[8:])
                if not 0<spend<=policy['max_order_lamports'] or minimum<=0 or flag!=0:raise SigningError('BUY_LIMIT')
            else:
                if len(data)!=24:raise SigningError('SELL_LAYOUT')
                amount,minimum=struct.unpack('<QQ',data[8:])
                if not 0<amount<=int(policy.get('max_sell_tokens',0)) or minimum<=0:raise SigningError('SELL_LIMIT')
            swap=named
        elif program=='11111111111111111111111111111111':
            if len(data)!=12 or struct.unpack('<I',data[:4])[0]!=2 or len(accounts)!=2:raise SigningError('SYSTEM_INSTRUCTION_NOT_ALLOWED')
            if accounts[0]!=public or accounts[1] not in policy.get('tip_accounts',[]):raise SigningError('TRANSFER_DESTINATION_NOT_ALLOWED')
            tip_total+=struct.unpack('<Q',data[4:])[0]
        elif program==COMPUTE:
            if data[:1]==b'\x02' and len(data)==5 and cu_limit is None:cu_limit=struct.unpack('<I',data[1:])[0]
            elif data[:1]==b'\x03' and len(data)==9 and cu_price is None:cu_price=struct.unpack('<Q',data[1:])[0]
            else:raise SigningError('COMPUTE_INSTRUCTION_NOT_ALLOWED')
        elif program==ATA:
            if data!=b'\x01' or len(accounts)!=6 or accounts[0]!=public or accounts[2]!=public or accounts[3] not in policy.get('allowed_mints',[]) or accounts[5] not in TOKEN_PROGRAMS:raise SigningError('ATA_INSTRUCTION_NOT_ALLOWED')
            import local_simulation as lab
            if accounts[1]!=lab.ata(public,accounts[3],accounts[5]) or accounts[4]!=lab.SYSTEM:raise SigningError('ATA_ADDRESS_MISMATCH')
        else:raise SigningError('PROGRAM_NOT_ALLOWED')
    if swap is None:raise SigningError('SWAP_REQUIRED')
    if tip_total>policy.get('max_tip_lamports',0):raise SigningError('TIP_CAP')
    if cu_limit is None or not 1<=cu_limit<=policy.get('max_compute_units',200000):raise SigningError('COMPUTE_LIMIT')
    price=cu_price or 0
    if (cu_limit*price+999999)//1000000>policy.get('max_priority_lamports',0):raise SigningError('PRIORITY_FEE_CAP')

def sign_isolated(message_b64,vault_path,policy_path,password):
    # No secrets in args/env/temp files/logs. Ciphertext stays outside exports.
    request=dict(message=message_b64,vault=str(vault_path),policy=str(policy_path),password=password)
    p=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker'],input=json.dumps(request),
                     text=True,capture_output=True,timeout=20,cwd=Path(__file__).parent)
    if p.returncode!=0:raise SigningError('ISOLATED_SIGNER_FAILED')
    try:
        data=json.loads(p.stdout)
        if not data.get('ok'):raise ValueError()
        return data['transaction']
    except Exception:raise SigningError('ISOLATED_SIGNER_FAILED') from None

def self_test_isolated(vault_path,password):
    request=dict(mode='offline_selftest',vault=str(vault_path),password=password)
    p=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker'],input=json.dumps(request),
                     text=True,capture_output=True,timeout=20,cwd=Path(__file__).parent)
    if p.returncode!=0:raise SigningError('OFFLINE_WALLET_CHECK_FAILED')
    data=json.loads(p.stdout)
    if not data.get('ok'):raise SigningError('OFFLINE_WALLET_CHECK_FAILED')
    tx=VersionedTransaction.from_bytes(base64.b64decode(data['transaction'],validate=True))
    tx.verify_and_hash_message()
    from solders.hash import Hash
    if tx.message.recent_blockhash!=Hash.default():raise SigningError('INVALID_SELFTEST_BLOCKHASH')
    return dict(ok=True,public_key=str(tx.message.account_keys[0]),signature=str(tx.signatures[0]),
                broadcast=False,blockhash='INVALID_FOR_NETWORK_ZERO_HASH',signature_verified=True)

def worker(request):
    from solders.message import from_bytes_versioned
    from wallet_manager import assert_private
    if request.get('mode')=='offline_selftest':
        # Construct internally: caller cannot use the test path to sign a live
        # transaction. The zero blockhash cannot land on Solana.
        from solders.hash import Hash
        from solders.message import MessageV0
        from solders.instruction import Instruction
        key=unlock(request['vault'],request['password'])
        memo=Pubkey.from_string('MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr')
        message=MessageV0.try_compile(key.pubkey(),[Instruction(memo,b'PUMPFUN_OFFLINE_SELFTEST_V1',[])],[],Hash.default())
        tx=VersionedTransaction(message,[key]);tx.verify_and_hash_message()
        return dict(ok=True,transaction=base64.b64encode(bytes(tx)).decode())
    assert_private(request['policy'])
    policy=json.loads(Path(request['policy']).read_text())
    key=unlock(request['vault'],request['password'])
    message=from_bytes_versioned(base64.b64decode(request['message'],validate=True))
    validate_message(message,str(key.pubkey()),policy)
    tx=VersionedTransaction(message,[key]);tx.verify_and_hash_message()
    if len(bytes(tx))>1232:raise SigningError('TRANSACTION_TOO_LARGE')
    return dict(ok=True,transaction=base64.b64encode(bytes(tx)).decode(),signature=str(tx.signatures[0]))

if __name__=='__main__':
    if sys.argv[1:]!=['--worker']:raise SystemExit('Use execution_lab.py for local setup and offline tests.')
    try:
        raw=sys.stdin.read(128*1024+1)
        if len(raw)>128*1024:raise SigningError('REQUEST_TOO_LARGE')
        print(json.dumps(worker(json.loads(raw))))
    except Exception:
        print(json.dumps(dict(ok=False,error='SIGNING_REJECTED')));raise SystemExit(1)
