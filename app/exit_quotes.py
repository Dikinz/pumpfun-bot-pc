"""Jupiter V2 quote-only observer. No wallet, transaction building, signing or sending.

PAPER dollars are interpreted as USDC solely for this sizing experiment.
Token inventory is hypothetical: entry feed price, frozen SOL/USDC reference.
Quote evidence never mutates trades, orders, positions or capital.
"""
import asyncio,json,time,sqlite3,argparse,socket,http.client,urllib.request,urllib.error,urllib.parse
from decimal import Decimal,InvalidOperation,localcontext
from pathlib import Path
import config
from cost_evidence import SOL,USDC

ENDPOINT='https://api.jup.ag/swap/v2/order'
FX=None
FX_STATUS='NOT_STARTED'
SCHEMA='''
CREATE TABLE IF NOT EXISTS quote_inventory_v0723(
 trade_id TEXT PRIMARY KEY,mint TEXT,entry_ts REAL,size_usdc TEXT,entry_sol_price TEXT,
 sol_usdc TEXT,fx_ts REAL,fx_response TEXT,token_units TEXT,decimals INTEGER,raw_amount TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS quote_jobs_v0723(
 order_id TEXT PRIMARY KEY,trade_id TEXT,mint TEXT,signal_ts REAL,signal_price TEXT,
 next_ts REAL,attempts INTEGER DEFAULT 0,last_status TEXT,last_ts REAL,
 max_loss_bps INTEGER,slippage_bps INTEGER);
CREATE TABLE IF NOT EXISTS exit_quotes_v0723(
 id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,started_ts REAL,received_ts REAL,
 elapsed_ms REAL,status TEXT,stage TEXT,http_status INTEGER,raw_amount TEXT,
 out_lamports TEXT,min_lamports TEXT,floor_lamports TEXT,response TEXT);
CREATE INDEX IF NOT EXISTS quote_history_order ON exit_quotes_v0723(order_id,id);
'''

class ProbeError(Exception):
    def __init__(self,status,stage,http_status=None,payload=None):
        self.status,self.stage,self.http_status,self.payload=status,stage,http_status,payload or {}
        super().__init__(status)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None

def request_json(url,stage,body=None,key=None):
    import rpc_timing
    method=body.get('method') if isinstance(body,dict) else 'HTTP_GET'
    with rpc_timing.request(method) as trace:
        if isinstance(body,dict) and body.get('jsonrpc')=='2.0':
            import rpc_budget
            try:
                with rpc_budget.guard(url) as permit:
                    trace.row['rpc_admission_v89']=permit.telemetry()
                    return _request_json(url,stage,body,key,trace)
            except rpc_budget.Deferred as exc:
                raise ProbeError('RPC_DEFERRED',stage,payload={'retry_after_s':exc.retry_after_s,'reason':exc.rpc_budget_reason}) from None
        return _request_json(url,stage,body,key,trace)

def _request_json(url,stage,body=None,key=None,trace=None):
    # Fixed quote host; optional RPC gets no Jupiter credential. No redirect leaks.
    headers={'Accept':'application/json','User-Agent':'paper-exit-quote-lab/0.7.23'}
    if key:headers['x-api-key']=key
    if body is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,headers=headers)
    try:
        import rpc_transport_v99 as transport
        if isinstance(body,dict) and body.get('jsonrpc')=='2.0' and key is None and getattr(config,'RPC_KEEPALIVE',False) and transport.available(url):
            raw=transport.post(url,req.data,trace,1024*1024)
        else:
            trace.row['transport_v99']='URLLIB'
            with trace.phase('OPEN_HEADERS'):
                response=urllib.request.build_opener(NoRedirect()).open(req,timeout=6)
            with response as r:
                with trace.phase('READ_BODY'):raw=r.read(1024*1024+1)
        with trace.phase('DECODE_VALIDATE'):
            if len(raw)>1024*1024:raise ProbeError('INVALID_RESPONSE',stage)
            data=json.loads(raw)
            if not isinstance(data,dict):raise ProbeError('INVALID_RESPONSE',stage)
            if data.get('error'):
                code=data['error'].get('code') if isinstance(data['error'],dict) else None
                trace.row['rpc_error_code']=code if type(code)==int else None
                trace.row['rpc_rejected']=True
            return data
    except urllib.error.HTTPError as e:
        status='AUTH_ERROR' if e.code in (401,403) else 'RATE_LIMITED' if e.code==429 else 'REQUEST_ERROR'
        try:data=json.loads(e.read(16384))
        except Exception:data={}
        if stage=='QUOTE' and e.code==400 and isinstance(data,dict) and no_route(data):status='NO_ROUTE_PROVIDER'
        failure=ProbeError(status,stage,e.code,sanitize(data))
        failure.retry_after_s=__import__('rpc_budget').retry_after(e.headers)
        raise failure from None
    except (TimeoutError,socket.timeout):raise ProbeError('TIMEOUT',stage) from None
    except urllib.error.URLError as e:
        raise ProbeError('TIMEOUT' if isinstance(e.reason,(TimeoutError,socket.timeout)) else 'REQUEST_ERROR',stage) from None
    except (OSError,http.client.HTTPException) as exc:raise ProbeError('REQUEST_ERROR',stage) from exc
    except (ValueError,UnicodeError):raise ProbeError('INVALID_RESPONSE',stage) from None

def sanitize(data):
    # Never persist transactions, headers, arbitrary error text or credentials.
    fields=('inputMint','outputMint','inAmount','outAmount','otherAmountThreshold','swapMode','slippageBps',
      'priceImpact','priceImpactPct','router','routePlan','feeBps','platformFee','signatureFeeLamports',
      'prioritizationFeeLamports','rentFeeLamports','contextSlot','errorCode')
    return {k:data[k] for k in fields if k in data} if isinstance(data,dict) else {}

def no_route(data):
    code=data.get('errorCode') or data.get('error')
    return code in ('NO_ROUTES_FOUND','COULD_NOT_FIND_ANY_ROUTE','TOKEN_NOT_TRADABLE')

def integer(value):
    if not isinstance(value,(str,int)) or isinstance(value,bool) or not str(value).isdigit():raise ValueError('integer required')
    return int(value)

def validate(data,mint,output,raw):
    if no_route(data):raise ProbeError('NO_ROUTE_PROVIDER','QUOTE',payload=sanitize(data))
    if data.get('error') or data.get('errorCode'):raise ProbeError('PROVIDER_ERROR','QUOTE',payload=sanitize(data))
    try:
        if data['inputMint']!=mint or data['outputMint']!=output or integer(data['inAmount'])!=int(raw):raise ValueError()
        out=integer(data['outAmount']);minimum=integer(data['otherAmountThreshold'])
        if out<=0 or not 0<minimum<=out or data.get('swapMode')!='ExactIn':raise ValueError()
        if not data.get('router') and not data.get('routePlan'):raise ValueError()
        return out,minimum
    except (KeyError,TypeError,ValueError):raise ProbeError('INVALID_RESPONSE','QUOTE',payload=sanitize(data)) from None

def get_quote(mint,output,raw,slippage):
    params={'inputMint':mint,'outputMint':output,'amount':str(raw),'swapMode':'ExactIn','slippageBps':slippage}
    # Intentionally omit taker: quote without a transaction or wallet.
    return request_json(ENDPOINT+'?'+urllib.parse.urlencode(params),'QUOTE',key=config.JUPITER_API_KEY)

def fetch_fx():
    started=time.monotonic();data=get_quote(SOL,USDC,1000000000,100)
    out,_=validate(data,SOL,USDC,1000000000)
    if time.monotonic()-started>3:raise ProbeError('SLOW_RESPONSE','FX')
    return {'ts':time.time(),'rate':str(Decimal(out)/Decimal(1000000)),'response':sanitize(data)}

def on_entry(c,tid,mint,price,size,now=None):
    now=time.time() if now is None else now
    fx=FX if FX and 0<=now-FX['ts']<=120 else None
    units=None
    if fx:
        with localcontext() as ctx:
            ctx.prec=60;units=str(Decimal(str(size))/Decimal(fx['rate'])/Decimal(str(price)))
    c.execute('INSERT INTO quote_inventory_v0723 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      (tid,mint,now,str(size),str(price),fx['rate'] if fx else None,fx['ts'] if fx else None,
       json.dumps(fx['response']) if fx else None,units,None,None,'NEEDS_DECIMALS' if fx else 'MISSING_ENTRY_FX'))

def on_exit(c,oid,tid,mint,price,now):
    import auto_simulation
    auto_simulation.enqueue(c,oid,tid,mint,price,now)
    c.execute('INSERT OR IGNORE INTO quote_jobs_v0723(order_id,trade_id,mint,signal_ts,signal_price,next_ts,last_status,max_loss_bps,slippage_bps) VALUES(?,?,?,?,?,?,?,?,?)',
      (oid,tid,mint,now,str(price),now+.25,'WAITING',config.QUOTE_MAX_LOSS_BPS,config.QUOTE_SLIPPAGE_BPS))

def resolve_amount(inv):
    if not inv or not inv['token_units']:raise ProbeError('MISSING_ENTRY_FX','SIZING')
    if inv['raw_amount']:return inv['decimals'],inv['raw_amount']
    data=request_json(config.QUOTE_RPC_URL,'METADATA',{'jsonrpc':'2.0','id':1,'method':'getTokenSupply','params':[inv['mint'],{'commitment':'confirmed'}]})
    if data.get('error'):raise ProbeError('METADATA_ERROR','METADATA')
    try:
        d=data['result']['value']['decimals']
        if type(d)!=int or not 0<=d<=18:raise ValueError()
        with localcontext() as ctx:
            ctx.prec=60;raw=int(Decimal(inv['token_units'])*(Decimal(10)**d))
        if not 0<raw<2**64:raise ValueError()
        return d,str(raw)
    except (KeyError,ValueError,TypeError,InvalidOperation):raise ProbeError('METADATA_ERROR','METADATA') from None

def probe(job,inv):
    wall=time.time();mono=time.monotonic();result={'started_ts':wall,'stage':'QUOTE','http_status':None,'response':{},'raw_amount':None,'decimals':None,'out_lamports':None,'min_lamports':None,'floor_lamports':None}
    try:
        if not config.JUPITER_API_KEY:raise ProbeError('NOT_CONFIGURED','CONFIG')
        d,raw=resolve_amount(inv);result.update(decimals=d,raw_amount=raw)
        quote_start=time.monotonic();data=get_quote(job['mint'],SOL,raw,job['slippage_bps'])
        out,minimum=validate(data,job['mint'],SOL,raw)
        if integer(data.get('slippageBps'))!=job['slippage_bps']:raise ProbeError('INVALID_RESPONSE','QUOTE')
        with localcontext() as ctx:
            ctx.prec=60
            floor=int(Decimal(raw)/(Decimal(10)**d)*Decimal(job['signal_price'])*Decimal(10**9)*(Decimal(10000-job['max_loss_bps'])/Decimal(10000)))
        result.update(out_lamports=str(out),min_lamports=str(minimum),floor_lamports=str(floor),response=sanitize(data))
        result['status']='SLOW_RESPONSE' if time.monotonic()-quote_start>3 else 'PRICE_UNACCEPTABLE' if minimum<floor else 'QUOTE_ACCEPTABLE'
    except ProbeError as e:result.update(status=e.status,stage=e.stage,http_status=e.http_status,response=e.payload)
    except Exception:result.update(status='INTERNAL_ERROR',stage='LOCAL')
    result.update(received_ts=time.time(),elapsed_ms=(time.monotonic()-mono)*1000)
    if result['status'] in ('QUOTE_ACCEPTABLE','PRICE_UNACCEPTABLE'):
        import execution_verification as verification
        sized=dict(inv,decimals=result['decimals'],raw_amount=result['raw_amount'])
        result['verification']=verification.verify(job,sized,result)
    return result

def save(c,job,result):
    r=result
    c.execute('INSERT INTO exit_quotes_v0723(order_id,started_ts,received_ts,elapsed_ms,status,stage,http_status,raw_amount,out_lamports,min_lamports,floor_lamports,response) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      (job['order_id'],r['started_ts'],r['received_ts'],r['elapsed_ms'],r['status'],r['stage'],r['http_status'],r['raw_amount'],r['out_lamports'],r['min_lamports'],r['floor_lamports'],json.dumps(r['response'])))
    if r.get('verification'):
        import execution_verification as verification
        verification.save(c,c.execute('SELECT last_insert_rowid()').fetchone()[0],job['order_id'],r['verification'])
    if r['raw_amount']:
        c.execute("UPDATE quote_inventory_v0723 SET decimals=?,raw_amount=?,status='SIZED' WHERE trade_id=? AND raw_amount IS NULL",(r['decimals'],r['raw_amount'],job['trade_id']))
    statuses={r['status']}|{x.get('status') for x in r.get('verification',{}).values() if isinstance(x,dict)}
    delay=60 if statuses & {'AUTH_ERROR','RATE_LIMITED','MISSING_ENTRY_FX','METADATA_ERROR'} or r['received_ts']-job['signal_ts']>600 else 15
    c.execute('UPDATE quote_jobs_v0723 SET attempts=attempts+1,last_ts=?,last_status=?,next_ts=? WHERE order_id=?',(r['received_ts'],r['status'],max(time.time(),r['received_ts'])+delay,job['order_id']))

async def refresh_fx():
    global FX,FX_STATUS
    try:FX=await asyncio.to_thread(fetch_fx);FX_STATUS='READY'
    except ProbeError as e:FX_STATUS=e.status
    except Exception:FX_STATUS='INTERNAL_ERROR'

async def worker():
    import database as db
    last_fx=time.monotonic();backoff=0
    while True:
        if time.monotonic()-last_fx>=60:
            await refresh_fx();last_fx=time.monotonic()
        now=time.time()
        with db.con() as c:
            c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('quote_fx_status',?)",(FX_STATUS,))
            c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('quote_worker_ts',?)",(str(now),))
            row=c.execute("SELECT q.* FROM quote_jobs_v0723 q JOIN paper_orders_v0720 o USING(order_id) WHERE q.next_ts<=? AND (o.status='PENDING' OR q.attempts=0) ORDER BY q.next_ts LIMIT 1",(now,)).fetchone()
            job=dict(row) if row else None
            row=c.execute('SELECT * FROM quote_inventory_v0723 WHERE trade_id=?',(job['trade_id'],)).fetchone() if job else None
            inv=dict(row) if row else None
        if job and time.monotonic()>=backoff:
            result=await asyncio.to_thread(probe,job,inv)
            with db.con() as c:save(c,job,result)
            statuses={result['status']}|{x.get('status') for x in result.get('verification',{}).values() if isinstance(x,dict)}
            if statuses & {'RATE_LIMITED','AUTH_ERROR'}:backoff=time.monotonic()+60
        await asyncio.sleep(2) # one request worker; no feed callbacks or unbounded task queue

def summary(c):
    meta=dict(c.execute('SELECT k,v FROM meta'))
    counts=[dict(r) for r in c.execute('SELECT status,COUNT(*) n FROM exit_quotes_v0723 GROUP BY status')]
    latest=[dict(r) for r in c.execute('SELECT q.mint,q.signal_ts,q.max_loss_bps,q.slippage_bps,q.attempts,q.last_status job_status,i.token_units,i.sol_usdc,i.fx_ts,o.status paper_status,r.* FROM quote_jobs_v0723 q JOIN paper_orders_v0720 o USING(order_id) LEFT JOIN quote_inventory_v0723 i ON i.trade_id=q.trade_id LEFT JOIN exit_quotes_v0723 r ON r.id=(SELECT MAX(id) FROM exit_quotes_v0723 WHERE order_id=q.order_id) ORDER BY q.signal_ts DESC LIMIT 30')]
    import execution_verification as verification
    return {'verification':verification.summary(c),'provider':'Jupiter Swap V2 quote-only','fx_status':meta.get('quote_fx_status','NOT_STARTED'),'worker_ts':float(meta.get('quote_worker_ts',0)),
      'counts':counts,'latest':latest,'jobs':c.execute('SELECT COUNT(*) FROM quote_jobs_v0723').fetchone()[0],
      'missing_entry_fx':c.execute("SELECT COUNT(*) FROM quote_inventory_v0723 WHERE status='MISSING_ENTRY_FX'").fetchone()[0],
      'note':'Provider quote is not a simulation or fill. NO_ROUTE is limited to this provider and volume. Hypothetical inventory uses PAPER-$=USDC, entry tick price and frozen SOL/USDC quote; no entry price impact. Price floor is relative to the exit signal, before unknown network costs. Ledger and pending orders are unchanged.'}

def check(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN');return summary(c)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=config.DB);ap.add_argument('--out',default='exit_quote_results.json');a=ap.parse_args()
    Path(a.out).write_text(json.dumps(check(a.db),ensure_ascii=False,indent=2),encoding='utf-8')
