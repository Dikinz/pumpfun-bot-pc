import json,time
from types import SimpleNamespace
import pytest
import stream_diagnostics as stream
import system_health as sh
import strategy_diagnostics as sd
import config
from test_primary_integrity import fresh

@pytest.mark.parametrize('reason,code,identity,newer,failed,expected',[
 ('USER_STOP',0,True,False,False,True),('USER_STOP',1,True,False,False,False),
 ('COMPONENT_FAILURE',0,True,False,False,False),('USER_STOP',0,False,False,False,False),
 ('USER_STOP',0,True,True,False,False),('USER_STOP',0,True,False,True,False)])
def test_stop_cannot_hide_new_run_or_failure(fresh,reason,code,identity,newer,failed,expected):
    now=time.time()
    sh.write_state(config.DB,dict(status='STOPPED',heartbeat_ts=now-1,stop_reason=reason,exit_code=code,
        database_identity=sh.identity(config.DB) if identity else 'different'))
    snap=dict(runtime=dict(state='FAILED' if failed else 'UNKNOWN',started_ts=now if newer else now-60,updated_ts=now if newer else now-10),positions=[],diagnostics={},market_feed={})
    result=sd.summary(fresh,snap,config.DB)
    codes={x['code'] for x in result['issues']}
    assert ('USER_STOP' in codes)==expected
    assert ('PRIMARY_HEARTBEAT_UNKNOWN' in codes)!=expected
    if expected:assert snap['runtime']['state']=='STOPPED' and snap['market_feed']['state']=='STOPPED'

def test_open_position_still_critical_on_user_stop(fresh):
    now=time.time();sh.write_state(config.DB,dict(status='STOPPED',heartbeat_ts=now-1,stop_reason='USER_STOP',exit_code=0,database_identity=sh.identity(config.DB)))
    snap=dict(runtime=dict(state='UNKNOWN',started_ts=now-60,updated_ts=now-10),positions=[{'mint':'a'}])
    r=sd.summary(fresh,snap,config.DB)
    assert r['status']=='CRITICAL' and 'STOPPED_WITH_OPEN_POSITIONS' in {x['code'] for x in r['issues']}

def test_buffer_probes_never_consume():
    frames=[1,2,3]
    ws=SimpleNamespace(recv_messages=SimpleNamespace(frames=frames,paused=True))
    assert stream.buffer_state(ws)==dict(unit='frames',depth=3,paused=True)
    assert frames==[1,2,3]
    assert stream.buffer_state(SimpleNamespace(messages=[1]))['unit']=='messages'
    assert stream.buffer_state(object())['depth'] is None

def test_bounded_aggregation_includes_ignored_messages_and_flushes_final_window():
    clock=[0.];written=[];wall=1792613000.
    p=stream.Probe(lambda k,v:written.append(json.loads(v)),clock=lambda:clock[0],wall=lambda:wall+clock[0])
    ws=SimpleNamespace(recv_messages=SimpleNamespace(frames=[1,2],paused=True));p.connected()
    for _ in range(130):
        start=p.received(ws,clock[0]-.02);p.provider({'action':'buy','timestamp':(wall+clock[0]-2)*1000},wall+clock[0])
        clock[0]+=.003;p.finished(ws,start);clock[0]+=5;p.flush()
    p.flush(force=True);last=written[-1]
    assert last['messages']==130 and len(last['windows'])==120
    windows=last['windows'];w=next(x for x in windows if x['messages'])
    assert w['buffer_max']==2 and w['buffer_unit']=='frames' and w['paused_samples']==2
    assert w['handler_max_ms']==pytest.approx(3) and w['provider_over_1000']==1
    assert len(written)<140

def test_invalid_timestamps_not_invented():
    p=stream.Probe(lambda *a:None)
    for x in [None,'bad',float('nan'),True,123]:p.provider({'action':'buy','timestamp':x},time.time())
    assert p.window['provider_samples']==0


def test_diagnostic_write_failure_does_not_interrupt_receiver():
    def fail(*args):raise OSError('disk')
    p=stream.Probe(fail)
    assert p.write_errors==1
    p.flush(force=True)
    assert p.write_errors==2

def test_real_websocket_buffer_is_observed_without_losing_messages():
    import asyncio,websockets
    async def exercise():
        async def serve(ws):
            for i in range(40):await ws.send(json.dumps({'sequence':i}))
            await ws.wait_closed()
        async with websockets.serve(serve,'127.0.0.1',0) as server:
            port=server.sockets[0].getsockname()[1]
            written=[];p=stream.Probe(lambda k,v:written.append(json.loads(v)))
            async with websockets.connect('ws://127.0.0.1:'+str(port),max_queue=8) as ws:
                await asyncio.sleep(.03)
                buffered=stream.buffer_state(ws)
                assert buffered['depth']>0
                got=[]
                for _ in range(40):
                    before=time.monotonic();raw=await ws.recv();token=p.received(ws,before)
                    got.append(json.loads(raw)['sequence']);p.finished(ws,token)
                p.flush(force=True)
                assert got==list(range(40)) and written[-1]['messages']==40
                assert written[-1]['windows'][-1]['buffer_max']>0
    asyncio.run(exercise())
