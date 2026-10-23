"""Read-only independent 0/100 ms candidate histories. No order/preflight API."""
import json
import config
from candidate_features import state,calc,trajectory_features,model_snapshot_features
SCHEMA='''
CREATE TABLE IF NOT EXISTS candidate_seen_v0742(variant TEXT,mint TEXT,first_ts REAL,PRIMARY KEY(variant,mint));
CREATE TABLE IF NOT EXISTS candidate_counts_v0742(variant TEXT,reason TEXT,n INTEGER,PRIMARY KEY(variant,reason));
CREATE TABLE IF NOT EXISTS candidate_first_v0742(variant TEXT,mint TEXT,stage TEXT,ts REAL,signature TEXT,details_json TEXT,PRIMARY KEY(variant,mint,stage));
'''
PARAMETERS=('MIN_TRADES','MIN_BUYERS','MIN_RATIO','MIN_MOMENTUM','MIN_SCORE','MAX_ENTRY_MOMENTUM','MAX_ENTRY_LAST_JUMP','MIN_ENTRY_AGE','ENTRY_QUALITY_V2','EQ_MIN_SAMPLES','EQ_MAX_PULLBACK_PCT','EQ_MIN_RECENT_SLOPE_PCT','EQ_MIN_BUY_PRESSURE_TREND','PRESSURE_GATE_ENABLED','PRESSURE_GATE_MIN_DELTA','STABLE_VOL_GATE_ENABLED','STABLE_VOL_MAX_RET_VOL','MAX_BUYERS_PER_MINT','MAX_POSITIONS','ALLOW_REENTRY','POSITION_SIZE','BLOCK_ENTRIES_WHEN_TRADE_STALLED','EXECUTION_MODEL_ENABLED','PREFLIGHT_ENABLED')
MILESTONES=(1,2,3,4,6,8,12,20,30)

def parameters():
    return dict(thresholds={k:getattr(config,k) for k in PARAMETERS},milestones=MILESTONES,scope='tracked_mints_feature_candidates_before_preflight',portfolio='actual_snapshot_before_primary_tick',history_reset='exclude_until_new_mint',orders=False)

def evaluate(s,now):
    score,gate,ratio,mom=calc(s)
    prices=list(s['prices']);sides=list(s['sides'])
    jump=(prices[-1]/prices[-2]-1)*100 if len(prices)>=2 else 0.
    pullback=(prices[-1]/max(prices)-1)*100 if prices else 0.
    slope=(prices[-1]/prices[-3]-1)*100 if len(prices)>=3 else 0.
    recent=sides[-min(6,len(sides)):];pressure=sum(x=='buy' for x in recent)/max(1,len(recent)) if sides else 1.
    detail=dict(score=score,signal_gate=gate,ratio=ratio,momentum_pct=mom,age_s=now-s['born'],last_jump_pct=jump,pullback_pct=pullback,recent_slope_pct=slope,buy_pressure=pressure,trades=s['trades'],buyers=len(s['buyers']))
    if gate!='PASS':return 'SIGNAL_'+gate,detail
    if score<config.MIN_SCORE:return 'SIGNAL_SCORE_BELOW_MIN',detail
    if now-s['born']<config.MIN_ENTRY_AGE:return 'ANTI_FOMO_TOO_EARLY',detail
    if mom>config.MAX_ENTRY_MOMENTUM:return 'ANTI_FOMO_MOMENTUM_HOT',detail
    if jump>config.MAX_ENTRY_LAST_JUMP:return 'ANTI_FOMO_LAST_JUMP',detail
    if config.ENTRY_QUALITY_V2 and len(prices)>=config.EQ_MIN_SAMPLES:
        if pullback<=-config.EQ_MAX_PULLBACK_PCT:return 'ENTRY_QUALITY_PULLBACK',detail
        if slope<config.EQ_MIN_RECENT_SLOPE_PCT:return 'ENTRY_QUALITY_DECELERATION',detail
        if pressure<config.EQ_MIN_BUY_PRESSURE_TREND:return 'ENTRY_QUALITY_PRESSURE_FADE',detail
    if config.PRESSURE_GATE_ENABLED and trajectory_features(s)[2]<config.PRESSURE_GATE_MIN_DELTA:return 'PRESSURE_GATE',detail
    if config.STABLE_VOL_GATE_ENABLED and model_snapshot_features(s,score)[2]>config.STABLE_VOL_MAX_RET_VOL:return 'STABLE_VOL_GATE',detail
    return 'FEATURE_PASS',detail

def count(c,v,r):
    c.execute('INSERT INTO candidate_counts_v0742 VALUES(?,?,1) ON CONFLICT(variant,reason) DO UPDATE SET n=n+1',(v,r))

def first(c,v,m,stage,now,e,d):
    return c.execute('INSERT OR IGNORE INTO candidate_first_v0742 VALUES(?,?,?,?,?,?)',(v,m,stage,now,e['signature'],json.dumps(d,ensure_ascii=False))).rowcount

def observe(c,v,m,p,e,history,context):
    now=context['now'];born=context['born']
    if 'features' not in history:
        existed=c.execute('SELECT 1 FROM candidate_seen_v0742 WHERE variant=? AND mint=?',(v,m)).fetchone()
        c.execute('INSERT OR IGNORE INTO candidate_seen_v0742 VALUES(?,?,?)',(v,m,now))
        s=history['features']=state();s['born']=born
        history['incomplete']=bool(existed)
        if existed:count(c,v,'HISTORY_RESTARTED')
    s=history['features']
    if s['born']!=born:
        history['incomplete']=True
    if history['incomplete']:
        count(c,v,'EXCLUDED_INCOMPLETE_HISTORY');return
    side=context['side'];trader=context['trader']
    s['trades']+=1;s['buys']+=side=='buy';s['sells']+=side=='sell';s['sides'].append(side)
    if side=='buy' and trader and len(s['buyers'])<config.MAX_BUYERS_PER_MINT:s['buyers'].add(trader)
    s['valid_prices']+=1
    if not s['first']:s['first']=p
    s['last']=p;s['prices'].append(p);s['model_events'].append((side,p,now))
    reason,d=evaluate(s,now);count(c,v,reason)
    d.update(price=p,provider_ts=e['provider_ts'],original_lag_ms=context.get('original_lag_ms'),preflight='NOT_CHECKED',orders_enabled=False)
    if d['signal_gate']=='PASS' and d['score']>=config.MIN_SCORE:first(c,v,m,'SIGNAL_PASS',now,e,d)
    if reason!='FEATURE_PASS':return
    available=context['availability']() if callable(context.get('availability')) else context.get('availability',{'reasons':['UNKNOWN']})
    d['actual_portfolio']=available
    created=first(c,v,m,'FEATURE_PASS',now,e,d)
    if created and v=='SHADOW_100':
        import shadow_preflight
        shadow_preflight.request(c,m,p,now,d)
    if s['trades'] in MILESTONES:first(c,v,m,'ENTRY_MILESTONE',now,e,d)
    for r in available['reasons']:count(c,v,'PORTFOLIO_'+r)
    if not available['reasons']:first(c,v,m,'ACTUAL_PORTFOLIO_AVAILABLE',now,e,d)

def summary(c,limit=100):
    totals={v:{} for v in ('STRICT_0','SHADOW_100')}
    for r in c.execute('SELECT variant,stage,COUNT(*) n FROM candidate_first_v0742 GROUP BY variant,stage'):totals[r[0]][r[1]]=r[2]
    matches={}
    for stage in ('SIGNAL_PASS','FEATURE_PASS','ENTRY_MILESTONE','ACTUAL_PORTFOLIO_AVAILABLE'):
        both=c.execute('SELECT COUNT(*) FROM candidate_first_v0742 a JOIN candidate_first_v0742 b ON a.mint=b.mint AND a.stage=b.stage WHERE a.variant=? AND b.variant=? AND a.stage=?',('STRICT_0','SHADOW_100',stage)).fetchone()[0]
        matches[stage]=dict(both=both,strict_only=totals['STRICT_0'].get(stage,0)-both,shadow_only=totals['SHADOW_100'].get(stage,0)-both)
    rows=[]
    for r in c.execute('SELECT * FROM candidate_first_v0742 ORDER BY ts DESC LIMIT ?',(limit,)):
        d=dict(r);d['details']=json.loads(d.pop('details_json'));rows.append(d)
    return dict(totals=totals,matches=matches,reasons=[dict(r) for r in c.execute('SELECT * FROM candidate_counts_v0742 ORDER BY variant,n DESC')],recent=rows,
      note='Unique tracked mints ever passing each stage, not trades. FEATURE_PASS evaluates every accepted tick; ENTRY_MILESTONE restricts to scanner request milestones (not approval-driven rechecks). Actual portfolio sampled before primary tick; no counterfactual balances, preflight, routes or purchases. Histories reset by eviction/restart/rediscovery are excluded; previous complete observations remain. All first-pass rows included in full JSON export.')
