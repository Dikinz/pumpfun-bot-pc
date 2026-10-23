"""Pooled asynchronous RPC: bounded read retries; writes never fail over.

No endpoint URLs or server exception messages in reports. All calls are opt-in;
this module does not change the PAPER scanner's RPC frequency or providers.
"""
import asyncio,itertools,json,random,time
from collections import deque,Counter
from urllib.parse import urlsplit
import aiohttp

READ_METHODS={'getGenesisHash','getHealth','getVersion','getSlot','getBlockHeight','getLatestBlockhash',
 'isBlockhashValid','getBalance','getTokenAccountBalance','getTokenAccountsByOwner',
 'getAccountInfo','getMultipleAccounts','getSignatureStatuses','getTransaction',
 'getFeeForMessage','getMinimumBalanceForRentExemption','simulateTransaction'}
SEND_METHODS={'sendTransaction','sendBundle'}

class RPCError(RuntimeError):
    def __init__(self,code,kind='RPC_REJECTED'):self.code=code;self.kind=kind;super().__init__(kind)
class UnknownSubmission(RPCError):
    def __init__(self):super().__init__(None,'SUBMISSION_OUTCOME_UNKNOWN_NO_RETRY')

class RobustRPC:
    def __init__(self,endpoints,timeout=5,retries=3,expected_genesis=None,allow_send=False,transport=None):
        if not endpoints or len(endpoints)>8:raise RPCError(None,'INVALID_ENDPOINT_COUNT')
        for url in endpoints:
            p=urlsplit(url)
            if p.scheme!='https' or not p.hostname or p.username or p.password:raise RPCError(None,'HTTPS_RPC_REQUIRED')
        if not 0<timeout<=15 or not 1<=retries<=5:raise RPCError(None,'INVALID_RETRY_BUDGET')
        if allow_send and not expected_genesis:raise RPCError(None,'EXPLICIT_CHAIN_REQUIRED_FOR_SEND')
        self.endpoints=list(endpoints);self.timeout=timeout;self.retries=retries;self.expected_genesis=expected_genesis
        self.allow_send=allow_send;self.transport=transport;self.session=None;self.idx=0;self.ids=itertools.count(1)
        self.verified={};self.events=deque(maxlen=64);self.counts=Counter();self.sem=asyncio.Semaphore(4)
    async def __aenter__(self):
        if self.transport is None:
            self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout),
              connector=aiohttp.TCPConnector(limit=8,limit_per_host=2,ttl_dns_cache=60),
              cookie_jar=aiohttp.DummyCookieJar(),trust_env=False)
        return self
    async def __aexit__(self,*_):
        if self.session is not None:await self.session.close();self.session=None
    async def _request(self,index,method,params):
        payload=dict(jsonrpc='2.0',id=next(self.ids),method=method,params=params)
        if self.transport is not None:
            data=await asyncio.wait_for(self.transport(index,payload),timeout=self.timeout)
        else:
            if self.session is None:raise RPCError(None,'USE_ASYNC_CONTEXT_MANAGER')
            async with self.session.post(self.endpoints[index],json=payload,allow_redirects=False) as response:
                if response.status!=200:raise RPCError(response.status,'HTTP_ERROR')
                chunks=[];size=0
                async for chunk in response.content.iter_chunked(65536):
                    size+=len(chunk)
                    if size>4*1024*1024:raise RPCError(None,'RESPONSE_TOO_LARGE')
                    chunks.append(chunk)
                try:data=json.loads(b''.join(chunks))
                except Exception:raise RPCError(None,'INVALID_JSON') from None
        if not isinstance(data,dict) or data.get('id')!=payload['id']:raise RPCError(None,'INVALID_RPC_ENVELOPE')
        if data.get('error'):
            error=data['error'];code=error.get('code') if isinstance(error,dict) else None
            raise RPCError(code)
        if 'result' not in data:raise RPCError(None,'RESULT_MISSING')
        return data['result']
    async def _chain(self,index):
        if not self.expected_genesis:return
        if time.monotonic()-self.verified.get(index,-1e9)<60:return
        if await self._request(index,'getGenesisHash',[])!=self.expected_genesis:raise RPCError(None,'CHAIN_MISMATCH')
        self.verified[index]=time.monotonic()
    async def call(self,method,params,retries=None):
        send=method in SEND_METHODS
        if method not in READ_METHODS and not send:raise RPCError(None,'METHOD_NOT_ALLOWED')
        if send and not self.allow_send:raise RPCError(None,'SEND_DISABLED')
        attempts=1 if send else (self.retries if retries is None else retries)
        if not 1<=attempts<=5:raise RPCError(None,'INVALID_RETRY_BUDGET')
        async with self.sem:
            last=None
            for attempt in range(attempts):
                index=self.idx%len(self.endpoints);start=time.monotonic();dispatch=False
                try:
                    await self._chain(index)
                    dispatch=True
                    result=await self._request(index,method,params)
                    self.counts['success']+=1
                    self.events.append(dict(endpoint=index,method=method,status='OK',elapsed_ms=(time.monotonic()-start)*1000))
                    return result
                except asyncio.CancelledError:raise
                except Exception as exc:
                    code=exc.code if isinstance(exc,RPCError) else None
                    kind=exc.kind if isinstance(exc,RPCError) else 'TRANSPORT_OR_TIMEOUT'
                    self.counts['errors']+=1;self.events.append(dict(endpoint=index,method=method,status=kind,code=code))
                    if send:
                        # Even a malformed/HTTP response can follow acceptance.
                        if dispatch and not (isinstance(exc,RPCError) and exc.kind=='RPC_REJECTED' and code==-32002):raise UnknownSubmission() from None
                        raise RPCError(code,kind) from None
                    if code in (-32600,-32601,-32602):raise RPCError(code,kind) from None
                    if kind=='USE_ASYNC_CONTEXT_MANAGER':raise RPCError(code,kind) from None
                    last=RPCError(code,kind);self.idx=(index+1)%len(self.endpoints)
                    if attempt+1<attempts:await asyncio.sleep(min(.8,.1*2**attempt)+random.random()*.025)
            raise last from None
    def summary(self):return dict(endpoints=len(self.endpoints),counts=dict(self.counts),recent=list(self.events),send_enabled=self.allow_send)
