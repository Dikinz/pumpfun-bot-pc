import asyncio,json,copy
import pytest
import run_audit as ra,exit_audit as ea,time_check as tc,paper_capital as pc,main,config
from test_primary_integrity import fresh

def fill(c,tid,side,price,ts):
    size=3.;gross=size*(price-1) if side=='SELL' else 0.
    c.execute('INSERT INTO trades(trade_id,size,ts,mint,side,price,pnl,reason,stop_trigger_price,slippage_gap_pct) VALUES(?,?,?,?,?,?,?,?,?,?)',(tid,size,ts,tid,side,price,gross,'TEST',1.,(price-1)*100))
    pc.record(c,tid,side,size if side=='BUY' else size*price,{'pool_fee_rate':.01})
    if side=='BUY':c.execute('INSERT INTO positions(trade_id,mint,size) VALUES(?,?,?)',(tid,tid,size))
    else:c.execute('DELETE FROM positions WHERE trade_id=?',(tid,))

@pytest.mark.parametrize('state',['PASS','OFFSET_DETECTED','UNCERTAIN','UNAVAILABLE'])
def test_startup_always_fresh_and_records_blocked(tmp_path,monkeypatch,state):
    monkeypatch.setattr(tc,'data_dir',lambda:tmp_path)
    report=dict(schema='device_time_check_v0744',id=state,created_ts=1.,local_time='test',version='test',source='test',windows={'status':'NOT_WINDOWS'},samples=[],reference=tc.assess([]))
    report['reference'].update(status=state,label=state)
    calls=[]
    async def collect():calls.append(1);return report
    monkeypatch.setattr(tc,'collect',collect)
    # Even a saved PASS is never used instead of a new probe.
    tc.atomic_json(tmp_path/'time_check.json',dict(report,reference=dict(report['reference'],status='PASS')))
    if state=='PASS':assert asyncio.run(tc.startup())==report
    else:
        with pytest.raises(RuntimeError,match='START BLOCKED'):asyncio.run(tc.startup())
    assert calls==[1]
    assert json.loads((tmp_path/'startup_attempts'/f'{state}.json').read_text())['status']==('READY' if state=='PASS' else 'BLOCKED_TIME')

def test_runs_frozen_carry_costs_and_clock_rollback(fresh):
    first=ra.begin(fresh,{'reference':{'status':'PASS'}})
    fill(fresh,'carry','BUY',1.,1000)
    ra.beat(fresh,'STOPPED',True)
    old=copy.deepcopy(ra.summary(fresh)['current'])
    second=ra.begin(fresh)
    fill(fresh,'carry','SELL',.2,10) # clock went backwards; sequence IDs still work
    fill(fresh,'new','BUY',1.,11)
    ra.beat(fresh,'STOPPED',True)
    rows=ra.summary(fresh)['rows'];current=rows[0]['result']
    assert rows[1]==old and first!=second
    assert current['entries']==1 and current['closed']==1 and current['carried_closed']==1
    assert current['entered_still_open']==1
    fees=fresh.execute("SELECT SUM(total_cost) FROM capital_ledger_v0721 WHERE trade_id='carry'").fetchone()[0]
    assert current['net_closed']==pytest.approx(-2.4-fees)
    assert current['cash_change']!=current['net_closed']
    assert current['end']['open_principal']==3

def test_interrupted_run_recovers_committed_tail(fresh):
    ra.begin(fresh);fill(fresh,'tail','BUY',1.,1)
    ra.begin(fresh)
    old=ra.summary(fresh)['rows'][1]
    assert old['status']=='INTERRUPTED' and old['result']['entries']==1
    assert old['result']['recovered_after_interruption']

def test_loss_evidence_bounded_costs_and_no_trading_mutations(fresh):
    tid='loss';fill(fresh,tid,'BUY',1.,1)
    q={'trade_id':tid,'size':3.,'entry':1.}
    for i in range(250):ea.event(tid,'sell',1.,'test',{'signature':str(i),'secret_key':'NO_EXPORT'},'FUTURE' if i%2 else 'ACCEPTED')
    ea.signal(fresh,tid,tid,None,q,{'signature':'signal'})
    fill(fresh,tid,'SELL',.2,2)
    before=[tuple(r) for r in fresh.execute('SELECT * FROM capital_ledger_v0721')]
    ea.close(fresh,tid,tid,q,{'signature':'fill'})
    raw=fresh.execute('SELECT * FROM exit_audit_v45').fetchone();d=json.loads(raw['data_json'])
    assert raw['flagged']==1 and d['events_dropped']==50 and len(d['events_before_fill'])==200
    assert d['net_pnl']<-2.4 and d['entry_fill']['price']==1.
    assert d['signal_evidence']['signature']=='signal' and d['fill_evidence']['signature']=='fill'
    assert d['events_before_fill'][-1]['validation']=='FUTURE'
    assert 'secret_key' not in raw['data_json']
    assert tid not in ea.BUFFERS
    assert [tuple(r) for r in fresh.execute('SELECT * FROM capital_ledger_v0721')]==before

def test_startup_gate_precedes_recovery_and_trading(monkeypatch):
    monkeypatch.setattr(config,'JUPITER_API_KEY','test')
    async def fx():main.exit_quotes.FX_STATUS='READY'
    monkeypatch.setattr(main.exit_quotes,'refresh_fx',fx)
    class Sock:
        def bind(self,*a):pass
        def listen(self,*a):pass
    loop=asyncio.new_event_loop()
    monkeypatch.setattr(main.socket,'socket',lambda *a,**k:Sock())
    async def blocked():raise RuntimeError('BLOCKED')
    monkeypatch.setattr(tc,'startup',blocked)
    def forbidden():pytest.fail('database initialization must not precede the fresh time check')
    monkeypatch.setattr(main.db,'init',forbidden)
    # Create event loop before patching socket: asyncio itself uses socketpair.
    try:
        with pytest.raises(RuntimeError,match='BLOCKED'):loop.run_until_complete(main.run())
    finally:loop.close()

from test_execution_model import env

def test_actual_exit_path_captures_rejected_event_and_fill(env):
    c,clock,tick,entry=env
    ra.begin(c)
    assert entry()[0]
    tick(p=1.,dt=.3)
    tid=main.P['a']['trade_id']
    main.sell('a',.9,'TEST_EXIT')
    tick(p=.5,dt=.3,timestamp=(clock['t']+10)*1000)
    assert 'a' in main.P
    tick(p=.2,dt=.3)
    assert 'a' not in main.P
    ra.beat(c,'STOPPED',True)
    row=c.execute('SELECT * FROM exit_audit_v45 WHERE trade_id=?',(tid,)).fetchone()
    d=json.loads(row['data_json'])
    assert row['flagged'] and d['order']['signal_price']==.9 and d['fill']['price']==.2
    assert any(x['validation']!='ACCEPTED' for x in d['events_before_fill'])
    assert ra.summary(c)['current']['result']['closed']==1
