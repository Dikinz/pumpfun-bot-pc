import asyncio,json,sys,time
from pathlib import Path
import pytest
from aiohttp import web,ClientSession
import bootstrap,network_check_v88 as check
from child_diagnostics import Trace
import spawn_report_v89 as report

@pytest.mark.parametrize('duration',[None,'1'])
def test_real_bootstrap_passes_only_network_arguments(tmp_path,monkeypatch,duration):
    monkeypatch.chdir(tmp_path);monkeypatch.setattr(sys,'prefix',str(bootstrap.ROOT/'.venv'))
    out=tmp_path/'network.json';flags=['--output',str(out)]
    if duration:flags+=['--duration',duration]
    monkeypatch.setattr(sys,'argv',[str(bootstrap.APP/'bootstrap.py'),'network-check',*flags])
    class Child:
        def __init__(self,command,**kwargs):
            assert 'network-check' not in command
            check.save(check.base_report(),Path(command[command.index('--output')+1]))
        def wait(self,timeout):return 2
    monkeypatch.setattr(check.subprocess,'Popen',Child)
    assert bootstrap.main()==2
    result=json.loads(out.read_text());assert result['version']=='v0.7.103.0' and result['diagnostic_revision']=='NETWORK_CHECK_FIX_2'


@pytest.mark.parametrize('genesis,expected',[
 ('5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d','PASS'),
 ('EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG','FAILED'),
 ('5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp','FAILED')])
def test_independent_full_rpc_chain_responses(genesis,expected):
    async def scenario():
        async def handler(request):
            p=await request.json();return web.json_response(dict(jsonrpc='2.0',id=p['id'],result=genesis if p['method']=='getGenesisHash' else 1234))
        app=web.Application();app.router.add_post('/',handler);runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        try:
            async with ClientSession() as session:return await check.rpc_check(session,'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]),'primary')
        finally:await runner.cleanup()
    row=asyncio.run(scenario());assert row['status']==expected
    assert row['reachable'] and row['expected_genesis']=='5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
    if expected=='PASS':assert row['slot']==1234 and row['mainnet_verified']
    else:assert 'slot' not in row


def test_spawn_context_and_slow_native_result_keep_errno_without_arguments(tmp_path):
    trace=Trace(tmp_path,dict(id='check',trade_id='trade'))
    trace.spawn_context([sys.executable,'-c','SECRET api-key=SECRET'],tmp_path)
    ident=trace.native_enter('CreateProcessW');start=trace.native_started(ident)
    trace.native_return(ident,'CreateProcessW',False,5,start+140,time.time())
    data=trace.snapshot();assert data['native_create_v89']['duration_ms']==pytest.approx(140000)
    assert data['native_create_v89']['winerror']==5 and data['native_create_v89']['root_cause']=='UNKNOWN'
    assert data['spawn_context_v89']['executable_exists'] and 'SECRET' not in json.dumps(data)


def test_spawn_report_records_incidents_but_not_wallet_or_event_messages(tmp_path):
    child=tmp_path/'monitor_evidence'/'job';child.mkdir(parents=True)
    trace=Trace(child,dict(id='job',trade_id='trade'));i=trace.native_enter('CreateProcessW');t=trace.native_started(i)
    trace.native_return(i,'CreateProcessW',False,5,t+140,time.time())
    saved=report.write_report(tmp_path/'report.json',include_events=False,app=tmp_path)
    assert len(saved['traces']['rows'])==1 and not saved['root_cause_fixed']
    assert saved['events']['status']=='NOT_REQUESTED'


def test_spawn_button_uses_real_bootstrap_parser(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);monkeypatch.setattr(sys,'prefix',str(bootstrap.ROOT/'.venv'))
    out=tmp_path/'spawn.json';monkeypatch.setattr(sys,'argv',[str(bootstrap.APP/'bootstrap.py'),'spawn-diagnostics','--no-events','--output',str(out)])
    assert bootstrap.main()==0 and json.loads(out.read_text())['schema']=='SPAWN_DIAGNOSTICS_V89'


def test_cost_basis_audit_does_not_invent_common_wallet_profit():
    from cost_model_audit_v89 import audit
    source=dict(totals=dict(primary=dict(net=-4,setup_coverage='UNKNOWN'),route=dict(net=-7,token_setup=1.6,volume_setup=1.5,setup_excluded_sensitivity_net=-3.9,setup_coverage='COMPLETE')))
    saved=json.dumps(source);result=audit(source)
    assert result['rows']['route']['observed_net']==-7 and result['common_cost_net'] is None
    assert not result['common_wallet_costs_proven'] and json.dumps(source)==saved
    assert result['rows']['primary']['token_setup'] is None


def test_missing_diagnostic_executable_does_not_invent_acl_failure(tmp_path):
    trace=Trace(tmp_path,dict(id='controlled',trade_id='t'))
    trace.spawn_context([],tmp_path)
    assert trace.snapshot()['spawn_context_v89']['executable_exists'] is None
    assert trace.snapshot()['spawn_context_v89']['security_product_cause']=='UNKNOWN'


def test_diagnostic_collection_failure_does_not_prevent_owned_spawn(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from windows_child import BackgroundChild
    trace=Trace(tmp_path,dict(id='t',trade_id='t'))
    def denied(*args):raise PermissionError('controlled diagnostic failure')
    monkeypatch.setattr(trace,'spawn_context',denied)
    group=SimpleNamespace(pid=1234567,created_ts=1.,poll=lambda:0,active=lambda:0,close=lambda:None,atomic_assignment=True)
    calls=[]
    def factory(*args):calls.append(True);return group
    owner=BackgroundChild([],tmp_path,tmp_path/'child.log',trace,factory=factory)
    owner.thread.join(timeout=2)
    assert owner.done.is_set() and owner.error is None and calls==[True]
    assert any(e['phase']=='SPAWN_CONTEXT_UNAVAILABLE_V89' for e in trace.data['events'])
