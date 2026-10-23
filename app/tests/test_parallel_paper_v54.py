import json,time,sqlite3
from pathlib import Path
import pytest
import config,main,database as db,paper_capital as cap,paper_branch as branch,primary_integrity
from test_primary_integrity import fresh

def opened(c):
    assert main.buy('m',1)[0]
    tid=main.P['m']['trade_id'];main.S.setdefault('m',main.state())
    cap.record(c,tid,'BUY',main.P['m']['size'],{'pool_fee_rate':.0125})
    config.EXECUTION_MODEL_ENABLED=True
    c.executescript(branch.SCHEMA)
    c.execute("INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,size,reason,trade_id) VALUES('exit','m','SELL','PENDING',?,1,3,'MAX_HOLD_WATCHDOG',?)",(time.time()-10,tid))
    return tid

def account():return dict(quote_received_lamports=40000000,fx='100',receipt_units='3.7')

def test_amm_settlement_closes_slot_and_records_actual_cost_once(fresh,monkeypatch):
    tid=opened(fresh);before=cap.cash(fresh)
    monkeypatch.setattr(config,'MAX_POSITIONS',1)
    import cost_evidence as ce
    next_state=main.state();next_state['cost_evidence']=ce.metadata(dict(signature='next-sig',timestamp=(time.time()-.05)*1000,pool='pump',quoteMint=ce.SOL,poolFeeRate=.0125),'pumpapi_free','provider_price',time.time(),time.monotonic())
    assert main.buy('next',1,s=next_state)==(False,'NO_SLOT')
    assert branch.settle(main,dict(trade_id=tid,mint='m'),account(),'check',time.time(),{})
    assert 'm' not in main.P and not fresh.execute('SELECT 1 FROM positions').fetchone()
    assert cap.cash(fresh)==pytest.approx(before+3.7)
    r=cap.summary(fresh);assert r['status']=='OK',r['errors']
    assert primary_integrity.summarize(fresh)['status']=='OK'
    assert not branch.settle(main,dict(trade_id=tid,mint='m'),account(),'check',time.time(),{})
    assert fresh.execute("SELECT note FROM paper_orders_v0720 WHERE order_id='exit'").fetchone()[0]=='LOCAL_AMM_VERIFIED_NO_BROADCAST'
    assert main.buy('next',1,s=next_state)[0] # freed slot can be used without touching baseline

def test_rollback_after_close_restores_cash_position_and_order(fresh,monkeypatch):
    tid=opened(fresh);before=cap.cash(fresh)
    original=cap.rebalance;calls=[0]
    def failure(c):
        calls[0]+=1
        if calls[0]==2:raise RuntimeError('write failure')
        original(c)
    monkeypatch.setattr(cap,'rebalance',failure)
    with pytest.raises(RuntimeError):branch.settle(main,dict(trade_id=tid,mint='m'),account(),'check',time.time(),{})
    assert cap.cash(fresh)==before and 'm' in main.P
    assert fresh.execute('SELECT count(*) FROM branch_amm_settlements').fetchone()[0]==0
    assert fresh.execute("SELECT status FROM paper_orders_v0720 WHERE order_id='exit'").fetchone()[0]=='PENDING'

def test_seed_is_persistent_independent_and_never_overwrites_primary(fresh):
    tid=opened(fresh);source=Path(config.DB);before=source.read_bytes()
    target=branch.seed(source)
    before=source.read_bytes()  # Identity registration is the only source metadata write.
    c=sqlite3.connect(target);c.execute('DELETE FROM positions');c.commit();c.close()
    assert branch.seed(source)==target
    assert fresh.execute('SELECT count(*) FROM positions').fetchone()[0]==1
    c=sqlite3.connect(target);assert c.execute('SELECT count(*) FROM positions').fetchone()[0]==0;c.close()
    assert source.read_bytes()==before

def test_local_fanout_is_bounded_and_sequence_exposes_loss():
    import branch_transport,queue
    p=branch_transport.Publisher.__new__(branch_transport.Publisher)
    p.queue=queue.Queue(maxsize=1);p.seq=0;p.dropped=0
    p.emit('m','buy',1,'x','pumpapi_free',{},1)
    p.emit('m','buy',2,'x','pumpapi_free',{},1)
    assert p.dropped==1 and p.seq==2 and p.queue.get()['seq']==1
    p.emit('m','buy',3,'x','pumpapi_free',{},1)
    assert p.queue.get()['seq']==3
