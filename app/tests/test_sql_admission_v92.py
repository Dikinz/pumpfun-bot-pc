import asyncio,copy,json,sqlite3,time
from contextlib import contextmanager
import pytest
import config,database as db,runtime_control as rt,counter_writer as cw
import execution_review_v92 as review,cost_price_audit
from test_primary_integrity import fresh


def busy():
    e=sqlite3.OperationalError('database is locked');e.sqlite_errorcode=5;e.sqlite_errorname='SQLITE_BUSY';return e


def test_receive_telemetry_retains_exact_counters_through_real_sqlite_busy(fresh):
    db.runtime_mode();blocker=sqlite3.connect(config.DB,isolation_level=None);blocker.execute('BEGIN IMMEDIATE')
    t=rt.MarketTelemetry()
    try:
        for i in range(100):t.received(100+i);t.market(100+i)
        assert not t.flush(db,True)
        assert t.count==100 and t.snapshot()['local_busy_deferrals']==1
        assert fresh.execute('pragma busy_timeout').fetchone()[0]==250 and not fresh.in_transaction
        # Force does not defeat the local retry delay, nor stamp deferred data as fresh.
        assert not t.flush(db,True) and t.deferred==1
        assert float(db.getm('pumpapi_messages',0))==0
    finally:blocker.rollback();blocker.close()
    t.retry_at=0;assert t.flush(db,True)
    assert float(db.getm('pumpapi_messages'))==100 and float(db.getm('trade_last_market_ts'))==199
    assert t.count==0 and t.committed==100 and t.error is None


def test_receive_metadata_partial_write_rolls_back_without_double_count(fresh,monkeypatch):
    real=db.setmany
    def fail(values):raise busy()
    monkeypatch.setattr(db,'setmany',fail);t=rt.MarketTelemetry();t.received(123)
    assert not t.flush(db,True) and not fresh.in_transaction
    assert float(db.getm('pumpapi_messages',0))==0
    monkeypatch.setattr(db,'setmany',real);t.retry_at=0;assert t.flush(db,True)
    assert float(db.getm('pumpapi_messages'))==1


def test_nonbusy_receive_write_error_is_not_retried(fresh,monkeypatch):
    def fail(values):
        e=sqlite3.OperationalError('disk I/O error');e.sqlite_errorcode=10;raise e
    monkeypatch.setattr(db,'setmany',fail);t=rt.MarketTelemetry();t.received(123)
    with pytest.raises(sqlite3.OperationalError):t.flush(db,True)
    assert t.deferred==0 and not fresh.in_transaction


def test_timeout_restore_failure_after_commit_is_fatal_not_counter_replay(fresh,monkeypatch):
    class Failing(db.Connection):
        def execute(self,sql,*args):
            if sql=='PRAGMA busy_timeout=30000':raise busy()
            return super().execute(sql,*args)
    c=sqlite3.connect(':memory:',isolation_level=None,factory=Failing);c.execute('create table meta(k text primary key,v text)')
    monkeypatch.setattr(db,'con',lambda:c);t=rt.MarketTelemetry();t.received(123)
    with pytest.raises(db.LocalDatabaseFailure,match='TIMEOUT_RESTORE_FAILED'):t.flush(db,True)
    assert t.deferred==0 and not c.in_transaction
    assert float(c.execute("select v from meta where k='pumpapi_messages'").fetchone()[0])==1
    c.close()


def test_commit_failure_rolls_back_before_any_replay(fresh,monkeypatch):
    class Failing(db.Connection):
        def commit(self):raise busy()
    c=sqlite3.connect(':memory:',isolation_level=None,factory=Failing);c.execute('create table t(n)')
    monkeypatch.setattr(db,'con',lambda:c)
    with pytest.raises(sqlite3.OperationalError):
        with db.atomic():c.execute('insert into t values(1)')
    assert c.atomic_depth==0 and not c.in_transaction and c.execute('select count(*) from t').fetchone()[0]==0
    c.close()


def test_unconfirmed_rollback_never_enters_retry_path(fresh,monkeypatch):
    class Failing(db.Connection):
        def rollback(self):raise busy()
    c=sqlite3.connect(':memory:',isolation_level=None,factory=Failing);c.execute('create table t(n)')
    monkeypatch.setattr(db,'con',lambda:c)
    with pytest.raises(db.LocalDatabaseFailure,match='ROLLBACK_UNCONFIRMED'):
        with db.atomic():c.execute('insert into t values(1)');raise busy()
    assert c.atomic_depth==0
    c.close()


def test_counter_json_publication_never_holds_sqlite_writer_transaction(tmp_path):
    p=tmp_path/'c.db'
    with sqlite3.connect(p) as c:c.executescript('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT)')
    connections=[]
    def connect(*a,**kw):
        c=sqlite3.connect(*a,**kw);connections.append(c);return c
    w=cw.Writer(p,connect=connect,interval=.001);seen=[];real=w.publish
    def publish():
        if connections:
            try:assert not connections[0].in_transaction
            except sqlite3.ProgrammingError:pass # final publication follows connection close
        seen.append(True);return real()
    w.publish=publish;w.add('events','',table='meta',amount=100);w.start();state=asyncio.run(w.close())
    assert seen and state['committed']==100 and state['unflushed']==0 and state['max_transaction_ms']>=0


def test_real_receiver_does_not_reconnect_for_receive_counter_contention(fresh,monkeypatch):
    import main,recovery_gate,websockets
    metrics={};connections=[];probe=rt.Probe(config.DB);monkeypatch.setattr(rt,'ACTIVE',probe)
    monkeypatch.setattr(main,'TRADE_GAP_START',None)
    monkeypatch.setattr(db,'setm',lambda k,v:metrics.__setitem__(k,v))
    monkeypatch.setattr(db,'setmany',lambda v:metrics.update(v))
    monkeypatch.setattr(db,'getm',lambda k,d=0:metrics.get(k,d))
    monkeypatch.setattr(db,'inc',lambda k,n=1:metrics.__setitem__(k,metrics.get(k,0)+n))
    monkeypatch.setattr(db,'stream_debug',lambda *a:None)
    monkeypatch.setattr(db,'reconnect_event',lambda *a:pytest.fail('local counter contention triggered reconnect'))
    monkeypatch.setattr(recovery_gate,'connection',lambda:None);monkeypatch.setattr(recovery_gate,'status',lambda:{'epoch':'same'})
    blocker=sqlite3.connect(config.DB,isolation_level=None);blocker.execute('BEGIN IMMEDIATE')
    async def scenario():
        async def server(ws):
            connections.append(1)
            try:
                while True:
                    await ws.send(json.dumps(dict(action='buy',pool='pump',mint='untracked-v92')));await asyncio.sleep(.005)
            except websockets.ConnectionClosed:pass
        async with websockets.serve(server,'127.0.0.1',0) as server:
            monkeypatch.setattr(config,'PUMPAPI_STREAM_URL','ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1]))
            task=asyncio.create_task(main.pumpapi_trades(asyncio.Queue()))
            try:
                async def wait_for(predicate):
                    while not predicate():
                        if task.done():task.result()
                        await asyncio.sleep(.01)
                await asyncio.wait_for(wait_for(lambda:(getattr(probe,'market_telemetry_v92',None) or {}).get('local_busy_deferrals',0)>0),3)
                assert connections==[1] and metrics.get('trade_ws_errors',0)==0
                blocker.rollback()
                await asyncio.wait_for(wait_for(lambda:metrics.get('pumpapi_messages',0)>0),3)
                assert connections==[1] and metrics.get('trade_ws_errors',0)==0
            finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    try:asyncio.run(scenario())
    finally:blocker.close()


def test_local_sql_error_closes_socket_and_propagates_without_provider_retry(fresh,monkeypatch):
    import main,recovery_gate
    metrics={};connects=[];closed=[];failures=[]
    monkeypatch.setattr(main,'TRADE_GAP_START',None)
    monkeypatch.setattr(db,'setm',lambda k,v:metrics.__setitem__(k,v))
    monkeypatch.setattr(db,'getm',lambda k,d=0:metrics.get(k,d))
    monkeypatch.setattr(db,'inc',lambda k,n=1:metrics.__setitem__(k,metrics.get(k,0)+n))
    monkeypatch.setattr(db,'stream_debug',lambda *a:None)
    monkeypatch.setattr(recovery_gate,'connection',lambda:None);monkeypatch.setattr(recovery_gate,'status',lambda:{'epoch':'same'})
    monkeypatch.setattr(main.system_health,'failure',lambda *a:failures.append(a[1]))
    def fail(values):
        e=sqlite3.OperationalError('disk I/O error');e.sqlite_errorcode=10;raise e
    monkeypatch.setattr(db,'setmany',fail)
    class Socket:
        async def recv(self):return '{}'
        async def close(self):closed.append(1)
    async def connect(*a,**kw):connects.append(1);return Socket()
    monkeypatch.setattr(main.websockets,'connect',connect)
    with pytest.raises(sqlite3.OperationalError):asyncio.run(asyncio.wait_for(main.pumpapi_trades(asyncio.Queue()),2))
    assert connects==[1] and closed==[1] and failures==['TRADE_STREAM_LOCAL_DATABASE']
    assert metrics.get('trade_ws_errors',0)==0 and metrics.get('trade_reconnects',0)==0


def test_timing_partitions_do_not_double_count_nested_rpc():
    r=review.timeline(10,20,dict(scheduled_ts=12,finished_ts=19),dict(snapshot_received_ts=17,
        scheduler_timing_v65={'worker_started_ts':13,'worker_duration_ms':6000},rpc_timing={'requests':[{'rpc_admission_v89':{'wait_ms':1000}}]}))
    assert r['status']=='VALID' and sum(r['partition_ms'].values())==10000
    assert r['rpc_admission_wait_ms_sum']==1000
    assert review.timeline(10,20,dict(scheduled_ts=9,finished_ts=19),{})['partition_ms'] is None


def test_matched_cost_review_excludes_incomplete_and_wrong_signal_pairs():
    pair=dict(match_status='IDENTICAL_ENTRY_INVENTORY',same_shared_sell_signal=True,primary_net=-.4,route_net=-1,
              recorded_gross_difference=-.3,recorded_cost_difference=.3)
    r=review.matched_costs({'pairs':[pair,dict(pair,same_shared_sell_signal=False),dict(pair,route_net=None)]})
    assert r['pairs']==1 and r['excluded_pairs']==2 and r['reconciled']
    assert r['net_difference']==pytest.approx(-.6) and r['common_wallet_net'] is None
    assert review.matched_costs({})['primary_net'] is None


def amm_fixture():
    inv=dict(mint='m',size_usdc='3',sol_usdc='100',token_units='1000',entry_sol_price='.00003',raw_amount='1000000000')
    sale=dict(status='AMM_SALE_EXECUTED',raw_amount='1000000000',tokens_debited='1000000000',pool_tokens_credited='1000000000',
      quote_received_lamports=7259534,wallet_sol_delta_lamports=-1351200,base_account_rent_lamports=1488440,quote_account_rent_lamports=1488440,
      local_fee_lamports=5000,volume_rent_delta_lamports=1346200,pool_rent_topup_lamports=0,net_after_setup_lamports=2931454,
      broadcast=False,paper_mutated=False,err=None)
    result=dict(status='AMM_SALE_EXECUTED',route='PUMP_AMM',sale=sale,broadcast=False,paper_mutated=False,zero_token_control_ok=True)
    row=dict(id='id',mint='m',status='AMM_SALE_EXECUTED',result_json=json.dumps(result),input_json=json.dumps(dict(mint='m',route='PUMP_AMM',inventory=inv)))
    return inv,row,result


def test_amm_costs_reconcile_setup_once_without_mutating_ledger():
    inv,row,result=amm_fixture();saved=copy.deepcopy((inv,row,result))
    out=cost_price_audit.local_components(row,inv,.05)
    assert out['accounting_status']=='RECONCILED' and out['receipt_units']==pytest.approx(.2931454)
    assert out['token_account_setup']==pytest.approx(.297688) and out['volume_account_setup']==pytest.approx(.13462)
    assert out['pnl_units']==pytest.approx(-2.7568546) and out['exit_fee_balance_residual']==0
    assert (inv,row,result)==saved


@pytest.mark.parametrize('field,value',[('tokens_debited','1'),('net_after_setup_lamports',2931455),('broadcast',True),('base_account_rent_lamports',True)])
def test_amm_costs_reject_inconsistent_amount_effects_and_types(field,value):
    inv,row,result=amm_fixture();result['sale'][field]=value;row['result_json']=json.dumps(result)
    assert cost_price_audit.local_components(row,inv,.05)['accounting_status']!='RECONCILED'
