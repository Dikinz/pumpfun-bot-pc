"""One public test wallet; local account lifecycle, never a real balance."""
import json,time,secrets
SCHEMA="""CREATE TABLE IF NOT EXISTS wallet_state_v0730(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS wallet_exits_v0730(order_id TEXT PRIMARY KEY,mint TEXT,ts REAL,status TEXT,token_setup INTEGER,volume_setup INTEGER,local_fee INTEGER,net_receipt INTEGER,result_json TEXT);"""
def state(c):
    rows=dict(c.execute('SELECT k,v FROM wallet_state_v0730'))
    return dict(wallet=rows.get('wallet'),volume=json.loads(rows.get('volume','null')))
def ensure(c):
    if not state(c)['wallet']:
        # Public bytes only; no private key or signer is ever generated or stored.
        from solders.pubkey import Pubkey
        key=Pubkey.from_bytes(secrets.token_bytes(32))
        while not key.is_on_curve():key=Pubkey.from_bytes(secrets.token_bytes(32))
        s=str(key)
        c.execute("INSERT OR IGNORE INTO wallet_state_v0730 VALUES('wallet',?)",(s,))
    return state(c)
def record(c,oid,mint,r,now=None):
    if c.execute('SELECT 1 FROM wallet_exits_v0730 WHERE order_id=?',(oid,)).fetchone():return
    ok=r.get('status')=='LOCAL_SWAP_EXECUTED' and r.get('zero_token_control_ok') is True
    if ok and (r.get('wallet')!=state(c)['wallet'] or not r.get('post_volume_account')):raise ValueError('WALLET_STATE_MISMATCH')
    used=c.execute("SELECT 1 FROM wallet_exits_v0730 WHERE mint=? AND status='LOCAL_SWAP_EXECUTED'",(mint,)).fetchone()
    token=int(r.get('synthetic_token_rent_lamports',0)) if ok and not used else 0
    volume=int(r.get('volume_account_rent_delta_lamports') or 0) if ok else 0
    fee=int(r.get('local_fee_from_balance_diff_lamports') or 0) if ok else 0
    net=int(r['seller_net_sol_delta_lamports'])-token if ok else None
    status=r.get('status','NO_RESULT') if not r.get('status')=='LOCAL_SWAP_EXECUTED' or ok else 'NEGATIVE_CONTROL_FAILED'
    c.execute('INSERT INTO wallet_exits_v0730 VALUES(?,?,?,?,?,?,?,?,?)',(oid,mint,now or time.time(),status,token,volume,fee,net,json.dumps(r)))
    if ok:c.execute("INSERT OR REPLACE INTO wallet_state_v0730 VALUES('volume',?)",(json.dumps(r['post_volume_account']),))
def summary(c):
    rows=[dict(r) for r in c.execute('SELECT order_id,mint,ts,status,token_setup,volume_setup,local_fee,net_receipt FROM wallet_exits_v0730 ORDER BY ts DESC')]
    return dict(wallet=state(c)['wallet'],rows=rows,token_setup_lamports=sum(r['token_setup'] for r in rows),volume_setup_lamports=sum(r['volume_setup'] for r in rows),local_fees_lamports=sum(r['local_fee'] for r in rows),note='Локальная история одного тестового кошелька. Токены и SOL синтетические; покупка не моделируется. Rent блокируется и не возвращается в этом сценарии. Комиссия и volume-rent уже входят в поступление; повторно не вычитать. Ошибки не отправлялись в сеть, сетевые расходы неизвестны.')
