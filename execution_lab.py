"""Offline execution lab CLI. No command here broadcasts a transaction."""
import json,sys,os,struct,base64
from pathlib import Path
APP=Path(__file__).resolve().parent

def build_message(names,side,amount,minimum,blockhash,compute_units=200000,priority_micro_lamports=0,tip_account=None,tip=0):
    from solders.message import MessageV0
    from solders.hash import Hash
    from solders.instruction import Instruction,AccountMeta
    from solders.compute_budget import set_compute_unit_limit,set_compute_unit_price
    from solders.system_program import transfer,TransferParams
    import local_simulation as lab
    from wallet_signer import ATA
    if side not in ('BUY','SELL') or not 0<amount<2**64 or not 0<minimum<2**64:raise ValueError('INVALID_SWAP_PLAN')
    instructions=[set_compute_unit_limit(compute_units),set_compute_unit_price(priority_micro_lamports)]
    wallet=names['user']
    if side=='BUY':
        instructions.append(Instruction(lab.key(ATA),b'\x01',[
          AccountMeta(lab.key(wallet),True,True),AccountMeta(lab.key(names['associated_base_user']),False,True),
          AccountMeta(lab.key(wallet),False,False),AccountMeta(lab.key(names['base_mint']),False,False),
          AccountMeta(lab.key(lab.SYSTEM),False,False),AccountMeta(lab.key(names['base_token_program']),False,False)]))
    idl=json.loads(lab.IDL.read_text());name='buy_exact_quote_in_v3' if side=='BUY' else 'sell_v3'
    spec=next(x for x in idl['instructions'] if x['name']==name)
    data=struct.pack('<QQB',amount,minimum,0) if side=='BUY' else struct.pack('<QQ',amount,minimum)
    metas=[AccountMeta(lab.key(names[x['name']]),x.get('signer',False),x.get('writable',False)) for x in spec['accounts']]
    instructions.append(Instruction(lab.key(lab.v.PUMP),bytes(spec['discriminator'])+data,metas))
    if tip:
        if not tip_account or tip<1000:raise ValueError('INVALID_TIP')
        instructions.append(transfer(TransferParams(from_pubkey=lab.key(wallet),to_pubkey=lab.key(tip_account),lamports=tip)))
    return MessageV0.try_compile(lab.key(wallet),instructions,[],Hash.from_string(blockhash))

def status():
    import config,live_execution
    import wallet_signer
    vault=wallet_signer.PRIVATE/'wallet.enc.json'
    p=live_execution.Store.wallet_path(json.loads(vault.read_text())['public_key']) if vault.exists() else None
    report=dict(mode='PAPER',execution_adapter='OFFLINE_TESTED_LAB',main_strategy_live_routing=False,
      key_storage='ENCRYPTED_EXTERNAL_VAULT',send_rpc=True,send_jito=True,
      mainnet_execution_verified=False,jito_phase3=dict(available=True,default_percentile=75,tip_in_swap=True,two_slots='OBSERVATION_ONLY',retry='EXPLICIT_NOT_FORWARDED_ONLY',broadcast_from_paper=False),direct_phase2=dict(instructions=['buy_v2','sell_v2'],quote_pair='SOL_ONLY_ORDINARY_CURVE',volume_rent='RPC_137_BYTES',confirmation='FINALIZED_BALANCE_RECONCILIATION',broadcast_from_paper=False),unresolved={},risk=[],recent=[])
    if p is not None and p.exists():
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=.1)) as c:
            c.row_factory=sqlite3.Row
            report['unresolved']=dict(c.execute('SELECT state,COUNT(*) FROM intents GROUP BY state'))
            report['risk']=[dict(r) for r in c.execute('SELECT * FROM risk ORDER BY day DESC LIMIT 7')]
            report['recent']=[dict(r) for r in c.execute('SELECT id,mint,side,state,signature,slot,bundle_id FROM intents ORDER BY created_ts DESC LIMIT 50')]
            import jito_execution
            report['jito_landing']=jito_execution.summary_connection(c)
    return report

def export(folder):
    import sqlite3,zipfile,tempfile,live_execution,wallet_signer
    from contextlib import closing
    vault=wallet_signer.PRIVATE/'wallet.enc.json'
    p=live_execution.Store.wallet_path(json.loads(vault.read_text())['public_key']) if vault.exists() else None
    with zipfile.ZipFile(Path(folder)/'execution_journal.zip','w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('readiness.json',json.dumps(status(),ensure_ascii=False))
        if p is not None and p.exists():
            with tempfile.TemporaryDirectory() as td:
                dest=Path(td)/'execution_journal.db'
                with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(dest)) as out:
                    src.backup(out)
                    if out.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('EXECUTION_BACKUP_INVALID')
                z.write(dest,dest.name)

def main():
    import wallet_signer as signer
    command=sys.argv[1] if len(sys.argv)>1 else 'status'
    if command=='status':print(json.dumps(status(),ensure_ascii=False,indent=2));return 0
    if command=='check':
        import subprocess
        return subprocess.call([sys.executable,'-m','pytest','-q','tests/test_execution_safety_v52.py','tests/test_wallet_rpc_phase1.py'],cwd=APP)
    if command=='kill':
        signer.PRIVATE.mkdir(parents=True,exist_ok=True)
        (signer.PRIVATE/'KILL_SWITCH').write_text('BLOCK_ALL_BROADCASTS\n')
        print('Kill switch set. Existing signatures must still be monitored; PAPER is unchanged.');return 0
    if command=='wallet-check':
        import getpass
        result=signer.self_test_isolated(signer.PRIVATE/'wallet.enc.json',getpass.getpass('Vault password (local only): '))
        path=APP/'data/phase1_wallet_check.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2))
        print('Offline VersionedTransaction signature verified. No RPC and no broadcast. Public key:',result['public_key']);return 0
    if command=='rpc-probe':
        if len(sys.argv)!=3:raise RuntimeError('Provide a local protected RPC settings JSON path')
        from wallet_manager import assert_private
        import asyncio,rpc_client
        path=Path(sys.argv[2]).resolve();assert_private(path);settings=json.loads(path.read_text())
        async def probe():
            async with rpc_client.RobustRPC(settings['endpoints'],expected_genesis=settings['expected_genesis']) as client:
                slot=await client.call('getSlot',[{'commitment':'confirmed'}])
                return dict(status='READ_ONLY_PROBE_OK',slot=slot,client=client.summary(),broadcast=False)
        result=asyncio.run(probe())
        target=APP/'data/phase1_rpc_probe.json';target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(result,indent=2))
        print('Read-only RPC probe passed. No broadcast. Slot:',result['slot']);return 0
    if command=='wallet-init':
        import getpass
        from solders.keypair import Keypair
        p=signer.PRIVATE/'wallet.enc.json'
        if p.exists() or (signer.PRIVATE/'signer_policy.json').exists():raise RuntimeError('PRIVATE_WALLET_ALREADY_EXISTS')
        secret=getpass.getpass('Wallet base58 secret (local terminal only, never paste into chat): ')
        password=getpass.getpass('New vault password (at least 12 characters): ')
        if password!=getpass.getpass('Repeat vault password: '):raise RuntimeError('PASSWORD_MISMATCH')
        public=signer.create_vault(p,Keypair.from_base58_string(secret),password)
        # Disabled signer policy; allowed mints and risk budgets require an
        # explicit local configuration before any isolated signing is possible.
        policy=dict(enabled=False,allowed_mints=[],max_order_lamports=1000000,
          max_sell_tokens='0',max_compute_units=200000,max_priority_lamports=10000,
          max_tip_lamports=10000,tip_accounts=[])
        path=signer.PRIVATE/'signer_policy.json'
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump(policy,f,indent=2)
        __import__('wallet_manager').restrict_access(path)
        print('Encrypted wallet created. Public key:',public,'Signer disabled; PAPER unchanged.');return 0
    print('Commands: status, check, kill, wallet-init, wallet-check, rpc-probe. No live trading command in this lab.');return 1

if __name__=='__main__':
    os.chdir(APP)
    try:raise SystemExit(main())
    except Exception as exc:print('FAILED:',type(exc).__name__);raise SystemExit(1)
