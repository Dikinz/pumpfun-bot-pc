"""Isolated read-only network acceptance. No wallet, trading DB or transaction API.

One connection per configured endpoint, no automatic retries. A parent process
bounds the entire diagnostic, including DNS/OS shutdown. Reports omit endpoints,
query strings, response bodies and exception messages.
"""
import argparse,asyncio,json,math,os,socket,subprocess,sys,time,uuid
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
import aiohttp,websockets
from connection_audit import diagnostic

VERSION='v0.7.101.9'
MAINNET_GENESIS='5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
# Full getGenesisHash value, not the truncated CAIP-2 chain identifier.
GENESIS_REFERENCE='https://solana.com/docs/payments/agentic-payments/x402'
APP=Path(__file__).resolve().parent
REPORT=APP.parent/'results/network_check_v89.json'


def valid_url(url,scheme):
    try:
        p=urlsplit(url)
        return bool(p.scheme==scheme and p.hostname and not p.username and not p.password and not p.fragment and p.port!=0)
    except ValueError:return False


def retry_after(value):
    if not value:return None
    try:
        if value.isdigit():return int(value)
        from email.utils import parsedate_to_datetime
        return max(0,math.ceil(parsedate_to_datetime(value).timestamp()-time.time()))
    except (ValueError,TypeError,OverflowError):return None


def error(exc):
    d=diagnostic(exc)
    if d['category']=='OTHER' and isinstance(exc,aiohttp.ClientConnectionError):d['category']='CONNECTION'
    return d


def public_genesis(value):
    # Public chain identifier only. Do not store arbitrary response text.
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    if not isinstance(value,str) or not 32<=len(value)<=44 or any(c not in alphabet for c in value):return None
    number=0
    for c in value:number=number*58+alphabet.index(c)
    leading=len(value)-len(value.lstrip('1'))
    size=(number.bit_length()+7)//8+leading
    return value if size==32 else None


async def rpc_check(session,url,label):
    row=dict(endpoint=label,status='FAILED',requests=[],read_only=True,reachable=False,expected_genesis=MAINNET_GENESIS)
    for ident,method in enumerate(('getGenesisHash','getSlot'),1):
        start=time.monotonic();request=dict(method=method,status='FAILED')
        row['requests'].append(request)
        try:
            params=[] if method=='getGenesisHash' else [{'commitment':'confirmed'}]
            async with session.post(url,json=dict(jsonrpc='2.0',id=ident,method=method,params=params),allow_redirects=False) as response:
                request['http_status']=response.status
                if response.status!=200:
                    request.update(status='HTTP_ERROR',retry_after_s=retry_after(response.headers.get('Retry-After')))
                    break
                row['reachable']=True
                parts=[];size=0
                async for part in response.content.iter_chunked(8192):
                    size+=len(part)
                    if size>65536:break
                    parts.append(part)
                if size>65536:request['status']='RESPONSE_TOO_LARGE';break
                raw=b''.join(parts)
                data=json.loads(raw)
            if not isinstance(data,dict) or data.get('jsonrpc')!='2.0' or type(data.get('id'))!=int or data['id']!=ident:
                request['status']='INVALID_ENVELOPE';break
            if data.get('error') is not None:
                code=data['error'].get('code') if isinstance(data['error'],dict) else None
                request.update(status='RPC_ERROR',rpc_code=code if type(code)==int else None);break
            value=data.get('result')
            if method=='getGenesisHash':
                row['observed_genesis']=public_genesis(value)
                if value!=MAINNET_GENESIS:request['status']='CHAIN_MISMATCH';break
                row['mainnet_verified']=True
            elif type(value)!=int or value<0:request['status']='INVALID_SLOT';break
            else:row['slot']=value
            request['status']='OK'
        except asyncio.CancelledError:raise
        except Exception as exc:request['error']=error(exc);break
        finally:request['elapsed_ms']=round((time.monotonic()-start)*1000,3)
    if len(row['requests'])==2 and all(x['status']=='OK' for x in row['requests']):row['status']='PASS'
    return row


async def ws_check(url,duration,pool='pump'):
    row=dict(status='FAILED',connected=False,messages=0,market_messages=0,parse_errors=0,
             ping_probe='NOT_RUN',closed_confirmed=False,automatic_reconnects=0)
    ws=None;start=time.monotonic();last_market=None
    try:
        ws=await websockets.connect(url,open_timeout=8,close_timeout=2,ping_interval=None,max_queue=64,max_size=262144)
        row['connected']=True;row['connect_ms']=round((time.monotonic()-start)*1000,3)
        try:
            t=time.monotonic();pong=await ws.ping();await asyncio.wait_for(pong,timeout=3)
            row['ping_probe']='PASS';row['ping_ms']=round((time.monotonic()-t)*1000,3)
        except asyncio.TimeoutError:row['ping_probe']='TIMEOUT'
        deadline=time.monotonic()+duration
        while time.monotonic()<deadline:
            try:raw=await asyncio.wait_for(ws.recv(),timeout=min(2,deadline-time.monotonic()))
            except asyncio.TimeoutError:continue
            row['messages']+=1
            try:data=json.loads(raw)
            except (ValueError,TypeError):row['parse_errors']+=1;continue
            if not isinstance(data,dict):continue
            action=str(data.get('action') or data.get('txType') or '').lower()
            if action in ('buy','sell') and str(data.get('pool') or '').lower()==pool and data.get('mint'):
                row['market_messages']+=1;last_market=time.monotonic()
        row['status']='PASS' if row['market_messages'] else 'NO_MARKET_DATA'
        row['last_market_age_s']=round(time.monotonic()-last_market,3) if last_market is not None else None
    except asyncio.CancelledError:raise
    except Exception as exc:row['error']=error(exc)
    finally:
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(),timeout=4)
                await asyncio.wait_for(ws.wait_closed(),timeout=2)
                row['closed_confirmed']=True
            except asyncio.CancelledError:raise
            except Exception as exc:row['close_error']=error(exc);row['status']='CLOSE_UNCONFIRMED'
        row['elapsed_s']=round(time.monotonic()-start,3)
    return row


def settings():
    from dotenv import dotenv_values
    values=dict(dotenv_values(APP/'.env'));values.update(os.environ)
    return dict(rpc=values.get('QUOTE_RPC_URL') or 'https://api.mainnet-beta.solana.com',
        backup=values.get('NETWORK_CHECK_BACKUP_RPC_URL') or '',
        ws=values.get('PUMPAPI_STREAM_URL') or 'wss://stream.pumpapi.io/',
        pool=(values.get('PUMPAPI_POOL') or 'pump').lower(),
        port=int(values.get('PUMPAPI_SINGLETON_PORT') or '49190'))


def base_report():
    return dict(schema='NETWORK_CHECK_V88',version=VERSION,diagnostic_revision='NETWORK_CHECK_FIX_2',created_utc=datetime.now(timezone.utc).isoformat(),
        status='FAILED',passed=False,broadcast=False,wallet_loaded=False,trading_database_opened=False,
        live_amm_exit_proven=False,windows_stop_proven=False,
        scope='READ_ONLY_RPC_AND_MARKET_WS_NOT_TRADING_OR_LONG_RUN',rpc_proxy_policy='ENVIRONMENT_AS_URLLIB_SCANNER',rpc=[])


async def collect(selected,duration):
    report=base_report();timeout=aiohttp.ClientTimeout(total=8,connect=3,sock_connect=3,sock_read=4)
    async with aiohttp.ClientSession(timeout=timeout,trust_env=True,cookie_jar=aiohttp.DummyCookieJar()) as session:
        # Sequential to keep a diagnostic from adding concurrent RPC load.
        for label,url in [('primary',selected['rpc']),('backup',selected['backup'])]:
            if not url:report['rpc'].append(dict(endpoint=label,status='NOT_CONFIGURED'));continue
            if not valid_url(url,'https'):report['rpc'].append(dict(endpoint=label,status='INVALID_CONFIGURATION'));continue
            report['rpc'].append(await rpc_check(session,url,label))
    if not valid_url(selected['ws'],'wss'):
        report['websocket']=dict(status='INVALID_CONFIGURATION')
    else:
        # Same fixed singleton as the bot. Never open a second provider socket.
        with socket.socket() as guard:
            try:guard.bind(('127.0.0.1',selected['port']));guard.listen(1)
            except OSError as exc:report['websocket']=dict(status='GUARD_UNAVAILABLE_BOT_MAY_BE_RUNNING',error=error(exc))
            else:report['websocket']=await ws_check(selected['ws'],duration,selected['pool'])
    primary=report['rpc'][0]['status']=='PASS';ws=report['websocket']['status']=='PASS'
    report['primary_path_passed']=primary and ws
    report['backup_rpc_verified']=len(report['rpc'])==2 and report['rpc'][1]['status']=='PASS'
    report['passed']=report['primary_path_passed'] and report['backup_rpc_verified']
    report['status']='PASS' if report['passed'] else 'PARTIAL' if report['primary_path_passed'] else 'FAILED'
    report['note']='Backup check is connectivity/chain only; automatic scanner failover is not enabled. A single probe is not RPC p95. Missing backup or market data is not PASS.'
    return report


def save(report,path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');tmp.replace(path)


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--worker',action='store_true')
    parser.add_argument('--duration',type=float,default=30);parser.add_argument('--output',type=Path,default=REPORT)
    args=parser.parse_args(argv)
    if not math.isfinite(args.duration) or not 1<=args.duration<=60:parser.error('duration must be 1..60 seconds')
    if args.worker:
        start=time.monotonic()
        try:report=asyncio.run(asyncio.wait_for(collect(settings(),args.duration),timeout=args.duration+48))
        except Exception as exc:report=base_report();report['error']=error(exc)
        report['elapsed_s']=round(time.monotonic()-start,3);save(report,args.output)
        return 0 if report['passed'] else 2
    # Isolate DNS threads and all library cleanup under a wall-time budget.
    worker_output=args.output.with_name(args.output.name+'.'+uuid.uuid4().hex+'.worker')
    worker_output.parent.mkdir(parents=True,exist_ok=True)
    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker','--duration',str(args.duration),'--output',str(worker_output)],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:code=proc.wait(timeout=args.duration+55)
    except (subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
        proc.kill();proc.wait(timeout=5)
        report=base_report();report.update(status='SUITE_TIMEOUT' if isinstance(exc,subprocess.TimeoutExpired) else 'INTERRUPTED',worker_exit_code=proc.returncode)
        save(report,worker_output);code=2
    try:
        report=json.loads(worker_output.read_text(encoding='utf-8'))
        # A nonzero/crashed worker must not inherit an earlier PASS.
        if code not in (0,2):
            report=base_report();report.update(status='WORKER_FAILED',worker_exit_code=code);save(report,args.output)
    except (OSError,ValueError):
        report=base_report();report.update(status='REPORT_UNAVAILABLE',worker_exit_code=code);save(report,args.output)
    save(report,args.output);worker_output.unlink(missing_ok=True)
    print('READ-ONLY NETWORK CHECK:',report['status'])
    print('RPC:',[(r['endpoint'],r['status']) for r in report.get('rpc',[])])
    print('WebSocket:',report.get('websocket',{}).get('status','NOT_MEASURED'))
    print('Report:',args.output)
    return 0 if report.get('passed') and code==0 else 2

if __name__=='__main__':raise SystemExit(main())
