import asyncio,json,socket,time
import pytest
from aiohttp import web,ClientSession,ClientTimeout
import websockets
from market_watchdog import MarketSilence
import network_check_v88 as check
from test_primary_integrity import fresh


def test_control_traffic_cannot_refresh_market_age_and_reports_are_bounded():
    mono=[0.];watch=MarketSilence(20,60,5,clock=lambda:mono[0]);published=[]
    for i in range(6100):
        mono[0]=i/100;state=watch.poll()
        if state['publish']:published.append(state)
    assert state['state']=='HARD_STALE' and state['age_s']==60.99
    assert len(published)<12 and published[0]['age_s']==20


def test_market_resets_silence_wall_clock_irrelevant(monkeypatch):
    mono=[10.];watch=MarketSilence(20,60,5,clock=lambda:mono[0])
    monkeypatch.setattr(time,'time',lambda:-999999.)
    mono[0]=35;assert watch.poll()['state']=='SOFT_STALE'
    watch.market();assert watch.poll()['state']=='FRESH'
    mono[0]=95;assert watch.poll()['state']=='HARD_STALE'


@pytest.mark.parametrize('limits',[(0,60,5),(20,10,5),(20,60,21),(20,float('nan'),5)])
def test_invalid_watchdog_limits(limits):
    with pytest.raises(ValueError):MarketSilence(*limits)


def test_real_receiver_reconnects_during_continuous_control_traffic(monkeypatch,fresh):
    import main,config,database as db,recovery_gate
    metrics={};closed=[]
    monkeypatch.setattr(main,'TRADE_GAP_START',None)
    monkeypatch.setattr(db,'setm',lambda k,v:metrics.__setitem__(k,v))
    monkeypatch.setattr(db,'setmany',lambda values:metrics.update(values))
    monkeypatch.setattr(db,'getm',lambda k,d=0:metrics.get(k,d))
    monkeypatch.setattr(db,'inc',lambda k,v=1:metrics.__setitem__(k,metrics.get(k,0)+v))
    monkeypatch.setattr(db,'stream_debug',lambda *a:None)
    monkeypatch.setattr(recovery_gate,'connection',lambda:None)
    monkeypatch.setattr(recovery_gate,'status',lambda:{'epoch':'same'})
    for k,v in dict(PUMPAPI_STALL_SECONDS=.03,PUMPAPI_HARD_STALL_SECONDS=.08,PUMPAPI_WATCHDOG_TICK_SECONDS=.01,PUMPAPI_CLOSE_GRACE=0,PUMPAPI_MIN_RECONNECT_DELAY=.01,PUMPAPI_WATCHDOG_RECONNECT_DELAY=0,RECONNECT_JITTER_MAX=0).items():monkeypatch.setattr(config,k,v)
    async def scenario():
        count=[0];second=asyncio.Event()
        async def serve(ws):
            count[0]+=1;market=count[0]>1
            if market:second.set()
            try:
                while True:
                    await ws.send(json.dumps(dict(action='buy',pool='pump',mint='network-v88-untracked')) if market else '{"action":"heartbeat"}')
                    await asyncio.sleep(.002)
            except websockets.ConnectionClosed:closed.append(True)
        async with websockets.serve(serve,'127.0.0.1',0) as server:
            monkeypatch.setattr(config,'PUMPAPI_STREAM_URL','ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1]))
            task=asyncio.create_task(main.pumpapi_trades(asyncio.Queue()))
            try:await asyncio.wait_for(second.wait(),timeout=3);await asyncio.sleep(.025)
            finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(scenario())
    assert metrics['trade_stall_restarts']==1 and metrics['trade_soft_stale_ticks']<10 and closed
    assert metrics['pumpapi_connects']==2 and metrics['pumpapi_socket_active']==0
    assert metrics.get('trade_last_market_ts',0)>0


async def rpc_server(handler):
    app=web.Application();app.router.add_post('/',handler);runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])+'/'
    try:
        async with ClientSession(timeout=ClientTimeout(total=.15)) as session:return await check.rpc_check(session,url+'?api-key=SECRET','primary')
    finally:await runner.cleanup()


def test_real_http_chain_slot_read_only_and_redacted():
    calls=[]
    async def handler(request):
        p=await request.json();calls.append(p['method'])
        return web.json_response(dict(jsonrpc='2.0',id=p['id'],result=check.MAINNET_GENESIS if p['method']=='getGenesisHash' else 123))
    row=asyncio.run(rpc_server(handler))
    assert row['status']=='PASS' and row['mainnet_verified'] and row['slot']==123
    assert calls==['getGenesisHash','getSlot'] and 'SECRET' not in json.dumps(row)


def test_real_http_429_retry_after_never_immediately_repeats():
    calls=[]
    async def handler(request):calls.append(1);return web.Response(status=429,headers={'Retry-After':'7'},text='SECRET')
    row=asyncio.run(rpc_server(handler))
    assert len(calls)==1 and row['status']=='FAILED'
    assert row['requests'][0]['retry_after_s']==7 and 'SECRET' not in json.dumps(row)


def test_real_http_hang_deadline_and_wrong_chain_stops_before_slot():
    async def hanging(request):await asyncio.sleep(.3);return web.json_response({})
    async def wrong(request):p=await request.json();return web.json_response(dict(jsonrpc='2.0',id=p['id'],result='other-chain'))
    row=asyncio.run(rpc_server(hanging))
    assert row['requests'][0]['error']['category']=='TIMEOUT' and len(row['requests'])==1
    row=asyncio.run(rpc_server(wrong))
    assert row['requests'][0]['status']=='CHAIN_MISMATCH' and len(row['requests'])==1


def ws_case(message):
    async def scenario():
        async def serve(ws):await ws.send(json.dumps(message));await ws.wait_closed()
        async with websockets.serve(serve,'127.0.0.1',0) as server:
            return await check.ws_check('ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1]),.05)
    return asyncio.run(scenario())


def test_real_ws_ping_data_and_confirmed_close():
    row=ws_case(dict(action='buy',pool='pump',mint='public-mint'))
    assert row['status']=='PASS' and row['market_messages']==1 and row['ping_probe']=='PASS' and row['closed_confirmed']


def test_real_ws_control_only_is_not_market_pass():
    row=ws_case(dict(action='heartbeat'))
    assert row['status']=='NO_MARKET_DATA' and row['connected'] and row['closed_confirmed']


def test_running_bot_guard_prevents_second_websocket(monkeypatch):
    async def forbidden(*a):raise AssertionError('must not connect')
    monkeypatch.setattr(check,'ws_check',forbidden)
    async def rpc(*a):return dict(endpoint='primary',status='PASS')
    monkeypatch.setattr(check,'rpc_check',rpc)
    with socket.socket() as owner:
        owner.bind(('127.0.0.1',0));owner.listen(1)
        row=asyncio.run(check.collect(dict(rpc='https://primary.invalid',backup='',ws='wss://source.invalid',pool='pump',port=owner.getsockname()[1]),1))
        assert row['websocket']['status']=='GUARD_UNAVAILABLE_BOT_MAY_BE_RUNNING' and not row['passed']
        assert owner.fileno()>=0


def test_failed_worker_does_not_reuse_old_pass(tmp_path,monkeypatch):
    target=tmp_path/'report.json';target.write_text('{"passed":true,"status":"PASS"}')
    class Worker:
        def wait(self,timeout):return 2
    monkeypatch.setattr(check.subprocess,'Popen',lambda *a,**k:Worker())
    assert check.main(['--duration','1','--output',str(target)])==2
    assert json.loads(target.read_text())['status']=='REPORT_UNAVAILABLE'


@pytest.mark.parametrize('url,scheme',[('http://host','https'),('https://u:p@host','https'),('wss://host#secret','wss')])
def test_invalid_endpoints_are_rejected(url,scheme):assert not check.valid_url(url,scheme)
