"""Sanitized transport diagnostics, bounded retries and observation coverage."""
import json,time,socket,ssl,urllib.error
SCHEMA='''CREATE TABLE IF NOT EXISTS monitor_alerts_v0737(check_id TEXT PRIMARY KEY,trade_id TEXT,ts REAL,kind TEXT,streak INTEGER,details_json TEXT);'''

def diagnostic(exc):
    chain=[];seen=set();current=exc;category='OTHER'
    for _ in range(6):
        if not isinstance(current,BaseException) or id(current) in seen:break
        seen.add(id(current));item={'type':type(current).__name__}
        for field in ('errno','winerror'):
            value=getattr(current,field,None)
            if isinstance(value,int):item[field]=value
        if isinstance(current,urllib.error.HTTPError):
            item['http_status']=current.code;category='HTTP'
            retry=current.headers.get('Retry-After') if current.headers else None
            if retry and retry.isdigit():item['retry_after_s']=int(retry)
            elif retry:
                try:
                    import email.utils,math
                    item['retry_after_s']=max(0,math.ceil(email.utils.parsedate_to_datetime(retry).timestamp()-time.time()))
                except (TypeError,ValueError,OverflowError):pass
        elif isinstance(current,socket.gaierror):category='DNS'
        elif isinstance(current,ssl.SSLError):category='TLS'
        elif isinstance(current,TimeoutError):category='TIMEOUT'
        elif isinstance(current,ConnectionError):category='CONNECTION'
        chain.append(item)
        current=getattr(current,'reason',None) or current.__cause__ or current.__context__
    return dict(category=category,chain=chain,note='No exception messages, URLs, headers or credentials retained.')

def streak(c,tid=None):
    import position_monitor as pm
    n=0;last=None
    where='trade_id=? AND ' if tid is not None else ''
    for row in c.execute('SELECT status,finished_ts,result_json,id FROM position_checks_v0730 WHERE '+where+'finished_ts IS NOT NULL ORDER BY finished_ts DESC,id DESC',(tid,) if tid is not None else ()):
        if row['status']=='INTERRUPTED_STOP':continue
        if row['status'] not in pm.TECHNICAL:break
        if last is None:last=row
        n+=1
    return n,last

def retry(c,tid):
    n,last=streak(c,tid)
    delay=min(300,30*2**min(4,n-3)) if n>=3 else 0
    return dict(streak=n,delay_s=delay,not_before_ts=(last['finished_ts']+delay) if last and delay else None)

def record_alert(c,job,status,now):
    import position_monitor as pm,price_exit_trial as trial
    n,last=streak(c,job['trade_id'])
    global_n,_=streak(c)
    if (n==3 or global_n==3) and status in pm.TECHNICAL and trial.timing(c,job['trade_id'],now)=='BEFORE_CLOSE':
        c.execute('INSERT OR IGNORE INTO monitor_alerts_v0737 VALUES(?,?,?,?,?,?)',(job['id'],job['trade_id'],now,'THREE_TECHNICAL_ERRORS',max(n,global_n),json.dumps(dict(retry(c,job['trade_id']),global_streak=global_n))))

def observation_gaps(c,now):
    """Union of missing recent-evidence windows per position, clipped at PAPER close."""
    import position_monitor as pm
    rows=[];total=0.;current_total=0.
    valid=pm.GOOD|{'AMM_SALE_EXECUTED','AMM_INSUFFICIENT_RESERVES','AMM_SIMULATION_FAILED'}
    for buy in c.execute("SELECT trade_id,mint,ts FROM trades WHERE side='BUY' ORDER BY ts"):
        tid=buy['trade_id'];start=buy['ts'];sell=c.execute("SELECT ts FROM trades WHERE side='SELL' AND trade_id=?",(tid,)).fetchone();end=min(now,sell['ts']) if sell else now
        end=max(start,end);cursor=start;gaps=[];last=None
        for ch in c.execute('SELECT status,finished_ts,result_json FROM position_checks_v0730 WHERE trade_id=? AND finished_ts IS NOT NULL ORDER BY finished_ts',(tid,)):
            t=ch['finished_ts'];r=json.loads(ch['result_json'] or '{}')
            if t<start or t>=end or ch['status'] not in valid:continue
            if not (0<=t-r.get('snapshot_received_ts',0)<=5 and 0<=t-r.get('scheduled_ts',0)<=25):continue
            ttl=90 if r.get('route')=='PUMP_AMM' else 30
            if t>cursor:gaps.append((cursor,t))
            cursor=max(cursor,min(end,t+ttl));last=t
        if cursor<end:gaps.append((cursor,end))
        duration=sum(b-a for a,b in gaps);ongoing=end-cursor if not sell and cursor<end else 0
        total+=duration;current_total+=ongoing
        rows.append(dict(trade_id=tid,mint=buy['mint'],closed=bool(sell),last_reliable_ts=last,uncovered_s=duration,current_uncovered_s=ongoing,
          observed_lifetime_s=end-start,gap_count=len(gaps),recent_gaps=[{'from_ts':a,'to_ts':b,'seconds':b-a} for a,b in gaps[-10:]]))
    return dict(total_position_seconds=total,current_position_seconds=current_total,rows=rows,
      curve_freshness_window_s=30,amm_freshness_window_s=90,note='Sum across positions, including time before first result. Overlapping positions count separately. Recent evidence is not continuous proof of sellability; late/stale results never fill gaps.')

def summary(c,now=None):
    now=time.time() if now is None else now
    active=[]
    for p in c.execute('SELECT trade_id,mint FROM positions'):
        r=retry(c,p['trade_id']);r.update(trade_id=p['trade_id'],mint=p['mint'],wait_s=max(0,(r['not_before_ts'] or now)-now));active.append(r)
    alerts=[dict(x) for x in c.execute('SELECT * FROM monitor_alerts_v0737 ORDER BY ts DESC LIMIT 50')]
    errors=[]
    for r in c.execute('SELECT mint,finished_ts,result_json,status FROM position_checks_v0730 WHERE result_json IS NOT NULL ORDER BY finished_ts DESC LIMIT 300'):
        d=json.loads(r['result_json']);network=d.get('network_error')
        if network:errors.append(dict(mint=r['mint'],ts=r['finished_ts'],status=r['status'],stage=d.get('error_stage') or d.get('progress',{}).get('stage'),error_code=d.get('error_code'),diagnostic=network))
        if len(errors)==20:break
    return dict(active=active,global_streak=streak(c)[0],warning=bool(active) and (streak(c)[0]>=3 or any(r['streak']>=3 for r in active)),alerts=alerts,recent_errors=errors,gaps=observation_gaps(c,now))
