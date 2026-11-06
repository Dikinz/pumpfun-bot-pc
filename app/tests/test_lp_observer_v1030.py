"""v0.7.103.0 LP recorder: observation only, own database, frozen pool selection, never blocks PRIMARY. No network."""
import base64,json,math,queue,sqlite3,struct,time,zlib
from contextlib import closing
from pathlib import Path
import pytest
import config,lp_observer as lp

ROOT=Path(__file__).parents[1]
POOLS={v:['%s%s'%(v[:4].upper(),str(i)*40)[:44] for i in range(6)] for v in lp.VENUES}

@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket,'connect',lambda *x:(_ for _ in ()).throw(AssertionError('No network in LP recorder tests')))
    yield
    lp.stop()

def msg(venue,pool,action='buy',mint='MINT',quote=lp.SOL,**extra):
    return dict(pool=venue,poolId=pool,action=action,mint=mint,quoteMint=quote,tokenAmount=1.0,quoteAmount=2.0,tokensInPool=10.,quoteInPool=20.,
        price=2.0,poolFeeRate=.003,priorityFee=1e-5,block=123,timestamp=1793899073000,signature='S'*88,decimals=6,binStep=10,**extra)

def drain(q):
    out=[]
    while True:
        try:out.append(q.get_nowait())
        except queue.Empty:return out

def test_tap_ignores_other_venues_and_counts_candidates_before_freeze():
    q=queue.Queue();t=lp.Tap(q)
    t.message(msg('pump-amm','X'*44),0.)
    for i in range(3):t.message(msg('orca-whirlpool',POOLS['orca-whirlpool'][0]),float(i))
    t.message(msg('orca-whirlpool',POOLS['orca-whirlpool'][0],'add'),3.)
    t.message(msg('orca-whirlpool',POOLS['orca-whirlpool'][0]),lp.COUNT_FLUSH_S+1)
    items=drain(q)
    assert t.stats['venue_messages']==5 and items[0][0]=='COUNTS' and items[0][3]=={('orca-whirlpool',POOLS['orca-whirlpool'][0]):3}
    assert items[0][4][('orca-whirlpool',POOLS['orca-whirlpool'][0])]['binStep']==10

def test_tap_after_freeze_records_only_selected_pools_all_actions_and_never_blocks():
    q=queue.Queue(maxsize=2);t=lp.Tap(q);sel=POOLS['meteora-dlmm'][0];t.selected=frozenset([sel])
    t.message(msg('meteora-dlmm',sel,'add'),1.);t.message(msg('meteora-dlmm',POOLS['meteora-dlmm'][1]),1.)
    t.message(msg('meteora-dlmm',sel,'sell'),2.);t.message(msg('meteora-dlmm',sel),3.)
    items=drain(q)
    assert [i[3]['action'] for i in items]==['add','sell'] and t.stats['dropped']==1
    assert items[0][3]['block']==123 and items[0][3]['sig16']=='S'*16 and items[0][3]['pool_fee_rate']==.003
    class Broken(dict):
        def get(self,*a):raise RuntimeError('bad')
    t.message(Broken(),4.);assert t.stats['errors']==1

def test_selection_rule_is_the_preregistered_one():
    rows=[]
    for venue in lp.VENUES:
        for i,pool in enumerate(POOLS[venue]):rows.append(dict(venue=venue,pool_id=pool,mint='M%d'%i,quote_mint=lp.SOL,swaps=1000-i*100,sample_json='{}'))
    rows.append(dict(venue='orca-whirlpool',pool_id='STABLE'*7,mint=lp.USDC,quote_mint=lp.USDC,swaps=5000,sample_json='{}'))
    rows.append(dict(venue='orca-whirlpool',pool_id='OTHERQ'*7,mint='M',quote_mint='BONK',swaps=5000,sample_json='{}'))
    rows.append(dict(venue='raydium-clmm',pool_id='HEAVY'*8,mint='M',quote_mint=lp.USDC,swaps=int(lp.MAX_SWAPS_PER_HOUR*2*2+1),sample_json='{}'))
    chosen=lp.choose(rows,2.)
    assert len(chosen)==12 and {c['venue'] for c in chosen}==set(lp.VENUES)
    assert all(c['pool_id'] in POOLS[c['venue']][:4] for c in chosen) and [c['rank'] for c in chosen if c['venue']=='orca-whirlpool']==[1,2,3,4]

def test_recorder_accumulates_candidates_and_freezes_once(tmp_path):
    now=[1000.];primary=tmp_path/'clean_0762.db';o=lp.Observer(primary,.3,clock=lambda:now[0]);rec=o.recorder
    with closing(lp.connect(primary)) as c,c:
        for venue in lp.VENUES:
            rec.apply(c,('COUNTS',0.,60.,{(venue,p):100-i for i,p in enumerate(POOLS[venue])},{(venue,p):dict(mint='M',quoteMint=lp.SOL,symbol='T') for p in POOLS[venue]}))
        rec.apply(c,('COUNTS',60.,120.,{('orca-whirlpool',POOLS['orca-whirlpool'][5]):500},{}))
        rec.maybe_freeze(c);assert o.tap.selected is None
        now[0]=o.started_ts+lp.SELECT_S;rec.maybe_freeze(c)
        assert POOLS['orca-whirlpool'][5] in o.tap.selected and len(o.tap.selected)==12
    o2=lp.Observer(primary,.3)
    assert o2.tap.selected==o.tap.selected and not lp.path(primary)==primary and not primary.exists()

def test_dlmm_calibration_expected_offset_scan_and_bin_array_address():
    raw=bytearray(200);struct.pack_into('<i',raw,lp.DLMM_ACTIVE_ID_OFFSET,-500)
    price=lp.dlmm_price(-500,10,9,6)
    hit=lp.calibrate_dlmm(bytes(raw),10,9,6,price)
    assert hit['offset']==lp.DLMM_ACTIVE_ID_OFFSET and hit['orientation']=='BASE_IS_X' and hit['method']=='EXPECTED_OFFSET'
    assert lp.calibrate_dlmm(bytes(raw),10,6,9,1/price)['orientation']=='BASE_IS_Y'
    moved=bytearray(200);struct.pack_into('<i',moved,120,-500)
    assert lp.calibrate_dlmm(bytes(moved),10,9,6,price)['method']=='UNIQUE_SCAN'
    assert lp.calibrate_dlmm(bytes(moved),10,9,6,price*1.5) is None and lp.calibrate_dlmm(bytes(raw),10,9,6,None) is None
    a=lp.bin_array_address(POOLS['meteora-dlmm'][0].replace('METE','1111')[:44] if False else '11111111111111111111111111111111',-8)
    assert isinstance(a,str) and 32<=len(a)<=44

def account(raw,owner='OWNER'):
    return dict(data=[base64.b64encode(raw).decode(),'base64'],owner=owner,lamports=1,executable=False)

def test_prober_snapshots_calibrates_and_reads_bin_arrays(tmp_path):
    now=[5000.];primary=tmp_path/'clean_0762.db';o=lp.Observer(primary,.3,clock=lambda:now[0])
    dl='11111111111111111111111111111111';orca=POOLS['orca-whirlpool'][0]
    raw=bytearray(200);struct.pack_into('<i',raw,lp.DLMM_ACTIVE_ID_OFFSET,-500);price=lp.dlmm_price(-500,10,9,9)
    reads=[]
    def read(addresses):
        reads.append(list(addresses))
        return dict(slot=7,received_ts=now[0],accounts={a:(account(bytes(raw)) if a==dl else account(b'pool') if a==orca else account(b'bins',lp.DLMM_PROGRAM)) for a in addresses})
    p=lp.Prober(o,clock=lambda:now[0],read=read,rpc=lambda body:dict(result=[]))
    with closing(lp.connect(primary)) as c:
        with c:
            for pool,venue in ((dl,'meteora-dlmm'),(orca,'orca-whirlpool')):
                c.execute('INSERT INTO pools_v1(pool_id,venue,mint,quote_mint,rank,swaps_per_hour,selected_ts,sample_json) VALUES(?,?,?,?,?,?,?,?)',
                    (pool,venue,'M',lp.SOL,1,10,1,json.dumps(dict(binStep=10,decimals=9))))
            c.execute("INSERT INTO tape_v1(pool_id,ts,action,price) VALUES(?,?,'buy',?)",(dl,4999,price))
        assert p.step(c)
        assert reads[0]==[dl,orca]
        rows=c.execute("SELECT pool_id,kind,slot,data_zlib FROM snapshots_v1").fetchall()
        assert {r[0] for r in rows}=={dl,orca} and zlib.decompress([r for r in rows if r[0]==dl][0][3])==bytes(raw)
        assert tuple(c.execute('SELECT dlmm_offset,dlmm_orientation FROM pools_v1 WHERE pool_id=?',(dl,)).fetchone())==(lp.DLMM_ACTIVE_ID_OFFSET,'BASE_IS_X')
        assert not p.step(c)                      # paced: one RPC job per 1/rate seconds
        now[0]+=1/.3+.01;p.step(c)                # next job: bin arrays around active bin -500 -> arrays -9..-7
        assert len(reads[-1])==3 and reads[-1][1]==lp.bin_array_address(dl,-500//70)
        assert c.execute("SELECT COUNT(*) FROM snapshots_v1 WHERE kind='BIN_ARRAY'").fetchone()[0]==3

def test_completeness_audit_checks_previous_cycle_and_classifies_a_miss(tmp_path):
    now=[5000.];primary=tmp_path/'clean_0762.db';o=lp.Observer(primary,.3,clock=lambda:now[0]);pool='11111111111111111111111111111111'
    calls=[]
    batches=[[dict(signature='A'*88,slot=10,err=None),dict(signature='B'*88,slot=9,err=None),dict(signature='C'*88,slot=8,err={'x':1})],[dict(signature='D'*88,slot=11,err=None)]]
    def rpc(body):
        calls.append(body)
        if body['method']=='getSignaturesForAddress':return dict(result=batches.pop(0) if batches else [])
        return dict(result=dict(meta=dict(preTokenBalances=[dict(accountIndex=1,mint='M',owner=pool,uiTokenAmount=dict(amount='5'))],
            postTokenBalances=[dict(accountIndex=1,mint='M',owner=pool,uiTokenAmount=dict(amount='7'))])))
    p=lp.Prober(o,clock=lambda:now[0],read=lambda a:dict(slot=1,received_ts=now[0],accounts={}),rpc=rpc)
    with closing(lp.connect(primary)) as c:
        pools=[dict(pool_id=pool,venue='orca-whirlpool')]
        p.do_sigs(c,pools,now[0])
        with c:c.execute("INSERT INTO tape_v1(pool_id,ts,action,sig16) VALUES(?,?,'buy',?)",(pool,now[0],'A'*16))
        now[0]+=lp.SIGNATURES_S;p.do_sigs(c,pools,now[0])
        audits=c.execute('SELECT checked,missing,truncated,status FROM audit_v1 ORDER BY id').fetchall()
        assert [tuple(a) for a in audits]==[(0,0,1,'BASELINE'),(2,1,1,'OK')]   # the first window is truncated by definition
        assert calls[1]['params'][1]['until']=='A'*88 and calls[2]['method']=='getTransaction' and calls[2]['params'][0]=='B'*88
        assert tuple(c.execute('SELECT signature,status FROM verify_v1').fetchone())==('B'*88,'STATE_CHANGING')

def test_start_stop_settings_and_isolated_database(tmp_path,monkeypatch):
    primary=tmp_path/'clean_0762.db'
    monkeypatch.setattr(config,'LP_OBSERVER_MODE','record');monkeypatch.setattr(config,'LP_OBSERVER_RPC_RATE',.3)
    o=lp.start(primary);assert o is not None and lp.TAP is o.tap
    lp.tap(msg('raydium-clmm',POOLS['raydium-clmm'][0]),time.time())
    final=lp.stop();assert final['state']=='STOPPED' and final['threads_alive']==[] and lp.TAP is None
    assert lp.path(primary).name=='clean_0762_lp_observer.db' and not primary.exists()
    s=lp.summary(primary);assert s['state']=='STOPPED' and s['pools']==[]
    out=tmp_path/'r';out.mkdir();assert lp.export(primary,out)['schema']=='LP_RECORDER_V1' and (out/'lp_observer_summary.json').exists()
    events=[];monkeypatch.setattr(__import__('system_health'),'event',lambda *a,**k:events.append(a[1]))
    monkeypatch.setattr(config,'LP_OBSERVER_RPC_RATE',5.);assert lp.start(primary) is None and events[-1]=='LP_OBSERVER_CONFIG_REJECTED'
    monkeypatch.setattr(config,'LP_OBSERVER_RPC_RATE',.3);monkeypatch.setattr(config,'LP_OBSERVER_MODE','off');assert lp.start(primary) is None

def test_wiring_defaults_and_dashboard():
    src=(ROOT/'main.py').read_text(encoding='utf-8')
    tap=src.index('lp_observer.tap(x,now)');assert src.index('amm_observer.tap(x,now)')<tap<src.index('if action not in ("buy","sell"):continue',tap)
    run=src[src.index('async def run():'):];assert 'lp_observer.start,config.DB' in run and 'lp_observer.stop' in run
    assert config.LP_OBSERVER_MODE=='record' and config.AMM_OBSERVER_MODE=='counts' and 0<config.LP_OBSERVER_RPC_RATE<=1
    assert "lp_observer').summary(config.DB)" in (ROOT/'dashboard/server.py').read_text(encoding='utf-8')
    assert "lp_observer').export(config.DB,folder)" in (ROOT/'launcher.py').read_text(encoding='utf-8')
    assert 'id="lpObserver1030"' in (ROOT/'dashboard/details.html').read_text(encoding='utf-8')
