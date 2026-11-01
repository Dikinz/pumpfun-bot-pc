import json,asyncio
from decimal import Decimal
import pytest
import exit_quotes as q,config,main,database as db,execution_model as ex
from test_execution_model import env
from test_primary_integrity import fresh

def quote(mint='a',output=q.SOL,raw='150000000',out='28000000',minimum='27720000'):
    return dict(inputMint=mint,outputMint=output,inAmount=raw,outAmount=out,otherAmountThreshold=minimum,swapMode='ExactIn',slippageBps=100,router='metis',routePlan=[{'swapInfo':{'label':'test'}}])

@pytest.fixture
def prepared(env,monkeypatch):
    c,clock,tick,entry=env
    monkeypatch.setattr(config,'JUPITER_API_KEY','fake-test-key')
    monkeypatch.setattr(q,'FX',{'ts':q.time.time(),'rate':'100','response':{}})
    entry();tick(p=.0002);main.sell('a',.0002,'TEST')
    job=dict(c.execute('select * from quote_jobs_v0723').fetchone())
    inv=dict(c.execute('select * from quote_inventory_v0723').fetchone())
    monkeypatch.setattr(q,'request_json',lambda *a,**k:{'result':{'value':{'decimals':6}}})
    monkeypatch.setattr(q,'get_quote',lambda mint,output,raw,slip:quote(mint,output,raw))
    return c,job,inv

def test_frozen_inventory_units_and_decimals(prepared,monkeypatch):
    c,j,i=prepared
    assert Decimal(i['token_units'])==150
    monkeypatch.setattr(q,'FX',{'ts':0,'rate':'999','response':{}})
    r=q.probe(j,i)
    assert r['raw_amount']=='150000000' and r['floor_lamports']=='27000000'
    assert r['status']=='QUOTE_ACCEPTABLE'
    q.save(c,j,r)
    assert c.execute('select raw_amount from quote_inventory_v0723').fetchone()[0]=='150000000'
    summary=q.summary(c)
    assert summary['latest'][0]['status']=='QUOTE_ACCEPTABLE' and Decimal(summary['latest'][0]['token_units'])==150

def test_quote_does_not_release_cash_or_close_position(prepared):
    c,j,i=prepared
    before=list(c.execute('select * from capital_ledger_v0721'));cash=db.getm('cash');n=c.execute('select count(*) from trades').fetchone()[0]
    q.save(c,j,q.probe(j,i))
    assert list(c.execute('select * from capital_ledger_v0721'))==before and db.getm('cash')==cash
    assert c.execute('select count(*) from trades').fetchone()[0]==n
    assert 'a' in main.P and ex.pending(c,'a','SELL')

def test_unacceptable_price(prepared,monkeypatch):
    c,j,i=prepared
    monkeypatch.setattr(q,'get_quote',lambda mint,output,raw,slip:quote(mint,output,raw,'24000000','23760000'))
    assert q.probe(j,i)['status']=='PRICE_UNACCEPTABLE'

@pytest.mark.parametrize('payload,status',[
 ({'errorCode':'NO_ROUTES_FOUND'},'NO_ROUTE_PROVIDER'),
 ({'error':'unknown issue'},'PROVIDER_ERROR'),
 ({},'INVALID_RESPONSE'),
 (quote(raw='1'),'INVALID_RESPONSE'),
 (quote(output=q.USDC),'INVALID_RESPONSE'),
 (quote(minimum='9999999999'),'INVALID_RESPONSE')])
def test_route_errors_are_not_fabricated(prepared,monkeypatch,payload,status):
    c,j,i=prepared;monkeypatch.setattr(q,'get_quote',lambda *a:payload)
    assert q.probe(j,i)['status']==status

def test_missing_fx_is_not_backfilled_with_future_rate(env,monkeypatch):
    c,clock,tick,entry=env;monkeypatch.setattr(q,'FX',None)
    entry();tick();main.sell('a',1,'TEST')
    monkeypatch.setattr(config,'JUPITER_API_KEY','fake');monkeypatch.setattr(q,'FX',{'ts':q.time.time(),'rate':'100','response':{}})
    job=dict(c.execute('select * from quote_jobs_v0723').fetchone());inv=dict(c.execute('select * from quote_inventory_v0723').fetchone())
    assert q.probe(job,inv)['status']=='MISSING_ENTRY_FX'

def test_no_key_never_calls_network(prepared,monkeypatch):
    c,j,i=prepared;monkeypatch.setattr(config,'JUPITER_API_KEY','')
    def bad(*a,**k):raise AssertionError('network called')
    monkeypatch.setattr(q,'request_json',bad)
    assert q.probe(j,i)['status']=='NOT_CONFIGURED'

def test_rpc_unknown_precision_is_not_assumed(prepared,monkeypatch):
    c,j,i=prepared;monkeypatch.setattr(q,'request_json',lambda *a,**k:{'result':{'value':{}}})
    assert q.probe(j,i)['status']=='METADATA_ERROR'

def test_slow_quote_not_accepted(prepared,monkeypatch):
    c,j,i=prepared;clock={'m':0}
    monkeypatch.setattr(q.time,'monotonic',lambda:clock['m'])
    def delayed(mint,output,raw,slip):clock['m']+=4;return quote(mint,output,raw)
    monkeypatch.setattr(q,'get_quote',delayed)
    assert q.probe(j,i)['status']=='SLOW_RESPONSE'

def test_request_has_no_wallet_or_transaction_fields(monkeypatch):
    seen={}
    def capture(url,stage,**kw):seen.update(url=url,stage=stage,**kw);return {}
    monkeypatch.setattr(q,'request_json',capture);q.get_quote('a',q.SOL,'12',100)
    assert seen['url'].startswith(q.ENDPOINT+'?') and 'amount=12' in seen['url']
    assert 'taker' not in seen['url'] and 'body' not in seen
    assert 'transaction' not in q.sanitize({'transaction':'should-not-persist','outAmount':'1'})

def test_rpc_error_keeps_stage(prepared,monkeypatch):
    c,j,i=prepared
    def bad(*a,**k):raise q.ProbeError('RATE_LIMITED','METADATA',429)
    monkeypatch.setattr(q,'request_json',bad)
    r=q.probe(j,i);assert r['status']=='RATE_LIMITED' and r['stage']=='METADATA' and r['http_status']==429
    q.save(c,j,r)
    assert c.execute('select next_ts-last_ts from quote_jobs_v0723').fetchone()[0]==60

def test_worker_queries_without_any_market_tick(prepared,monkeypatch):
    c,j,i=prepared
    c.execute('update quote_jobs_v0723 set next_ts=0');c.commit()
    async def end_cycle(*a):raise asyncio.CancelledError()
    monkeypatch.setattr(q.asyncio,'sleep',end_cycle)
    with pytest.raises(asyncio.CancelledError):asyncio.run(q.worker())
    assert c.execute('select count(*) from exit_quotes_v0723').fetchone()[0]==1
    assert ex.pending(c,'a','SELL')

def test_http_statuses_and_no_route_are_distinct(monkeypatch):
    import io,urllib.error
    class Opener:
        code=429;body=b'{}'
        def open(self,*a,**kw):raise urllib.error.HTTPError(q.ENDPOINT,self.code,'test',{},io.BytesIO(self.body))
    o=Opener();monkeypatch.setattr(q.urllib.request,'build_opener',lambda *a:o)
    for code,body,expected in [(429,b'{}','RATE_LIMITED'),(401,b'{}','AUTH_ERROR'),(500,b'{}','REQUEST_ERROR'),(400,b'{"errorCode":"NO_ROUTES_FOUND"}','NO_ROUTE_PROVIDER'),(400,b'{"error":"unexpected"}','REQUEST_ERROR')]:
        o.code,o.body=code,body
        with pytest.raises(q.ProbeError) as err:q.request_json(q.ENDPOINT,'QUOTE',key='fake')
        assert err.value.status==expected

@pytest.fixture(autouse=True)
def legacy_quote_opener_backend(monkeypatch):
    import config
    monkeypatch.setattr(config,"RPC_KEEPALIVE",False)
