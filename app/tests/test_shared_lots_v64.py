import copy,json,time,sqlite3
from contextlib import contextmanager
from pathlib import Path
import pytest
import shared_lots as shared,main,config,database as db,paper_capital as cap,exit_quotes,execution_model as ex,route_branch
from test_primary_integrity import fresh
from test_route_exit_v59 import candidate,save_check,WALLET_RECEIPT

def entry(c,monkeypatch,dec=6):
    db.setm('matched_mode_v64',1)
    assert main.buy('m',1)[0];tid=main.P['m']['trade_id']
    cap.record(c,tid,'BUY',3,{'pool_fee_rate':.0125})
    monkeypatch.setattr(exit_quotes,'FX',dict(rate='100',ts=time.time(),response={}))
    exit_quotes.on_entry(c,tid,'m',1,3)
    c.execute("INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,size,reason,trade_id,fill_ts,fill_price,fill_signature,wait_ms,price_age_ms,provider_lag_ms) VALUES('buy','m','BUY','FILLED',?,1,3,'ENTRY',?,?,1,'entrysig',300,1,1)",(time.time()-.3,tid,time.time()))
    c.execute("INSERT INTO preentry_v0729(job_id,mint,status,result_json,order_id) VALUES('pre','m','USED',?,'buy')",(json.dumps(dict(amount_decimals=dec)),))
    shared.publish_entry(c,main.P['m'],'buy',time.time()-2)
    return c.execute('SELECT * FROM comparison_lots_v64').fetchone()

@contextmanager
def child(monkeypatch,tmp_path):
    source= db.con()
    with monkeypatch.context() as mp:
        mp.setattr(config,'DB',str(tmp_path/'child.db'));db._LOCAL.conn=None
        mp.setattr(main,'P',{});mp.setattr(main,'S',{});mp.setattr(main,'ENTERED',set())
        db.init();db.setm('branch_role','ROUTE_PAPER');db.setm('matched_mode_v64',1)
        try:yield db.con()
        finally:db.con().close();db._LOCAL.conn=source

def test_shared_snapshot_ignores_child_fx_and_is_idempotent(fresh,monkeypatch,tmp_path):
    row=entry(fresh,monkeypatch);p=json.loads(row['payload']);original=Path(config.DB).read_bytes()
    with child(monkeypatch,tmp_path) as c:
        monkeypatch.setattr(exit_quotes,'FX',dict(rate='999',ts=time.time(),response={}))
        assert shared.apply_entry(main,row)=='APPLIED'
        assert shared.apply_entry(main,row)=='APPLIED'
        inv=dict(c.execute('SELECT * FROM quote_inventory_v0723').fetchone())
        assert inv==p['inventory'] and inv['sol_usdc']=='100'
        assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==1
        assert cap.cash(c)==pytest.approx(26.95)
        assert main.P['m']==p['memory_position']
        assert __import__('primary_integrity').summarize(c)['status']=='OK'
    assert Path(config.DB).read_bytes()==original

@pytest.mark.parametrize('reason',['late','slot','capital','unsized','gap'])
def test_ineligible_branch_never_invents_or_retries_different_entry(fresh,monkeypatch,tmp_path,reason):
    row=dict(entry(fresh,monkeypatch,dec=None if reason=='unsized' else 6))
    with child(monkeypatch,tmp_path) as c:
        if reason=='slot':monkeypatch.setattr(config,'MAX_POSITIONS',0)
        if reason=='capital':c.execute("UPDATE capital_settings_v0721 SET v=1 WHERE k='start'")
        if reason=='gap':db.setm('branch_input_gap',1)
        before=cap.cash(c)
        status=shared.apply_entry(main,row,now=row['created_ts']+2 if reason=='late' else row['created_ts'])
        assert status.startswith('EXCLUDED_')
        assert shared.apply_entry(main,row,now=row['created_ts'])==status
        assert not main.P and cap.cash(c)==before
        assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==0

def test_changed_snapshot_rejected_before_ledger_mutation(fresh,monkeypatch,tmp_path):
    row=dict(entry(fresh,monkeypatch));p=json.loads(row['payload']);p['inventory']['token_units']='999';row['payload']=json.dumps(p)
    with child(monkeypatch,tmp_path) as c:
        with pytest.raises(RuntimeError,match='PAYLOAD_MISMATCH'):shared.apply_entry(main,row)
        assert cap.cash(c)==30 and not main.P

def test_source_rollback_does_not_publish_a_comparison_entry(fresh,monkeypatch):
    with pytest.raises(ValueError):
        with db.atomic():entry(fresh,monkeypatch);raise ValueError()
    assert fresh.execute('SELECT COUNT(*) FROM comparison_lots_v64').fetchone()[0]==0

def test_same_sell_signal_origin_and_verified_exit_release_slot_once(fresh,monkeypatch,tmp_path):
    row=entry(fresh,monkeypatch);primary=config.DB;tid=row['trade_id']
    origin=time.monotonic()-.4
    oid=ex.request(fresh,'m','SELL',1,3,'TP2',dict(signature='sell-signal',provider_ts=time.time()-.1),trade_id=tid)
    shared.publish_signal(fresh,tid,oid,origin,dict(signature='sell-signal'))
    with child(monkeypatch,tmp_path) as c:
        c.executescript(route_branch.SCHEMA);shared.sync(main,primary)
        order=c.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL'").fetchone()
        assert order['order_id']==oid and order['reason']=='TP2' and ex.ORIGINS[oid]==origin
        assert c.execute('SELECT signal_status FROM comparison_receipts_v64').fetchone()[0]=='SHARED_SIGNAL'
        before=cap.cash(c)
        r,j,result,inv=candidate(tid);j['scheduled_ts']=time.time();r['started_ts']=time.time();result['snapshot_received_ts']=time.time();r['finished_ts']=time.time()
        # Use the common inventory amount, not the fixture's independently sized lot.
        inv=dict(c.execute('SELECT * FROM quote_inventory_v0723').fetchone());j['inventory']=dict(inv)
        for key in ('raw_amount','tokens_debited','pool_tokens_credited'):result['sale'][key]=inv['raw_amount']
        save_check(c,r,j,result,inv);monkeypatch.setattr(config,'EXECUTION_MODEL_ENABLED',True)
        route_branch.poll(main);route_branch.poll(main);shared.sync(main,primary)
        assert not main.P and c.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==1
        assert cap.cash(c)==pytest.approx(before+WALLET_RECEIPT)
        assert __import__('primary_integrity').summarize(c)['status']=='OK'

def test_branch_cannot_create_independent_entries_or_sell_signals(monkeypatch):
    class Model:
        buy=lambda *a:None
        sell=lambda *a,**kw:'filled'
    model=Model();shared.configure(model)
    assert model.buy('m',1)==(False,'MATCHED_PRIMARY_ENTRY_ONLY')
    assert model.sell('m',1,'TP2') is None
    assert model.sell('m',1,'TP2',_fill=True)=='filled'

def test_two_real_workers_receive_one_commit_bound_lot(fresh,monkeypatch,tmp_path):
    import subprocess,sys,branch_transport as transport,paper_branch,candidate_lab
    db.setm('matched_mode_v64',1);primary=config.DB;real=subprocess.Popen
    script='''import sys,asyncio,json,time,config
for k,v in json.loads(sys.argv[5]).items():setattr(config,k,v)
import paper_branch,route_branch,exit_quotes,subprocess
async def fx():
    exit_quotes.FX_STATUS='READY';exit_quotes.FX=dict(rate='999',ts=time.time(),response={})
async def idle():
    while True:await asyncio.sleep(1)
exit_quotes.refresh_fx=fx;exit_quotes.worker=idle
class Monitor:
    stdin=type('Pipe',(),{'close':lambda self:None})()
    def __init__(self,*a,**kw):pass
    def poll(self):return None
    def wait(self,*a,**kw):return 0
subprocess.Popen=Monitor
engine=route_branch if sys.argv[1]=='route_branch' else None
asyncio.run(paper_branch.worker(sys.argv[2],sys.argv[3],sys.argv[4],scenario=engine))
'''
    def spawn(cmd,**kw):
        if len(cmd)>2 and cmd[1] in ('paper_branch.py','route_branch.py') and cmd[2]=='--worker':
            values=json.dumps({k:getattr(config,k) for k in candidate_lab.PARAMETERS})
            return real([sys.executable,'-c',script,cmd[1][:-3],*cmd[3:],values],**kw)
        return real(cmd,**kw)
    monkeypatch.setattr(transport.subprocess,'Popen',spawn)
    fanout=transport.Fanout(primary)
    try:
        row=entry(fresh,monkeypatch);payload=json.loads(row['payload']);deadline=time.monotonic()+5
        got=[]
        while time.monotonic()<deadline:
            got=[]
            for engine in (paper_branch,route_branch):
                with sqlite3.connect(engine.path(primary)) as c:
                    c.row_factory=sqlite3.Row
                    inv=c.execute('SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(row['trade_id'],)).fetchone()
                    got.append(dict(inv) if inv else None)
            if all(got):break
            time.sleep(.05)
        assert got==[payload['inventory'],payload['inventory']]
        for engine in (paper_branch,route_branch):
            with sqlite3.connect(engine.path(primary)) as c:
                assert c.execute('SELECT status,lot_hash FROM comparison_receipts_v64').fetchone()==('APPLIED',row['lot_hash'])
                assert c.execute("SELECT COUNT(*) FROM trades WHERE side='BUY'").fetchone()[0]==1
        out=tmp_path/'comparison';out.mkdir()
        report=shared.export(primary,out)
        assert len(report)==2 and all(x['identical_entry_lot'] for x in report)
        assert not any(x['eligible_pair'] for x in report) # open pairs are not completed results
    finally:
        statuses=fanout.close()
    assert all(s['status']=='STOPPED' and not s['forced'] for s in statuses)
