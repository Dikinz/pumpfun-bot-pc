"""One HTTPS connection per thread/endpoint; no automatic request retries."""
import hashlib,http.client,os,sys,threading,urllib.error,urllib.parse,urllib.request

_LOCAL=threading.local()

def available(url):
    parts=urllib.parse.urlsplit(url)
    # Preserve urllib's explicit/environment/system proxy behavior. Never
    # silently bypass it; direct keep-alive is used only without an active proxy.
    return not (urllib.request.getproxies().get('https') and not urllib.request.proxy_bypass(parts.hostname or ''))

def close():
    connection=getattr(_LOCAL,'connection',None)
    _LOCAL.connection=None;_LOCAL.key=None
    if connection is not None:
        try:connection.close()
        except Exception:pass # Never mask the failed request.

def post(url,body,trace,limit):
    parts=urllib.parse.urlsplit(url)
    if parts.scheme!='https' or not parts.hostname:raise ValueError('RPC_REQUIRES_HTTPS')
    key=(os.getpid(),hashlib.sha256(url.encode()).hexdigest())
    if getattr(_LOCAL,'key',None)!=key:close()
    conn=getattr(_LOCAL,'connection',None)
    reused=conn is not None and conn.sock is not None
    if conn is None:
        conn=http.client.HTTPSConnection(parts.hostname,parts.port or 443,timeout=6)
        _LOCAL.connection=conn;_LOCAL.key=key
    trace.row.update(transport_v99='HTTPS_KEEPALIVE',connection_reused_v99=reused)
    path=(parts.path or '/')+('?'+parts.query if parts.query else '')
    headers={'Content-Type':'application/json','Accept-Encoding':'identity','User-Agent':'Python-urllib/%d.%d'%sys.version_info[:2]}
    try:
        if conn.sock is not None:conn.sock.settimeout(6)
        with trace.phase('OPEN_HEADERS'):
            conn.request('POST',path,body,headers);response=conn.getresponse()
        if not 200<=response.status<300:raise urllib.error.HTTPError(url,response.status,response.reason,response.msg,None)
        with trace.phase('READ_BODY'):data=response.read(limit+1)
        if response.will_close or len(data)>limit:close()
        return data
    except BaseException:
        close();raise
