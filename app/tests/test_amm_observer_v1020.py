"""v0.7.102.0 AMM observer: observation only, own database, never blocks or stops PRIMARY. No network."""
import json,queue,sqlite3,time,zipfile
from contextlib import closing
from pathlib import Path
import pytest
import config,amm_observer as obs

ROOT=Path(__file__).parents[1]

@pytest.fixture(autouse=True)
def observer_isolation(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket,'connect',lambda *x:(_ for _ in ()).throw(AssertionError('No network in observer tests')))
    yield
    obs.stop()

@pytest.fixture
def bundle():
    with zipfile.ZipFile(ROOT/'research/amm_fixture_v36.zip') as z:return json.loads(z.read('snapshot.json'))

def reader(bundle,calls=None):
    accounts=bundle['snapshot']['accounts']
    def read(addresses,min_slot=None,**kw):
        if calls is not None:calls.append(list(addresses))
        return dict(slot=bundle['snapshot']['slot'],started_ts=1.,received_ts=2.,accounts={a:accounts.get(a) for a in addresses})
    return read

def msg(mint,pool,action='buy',price=1e-7,**extra):
    return dict(mint=mint,pool=pool,action=action,price=price,txSigner='S',signature='sig-'+mint+pool+action,timestamp=1793890000000,**extra)

def drain(q):
    out=[]
    while True:
        try:out.append(q.get_nowait())
        except queue.Empty:return out

def test_migration_needs_a_recent_curve_trade_in_this_session():
    q=queue.Queue();t=obs.Tap(q)
    t.message(msg('A','pump'),0.);t.message(msg('A','pump-amm'),100.);t.message(msg('A','pump-amm','sell'),101.)
    t.message(msg('B','pump-amm'),100.)                              # never seen on the curve
    t.message(msg('C','pump'),0.);t.message(msg('C','pump-amm'),obs.CURVE_WINDOW_S+1)   # curve trade too old
    kinds=[(i[0],i[1]) for i in drain(q) if i[0] in ('MIGRATION','TICK')]
    assert kinds==[('MIGRATION','A'),('TICK','A'),('TICK','A')]
    assert t.stats['migrations']==1 and t.stats['untracked_amm']==2 and t.stats['errors']==0

def test_tap_counts_every_message_and_samples_each_pair_hourly():
    q=queue.Queue();t=obs.Tap(q)
    for i in range(5):t.message(msg('A','pump'),float(i))
    t.message(dict(pool='pump',action='migrate',mint='A'),5.)
    t.message(msg('A','pump'),obs.COUNT_FLUSH_S+1)
    items=drain(q)
    counts=[i for i in items if i[0]=='COUNTS'];samples=[i for i in items if i[0]=='SAMPLE']
    assert len(counts)==1 and counts[0][3]=={('pump','buy'):5,('pump','migrate'):1}
    assert sorted((s[1],s[2]) for s in samples)==[('pump','buy'),('pump','migrate')]
    assert all(len(json.dumps(s[4]))<10000 for s in samples)

def test_tap_never_raises_never_blocks_and_counts_drops():
    class Broken(dict):
        def get(self,*a):raise RuntimeError('bad message')
    t=obs.Tap(queue.Queue(maxsize=1))
    t.message(Broken(),0.)
    assert t.stats['errors']==1
    t.message(msg('A','pump'),0.);t.message(msg('A','pump-amm'),1.);t.message(msg('A','pump-amm'),2.)
    assert t.stats['dropped']>=1 and t.out.qsize()==1

def test_tracking_window_and_capacity(monkeypatch):
    monkeypatch.setattr(obs,'MAX_TRACKED',1)
    q=queue.Queue();t=obs.Tap(q)
    t.message(msg('A','pump'),0.);t.message(msg('B','pump'),0.)
    t.message(msg('A','pump-amm'),1.);t.message(msg('B','pump-amm'),2.)
    assert t.stats['tracked_full']==1 and list(t.tracked)==['A']
    drain(q);t.message(msg('A','pump-amm'),1.+obs.TRACK_S+1)
    assert not [i for i in drain(q) if i[0]=='TICK']

def test_counts_mode_records_no_migrations():
    q=queue.Queue();t=obs.Tap(q,'counts')
    t.message(msg('A','pump'),0.);t.message(msg('A','pump-amm'),1.)
    assert {i[0] for i in drain(q)}<={'SAMPLE','COUNTS'} and t.stats['migrations']==0

def test_recorder_writes_own_database_and_schedules_jobs(tmp_path,monkeypatch):
    monkeypatch.setattr(obs,'MAX_TICKS_PER_MINT',2)
    primary=tmp_path/'clean_0762.db';o=obs.Observer(primary,'observe',.2,True);rec=obs.Recorder(o)
    with closing(obs.connect(primary)) as c,c:
        rec.apply(c,('MIGRATION','A',100.,'pump-amm','buy',90.))
        rec.apply(c,('MIGRATION','A',101.,'pump-amm','buy',90.))   # duplicate: ignored
        for i in range(3):rec.apply(c,('TICK','A',100.+i,'pump-amm','buy',obs.tick_of(msg('A','pump-amm'))))
        rec.apply(c,('COUNTS',0.,60.,{('pump-amm','buy'):3}))
    with closing(sqlite3.connect(obs.path(primary))) as c:
        m=c.execute('SELECT status,ticks,ticks_dropped,first_pool,last_curve_ts FROM migrations_v1').fetchall()
        jobs=c.execute('SELECT kind,offset_s,due_ts,expire_ts FROM jobs_v1 ORDER BY id').fetchall()
        assert m==[('OBSERVED',2,1,'pump-amm',90.)]
        assert [(k,o_) for k,o_,*_ in jobs]==[('CONFIRM',0.)]+[('QUOTE',float(x)) for x in obs.QUOTE_OFFSETS]
        assert jobs[1][2]==100.+obs.QUOTE_OFFSETS[0] and jobs[1][3]==jobs[1][2]+obs.JOB_EXPIRE_S
        assert c.execute('SELECT COUNT(*) FROM ticks_v1').fetchone()[0]==2
        assert c.execute('SELECT pool,action,n FROM stream_counts_v1').fetchall()==[('pump-amm','buy',3)]
    assert obs.path(primary).name=='clean_0762_amm_observer.db' and not primary.exists()

def test_confirm_reads_canonical_pool_fees_and_reserves_in_one_request(bundle):
    import amm_simulation as amm
    calls=[];r=obs.confirm(bundle['mint'],read=reader(bundle,calls))
    assert len(calls)==1 and len(calls[0])<=8
    expected=amm.reserves(bundle)
    assert r['status']=='CONFIRMED' and r['pool']==bundle['account_names']['pool']
    assert r['reserves']['base_raw']==expected['base_raw'] and r['reserves']['effective_quote_raw']==expected['effective_quote_raw']
    assert r['base_vault']==bundle['account_names']['pool_base_token_account'] and r['base_program']==bundle['account_names']['base_token_program']
    assert r['fee_config']['status']=='DECODED' and r['fee_config']['fee_tiers']
    assert all(type(v)==int for v in r['global_fees'].values())
    assert r['market_cap_lamports']>0 and r['fee_tier_estimate'] is not None
    json.dumps(r)

def test_confirm_reports_missing_pool_without_raising(bundle):
    def empty(addresses,**kw):return dict(slot=1,started_ts=1.,received_ts=2.,accounts={a:None for a in addresses})
    assert obs.confirm(bundle['mint'],read=empty)['status']=='AMM_POOL_NOT_FOUND'

def test_probe_reads_three_accounts(bundle):
    mig=dict(obs.confirm(bundle['mint'],read=reader(bundle)),status='CONFIRMED')
    calls=[];r=obs.probe(bundle['mint'],mig,read=reader(bundle,calls))
    assert calls==[[mig['pool'],mig['base_vault'],mig['quote_vault']]]
    assert r['status']=='RESERVES' and r['quote_raw']==mig['reserves']['quote_raw']
    assert obs.probe(bundle['mint'],dict(status='AMM_POOL_NOT_FOUND'))['status']=='NOT_CONFIRMED'

def test_quote_buys_then_sells_the_bought_amount(monkeypatch):
    import exit_quotes as q
    from cost_evidence import SOL
    monkeypatch.setattr(config,'JUPITER_API_KEY','k');monkeypatch.setattr(q,'FX',dict(ts=1000.,rate='150'))
    seen=[]
    def fake(inp,out,raw,slippage):
        seen.append((inp,out,int(raw)))
        got=int(raw)*1000 if inp==SOL else int(raw)//1000*97//100
        return dict(inputMint=inp,outputMint=out,inAmount=str(raw),outAmount=str(got),otherAmountThreshold=str(got),swapMode='ExactIn',
            slippageBps=slippage,router='metis',platformFee=dict(feeBps=10),routePlan=[dict(percent=100,swapInfo=dict(label='Pump.fun Amm',ammKey='P'))])
    monkeypatch.setattr(q,'get_quote',fake)
    r=obs.quote('M',now=1010.)
    lamports=int(3/150*1e9)
    assert seen==[(SOL,'M',lamports),('M',SOL,lamports*1000)]
    assert r['status']=='QUOTED' and r['round_trip_bps']==pytest.approx(-300,abs=1) and r['buy_route'][0]['label']=='Pump.fun Amm'
    assert obs.quote('M',now=2000.)['status']=='MISSING_FX'
    def limited(*a):raise q.ProbeError('RATE_LIMITED','QUOTE',429)
    monkeypatch.setattr(q,'get_quote',limited)
    assert obs.quote('M',now=1010.)['status']=='RATE_LIMITED'

def test_prober_paces_rpc_backs_off_jupiter_and_expires(tmp_path,monkeypatch):
    import rpc_budget
    primary=tmp_path/'clean_0762.db';o=obs.Observer(primary,'observe',.2,True)
    now=[1000.];p=obs.Prober(o,clock=lambda:now[0]);calls=[]
    monkeypatch.setattr(obs,'confirm',lambda mint:calls.append(('CONFIRM',mint)) or dict(status='CONFIRMED',pool='P',base_vault='B',quote_vault='Q',base_program='T'))
    monkeypatch.setattr(obs,'quote',lambda mint:calls.append(('QUOTE',mint)) or dict(status='RATE_LIMITED'))
    with closing(obs.connect(primary)) as c:
        with c:
            for m in ('A','B'):
                c.execute("INSERT INTO migrations_v1(mint,first_seen_ts,status) VALUES(?,1000,'OBSERVED')",(m,))
                o.schedule(c,m,'CONFIRM',1000.,0,obs.CONFIRM_EXPIRE_S)
            o.schedule(c,'A','QUOTE',1000.,0,obs.JOB_EXPIRE_S);o.schedule(c,'A','PROBE',800.,0,10)
        while p.step(c):pass
        assert calls==[('CONFIRM','A'),('QUOTE','A')]          # one RPC job per 1/rate seconds; Jupiter separate
        now[0]+=1/.2
        while p.step(c):pass
        assert calls[-1]==('CONFIRM','B')
        assert c.execute("SELECT status FROM jobs_v1 WHERE mint='A' AND kind='PROBE' AND due_ts=800").fetchone()[0]=='EXPIRED'
        assert tuple(c.execute("SELECT status,attempts FROM jobs_v1 WHERE mint='A' AND kind='QUOTE'").fetchone())==('PENDING',1)
        assert p.next_jupiter>=1000.+obs.JUPITER_BACKOFF_S
        assert c.execute("SELECT COUNT(*) FROM jobs_v1 WHERE kind='PROBE' AND status='PENDING' AND mint='A'").fetchone()[0]==len(obs.PROBE_OFFSETS)
        assert tuple(c.execute("SELECT status,pool FROM migrations_v1 WHERE mint='A'").fetchone())==('CONFIRMED','P')
        def deferred(mint):raise rpc_budget.Deferred('RATE',3.)
        monkeypatch.setattr(obs,'confirm',deferred)
        with c:o.schedule(c,'C','CONFIRM',now[0],0,obs.CONFIRM_EXPIRE_S)
        now[0]+=10;p.step(c)
        job=c.execute("SELECT status,attempts,due_ts FROM jobs_v1 WHERE mint='C'").fetchone()
        assert job[0]=='PENDING' and job[1]==1 and job[2]>=now[0]+3

def test_start_tap_stop_round_trip_with_isolated_database(tmp_path,monkeypatch):
    primary=tmp_path/'clean_0762.db';monkeypatch.setattr(config,'AMM_OBSERVER_MODE','counts')
    o=obs.start(primary);assert o is not None and obs.TAP is o.tap
    for i in range(3):obs.tap(msg('A','pump'),time.time())
    final=obs.stop()
    assert final['state']=='STOPPED' and final['threads_alive']==[] and obs.TAP is None and obs.ACTIVE is None
    assert obs.path(primary).exists() and not primary.exists()
    s=obs.summary(primary);assert s['state']=='STOPPED' and s['mode']=='counts'
    assert obs.stop() is None

def test_invalid_or_off_settings_leave_the_engine_alone(tmp_path,monkeypatch):
    events=[];monkeypatch.setattr(__import__('system_health'),'event',lambda *a,**k:events.append((a,k)))
    monkeypatch.setattr(config,'AMM_OBSERVER_MODE','bogus');assert obs.start(tmp_path/'p.db') is None and obs.TAP is None
    monkeypatch.setattr(config,'AMM_OBSERVER_MODE','observe');monkeypatch.setattr(config,'AMM_OBSERVER_RPC_RATE',2.)
    assert obs.start(tmp_path/'p.db') is None and events[-1][0][1]=='AMM_OBSERVER_CONFIG_REJECTED'
    monkeypatch.setattr(config,'AMM_OBSERVER_MODE','off');monkeypatch.setattr(config,'AMM_OBSERVER_RPC_RATE',.2)
    assert obs.start(tmp_path/'p.db') is None and not obs.path(tmp_path/'p.db').exists()

def test_recorder_failure_disables_tap_without_raising(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'AMM_OBSERVER_MODE','counts')
    monkeypatch.setattr(obs.Recorder,'apply',lambda *a:(_ for _ in ()).throw(RuntimeError('disk')))
    o=obs.start(tmp_path/'clean_0762.db')
    obs.tap(msg('A','pump'),time.time())
    deadline=time.time()+5
    while o.failure is None and time.time()<deadline:
        obs.tap(msg('A','pump'),time.time());time.sleep(.05)
    assert o.failure['part']=='recorder' and obs.TAP is None
    obs.tap(msg('A','pump'),time.time())
    assert obs.stop()['state']=='FAILED'

def test_summary_and_export(tmp_path):
    primary=tmp_path/'clean_0762.db';o=obs.Observer(primary,'observe',.2,True);rec=obs.Recorder(o)
    with closing(obs.connect(primary)) as c,c:
        rec.apply(c,('MIGRATION','A',time.time(),'pump-amm','buy',0.));o.write_status(c)
        c.execute("UPDATE jobs_v1 SET status='DONE',result_json=? WHERE kind='QUOTE'",(json.dumps(dict(status='QUOTED',round_trip_bps=-250.)),))
    out=tmp_path/'results';out.mkdir()
    r=obs.export(primary,out)
    assert r['migrations']['total']==1 and r['quotes']['n']==len(obs.QUOTE_OFFSETS) and r['quotes']['round_trip_bps_median']==-250.
    assert json.loads((out/'amm_observer_summary.json').read_text(encoding='utf-8'))['schema']=='AMM_OBSERVER_V1'
    with zipfile.ZipFile(out/'amm_observer.zip') as z:assert set(z.namelist())=={'clean_0762_amm_observer.db','summary.json'}
    assert len(json.dumps(r))<60000
    empty=tmp_path/'none';empty.mkdir()
    assert obs.export(tmp_path/'other.db',empty)['state']=='NOT_STARTED' and not (empty/'amm_observer.zip').exists()

def test_main_wiring_keeps_primary_filters():
    src=(ROOT/'main.py').read_text(encoding='utf-8')
    tap=src.index('amm_observer.tap(x,now)');action=src.index('if action not in ("buy","sell"):continue',tap)
    pool=src.index('if str(x.get("pool") or "").lower()!=config.PUMPAPI_POOL:',tap)
    assert tap<action<pool and 'continue' in src[pool:src.index('\n',src.index('\n',src.index('\n',pool)+1)+1)]
    run=src[src.index('async def run():'):]
    assert 'amm_observer.start,config.DB' in run and 'amm_observer.stop' in run
    assert config.AMM_OBSERVER_MODE in obs.MODES and 0<config.AMM_OBSERVER_RPC_RATE<=1

def test_dashboard_and_results_include_observer():
    assert "amm_observer').summary(config.DB)" in (ROOT/'dashboard/server.py').read_text(encoding='utf-8')
    details=(ROOT/'dashboard/details.html').read_text(encoding='utf-8')
    assert 'id="ammObserver1020"' in details and "table('ammObsRows1020'" in details
    assert "amm_observer').export(config.DB,folder)" in (ROOT/'launcher.py').read_text(encoding='utf-8')

def test_review_bundle_streams_oversize_report(tmp_path,monkeypatch):
    import review_bundle_v100 as rb,hashlib
    monkeypatch.setattr(rb,'MAX_REPORT_BYTES',200)
    big=json.dumps({'created_ts':1000,'x':'a'*1000}).encode();(tmp_path/'exit_audit.json').write_bytes(big)
    (tmp_path/'small.json').write_text('{"x":1}');(tmp_path/'bad.json').write_text('{')
    r=rb.export(tmp_path)
    assert r['errors']==[{'name':'bad.json','error_type':'JSONDecodeError'}]
    streamed=[v for v in r['volumes'] if v.get('streamed_report')]
    assert [v['streamed_report'] for v in streamed]==['exit_audit.json'] and streamed[0]['oversize_single_report']
    meta=next(f for f in r['files'] if f['source_name']=='exit_audit.json')
    assert meta['oversize_streamed'] and not meta['json_validated'] and meta['sha256']==hashlib.sha256(big).hexdigest()
    with zipfile.ZipFile(tmp_path/streamed[0]['name']) as z:assert z.read('exit_audit.json')==big
