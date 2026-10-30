import copy,io,json,sqlite3,zipfile
from contextlib import closing
from pathlib import Path
import pytest
import config,position_monitor as pm,monitor_service as ms,priority_exit as pe,fresh_retry,connection_audit
from test_primary_integrity import fresh
from test_priority_exit_v65 import opened
import rpc_budget,roadmap_progress,json_arrays


def local_rejection():
    return dict(status='DATA_REJECTED',error_code='Deferred',network_error=connection_audit.diagnostic(rpc_budget.Deferred('STATE_LOCKED',.025)))


def test_local_admission_retries_priority_exit_before_regular_ten_seconds(fresh):
    import time
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        pe.observe(c,now);first=pe.dispatch(c,now)
        pm.finish(c,first,local_rejection(),now=now+.1)
        assert c.execute('select status from exit_retry_v69').fetchone()[0]=='WAITING'
        assert pe.dispatch(c,now+.34) is None
        retry=pe.dispatch(c,now+.36)
        assert retry and retry['id']!=first['id'] and retry['retry_v69']['attempt']==1
        assert retry['scheduled_ts']<first['scheduled_ts']+1
        assert json.loads(c.execute('select input_json from position_checks_v0730 where id=?',(retry['id'],)).fetchone()[0])['id']==retry['id']
        pm.finish(c,retry,local_rejection(),now=now+.5)
        last=pe.dispatch(c,now+1.51);assert last and last['retry_v69']['attempt']==2
        pm.finish(c,last,local_rejection(),now=now+2)
        assert fresh_retry.summary(c)['rows'][0]['status']=='EXHAUSTED'
        assert connection_audit.retry(c,first['trade_id'])['delay_s']==0
        assert fresh_retry.cooldown(c)==0
        assert pe.dispatch(c,now+3) is None


def test_generic_data_rejection_is_not_mistaken_for_local_contention(fresh):
    import time
    opened(fresh);now=time.time()
    with closing(ms.connect()) as c:
        pe.observe(c,now);j=pe.dispatch(c,now)
        pm.finish(c,j,dict(status='DATA_REJECTED',error_code='ValueError'),now=now+.1)
        assert c.execute('select count(*) from exit_retry_v69').fetchone()[0]==0
        assert pe.dispatch(c,now+.5) is None


def test_warm_control_connect_does_not_recreate_schema_or_write_settings(tmp_path,monkeypatch):
    b=rpc_budget.Budget(tmp_path,rate=1000,slots=1)
    with b.request('https://rpc.invalid'):pass
    real=sqlite3.connect;statements=[]
    def connect(*a,**kw):
        c=real(*a,**kw);c.set_trace_callback(statements.append);return c
    monkeypatch.setattr(rpc_budget.sqlite3,'connect',connect)
    with closing(b.connect()) as c:assert c.execute('select rate,slots from settings').fetchone()[0]==1000
    assert statements and not any(s.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE','BEGIN')) for s in statements)
    assert not any('JOURNAL_MODE=' in s.upper().replace(' ','') for s in statements)


@pytest.mark.parametrize('chunk',[1,7,65536])
def test_streamed_json_roundtrip_unicode_nested_and_large_rows(chunk):
    rows=[{},dict(id='аб🙂',nested=[1,-12.3e-6,None,True,'x'*150000]),2.5e19,'終']
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,'w') as z:json_arrays.write(z,'rows.json',iter(rows))
    with zipfile.ZipFile(payload) as z:assert list(json_arrays.read(z.open('rows.json'),chunk_size=chunk))==rows


@pytest.mark.parametrize('data',['[1,]','[{}] garbage','[{}','{}','[1 2]'])
def test_streamed_json_rejects_truncated_or_malformed_history(data):
    with pytest.raises(ValueError):list(json_arrays.read(io.BytesIO(data.encode()),chunk_size=2))


def test_bundle_compaction_preserves_original_and_replay(tmp_path):
    bundle=dict(snapshot=dict(accounts={'a':dict(data=['x'*150000,'base64'],lamports=1),'b':None}),amount=2)
    saved=copy.deepcopy(bundle)
    compact=pm.pack_bundle(bundle,tmp_path)
    assert bundle==saved and isinstance(compact['snapshot']['accounts']['a']['data'],dict)
    assert pm.unpack_bundle(compact,tmp_path)==saved
    assert bundle==saved


def test_roadmap_progress_requires_current_native_and_route_evidence():
    stages=[dict(id=i,title=str(i)) for i in range(1,5)]
    a=dict(new_closed=4);route=dict(verified_routes={'PUMP_CURVE':3},entries=4,closed=3,positions=[{}],input_gap=False)
    rpc=dict(status='READY',endpoints=[dict(started=1607)])
    native=dict(version=config.VERSION,passed=True,native_windows_verified=True)
    model=roadmap_progress.build(stages,{},a,route,rpc,native)
    assert model['stages'][0]['percent']==60 and model['stages'][1]['percent']==2
    assert model['percent']==15.5 and model['stages'][2]['percent']==0
    old=roadmap_progress.build(stages,{},a,route,rpc,dict(native,version='old'))
    assert old['stages'][0]['percent']==40
    full=roadmap_progress.build(stages,{},dict(new_closed=100),dict(route,verified_routes={'PUMP_CURVE':3,'PUMP_AMM':1},closed=4,positions=[]),rpc,native)
    assert full['stages'][0]['percent']==100 and full['stages'][1]['percent']==50
    assert full['percent']==37.5


def test_route_counts_do_not_count_curve_as_amm(fresh):
    import campaign_view,clean_campaign,time
    # Read the real branch projection against an otherwise normal campaign DB.
    c=fresh;c.row_factory=sqlite3.Row
    c.executescript(__import__('paper_branch').SCHEMA)
    c.executemany('insert into branch_amm_settlements(trade_id,details) values(?,?)',[(str(i),json.dumps({'route':route})) for i,route in enumerate(['PUMP_CURVE','PUMP_CURVE','PUMP_AMM',None])]);c.commit()
    row=campaign_view.branch(c,config.DB,'primary',time.time())
    assert row['verified_exits']==4 and row['curve_exits']==2 and row['amm_exits']==1 and row['unknown_route_exits']==1
