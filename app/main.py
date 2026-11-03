import system_health
import run_audit,exit_audit,time_check
import runtime_status
import clock_lab
import time_order_audit as time_audit
import entry_diagnostics
import shadow_preflight
import diagnostics,exit_quotes,auto_simulation,preentry,position_monitor,wallet_costs
import asyncio,json,os,time,math,gc,random,urllib.request,urllib.parse,hashlib,socket
import psutil
from collections import OrderedDict, deque
import stream_diagnostics
import websockets,config,database as db
import model_gate
import branch_transport
import runtime_control
import observation_lab as observation
import replay_log
import cost_evidence
import execution_model as execution
import paper_capital as capital
import uuid,copy,functools

S={}; P={}; SHADOW_AB={}; SHADOW_ENTERED=set(); MICRO_PENDING={}; MICRO_POS={}; MICRO_DONE=set(); ENTERED=set(); PASS_SEEN=set(); PASS_PIN={}; ACTIVE=OrderedDict(); FIRST_TRADE_SEEN=set(); SUB_OK_MINTS=set()
TRADE_GAP_START=None
PP_SHADOW={}
PP_V2_HITS={}
PP_V2_LAST_CHECK={}
PP_V2_ARMED={}
FAST_SHADOW_V3={}
FAST_RECOVERY_30={}
FAST_RECOVERY_SIGNAL={}
FAST_RECOVERY_RISK={}
FAST_REENTRY={}
SHADOW_DB_QUEUE=deque(maxlen=4096)
LAST_SHADOW_FLUSH=0.0
LAST_CLEANUP_TS=0.0
SHADOW_PAUSED=False
HOT={"shadow_checks":0,"shadow_triggers":0,"pp_calls":0,"trade_calls":0,"trade_ms":0.0,"manage_ms":0.0,"db_ms":0.0,"signal_ms":0.0}

def atomic_primary(fn):
    @functools.wraps(fn)
    def wrapped(*args,**kwargs):
        positions=copy.deepcopy(P);entered=set(ENTERED)
        try:
            with db.atomic():return fn(*args,**kwargs)
        except BaseException as exc:
            P.clear();P.update(positions);ENTERED.clear();ENTERED.update(entered)
            system_health.failure(config.DB,"PRIMARY_TRANSACTION_ROLLBACK",exc)
            raise SystemExit('Primary accounting transaction rolled back. Inspect error before restart: '+repr(exc)) from exc
    return wrapped

def restore_positions():
    with db.con() as c:
        rows=c.execute("SELECT * FROM positions").fetchall()
        ENTERED.update(r[0] for r in c.execute("SELECT DISTINCT mint FROM trades WHERE side='BUY'"))
    for x in rows:
        m=x["mint"]; P[m]={"entry":float(x["entry"]),"last":float(x["current"]),"last_update":float(x["last_update_ts"]),"high":float(x["high"]),"ts":float(x["opened_ts"]),"mfe":float(x["mfe"]),"mae":float(x["mae"]),"kind":x["entry_kind"],"post_trades":0,"post_buys":0,"post_sells":0,"gap_seconds":0.0,"gap_count":0,"pp_armed":False,"ab_variant":getattr(config,"FINAL_PP_VARIANT","PP_10_7")}
        P[m].update(trade_id=x['trade_id'],size=float(x['size']),cost_profiles=json.loads(x['cost_profiles']),gap_seconds=float(x['gap_seconds']),gap_count=int(x['gap_count']))
        ENTERED.add(m)
        S.setdefault(m,state())
    # Resume the remaining observation window after a restart; missing ticks stay missing.
    for r in db.con().execute("SELECT mint FROM signal_observations_v0722 WHERE ts+300>?",(time.time(),)):
        S.setdefault(r[0],state())

def num(x):
    try:
        v=float(x); return v if math.isfinite(v) and v>0 else 0.0
    except:return 0.0

def extract_price(x):
    for k in ("price","tokenPrice","priceSol","priceSOL"):
        p=num(x.get(k))
        if p:return p,k
    sol=num(x.get("vSolInBondingCurve") or x.get("virtualSolReserves")); tok=num(x.get("vTokensInBondingCurve") or x.get("virtualTokenReserves"))
    if sol and tok:return sol/tok,"virtual_reserves"
    mc=num(x.get("marketCapSol")); supply=num(x.get("tokenTotalSupply") or x.get("totalSupply"))
    if mc and supply:return mc/supply,"marketcap_supply"
    return 0.0,"missing"

def state():
    return {"born":time.time(),"trades":0,"buys":0,"sells":0,"buyers":set(),"first":0.0,"last":0.0,"valid_prices":0,"price_source":"","milestone":0,"prices":deque(maxlen=8),"sides":deque(maxlen=8),"model_events":deque(maxlen=20)}

def calc(s):
    ratio=s["buys"]/max(1,s["sells"]); mom=((s["last"]/s["first"])-1)*100 if s["first"]>0 and s["last"]>0 and s["valid_prices"]>=2 else 0.0
    sc=0
    if s["trades"]>=config.MIN_TRADES:sc+=25
    if len(s["buyers"])>=config.MIN_BUYERS:sc+=20
    if ratio>=config.MIN_RATIO:sc+=20
    if s["valid_prices"]>=2 and mom>=config.MIN_MOMENTUM:sc+=25
    if s["trades"]>=6:sc+=10
    gate=("samples" if s["trades"]<config.MIN_TRADES else "buyers" if len(s["buyers"])<config.MIN_BUYERS else "buy/sell" if ratio<config.MIN_RATIO else "price data" if s["valid_prices"]<2 else "momentum" if mom<config.MIN_MOMENTUM else "PASS")
    return sc,gate,ratio,mom

def trajectory_features(s):
    prices=list(s.get("prices",())) if s else []
    sides=list(s.get("sides",())) if s else []
    rets=[]
    for a,b in zip(prices,prices[1:]):
        if a>0: rets.append((b/a-1)*100.0)
    n=len(rets); half=max(1,n//2)
    early=(sum(rets[:half])/len(rets[:half])) if rets[:half] else 0.0
    recent=(sum(rets[half:])/len(rets[half:])) if rets[half:] else early
    accel=recent-early
    mean=(sum(rets)/n) if n else 0.0
    vol=(sum((x-mean)**2 for x in rets)/n)**0.5 if n else 0.0
    last_ret=rets[-1] if rets else 0.0
    pullback=(prices[-1]/max(prices)-1)*100.0 if prices and max(prices)>0 else 0.0
    sh=max(1,len(sides)//2)
    es=sides[:sh]; rs=sides[sh:] or es
    ep=sum(x=="buy" for x in es)/max(1,len(es)); rp=sum(x=="buy" for x in rs)/max(1,len(rs))
    streak=0
    for x in reversed(sides):
        if x=="buy": streak+=1
        else: break
    return len(prices),accel,(rp-ep),pullback,vol,last_ret,streak,rp,ep


def sweet_score_v0696(s):
    _,a,pd,pb,v,lr,bs,_,_=trajectory_features(s)
    z=(20 if a>=1 else 10 if a>=0 else 0)+(25 if pd>=.15 else 15 if pd>=.08 else 5 if pd>=0 else 0)+(15 if v>=2.5 else 8 if v>=1.5 else 0)+(15 if lr>=2 else 8 if lr>=1 else 0)+(15 if bs>=4 else 8 if bs>=3 else 0)+(10 if pb>=-1 else 5 if pb>=-2 else 0)
    if a>4:z-=15
    elif a>2.5:z-=7
    if pd>.35:z-=15
    elif pd>.25:z-=7
    if v>6:z-=12
    elif v>4.5:z-=5
    if lr>6:z-=10
    if pb<-2:z-=8
    return max(0,z)

def model_snapshot_features(s,score=0):
    ev=list(s.get("model_events",())) if s else []
    prices=[float(x[1]) for x in ev if float(x[1])>0]
    sides=[x[0] for x in ev]
    rets=[(b/a-1)*100.0 for a,b in zip(prices,prices[1:]) if a>0]
    def mean(xs): return sum(xs)/len(xs) if xs else 0.0
    mu=mean(rets); vol=(mean([(x-mu)**2 for x in rets])**0.5) if rets else 0.0
    half=max(1,len(rets)//2); accel=mean(rets[half:])-mean(rets[:half]) if rets else 0.0
    last1=rets[-1] if rets else 0.0; last3=mean(rets[-3:])
    pullback=(prices[-1]/max(prices)-1)*100.0 if prices and max(prices)>0 else 0.0
    def bp(n):
        z=sides[-n:] if sides else []
        return sum(x=="buy" for x in z)/max(1,len(z))
    streak=0
    for x in reversed(sides):
        if x=="buy": streak+=1
        else: break
    ratio=(s["buys"]/max(1,s["sells"])) if s else 0.0
    mom=((s["last"]/s["first"])-1)*100 if s and s["first"] and s["last"] and s["valid_prices"]>=2 else 0.0
    return (len(ev),mu,vol,accel,last1,last3,pullback,bp(5),bp(10),bp(20),bp(5)-bp(20),streak,int(score),int(s["trades"] if s else 0),int(len(s["buyers"]) if s else 0),ratio,mom)


def _shadow_cost(m,entry,exit_price,ideal_pnl,profile):
    a=config.EXEC_AUDIT_PROFILES[profile]
    h=int(hashlib.sha256((m+"|v061|"+profile).encode()).hexdigest()[:16],16)
    u=(h%1000000)/1000000.0; latency=a["latency_base"]+a["latency_jitter"]*((h>>20)%10000)/10000.0; failed=u<a["failed_prob"]
    exit_notional=max(0.0,config.POSITION_SIZE*(exit_price/entry))
    cost=config.POSITION_SIZE*a["buy_slip"]+exit_notional*a["sell_slip"]+a["fee"]*(config.POSITION_SIZE+exit_notional)+a["priority"]*(3 if failed else 2)+exit_notional*a["latency_slip_per_s"]*(latency/1000.0)+(exit_notional*a["failed_extra"] if failed else 0.0)
    return ideal_pnl-cost

def _shadow_roundtrip_cost(m,entry,exit_price,ideal_pnl,profile,salt="v0711_reentry"):
    """Execution-net PnL for an additional buy+sell round trip after the real FAST exit."""
    a=config.EXEC_AUDIT_PROFILES[profile]
    h=int(hashlib.sha256((m+"|"+salt+"|"+profile).encode()).hexdigest()[:16],16)
    u=(h%1000000)/1000000.0
    latency=a["latency_base"]+a["latency_jitter"]*((h>>20)%10000)/10000.0
    failed=u<a["failed_prob"]
    exit_notional=max(0.0,config.POSITION_SIZE*(exit_price/entry))
    cost=config.POSITION_SIZE*a["buy_slip"]+exit_notional*a["sell_slip"]+a["fee"]*(config.POSITION_SIZE+exit_notional)+a["priority"]*(3 if failed else 2)+exit_notional*a["latency_slip_per_s"]*(latency/1000.0)+(exit_notional*a["failed_extra"] if failed else 0.0)
    return ideal_pnl-cost,cost,latency,1 if failed else 0

def fast30_mfe_bin(mfe):
    return "MFE>=10%" if float(mfe)>=0.10 else ("MFE 5-10%" if float(mfe)>=0.05 else "MFE<5%")

def fast_recovery30_start(m,q,p,fast_pnl):
    if not getattr(config,"FAST_RECOVERY30_ENABLED",False): return
    now=time.time(); mfe=float(q.get("mfe",0.0)); mae=float(q.get("mae",0.0)); hold=max(0.0,now-q["ts"])
    FAST_RECOVERY_30[m]={"entry":q["entry"],"fast_exit":p,"fast_pnl":fast_pnl,"fast_mfe":mfe,"fast_mae":mae,"fast_hold":hold,"mfe_bin":fast30_mfe_bin(mfe),
      "started":now,"last":p,"last_event_ts":now,"events":0,"max_price":p,"max_pnl":fast_pnl,"max_delta":0.0,"max_time":0.0,
      "min_price":p,"min_pnl":fast_pnl,"min_delta":0.0,"min_time":0.0}
    db.inc("fast30_started")

def fast_recovery30_finalize(m,source="WATCHDOG_LAST",now=None):
    q=FAST_RECOVERY_30.pop(m,None)
    if not q:return
    now=time.time() if now is None else now; p=float(q.get("last") or q["fast_exit"]); entry=float(q["entry"]); fast_pnl=float(q["fast_pnl"])
    final_pnl=config.POSITION_SIZE*(p/entry-1); delta=final_pnl-fast_pnl
    low_fast=_shadow_cost(m,entry,q["fast_exit"],fast_pnl,"LOW"); low_rec=_shadow_cost(m,entry,p,final_pnl,"LOW")
    base_fast=_shadow_cost(m,entry,q["fast_exit"],fast_pnl,"BASE"); base_rec=_shadow_cost(m,entry,p,final_pnl,"BASE")
    max_base=_shadow_cost(m,entry,q["max_price"],q["max_pnl"],"BASE")
    payload={"ts":now,"mint":m,"entry":entry,"fast_exit":q["fast_exit"],"fast_pnl":fast_pnl,"fast_mfe":q["fast_mfe"],"fast_mae":q["fast_mae"],"fast_hold":q["fast_hold"],"mfe_bin":q["mfe_bin"],
      "final_price":p,"final_pnl":final_pnl,"delta_vs_fast":delta,"low_fast_pnl":low_fast,"low_recovery_pnl":low_rec,"low_delta":low_rec-low_fast,
      "base_fast_pnl":base_fast,"base_recovery_pnl":base_rec,"base_delta":base_rec-base_fast,"max_price":q["max_price"],"max_pnl":q["max_pnl"],"max_delta":q["max_delta"],
      "max_base_pnl":max_base,"max_base_delta":max_base-base_fast,"time_to_max":q["max_time"],"min_price":q["min_price"],"min_pnl":q["min_pnl"],"min_delta":q["min_delta"],"time_to_min":q["min_time"],
      "events_after_fast":q["events"],"final_source":source,"last_event_age":max(0.0,now-float(q.get("last_event_ts",now))),"observed_age":max(0.0,now-float(q["started"]))}
    SHADOW_DB_QUEUE.append(("fast30_done",payload))

def fast_recovery30_tick(m,p):
    q=FAST_RECOVERY_30.get(m)
    if not q or p<=0:return
    now=time.time(); age=max(0.0,now-q["started"])
    # Do not let the first print after the 30s boundary leak into the fixed-horizon result.
    # Finalize with the last price observed at or before 30s; freshness is exposed separately.
    if age>=config.FAST_RECOVERY30_SECONDS:
        fast_recovery30_finalize(m,"TICK",now); return
    q["last"]=p; q["last_event_ts"]=now; q["events"]+=1
    pnl=config.POSITION_SIZE*(p/q["entry"]-1); delta=pnl-q["fast_pnl"]
    if delta>q["max_delta"]: q["max_delta"]=delta; q["max_pnl"]=pnl; q["max_price"]=p; q["max_time"]=age
    if delta<q["min_delta"]: q["min_delta"]=delta; q["min_pnl"]=pnl; q["min_price"]=p; q["min_time"]=age

def fast_recovery_signal_start(m,q,p,fast_pnl):
    if not getattr(config,"FAST_RECOVERY_SIGNAL_ENABLED",False): return
    now=time.time(); mfe=float(q.get("mfe",0.0)); mae=float(q.get("mae",0.0)); hold=max(0.0,now-q["ts"])
    FAST_RECOVERY_SIGNAL[m]={"entry":float(q["entry"]),"fast_exit":float(p),"fast_pnl":float(fast_pnl),"fast_mfe":mfe,"fast_mae":mae,"fast_hold":hold,
      "mfe_bin":fast30_mfe_bin(mfe),"started":now,"last":float(p),"last_event_ts":now,"events":0,"prices":deque(maxlen=12),"sides":deque(maxlen=12),
      "armed":False,"arm_age":0.0,"arm_price":0.0,"peak_price":float(p),"peak_recovery":0.0,"max_pnl":float(fast_pnl),"max_delta":0.0,"max_time":0.0,
      "min_price":float(p),"min_pnl":float(fast_pnl),"min_delta":0.0,"min_time":0.0,"recent_momentum":0.0,"buy_pressure":0.0}
    db.inc("fast_signal_started")

def fast_recovery_signal_finalize(m,reason,source="TICK",now=None):
    q=FAST_RECOVERY_SIGNAL.pop(m,None)
    if not q:return
    now=time.time() if now is None else now
    p=float(q.get("last") or q["fast_exit"]); entry=float(q["entry"]); fast_pnl=float(q["fast_pnl"])
    pnl=config.POSITION_SIZE*(p/entry-1); delta=pnl-fast_pnl
    low_fast=_shadow_cost(m,entry,q["fast_exit"],fast_pnl,"LOW"); low_sig=_shadow_cost(m,entry,p,pnl,"LOW")
    base_fast=_shadow_cost(m,entry,q["fast_exit"],fast_pnl,"BASE"); base_sig=_shadow_cost(m,entry,p,pnl,"BASE")
    max_base=_shadow_cost(m,entry,q["peak_price"],q["max_pnl"],"BASE")
    payload={"ts":now,"mint":m,"entry":entry,"fast_exit":q["fast_exit"],"fast_pnl":fast_pnl,"fast_mfe":q["fast_mfe"],"fast_mae":q["fast_mae"],"fast_hold":q["fast_hold"],"mfe_bin":q["mfe_bin"],
      "exit_price":p,"exit_pnl":pnl,"exit_reason":reason,"exit_age":max(0.0,now-q["started"]),"events_after_fast":q["events"],"armed":1 if q["armed"] else 0,
      "arm_age":q["arm_age"],"arm_price":q["arm_price"],"peak_price":q["peak_price"],"peak_recovery_pct":q["peak_recovery"]*100.0,"max_delta":q["max_delta"],"time_to_max":q["max_time"],
      "min_price":q["min_price"],"min_delta":q["min_delta"],"time_to_min":q["min_time"],"recent_momentum_pct":q["recent_momentum"]*100.0,"buy_pressure":q["buy_pressure"],
      "delta_vs_fast":delta,"low_fast_pnl":low_fast,"low_signal_pnl":low_sig,"low_delta":low_sig-low_fast,"base_fast_pnl":base_fast,"base_signal_pnl":base_sig,"base_delta":base_sig-base_fast,
      "oracle_base_delta":max_base-base_fast,"big_worse":1 if (base_sig-base_fast)<=-float(config.FAST_RECOVERY_SIGNAL_BIG_WORSE_USD) else 0,
      "final_source":source,"last_event_age":max(0.0,now-float(q.get("last_event_ts",now)))}
    SHADOW_DB_QUEUE.append(("fast_signal_done",payload))

def fast_recovery_signal_tick(m,p,side):
    q=FAST_RECOVERY_SIGNAL.get(m)
    if not q or p<=0:return
    now=time.time(); age=max(0.0,now-q["started"])
    # Keep the policy executable: if the first print arrives after the 30s boundary,
    # score the last observed price rather than leaking a future tick into the decision.
    if age>=config.FAST_RECOVERY_SIGNAL_MAX_SECONDS:
        fast_recovery_signal_finalize(m,"TIMEOUT","TICK_BOUNDARY",now); return
    q["last"]=float(p);q["last_event_ts"]=now;q["events"]+=1;q["prices"].append(float(p));q["sides"].append(side)
    pnl=config.POSITION_SIZE*(p/q["entry"]-1);delta=pnl-q["fast_pnl"];recovery=p/q["fast_exit"]-1
    if recovery>q["peak_recovery"]:q["peak_recovery"]=recovery
    if delta>q["max_delta"]:q["max_delta"]=delta;q["max_pnl"]=pnl;q["peak_price"]=p;q["max_time"]=age
    else:q["peak_price"]=max(q["peak_price"],p)
    if delta<q["min_delta"]:q["min_delta"]=delta;q["min_pnl"]=pnl;q["min_price"]=p;q["min_time"]=age
    prices=list(q["prices"]); sides=list(q["sides"])
    recent_mom=(prices[-1]/prices[-3]-1) if len(prices)>=3 and prices[-3]>0 else 0.0
    recent_sides=sides[-5:]; bp=(sum(x=="buy" for x in recent_sides)/len(recent_sides)) if recent_sides else 0.0
    q["recent_momentum"]=recent_mom;q["buy_pressure"]=bp
    if age<config.FAST_RECOVERY_SIGNAL_MIN_AGE:return
    if recovery<=config.FAST_RECOVERY_SIGNAL_HARD_ABORT_PCT:
        fast_recovery_signal_finalize(m,"HARD_ABORT","TICK",now);return
    if not q["armed"]:
        if recovery>=config.FAST_RECOVERY_SIGNAL_ARM_PCT and recent_mom>=config.FAST_RECOVERY_SIGNAL_ARM_MOMENTUM_PCT and bp>=config.FAST_RECOVERY_SIGNAL_MIN_BUY_PRESSURE:
            q["armed"]=True;q["arm_age"]=age;q["arm_price"]=p
        elif age>=config.FAST_RECOVERY_SIGNAL_NO_RECOVERY_SECONDS and q["peak_recovery"]<config.FAST_RECOVERY_SIGNAL_NO_RECOVERY_PEAK_PCT and recent_mom<=0:
            fast_recovery_signal_finalize(m,"NO_RECOVERY","TICK",now);return
    if q["armed"]:
        recovery=p/q["fast_exit"]-1
        drawdown=p/max(q["peak_price"],1e-30)-1
        if recovery<=config.FAST_RECOVERY_SIGNAL_LOCK_PCT:
            fast_recovery_signal_finalize(m,"LOCK_FLOOR","TICK",now);return
        if drawdown<=-config.FAST_RECOVERY_SIGNAL_TRAIL_PCT:
            fast_recovery_signal_finalize(m,"TRAIL_LOCK","TICK",now);return
        if recent_mom<=config.FAST_RECOVERY_SIGNAL_MOMENTUM_FADE_PCT and bp<=config.FAST_RECOVERY_SIGNAL_PRESSURE_FADE:
            fast_recovery_signal_finalize(m,"MOMENTUM_PRESSURE_FADE","TICK",now);return

def fast_reentry_start(m,q,p,fast_pnl):
    if not getattr(config,"FAST_REENTRY_ENABLED",False): return
    now=time.time(); mfe=float(q.get("mfe",0.0)); mae=float(q.get("mae",0.0)); hold=max(0.0,now-q["ts"])
    FAST_REENTRY[m]={"entry":float(q["entry"]),"fast_exit":float(p),"fast_pnl":float(fast_pnl),"fast_mfe":mfe,"fast_mae":mae,"fast_hold":hold,
      "mfe_bin":fast30_mfe_bin(mfe),"started":now,"last":float(p),"last_event_ts":now,"events":0,"prices":deque(maxlen=12),"sides":deque(maxlen=12),
      "reentered":False,"reentry_ts":0.0,"reentry_age":0.0,"reentry_price":0.0,"signal_recovery":0.0,"signal_momentum":0.0,"signal_buy_pressure":0.0,
      "peak_price":0.0,"peak_gain":0.0,"min_price":0.0,"mae_reentry":0.0,"recent_momentum":0.0,"buy_pressure":0.0,"fb_snaps":{}}
    replay_log.begin(m,FAST_REENTRY[m],{k:getattr(config,k) for k in dir(config) if k.startswith("FAST_REENTRY_") or k in ("POSITION_SIZE","EXEC_AUDIT_PROFILES","VERSION")})
    db.inc("fast_reentry_started")

def fast_reentry_finalize(m,reason,source="TICK",now=None):
    q=FAST_REENTRY.pop(m,None)
    if not q:return
    now=time.time() if now is None else now
    p=float(q.get("last") or q["fast_exit"]); reentered=bool(q.get("reentered")); ideal=low=base=stress=0.0; low_cost=base_cost=stress_cost=0.0
    if reentered and q.get("reentry_price",0)>0:
        rp=float(q["reentry_price"]); ideal=config.POSITION_SIZE*(p/rp-1)
        low,low_cost,_,_= _shadow_roundtrip_cost(m,rp,p,ideal,"LOW")
        base,base_cost,_,_= _shadow_roundtrip_cost(m,rp,p,ideal,"BASE")
        stress,stress_cost,_,_= _shadow_roundtrip_cost(m,rp,p,ideal,"STRESS")
    base_fast=_shadow_cost(m,float(q["entry"]),float(q["fast_exit"]),float(q["fast_pnl"]),"BASE")
    payload={"ts":now,"mint":m,"entry":q["entry"],"fast_exit":q["fast_exit"],"fast_pnl":q["fast_pnl"],"fast_mfe":q["fast_mfe"],"fast_mae":q["fast_mae"],"fast_hold":q["fast_hold"],"mfe_bin":q["mfe_bin"],
      "reentered":1 if reentered else 0,"reentry_age":q.get("reentry_age",0.0),"reentry_price":q.get("reentry_price",0.0),"signal_recovery_pct":q.get("signal_recovery",0.0)*100.0,"signal_momentum_pct":q.get("signal_momentum",0.0)*100.0,"signal_buy_pressure":q.get("signal_buy_pressure",0.0),
      "exit_price":p if reentered else 0.0,"exit_reason":reason,"reentry_hold":max(0.0,now-float(q.get("reentry_ts") or now)) if reentered else 0.0,"events_after_fast":q.get("events",0),
      "peak_gain_pct":q.get("peak_gain",0.0)*100.0,"mae_reentry_pct":q.get("mae_reentry",0.0)*100.0,"recent_momentum_pct":q.get("recent_momentum",0.0)*100.0,"buy_pressure":q.get("buy_pressure",0.0),
      "ideal_reentry_pnl":ideal,"low_reentry_pnl":low,"base_reentry_pnl":base,"stress_reentry_pnl":stress,"low_cost":low_cost,"base_cost":base_cost,"stress_cost":stress_cost,
      "base_fast_pnl":base_fast,"combined_base_pnl":base_fast+base,"big_loss":1 if reentered and base<=-float(config.FAST_REENTRY_BIG_LOSS_USD) else 0,
      "final_source":source,"last_event_age":max(0.0,now-float(q.get("last_event_ts",now)))}
    replay_log.finish(m,payload)
    observation.finish(m,reason,base,now)
    SHADOW_DB_QUEUE.append(("fast_reentry_done",payload))
    if reentered:
        snaps=q.get("fb_snaps",{})
        def _ss(k,f): return (snaps.get(str(k)) or {}).get(f)
        label="WINNER" if reason=="REENTRY_TRAIL" else ("FALSE_BOUNCE" if reason=="REENTRY_STOP" else "OTHER")
        SHADOW_DB_QUEUE.append(("false_bounce_done",{
          "snap_meta":snaps,"ts":now,"mint":m,"label":label,"base_pnl":base,"signal_age":q.get("reentry_age",0.0),"signal_recovery_pct":q.get("signal_recovery",0.0)*100.0,"signal_momentum_pct":q.get("signal_momentum",0.0)*100.0,"signal_buy_pressure":q.get("signal_buy_pressure",0.0),
          "s1_ret":_ss(1,"ret"),"s1_mom":_ss(1,"mom"),"s1_bp":_ss(1,"bp"),"s1_events":_ss(1,"events"),"s2_ret":_ss(2,"ret"),"s2_mom":_ss(2,"mom"),"s2_bp":_ss(2,"bp"),"s2_events":_ss(2,"events"),"s3_ret":_ss(3,"ret"),"s3_mom":_ss(3,"mom"),"s3_bp":_ss(3,"bp"),"s3_events":_ss(3,"events"),"s5_ret":_ss(5,"ret"),"s5_mom":_ss(5,"mom"),"s5_bp":_ss(5,"bp"),"s5_events":_ss(5,"events"),
          "peak_gain_pct":q.get("peak_gain",0.0)*100.0,"mae_reentry_pct":q.get("mae_reentry",0.0)*100.0,"reentry_hold":max(0.0,now-float(q.get("reentry_ts") or now)),"exit_reason":reason}))

def fast_reentry_tick(m,p,side):
    q=FAST_REENTRY.get(m)
    if not q or p<=0:return
    now=time.time(); replay_log.decision(m,now); age=max(0.0,now-q["started"])
    # Before re-entry, 30s is the fixed signal-search horizon. After re-entry, use its own hold clock.
    if not q["reentered"] and age>=config.FAST_REENTRY_WATCH_SECONDS:
        fast_reentry_finalize(m,"NO_SIGNAL","TICK_BOUNDARY",now);return
    q["last"]=float(p);q["last_event_ts"]=now;q["events"]+=1;q["prices"].append(float(p));q["sides"].append(side)
    prices=list(q["prices"]);sides=list(q["sides"])
    recent_mom=(prices[-1]/prices[-3]-1) if len(prices)>=3 and prices[-3]>0 else 0.0
    recent_sides=sides[-5:];bp=(sum(x=="buy" for x in recent_sides)/len(recent_sides)) if recent_sides else 0.0
    q["recent_momentum"]=recent_mom;q["buy_pressure"]=bp
    if not q["reentered"]:
        recovery=p/q["fast_exit"]-1
        if age>=config.FAST_REENTRY_MIN_AGE and q["events"]>=config.FAST_REENTRY_MIN_EVENTS and recovery>=config.FAST_REENTRY_TRIGGER_RECOVERY_PCT and recent_mom>=config.FAST_REENTRY_TRIGGER_MOMENTUM_PCT and bp>=config.FAST_REENTRY_MIN_BUY_PRESSURE:
            q["reentered"]=True;q["reentry_ts"]=now;q["reentry_age"]=age;q["reentry_price"]=float(p);q["signal_recovery"]=recovery;q["signal_momentum"]=recent_mom;q["signal_buy_pressure"]=bp;q["peak_price"]=float(p);q["min_price"]=float(p);q["peak_gain"]=0.0;q["mae_reentry"]=0.0
            observation.start(m,float(p),now)
            replay_log.entered(m,now,observation.ACTIVE[m]["case"],float(p),recovery*100.,recent_mom*100.,bp,age)
            db.inc("fast_reentry_signals")
        return
    rp=float(q["reentry_price"]); hold=max(0.0,now-q["reentry_ts"]); ret=p/rp-1
    # v0.7.12 observational snapshots only; never changes the v0.7.11 re-entry decision or exit.
    for _sec in (1,2,3,5):
        if hold>=_sec and str(_sec) not in q["fb_snaps"]:
            q["fb_snaps"][str(_sec)]={"ret":ret*100.0,"mom":recent_mom*100.0,"bp":bp,"events":q["events"],"actual_hold":hold,"base_abort":_shadow_roundtrip_cost(m,rp,p,config.POSITION_SIZE*ret,"BASE")[0]}
    q["peak_price"]=max(float(q.get("peak_price") or rp),float(p));q["min_price"]=min(float(q.get("min_price") or rp),float(p));q["peak_gain"]=max(float(q.get("peak_gain") or 0.0),q["peak_price"]/rp-1);q["mae_reentry"]=min(float(q.get("mae_reentry") or 0.0),ret)
    drawdown=p/max(q["peak_price"],1e-30)-1
    if ret<=config.FAST_REENTRY_HARD_STOP_PCT:
        fast_reentry_finalize(m,"REENTRY_STOP","TICK",now);return
    if q["peak_gain"]>=config.FAST_REENTRY_TRAIL_ARM_PCT and ret<=config.FAST_REENTRY_LOCK_PCT:
        fast_reentry_finalize(m,"REENTRY_LOCK_FLOOR","TICK",now);return
    if q["peak_gain"]>=config.FAST_REENTRY_TRAIL_ARM_PCT and drawdown<=-config.FAST_REENTRY_TRAIL_PCT:
        fast_reentry_finalize(m,"REENTRY_TRAIL","TICK",now);return
    if hold>=config.FAST_REENTRY_FADE_MIN_AGE and recent_mom<=config.FAST_REENTRY_FADE_MOMENTUM_PCT and bp<=config.FAST_REENTRY_FADE_MAX_BUY_PRESSURE:
        fast_reentry_finalize(m,"REENTRY_FADE","TICK",now);return
    if hold>=config.FAST_REENTRY_MAX_HOLD:
        fast_reentry_finalize(m,"REENTRY_TIMEOUT","TICK",now);return
    observation.tick(m,p,recent_mom*100.0,bp,now,cost_fn=observation_cost)

def fast_recovery_risk_start(m,q,p,fast_pnl):
    if not getattr(config,"FAST_RECOVERY_RISK_ENABLED",False): return
    now=time.time(); mfe=float(q.get("mfe",0.0)); mae=float(q.get("mae",0.0)); hold=max(0.0,now-q["ts"])
    FAST_RECOVERY_RISK[m]={"entry":float(q["entry"]),"fast_exit":float(p),"fast_pnl":float(fast_pnl),"fast_mfe":mfe,"fast_mae":mae,"fast_hold":hold,
      "mfe_bin":fast30_mfe_bin(mfe),"started":now,"last":float(p),"last_event_ts":now,"events":0,"prices":deque(maxlen=12),"sides":deque(maxlen=12),
      "armed":False,"arm_age":0.0,"arm_price":0.0,"peak_price":float(p),"peak_recovery":0.0,"max_pnl":float(fast_pnl),"max_delta":0.0,"max_time":0.0,
      "min_price":float(p),"min_pnl":float(fast_pnl),"min_delta":0.0,"min_time":0.0,"recent_momentum":0.0,"buy_pressure":0.0}
    db.inc("fast_risk_started")

def fast_recovery_risk_finalize(m,reason,source="TICK",now=None):
    q=FAST_RECOVERY_RISK.pop(m,None)
    if not q:return
    now=time.time() if now is None else now
    p=float(q.get("last") or q["fast_exit"]); entry=float(q["entry"]); fast_pnl=float(q["fast_pnl"])
    pnl=config.POSITION_SIZE*(p/entry-1); delta=pnl-fast_pnl
    low_fast=_shadow_cost(m,entry,q["fast_exit"],fast_pnl,"LOW"); low_sig=_shadow_cost(m,entry,p,pnl,"LOW")
    base_fast=_shadow_cost(m,entry,q["fast_exit"],fast_pnl,"BASE"); base_sig=_shadow_cost(m,entry,p,pnl,"BASE")
    max_base=_shadow_cost(m,entry,q["peak_price"],q["max_pnl"],"BASE")
    payload={"ts":now,"mint":m,"entry":entry,"fast_exit":q["fast_exit"],"fast_pnl":fast_pnl,"fast_mfe":q["fast_mfe"],"fast_mae":q["fast_mae"],"fast_hold":q["fast_hold"],"mfe_bin":q["mfe_bin"],
      "exit_price":p,"exit_pnl":pnl,"exit_reason":reason,"exit_age":max(0.0,now-q["started"]),"events_after_fast":q["events"],"armed":1 if q["armed"] else 0,
      "arm_age":q["arm_age"],"arm_price":q["arm_price"],"peak_price":q["peak_price"],"peak_recovery_pct":q["peak_recovery"]*100.0,"max_delta":q["max_delta"],"time_to_max":q["max_time"],
      "min_price":q["min_price"],"min_delta":q["min_delta"],"time_to_min":q["min_time"],"recent_momentum_pct":q["recent_momentum"]*100.0,"buy_pressure":q["buy_pressure"],
      "delta_vs_fast":delta,"low_fast_pnl":low_fast,"low_signal_pnl":low_sig,"low_delta":low_sig-low_fast,"base_fast_pnl":base_fast,"base_signal_pnl":base_sig,"base_delta":base_sig-base_fast,
      "oracle_base_delta":max_base-base_fast,"big_worse":1 if (base_sig-base_fast)<=-float(config.FAST_RECOVERY_SIGNAL_BIG_WORSE_USD) else 0,
      "final_source":source,"last_event_age":max(0.0,now-float(q.get("last_event_ts",now)))}
    SHADOW_DB_QUEUE.append(("fast_risk_done",payload))

def fast_recovery_risk_tick(m,p,side):
    q=FAST_RECOVERY_RISK.get(m)
    if not q or p<=0:return
    now=time.time(); age=max(0.0,now-q["started"])
    if age>=config.FAST_RECOVERY_SIGNAL_MAX_SECONDS:
        fast_recovery_risk_finalize(m,"TIMEOUT","TICK_BOUNDARY",now); return
    q["last"]=float(p);q["last_event_ts"]=now;q["events"]+=1;q["prices"].append(float(p));q["sides"].append(side)
    pnl=config.POSITION_SIZE*(p/q["entry"]-1);delta=pnl-q["fast_pnl"];recovery=p/q["fast_exit"]-1
    if recovery>q["peak_recovery"]:q["peak_recovery"]=recovery
    if delta>q["max_delta"]:q["max_delta"]=delta;q["max_pnl"]=pnl;q["peak_price"]=p;q["max_time"]=age
    else:q["peak_price"]=max(q["peak_price"],p)
    if delta<q["min_delta"]:q["min_delta"]=delta;q["min_pnl"]=pnl;q["min_price"]=p;q["min_time"]=age
    prices=list(q["prices"]); sides=list(q["sides"])
    recent_mom=(prices[-1]/prices[-3]-1) if len(prices)>=3 and prices[-3]>0 else 0.0
    recent_sides=sides[-5:]; bp=(sum(x=="buy" for x in recent_sides)/len(recent_sides)) if recent_sides else 0.0
    q["recent_momentum"]=recent_mom;q["buy_pressure"]=bp
    if age<config.FAST_RECOVERY_SIGNAL_MIN_AGE:return
    # v0.7.10 only change: pre-arm tail-risk control. The profitable v0.7.9 arm/trail logic below is frozen.
    if not q["armed"]:
        if age>=config.FAST_RECOVERY_RISK_EARLY_MIN_AGE and q["events"]>=config.FAST_RECOVERY_RISK_EARLY_MIN_EVENTS and recovery<=config.FAST_RECOVERY_RISK_COLLAPSE_PCT and recent_mom<=config.FAST_RECOVERY_RISK_COLLAPSE_MOM_PCT and bp<=config.FAST_RECOVERY_RISK_COLLAPSE_MAX_BUY_PRESSURE:
            fast_recovery_risk_finalize(m,"EARLY_COLLAPSE","TICK",now);return
        if age>=config.FAST_RECOVERY_RISK_NO_BOUNCE_AGE and q["events"]>=config.FAST_RECOVERY_RISK_NO_BOUNCE_MIN_EVENTS and q["peak_recovery"]<config.FAST_RECOVERY_RISK_NO_BOUNCE_PEAK_PCT and recovery<=config.FAST_RECOVERY_RISK_NO_BOUNCE_LOSS_PCT and recent_mom<=config.FAST_RECOVERY_RISK_NO_BOUNCE_MOM_PCT and bp<=config.FAST_RECOVERY_RISK_NO_BOUNCE_MAX_BUY_PRESSURE:
            fast_recovery_risk_finalize(m,"PREARM_NO_BOUNCE","TICK",now);return
    if recovery<=config.FAST_RECOVERY_SIGNAL_HARD_ABORT_PCT:
        fast_recovery_risk_finalize(m,"HARD_ABORT","TICK",now);return
    if not q["armed"]:
        if recovery>=config.FAST_RECOVERY_SIGNAL_ARM_PCT and recent_mom>=config.FAST_RECOVERY_SIGNAL_ARM_MOMENTUM_PCT and bp>=config.FAST_RECOVERY_SIGNAL_MIN_BUY_PRESSURE:
            q["armed"]=True;q["arm_age"]=age;q["arm_price"]=p
        elif age>=config.FAST_RECOVERY_SIGNAL_NO_RECOVERY_SECONDS and q["peak_recovery"]<config.FAST_RECOVERY_SIGNAL_NO_RECOVERY_PEAK_PCT and recent_mom<=0:
            fast_recovery_risk_finalize(m,"NO_RECOVERY","TICK",now);return
    if q["armed"]:
        recovery=p/q["fast_exit"]-1
        drawdown=p/max(q["peak_price"],1e-30)-1
        if recovery<=config.FAST_RECOVERY_SIGNAL_LOCK_PCT:
            fast_recovery_risk_finalize(m,"LOCK_FLOOR","TICK",now);return
        if drawdown<=-config.FAST_RECOVERY_SIGNAL_TRAIL_PCT:
            fast_recovery_risk_finalize(m,"TRAIL_LOCK","TICK",now);return
        if recent_mom<=config.FAST_RECOVERY_SIGNAL_MOMENTUM_FADE_PCT and bp<=config.FAST_RECOVERY_SIGNAL_PRESSURE_FADE:
            fast_recovery_risk_finalize(m,"MOMENTUM_PRESSURE_FADE","TICK",now);return

def micro_register(m,p):
    if not getattr(config,"MICRO_CONFIRM_LAB_ENABLED",False) or m in MICRO_PENDING:return
    MICRO_PENDING[m]={"ts":time.time(),"signal":p,"events":0,"buys":0,"sells":0}

def micro_open(m,variant,p,meta):
    key=(m,variant)
    if key in MICRO_POS or key in MICRO_DONE:return
    MICRO_POS[key]={"entry":p,"last":p,"high":p,"ts":time.time(),"mfe":0.0,"mae":0.0,"post_trades":0,"post_buys":0,"post_sells":0,
      "signal":meta["signal"],"signal_return":meta["signal_return"],"buy_pressure":meta["buy_pressure"],"post_events":meta["post_events"]}

def micro_sell(key,p,reason):
    q=MICRO_POS.pop(key,None)
    if not q:return
    m,variant=key; MICRO_DONE.add(key); pnl=config.POSITION_SIZE*(p/q["entry"]-1); hold=time.time()-q["ts"]
    low=_shadow_cost(m+variant,q["entry"],p,pnl,"LOW"); base=_shadow_cost(m+variant,q["entry"],p,pnl,"BASE")
    with db.con() as c:c.execute("INSERT OR REPLACE INTO micro_confirm_v074(ts,mint,variant,signal_price,entry,exit,reason,pnl,mfe,mae,hold,low_pnl,base_pnl,signal_return,buy_pressure,post_events) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(time.time(),m,variant,q["signal"],q["entry"],p,reason,pnl,q["mfe"],q["mae"],hold,low,base,q["signal_return"],q["buy_pressure"],q["post_events"]))

def micro_tick(m,p,side):
    q=MICRO_PENDING.get(m)
    if q:
        q["events"]+=1; q["buys"]+=side=="buy"; q["sells"]+=side=="sell"
        age=time.time()-q["ts"]; sr=(p/q["signal"]-1)*100 if q["signal"]>0 else 0.0; bp=q["buys"]/max(1,q["events"])
        for d in config.MICRO_CONFIRM_DELAYS:
            tag=str(int(d))
            if age>=d:
                meta={"signal":q["signal"],"signal_return":sr,"buy_pressure":bp,"post_events":q["events"]}
                micro_open(m,"DELAY_"+tag+"S",p,meta)
                if sr>=config.MICRO_CONFIRM_MIN_RETURN_PCT and bp>=config.MICRO_CONFIRM_MIN_BUY_PRESSURE:
                    micro_open(m,"CONF_"+tag+"S",p,meta)
                else: MICRO_DONE.add((m,"CONF_"+tag+"S"))
        if all((m,"DELAY_"+str(int(d))+"S") in MICRO_POS or (m,"DELAY_"+str(int(d))+"S") in MICRO_DONE for d in config.MICRO_CONFIRM_DELAYS): MICRO_PENDING.pop(m,None)
    for key,x in list(MICRO_POS.items()):
        if key[0]!=m:continue
        x["last"]=p;x["high"]=max(x["high"],p);r=p/x["entry"]-1;x["mfe"]=max(x["mfe"],r);x["mae"]=min(x["mae"],r);x["post_trades"]+=1;x["post_buys"]+=side=="buy";x["post_sells"]+=side=="sell";age=max(0,time.time()-x["ts"]);pr=x["post_buys"]/max(1,x["post_sells"])
        if age>=config.PP_MIN_AGE and x["mfe"]>=.10 and r<=max(config.PP_SHADOW_MIN_LOCK,x["mfe"]-.07):micro_sell(key,p,"PP_REAL_10_7")
        elif age<=config.FAST_PROTECT_SECONDS and x["post_trades"]>=config.FAST_STOP_MIN_EVENTS and r<=config.FAST_STOP:micro_sell(key,p,"FAST_STOP")
        elif r<=config.STOP_LOSS:micro_sell(key,p,"STOP_LOSS")
        elif (config.PROBATION_MIN_SECONDS<=age<=config.PROBATION_MAX_SECONDS and ((x["post_trades"]>=config.EARLY_FAIL_MIN_POST_TRADES and x["mfe"]<config.EARLY_FAIL_MAX_MFE and r<=config.EARLY_FAIL_RETURN and pr<=config.EARLY_FAIL_MAX_POST_RATIO) or (x["mfe"]<config.EARLY_FAIL_HARD_MAX_MFE and r<=config.EARLY_FAIL_HARD_RETURN))):micro_sell(key,p,"EARLY_FAIL")
        elif r>=config.TP2:micro_sell(key,p,"TP2")
        elif x["high"]/x["entry"]-1>=config.TP1 and p/x["high"]-1<=-config.TRAIL:micro_sell(key,p,"TRAIL")
        elif age>=config.MAX_HOLD:micro_sell(key,p,"TIME")

def shadow_ab_open(m,p,decision,prob):
    if not getattr(config,"TRUE_SHADOW_AB_ENABLED",False) or m in SHADOW_AB or m in SHADOW_ENTERED:return
    SHADOW_ENTERED.add(m); SHADOW_AB[m]={"entry":p,"last":p,"high":p,"ts":time.time(),"mfe":0.0,"mae":0.0,"post_trades":0,"post_buys":0,"post_sells":0,"decision":decision,"prob":prob}

def shadow_ab_sell(m,p,reason):
    q=SHADOW_AB.pop(m,None)
    if not q:return
    pnl=config.POSITION_SIZE*(p/q["entry"]-1); hold=time.time()-q["ts"]
    low=_shadow_cost(m,q["entry"],p,pnl,"LOW"); base=_shadow_cost(m,q["entry"],p,pnl,"BASE")
    with db.con() as c:c.execute("INSERT OR REPLACE INTO final_gate_validation_v0692(ts,mint,gate_decision,model_p,entry,exit,reason,pnl,mfe,mae,hold,low_pnl,base_pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(time.time(),m,q["decision"],q["prob"],q["entry"],p,reason,pnl,q["mfe"],q["mae"],hold,low,base))

def shadow_ab_manage(m,p):
    q=SHADOW_AB.get(m)
    if not q or p<=0:return
    q["last"]=p;q["high"]=max(q["high"],p);r=p/q["entry"]-1;q["mfe"]=max(q["mfe"],r);q["mae"]=min(q["mae"],r);age=max(0.0,time.time()-q["ts"]);post_ratio=q["post_buys"]/max(1,q["post_sells"])
    if age>=config.PP_MIN_AGE and q["mfe"]>=.10 and r<=max(config.PP_SHADOW_MIN_LOCK,q["mfe"]-.07): shadow_ab_sell(m,p,"PP_REAL_10_7")
    elif age<=config.FAST_PROTECT_SECONDS and q["post_trades"]>=config.FAST_STOP_MIN_EVENTS and r<=config.FAST_STOP: shadow_ab_sell(m,p,"FAST_STOP")
    elif r<=config.STOP_LOSS: shadow_ab_sell(m,p,"STOP_LOSS")
    elif (config.PROBATION_MIN_SECONDS<=age<=config.PROBATION_MAX_SECONDS and ((q["post_trades"]>=config.EARLY_FAIL_MIN_POST_TRADES and q["mfe"]<config.EARLY_FAIL_MAX_MFE and r<=config.EARLY_FAIL_RETURN and post_ratio<=config.EARLY_FAIL_MAX_POST_RATIO) or (q["mfe"]<config.EARLY_FAIL_HARD_MAX_MFE and r<=config.EARLY_FAIL_HARD_RETURN))): shadow_ab_sell(m,p,"EARLY_FAIL")
    elif r>=config.TP2: shadow_ab_sell(m,p,"TP2")
    elif q["high"]/q["entry"]-1>=config.TP1 and p/q["high"]-1<=-config.TRAIL: shadow_ab_sell(m,p,"TRAIL")
    elif age>=config.MAX_HOLD: shadow_ab_sell(m,p,"TIME")

def transport_block_reason():
    if db.getm('branch_role',''):
        if int(db.getm('branch_input_gap',0)):
            try:first=json.loads(db.getm('branch_first_gap_v60','null')) or {}
            except (TypeError,ValueError):first={}
            return 'BRANCH_CURRENT_INPUT_GAP' if first.get('session')==db.getm('branch_session') and first.get('session') else 'BRANCH_PERSISTED_INPUT_GAP'
        if not int(db.getm('branch_input_seq',0)):return 'BRANCH_NO_INPUT_YET'
    return 'TRADE_STREAM_NOT_RECEIVING'

def transport_entry_ready():
    age=time.time()-float(db.getm("trade_last_market_ts",0))
    return str(db.getm("trade_watchdog_state","STARTING"))=="RECEIVING" and 0<=age<config.PUMPAPI_STALL_SECONDS

def buy(m,p,score=0,s=None,_fill=False,_order_id=None):
    if not __import__('recovery_gate').allowed():return False,'RECOVERY_BLOCKED'
    audit_ts=time.time()
    result=_buy(m,p,score,s,_fill,_order_id)
    if not _fill:
        with db.con() as c:
            db.setm("last_entry_result_v0722",result[1]);db.setm("last_entry_ts_v0722",time.time())
            diagnostics.observe(c,m,p,score,s,result[1])
            if not result[0]:entry_diagnostics.decision(c,m,result[1],audit_ts)
    return result

@atomic_primary
def _buy(m,p,score=0,s=None,_fill=False,_order_id=None):
    if __import__('late_spawn').blocks_entries(config.DB):return False,'CHILD_CREATION_CLEANUP_PENDING'
    db.inc("entry_attempts"); cash=float(db.getm("cash")); reason=None
    enabled=config.EXECUTION_MODEL_ENABLED
    reserved_n,reserved_cash=execution.reserve(db.con(),_order_id) if enabled else (0,0)
    spendable=capital.cash(db.con()) if enabled else cash
    required=capital.budget(db.con(),config.POSITION_SIZE) if enabled else config.POSITION_SIZE
    if enabled and _fill:
        fee=capital.quote(db.con(),config.POSITION_SIZE,(s or {}).get("cost_evidence"),"BUY")
        if fee is None:return False,"ENTRY_FEE_UNKNOWN_OR_ABOVE_CAP"
        required=config.POSITION_SIZE+fee["total_cost"]
    if enabled and not _fill:
        if execution.pending(db.con(),m,"BUY"):return False,"ENTRY_PENDING"
        if not execution.fresh((s or {}).get("cost_evidence")):
            db.inc("execution_entry_bad_evidence");return False,"UNSUPPORTED_OR_STALE_PRICE"
    if p<=0:reason="NO_PRICE"
    elif getattr(config,"BLOCK_ENTRIES_WHEN_TRADE_STALLED",False) and not transport_entry_ready():
        reason=transport_block_reason(); db.inc("entries_blocked_stall")
    elif m in P:reason="ALREADY_OPEN"
    elif m in ENTERED and not config.ALLOW_REENTRY:reason="ALREADY_TRADED"
    elif len(P)+reserved_n>=config.MAX_POSITIONS:reason="NO_SLOT"
    elif spendable-reserved_cash<required:reason="CASH_AFTER_COSTS"
    ratio=(s["buys"]/max(1,s["sells"])) if s else 0; mom=((s["last"]/s["first"])-1)*100 if s and s["first"] and s["last"] and s["valid_prices"]>=2 else 0
    age=(time.time()-s["born"]) if s else 0
    last_jump=0.0
    if s and len(s.get("prices",()))>=2 and s["prices"][-2]>0:
        last_jump=(s["prices"][-1]/s["prices"][-2]-1)*100
    # ENTRY QUALITY V2: trajectory/pressure features. These are also logged as WOULD_BLOCK_* for analysis.
    prices=list(s.get("prices",())) if s else []
    sides=list(s.get("sides",())) if s else []
    pullback=0.0; recent_slope=0.0; buy_pressure=1.0
    if prices:
        hi=max(prices)
        if hi>0: pullback=(prices[-1]/hi-1)*100
    if len(prices)>=3 and prices[-3]>0:
        recent_slope=(prices[-1]/prices[-3]-1)*100
    if sides:
        recent=sides[-min(6,len(sides)):]
        buy_pressure=sum(1 for x in recent if x=="buy")/max(1,len(recent))
    eq_reason=None
    if len(prices)>=config.EQ_MIN_SAMPLES:
        if pullback <= -config.EQ_MAX_PULLBACK_PCT: eq_reason="WOULD_BLOCK_PULLBACK"
        elif recent_slope < config.EQ_MIN_RECENT_SLOPE_PCT: eq_reason="WOULD_BLOCK_DECELERATION"
        elif buy_pressure < config.EQ_MIN_BUY_PRESSURE_TREND: eq_reason="WOULD_BLOCK_PRESSURE_FADE"
    if eq_reason:
        db.inc("eq_would_block"); db.inc("eq_"+eq_reason.lower())
        db.entry_quality(m,eq_reason,pullback,recent_slope,buy_pressure,mom,score)
    else:
        db.entry_quality(m,"EQ_PASS",pullback,recent_slope,buy_pressure,mom,score)
    if not reason and age < config.MIN_ENTRY_AGE: reason="ANTI_FOMO_TOO_EARLY"
    elif not reason and mom > config.MAX_ENTRY_MOMENTUM: reason="ANTI_FOMO_MOMENTUM_HOT"
    elif not reason and last_jump > config.MAX_ENTRY_LAST_JUMP: reason="ANTI_FOMO_LAST_JUMP"
    elif not reason and config.ENTRY_QUALITY_V2 and eq_reason: reason=eq_reason.replace("WOULD_BLOCK_","ENTRY_QUALITY_")
    if reason:
        db.inc("entry_blocked");db.entry_debug(m,score,"BLOCKED",reason,p,cash,len(P),s["trades"] if s else 0,len(s["buyers"]) if s else 0,ratio,mom);return False,reason
    # v0.7.6 frozen PRESSURE_RISING independent gate.
    # Uses only pre-entry trajectory events: pressure_delta = recent-half buy fraction - early-half buy fraction.
    if getattr(config,"PRESSURE_GATE_ENABLED",False):
        traj=trajectory_features(s)
        pressure_delta=float(traj[2])
        decision="ALLOW" if pressure_delta >= config.PRESSURE_GATE_MIN_DELTA else "BLOCK"
        # model_p is compatibility storage only: map pressure_delta [-1,+1] to [0,1], not a probability.
        compat=min(1.0,max(0.0,(pressure_delta+1.0)/2.0))
        shadow_ab_open(m,p,decision,compat)
        db.model_gate_v069(m,compat,config.PRESSURE_GATE_MIN_DELTA,decision,0,p,score)
        if decision=="BLOCK":
            db.inc("entry_blocked");db.inc("pressure_gate_blocked");db.entry_debug(m,score,"BLOCKED","PRESSURE_GATE",p,cash,len(P),s["trades"] if s else 0,len(s["buyers"]) if s else 0,ratio,mom);return False,"PRESSURE_GATE"
        db.inc("pressure_gate_allowed")
    # Legacy STABLE_VOL gate path retained but disabled in v0.7.3; research cohorts never block real PAPER entries.
    if getattr(config,"STABLE_VOL_GATE_ENABLED",False):
        snap=model_snapshot_features(s,score)
        ret_vol=float(snap[2])
        decision="ALLOW" if ret_vol <= config.STABLE_VOL_MAX_RET_VOL else "BLOCK"
        # model_p is a compatibility field only here: store ret_vol/10, not a probability.
        compat=min(1.0,max(0.0,ret_vol/10.0))
        shadow_ab_open(m,p,decision,compat)
        db.model_gate_v069(m,compat,config.STABLE_VOL_MAX_RET_VOL/10.0,decision,0,p,score)
        if decision=="BLOCK":
            db.inc("entry_blocked");db.entry_debug(m,score,"BLOCKED","STABLE_VOL_GATE",p,cash,len(P),s["trades"] if s else 0,len(s["buyers"]) if s else 0,ratio,mom);return False,"STABLE_VOL_GATE"
    if enabled and getattr(config,'PREFLIGHT_ENABLED',False):
        allowed,gate_reason=preentry.allow(db.con(),m,p,config.POSITION_SIZE,time.time(),_order_id if _fill else None)
        if not allowed:
            db.entry_debug(m,score,"BLOCKED",gate_reason,p,cash,len(P),s["trades"] if s else 0,len(s["buyers"]) if s else 0,ratio,mom)
            return False,gate_reason
    if enabled and not _fill:
        oid=execution.request(db.con(),m,"BUY",p,config.POSITION_SIZE,"ENTRY",(s or {}).get("cost_evidence"),score,snapshot=s)
        if getattr(config,'PREFLIGHT_ENABLED',False):preentry.consume(db.con(),m,oid)
        return True,"ENTRY_PENDING"
    kind="REENTRY" if m in ENTERED else "PRIMARY";ENTERED.add(m);P[m]={"entry":p,"last":p,"last_update":time.time(),"high":p,"ts":time.time(),"mfe":0,"mae":0,"kind":kind,"post_trades":0,"post_buys":0,"post_sells":0,"gap_seconds":0.0,"gap_count":0,"pp_armed":False,"ab_variant":getattr(config,"FINAL_PP_VARIANT","PP_10_7")}
    P[m].update(trade_id=uuid.uuid4().hex,size=config.POSITION_SIZE,cost_profiles=copy.deepcopy(config.EXEC_AUDIT_PROFILES))
    db.setm("cash",cash-config.POSITION_SIZE);db.inc("entries");db.inc("entry_ok");db.entry_debug(m,score,"ENTRY","ENTRY_OK",p,cash,len(P)-1,s["trades"] if s else 0,len(s["buyers"]) if s else 0,ratio,mom)
    db.entry_feature(m,score,s["trades"] if s else 0,len(s["buyers"]) if s else 0,ratio,mom,p,trade_id=P[m]["trade_id"])
    if getattr(config,"TRAJECTORY_LAB_ENABLED",False): db.entry_trajectory_v064(m,*trajectory_features(s),trade_id=P[m]["trade_id"])
    if getattr(config,"OFFLINE_MODEL_LAB_ENABLED",False): db.entry_model_snapshot_v067(m,*model_snapshot_features(s,score),trade_id=P[m]["trade_id"])
    with db.con() as c:
        c.execute("INSERT INTO trades(size,trade_id,ts,mint,side,price,reason,entry_kind,ab_variant) VALUES(?,?,?,?,?,?,?,?,?)",(P[m]["size"],P[m]["trade_id"],time.time(),m,"BUY",p,"ENTRY",kind,P[m].get("ab_variant","BASELINE")))
        c.execute("INSERT OR REPLACE INTO positions(trade_id,size,cost_profiles,mint,entry,current,opened_ts,last_update_ts,high,mfe,mae,entry_kind,ab_variant) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(P[m]["trade_id"],P[m]["size"],json.dumps(P[m]["cost_profiles"]),m,p,p,time.time(),time.time(),p,0,0,kind,P[m].get("ab_variant","BASELINE")))
    cost_evidence.record(db.con(),P[m]["trade_id"],"BUY",m,p,P[m]["size"],"ENTRY",(s or {}).get("cost_evidence"))
    if enabled:
        capital.record(db.con(),P[m]["trade_id"],"BUY",P[m]["size"],(s or {}).get("cost_evidence",{}))
        exit_quotes.on_entry(db.con(),P[m]["trade_id"],m,p,P[m]["size"])
        execution.filled(db.con(),_order_id,P[m]["trade_id"],p,(s or {}).get("cost_evidence",{}))
        __import__('shared_lots').publish_entry(db.con(),P[m],_order_id,(s or {}).get('born',time.time()))
    micro_register(m,p)
    return True,"ENTRY_OK"

def execution_sim_record(m,q,exit_price,reason,ideal_pnl):
    if not getattr(config,"EXECUTION_SIM_ENABLED",False): return
    # Deterministic per-mint execution stress model. These are configurable assumptions, not claimed provider quotes.
    h=int(hashlib.sha256((m+"|v060").encode()).hexdigest()[:16],16); u=(h%1000000)/1000000.0
    latency=config.SIM_BASE_LATENCY_MS+config.SIM_LATENCY_JITTER_MS*((h>>20)%10000)/10000.0
    failed=u < config.SIM_FAILED_TX_PROB
    buy_slip=config.SIM_BUY_SLIPPAGE_PCT
    # adverse exit slippage increases slightly for violent drawdowns; failed first attempt adds explicit penalty.
    stress=max(0.0,min(0.03,abs(min(0.0,float(q.get("mae",0))))*0.04))
    sell_slip=config.SIM_SELL_SLIPPAGE_PCT+stress+(config.SIM_FAILED_EXIT_EXTRA_SLIPPAGE_PCT if failed else 0.0)
    sim_entry=q["entry"]*(1+buy_slip); sim_exit=max(0.0,exit_price*(1-sell_slip))
    gross=q.get("size",config.POSITION_SIZE)*(sim_exit/sim_entry-1)
    exit_notional=q.get("size",config.POSITION_SIZE)*(sim_exit/sim_entry)
    fee_cost=config.SIM_TRADE_FEE_PCT*(q.get("size",config.POSITION_SIZE)+max(0.0,exit_notional))
    priority_cost=config.SIM_PRIORITY_FEE_USD*(3 if failed else 2)
    sim_pnl=gross-fee_cost-priority_cost; total_cost=ideal_pnl-sim_pnl
    with db.con() as c:
        c.execute("INSERT INTO execution_sim_v060(trade_id,ts,mint,reason,ideal_entry,ideal_exit,ideal_pnl,sim_entry,sim_exit,sim_pnl,total_cost,buy_slip,sell_slip,fee_cost,priority_cost,latency_ms,failed_tx,delta_vs_ideal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(q["trade_id"],time.time(),m,reason,q["entry"],exit_price,ideal_pnl,sim_entry,sim_exit,sim_pnl,total_cost,buy_slip,sell_slip,fee_cost,priority_cost,latency,1 if failed else 0,sim_pnl-ideal_pnl))
    db.inc("sim_trades"); db.inc("sim_failed_tx",1 if failed else 0); db.inc("sim_total_cost",total_cost); db.inc("sim_realistic_pnl",sim_pnl)

def execution_audit_record(m,q,exit_price,reason,ideal_pnl):
    if not getattr(config,"EXEC_AUDIT_ENABLED",False): return
    for profile,a in q.get("cost_profiles",config.EXEC_AUDIT_PROFILES).items():
        h=int(hashlib.sha256((m+"|v061|"+profile).encode()).hexdigest()[:16],16)
        u=(h%1000000)/1000000.0
        latency=a["latency_base"]+a["latency_jitter"]*((h>>20)%10000)/10000.0
        failed=u < a["failed_prob"]
        # Dollar components are kept separate so the dashboard can audit exactly what consumed the paper edge.
        buy_slippage_cost=q.get("size",config.POSITION_SIZE)*a["buy_slip"]
        ideal_exit_notional=max(0.0,q.get("size",config.POSITION_SIZE)*(exit_price/q["entry"]))
        sell_slippage_cost=ideal_exit_notional*a["sell_slip"]
        trade_fee_cost=a["fee"]*(q.get("size",config.POSITION_SIZE)+ideal_exit_notional)
        priority_fee_cost=a["priority"]*(3 if failed else 2)
        latency_impact_cost=ideal_exit_notional*a["latency_slip_per_s"]*(latency/1000.0)
        failed_tx_penalty=ideal_exit_notional*a["failed_extra"] if failed else 0.0
        total_cost=buy_slippage_cost+sell_slippage_cost+trade_fee_cost+priority_fee_cost+latency_impact_cost+failed_tx_penalty
        sim_pnl=ideal_pnl-total_cost
        with db.con() as c:
            c.execute("INSERT INTO execution_audit_v061(trade_id,ts,mint,profile,reason,ideal_pnl,sim_pnl,total_cost,buy_slippage_cost,sell_slippage_cost,trade_fee_cost,priority_fee_cost,latency_impact_cost,failed_tx_penalty,latency_ms,failed_tx) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(q["trade_id"],time.time(),m,profile,reason,ideal_pnl,sim_pnl,total_cost,buy_slippage_cost,sell_slippage_cost,trade_fee_cost,priority_fee_cost,latency_impact_cost,failed_tx_penalty,latency,1 if failed else 0))

@atomic_primary
def sell(m,p,reason,trigger_price=0.0,trigger_ts=0.0,_fill=False,_order_id=None,_settlement_evidence=None):
    settlement_evidence=_settlement_evidence if _settlement_evidence is not None else S.get(m,{}).get("cost_evidence")
    q=P.get(m)
    if not q or p<=0:return
    if config.EXECUTION_MODEL_ENABLED and not _fill:
        audit_order=execution.request(db.con(),m,"SELL",p,q["size"],reason,settlement_evidence,trade_id=q["trade_id"])
        exit_audit.signal(db.con(),q["trade_id"],m,audit_order,q,settlement_evidence)
        __import__('shared_lots').publish_signal(db.con(),q['trade_id'],audit_order,execution.ORIGINS.get(audit_order),settlement_evidence)
        return
    r=p/q["entry"]-1
    if reason=="FAST_STOP" and config.FAST_SHADOW_V3:
        hold=max(0.0,time.time()-q["ts"]); post_trades=int(q.get("post_trades",0)); post_buys=int(q.get("post_buys",0)); post_sells=int(q.get("post_sells",0)); post_ratio=post_buys/max(1,post_sells)
        drawdown=(p/q["high"]-1) if q.get("high",0)>0 else 0.0
        # Diagnostic classifier only. It never changes the real FAST_STOP.
        class_score=(2 if q.get("mfe",0)>=0.05 else 0)+(1 if post_ratio>=1.0 else 0)+(1 if hold>=3.0 else 0)+(1 if drawdown>-0.20 else 0)
        class_band="HIGH_RECOVERY" if class_score>=4 else ("MID_RECOVERY" if class_score>=2 else "LOW_RECOVERY")
        pp6=(PP_V2_HITS.get(m,{}) or {}).get(getattr(config,"SMART_EXIT_PP_VARIANT","PP_6_7"))
        FAST_SHADOW_V3[m]={"entry":q["entry"],"fast_exit":p,"fast_pnl":q.get("size",config.POSITION_SIZE)*r,"started":time.time(),"last":p,"done":set(),"wait_low":p,"pp6_pnl":(pp6.get("pnl") if pp6 else None)}
        SHADOW_DB_QUEUE.append(("fast_started",m,q["entry"],p,q.get("size",config.POSITION_SIZE)*r,q.get("mfe",0),q.get("mae",0),hold,post_trades,post_buys,post_sells,post_ratio,drawdown,class_score,class_band))
    if reason=="FAST_STOP" and getattr(config,"FAST_RECOVERY30_ENABLED",False):
        fast_recovery30_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)
    if reason=="FAST_STOP" and getattr(config,"FAST_RECOVERY_SIGNAL_ENABLED",False):
        fast_recovery_signal_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)
    if reason=="FAST_STOP" and getattr(config,"FAST_RECOVERY_RISK_ENABLED",False):
        fast_recovery_risk_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)
    if reason=="FAST_STOP" and getattr(config,"FAST_REENTRY_ENABLED",False):
        fast_reentry_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)
    finalize_pp_v2(m,p,reason,r)
    q=P.pop(m,None)
    if not q:return
    r=p/q["entry"]-1;pnl=q.get("size",config.POSITION_SIZE)*r; execution_sim_record(m,q,p,reason,pnl); execution_audit_record(m,q,p,reason,pnl); db.setm("cash",float(db.getm("cash"))+q.get("size",config.POSITION_SIZE)+pnl);db.inc("realized",pnl);db.inc("exits")
    with db.con() as c:
        lat_ms=max(0.0,(time.time()-trigger_ts)*1000) if trigger_ts else 0.0; gap=((p/trigger_price)-1)*100 if trigger_price>0 else 0.0
        c.execute("INSERT INTO trades(size,trade_id,ts,mint,side,price,pnl,reason,mfe,mae,hold,entry_kind,stop_trigger_price,exit_latency_ms,slippage_gap_pct,ab_variant) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(q["size"],q["trade_id"],time.time(),m,"SELL",p,pnl,reason,q["mfe"],q["mae"],time.time()-q["ts"],q["kind"],trigger_price,lat_ms,gap,q.get("ab_variant","BASELINE")))
        c.execute("DELETE FROM positions WHERE mint=?",(m,))
        cost_evidence.record(c,q["trade_id"],"SELL",m,p,q["size"],reason,settlement_evidence)
        if config.EXECUTION_MODEL_ENABLED:
            capital.record(c,q["trade_id"],"SELL",q["size"]*p/q["entry"],settlement_evidence)
            capital.rebalance(c)
            execution.filled(c,_order_id,q["trade_id"],p,settlement_evidence)
        exit_audit.close(c,q["trade_id"],m,q,settlement_evidence)

def manage(m,p):
    q=P.get(m)
    if not q or p<=0:return
    q["last"]=p;q["last_update"]=time.time();q["high"]=max(q["high"],p);r=p/q["entry"]-1;q["mfe"]=max(q["mfe"],r);q["mae"]=min(q["mae"],r)
    with db.con() as c:c.execute("UPDATE positions SET current=?,last_update_ts=?,high=?,mfe=?,mae=?,gap_seconds=?,gap_count=? WHERE mint=?",(p,q["last_update"],q["high"],q["mfe"],q["mae"],q.get("gap_seconds",0),q.get("gap_count",0),m))
    if config.EXECUTION_MODEL_ENABLED and execution.pending(db.con(),m,"SELL"):return
    age=max(0.0,time.time()-q["ts"]-float(q.get("gap_seconds",0.0)))
    post_ratio=q.get("post_buys",0)/max(1,q.get("post_sells",0))
    probation=(config.PROBATION_MIN_SECONDS <= age <= config.PROBATION_MAX_SECONDS)
    early_soft=(probation and q.get("post_trades",0)>=config.EARLY_FAIL_MIN_POST_TRADES and q["mfe"]<config.EARLY_FAIL_MAX_MFE and r<=config.EARLY_FAIL_RETURN and post_ratio<=config.EARLY_FAIL_MAX_POST_RATIO)
    early_hard=(age>=config.PROBATION_MIN_SECONDS and age<=config.PROBATION_MAX_SECONDS and q["mfe"]<config.EARLY_FAIL_HARD_MAX_MFE and r<=config.EARLY_FAIL_HARD_RETURN)
    # v0.4.8.8.7.4: evaluate diagnostic PP BEFORE real exits.
    # This is shadow-only and does not alter the baseline sell decision.
    if config.PP_SHADOW_V2: shadow_profit_protect_v2(m,p,r,age)
    # v0.5 STRATEGY FINALIZATION: PP_10_7 is frozen for every PAPER position.
    ab=getattr(config,"FINAL_PP_VARIANT","PP_10_7")
    if age>=config.PP_MIN_AGE:
        activate=0.10
        giveback=0.07
        if q["mfe"]>=activate:
            floor=max(config.PP_SHADOW_MIN_LOCK,q["mfe"]-giveback)
            if r<=floor:
                db.inc("pp_real_exits"); db.inc("pp_real_"+ab.lower())
                db.entry_debug(m,0,"PP_REAL_EXIT",ab,p,float(db.getm("cash")),len(P),q.get("post_trades",0),0,post_ratio,r*100)
                sell(m,p,"PP_REAL_10_7",p,time.time())
    if m not in P: return
    # Independent first-seconds circuit breaker: no EARLY_FAIL confirmation required.
    if age<=config.FAST_PROTECT_SECONDS and q.get("post_trades",0)>=config.FAST_STOP_MIN_EVENTS and r<=config.FAST_STOP:
        db.inc("fast_stop_triggered"); db.inc("fast_stop_exits"); db.entry_debug(m,0,"FAST_PROTECT_EXIT","FAST_STOP",p,float(db.getm("cash")),len(P),q.get("post_trades",0),0,post_ratio,r*100); sell(m,p,"FAST_STOP",p,time.time())
    elif r<=config.STOP_LOSS:sell(m,p,"STOP_LOSS",q["entry"]*(1+config.STOP_LOSS),time.time())
    elif early_hard or early_soft:
        db.inc("early_fail_exits"); db.entry_debug(m,0,"PROBATION_EXIT","EARLY_FAIL",p,float(db.getm("cash")),len(P),q.get("post_trades",0),0,post_ratio,r*100); sell(m,p,"EARLY_FAIL")
    if m not in P: return
    if r>=config.TP2:sell(m,p,"TP2")
    elif q["high"]/q["entry"]-1>=config.TP1 and p/q["high"]-1<=-config.TRAIL:sell(m,p,"TRAIL")
    elif age>=config.MAX_HOLD:sell(m,p,"TIME")

def shadow_profit_protect_v2(m,p,r,age):
    """PP V2 shadow lifecycle fix.

    This path is intentionally in-memory and tiny (four variants).  Unlike .7.4,
    it is NOT throttled: the event that crosses a PP floor may also be the real
    FAST_STOP/STOP_LOSS event, so skipping that event loses the comparison.
    """
    HOT["pp_calls"]+=1
    q=P.get(m)
    if not q or not config.PP_SHADOW_V2 or SHADOW_PAUSED:return
    HOT["shadow_checks"]+=1
    hits=PP_V2_HITS.setdefault(m,{})
    armed=PP_V2_ARMED.setdefault(m,set())
    for name,activate,giveback in config.PP_SHADOW_VARIANTS:
        if age < config.PP_MIN_AGE or q["mfe"] < activate: continue
        if name not in armed:
            armed.add(name)
            SHADOW_DB_QUEUE.append(("pp_armed",m,name))
        if name in hits: continue
        floor=max(config.PP_SHADOW_MIN_LOCK,q["mfe"]-giveback)
        if r <= floor:
            hits[name]={"ts":time.time(),"price":p,"pnl":config.POSITION_SIZE*r,"mfe":q["mfe"],"floor":floor}
            HOT["shadow_triggers"]+=1
            SHADOW_DB_QUEUE.append(("pp_trigger",m,name,hits[name]["pnl"],q["mfe"],floor))

def finalize_pp_v2(m,p,reason,actual_r):
    PP_V2_LAST_CHECK.pop(m,None)
    armed=PP_V2_ARMED.pop(m,set())
    hits=PP_V2_HITS.pop(m,{})
    if armed: db.inc("pp_v4_baseline_closed",len(armed))
    if not hits:return
    actual_pnl=config.POSITION_SIZE*actual_r
    total_delta=0.0; saved=0
    with db.con() as c:
        for name,h in hits.items():
            delta=h["pnl"]-actual_pnl; total_delta+=delta
            c.execute("INSERT INTO profit_protection_v2(ts,mint,variant,trigger_ts,trigger_price,hypo_pnl,mfe_at_trigger,floor_return,actual_reason,actual_exit,actual_pnl,delta_vs_actual) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(time.time(),m,name,h["ts"],h["price"],h["pnl"],h["mfe"],h["floor"],reason,p,actual_pnl,delta))
            saved+=1
    db.inc("pp_v2_shadow_done",saved); db.inc("pp_v2_delta",total_delta); db.inc("pp_v4_comparison_saved",saved)

def profit_protect(m,p,r,age):
    q=P.get(m)
    if not q or not config.PROFIT_PROTECTION_V1:return False
    if q["mfe"]>=config.PP_ACTIVATE_MFE:q["pp_armed"]=True
    if not q.get("pp_armed") or age<config.PP_MIN_AGE:return False
    floor=max(config.PP_MIN_LOCK,q["mfe"]-config.PP_MAX_GIVEBACK)
    if r>floor:return False
    # Preserve a counterfactual shadow so dashboard can compare against the old exit logic.
    PP_SHADOW[m]={"entry":q["entry"],"high":q["high"],"mfe":q["mfe"],"mae":q["mae"],"ts":q["ts"],"protect_price":p,"protect_pnl":config.POSITION_SIZE*r,"shadow_started":time.time()}
    db.inc("profit_protect_exits")
    with db.con() as c:
        c.execute("INSERT INTO profit_protection(ts,mint,entry,protect_exit,protect_pnl,mfe,mae,hold) VALUES(?,?,?,?,?,?,?,?)",(time.time(),m,q["entry"],p,config.POSITION_SIZE*r,q["mfe"],q["mae"],age))
    db.stream_debug(m,"PROFIT_PROTECT",f"mfe={q['mfe']*100:.2f}% current={r*100:.2f}% floor={floor*100:.2f}%")
    sell(m,p,"PROFIT_PROTECT_EXIT")
    return True

def manage_shadow(m,p):
    q=PP_SHADOW.get(m)
    if not q or p<=0:return
    r=p/q["entry"]-1; q["high"]=max(q["high"],p); q["mfe"]=max(q["mfe"],r); q["mae"]=min(q["mae"],r); age=time.time()-q["ts"]
    reason=None
    if r<=config.STOP_LOSS: reason="STOP_LOSS"
    elif r>=config.TP2: reason="TP2"
    elif q["high"]/q["entry"]-1>=config.TP1 and p/q["high"]-1<=-config.TRAIL: reason="TRAIL"
    elif age>=config.MAX_HOLD: reason="MAX_HOLD_WATCHDOG"
    elif time.time()-q["shadow_started"]>=config.PP_SHADOW_MAX_SECONDS: reason="SHADOW_TIMEOUT"
    if reason:
        original_pnl=config.POSITION_SIZE*r; saved=q["protect_pnl"]-original_pnl
        with db.con() as c:
            row=c.execute("SELECT id FROM profit_protection WHERE mint=? AND shadow_done=0 ORDER BY id DESC LIMIT 1",(m,)).fetchone()
            if row:c.execute("UPDATE profit_protection SET shadow_done=1,original_reason=?,original_exit=?,original_pnl=?,saved_vs_original=? WHERE id=?",(reason,p,original_pnl,saved,row["id"]))
        db.inc("profit_protect_shadow_done"); db.inc("profit_protect_saved",saved); db.stream_debug(m,"PROFIT_PROTECT_SHADOW",f"original={reason} pnl={original_pnl:.4f} saved={saved:.4f}"); PP_SHADOW.pop(m,None)

def fast_shadow_v3_tick(m,p):
    q=FAST_SHADOW_V3.get(m)
    if not q or p<=0:return
    age=time.time()-q["started"];q["last"]=p;q["wait_low"]=min(q.get("wait_low",p),p)
    for horizon in config.FAST_SHADOW_HORIZONS:
        if horizon in q["done"] or age < horizon:continue
        q["done"].add(horizon)
        shadow_pnl=config.POSITION_SIZE*(p/q["entry"]-1);delta=shadow_pnl-q["fast_pnl"]
        wait_mae=pct_low=(q["wait_low"]/q["entry"]-1)
        SHADOW_DB_QUEUE.append(("fast_done",m,horizon,q["entry"],q["fast_exit"],q["fast_pnl"],p,shadow_pnl,delta,age))
        if getattr(config,"SMART_EXIT_AB_V491",False) and horizon in config.SMART_EXIT_HORIZONS:
            SHADOW_DB_QUEUE.append(("smart_ab",m,f"RECOVERY_{horizon}s",horizon,q["fast_pnl"],shadow_pnl,delta,wait_mae,0))
            if horizon==config.SMART_EXIT_PRIMARY_HORIZON:
                combo_pnl=q.get("pp6_pnl") if q.get("pp6_pnl") is not None else shadow_pnl
                combo_delta=combo_pnl-q["fast_pnl"]
                SHADOW_DB_QUEUE.append(("smart_ab",m,"PP_6_7_PLUS_RECOVERY_20s",horizon,q["fast_pnl"],combo_pnl,combo_delta,wait_mae,1 if q.get("pp6_pnl") is not None else 0))
    if age>=config.FAST_SHADOW_MAX_SECONDS or len(q["done"])>=len(config.FAST_SHADOW_HORIZONS):FAST_SHADOW_V3.pop(m,None)

def flush_shadow_queue():
    global LAST_SHADOW_FLUSH
    now=time.monotonic()
    if now-LAST_SHADOW_FLUSH < config.SHADOW_FLUSH_INTERVAL:return
    LAST_SHADOW_FLUSH=now
    items=[]
    while SHADOW_DB_QUEUE and len(items)<256:items.append(SHADOW_DB_QUEUE.popleft())
    if not items:return
    ppn=ppa=fs=fd=0;delta=0.0
    with db.con() as c:
        for x in items:
            if x[0]=="pp_armed":ppa+=1
            elif x[0]=="pp_trigger":ppn+=1
            elif x[0]=="fast_started":
                fs+=1
                if len(x)>2:
                    _,m,e,fx,fp,mfe,mae,hold,pt,pb,ps,pr,dd,score,band=x
                    c.execute("INSERT OR REPLACE INTO fast_stop_cases_v49(ts,mint,entry,fast_exit,fast_pnl,mfe,mae,hold,post_trades,post_buys,post_sells,post_ratio,drawdown_from_high,class_score,class_band) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(time.time(),m,e,fx,fp,mfe,mae,hold,pt,pb,ps,pr,dd,score,band))
            elif x[0]=="fast_done":
                _,m,h,e,fx,fp,sp,spl,d,a=x;fd+=1;delta+=d
                c.execute("INSERT INTO fast_stop_shadow_v3(ts,mint,horizon_s,entry,fast_exit,fast_pnl,shadow_price,shadow_pnl,delta_vs_fast,observed_age) VALUES(?,?,?,?,?,?,?,?,?,?)",(time.time(),m,h,e,fx,fp,sp,spl,d,a))
            elif x[0]=="fast30_done":
                v=x[1]
                c.execute("""INSERT OR REPLACE INTO fast_stop_recovery_v078(ts,mint,entry,fast_exit,fast_pnl,fast_mfe,fast_mae,fast_hold,mfe_bin,final_price,final_pnl,delta_vs_fast,low_fast_pnl,low_recovery_pnl,low_delta,base_fast_pnl,base_recovery_pnl,base_delta,max_price,max_pnl,max_delta,max_base_pnl,max_base_delta,time_to_max,min_price,min_pnl,min_delta,time_to_min,events_after_fast,final_source,last_event_age,observed_age) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (v["ts"],v["mint"],v["entry"],v["fast_exit"],v["fast_pnl"],v["fast_mfe"],v["fast_mae"],v["fast_hold"],v["mfe_bin"],v["final_price"],v["final_pnl"],v["delta_vs_fast"],v["low_fast_pnl"],v["low_recovery_pnl"],v["low_delta"],v["base_fast_pnl"],v["base_recovery_pnl"],v["base_delta"],v["max_price"],v["max_pnl"],v["max_delta"],v["max_base_pnl"],v["max_base_delta"],v["time_to_max"],v["min_price"],v["min_pnl"],v["min_delta"],v["time_to_min"],v["events_after_fast"],v["final_source"],v["last_event_age"],v["observed_age"]))
                db.inc("fast30_completed");db.inc("fast30_base_delta",v["base_delta"]);db.inc("fast30_ideal_delta",v["delta_vs_fast"])
            elif x[0]=="fast_signal_done":
                v=x[1]
                c.execute("""INSERT OR REPLACE INTO fast_recovery_signal_v079(ts,mint,entry,fast_exit,fast_pnl,fast_mfe,fast_mae,fast_hold,mfe_bin,exit_price,exit_pnl,exit_reason,exit_age,events_after_fast,armed,arm_age,arm_price,peak_price,peak_recovery_pct,max_delta,time_to_max,min_price,min_delta,time_to_min,recent_momentum_pct,buy_pressure,delta_vs_fast,low_fast_pnl,low_signal_pnl,low_delta,base_fast_pnl,base_signal_pnl,base_delta,oracle_base_delta,big_worse,final_source,last_event_age) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (v["ts"],v["mint"],v["entry"],v["fast_exit"],v["fast_pnl"],v["fast_mfe"],v["fast_mae"],v["fast_hold"],v["mfe_bin"],v["exit_price"],v["exit_pnl"],v["exit_reason"],v["exit_age"],v["events_after_fast"],v["armed"],v["arm_age"],v["arm_price"],v["peak_price"],v["peak_recovery_pct"],v["max_delta"],v["time_to_max"],v["min_price"],v["min_delta"],v["time_to_min"],v["recent_momentum_pct"],v["buy_pressure"],v["delta_vs_fast"],v["low_fast_pnl"],v["low_signal_pnl"],v["low_delta"],v["base_fast_pnl"],v["base_signal_pnl"],v["base_delta"],v["oracle_base_delta"],v["big_worse"],v["final_source"],v["last_event_age"]))
                db.inc("fast_signal_completed");db.inc("fast_signal_base_delta",v["base_delta"]);db.inc("fast_signal_ideal_delta",v["delta_vs_fast"]);db.inc("fast_signal_big_worse",v["big_worse"])
            elif x[0]=="fast_risk_done":
                v=x[1]
                c.execute("""INSERT OR REPLACE INTO fast_recovery_risk_v0710(ts,mint,entry,fast_exit,fast_pnl,fast_mfe,fast_mae,fast_hold,mfe_bin,exit_price,exit_pnl,exit_reason,exit_age,events_after_fast,armed,arm_age,arm_price,peak_price,peak_recovery_pct,max_delta,time_to_max,min_price,min_delta,time_to_min,recent_momentum_pct,buy_pressure,delta_vs_fast,low_fast_pnl,low_signal_pnl,low_delta,base_fast_pnl,base_signal_pnl,base_delta,oracle_base_delta,big_worse,final_source,last_event_age) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (v["ts"],v["mint"],v["entry"],v["fast_exit"],v["fast_pnl"],v["fast_mfe"],v["fast_mae"],v["fast_hold"],v["mfe_bin"],v["exit_price"],v["exit_pnl"],v["exit_reason"],v["exit_age"],v["events_after_fast"],v["armed"],v["arm_age"],v["arm_price"],v["peak_price"],v["peak_recovery_pct"],v["max_delta"],v["time_to_max"],v["min_price"],v["min_delta"],v["time_to_min"],v["recent_momentum_pct"],v["buy_pressure"],v["delta_vs_fast"],v["low_fast_pnl"],v["low_signal_pnl"],v["low_delta"],v["base_fast_pnl"],v["base_signal_pnl"],v["base_delta"],v["oracle_base_delta"],v["big_worse"],v["final_source"],v["last_event_age"]))
                db.inc("fast_risk_completed");db.inc("fast_risk_base_delta",v["base_delta"]);db.inc("fast_risk_ideal_delta",v["delta_vs_fast"]);db.inc("fast_risk_big_worse",v["big_worse"])
            elif x[0]=="fast_reentry_done":
                v=x[1]
                c.execute("""INSERT OR REPLACE INTO fast_reentry_v0711(ts,mint,entry,fast_exit,fast_pnl,fast_mfe,fast_mae,fast_hold,mfe_bin,reentered,reentry_age,reentry_price,signal_recovery_pct,signal_momentum_pct,signal_buy_pressure,exit_price,exit_reason,reentry_hold,events_after_fast,peak_gain_pct,mae_reentry_pct,recent_momentum_pct,buy_pressure,ideal_reentry_pnl,low_reentry_pnl,base_reentry_pnl,stress_reentry_pnl,low_cost,base_cost,stress_cost,base_fast_pnl,combined_base_pnl,big_loss,final_source,last_event_age) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (v["ts"],v["mint"],v["entry"],v["fast_exit"],v["fast_pnl"],v["fast_mfe"],v["fast_mae"],v["fast_hold"],v["mfe_bin"],v["reentered"],v["reentry_age"],v["reentry_price"],v["signal_recovery_pct"],v["signal_momentum_pct"],v["signal_buy_pressure"],v["exit_price"],v["exit_reason"],v["reentry_hold"],v["events_after_fast"],v["peak_gain_pct"],v["mae_reentry_pct"],v["recent_momentum_pct"],v["buy_pressure"],v["ideal_reentry_pnl"],v["low_reentry_pnl"],v["base_reentry_pnl"],v["stress_reentry_pnl"],v["low_cost"],v["base_cost"],v["stress_cost"],v["base_fast_pnl"],v["combined_base_pnl"],v["big_loss"],v["final_source"],v["last_event_age"]))
                db.inc("fast_reentry_completed");db.inc("fast_reentry_base_delta",v["base_reentry_pnl"]);db.inc("fast_reentry_ideal_delta",v["ideal_reentry_pnl"]);db.inc("fast_reentry_big_loss",v["big_loss"]);db.inc("fast_reentry_executed",v["reentered"])
            elif x[0]=="false_bounce_done":
                for sec, snap in x[1].get("snap_meta",{}).items():
                    c.execute("INSERT OR REPLACE INTO probation_snaps_v0713 VALUES(?,?,?,?)",(x[1]["mint"],int(sec),snap.get("actual_hold"),snap.get("base_abort")))
                v=x[1]; cols=["ts","mint","label","base_pnl","signal_age","signal_recovery_pct","signal_momentum_pct","signal_buy_pressure","s1_ret","s1_mom","s1_bp","s1_events","s2_ret","s2_mom","s2_bp","s2_events","s3_ret","s3_mom","s3_bp","s3_events","s5_ret","s5_mom","s5_bp","s5_events","peak_gain_pct","mae_reentry_pct","reentry_hold","exit_reason"]
                c.execute("INSERT OR REPLACE INTO false_bounce_v0712("+",".join(cols)+") VALUES("+",".join("?" for _ in cols)+")",tuple(v.get(k) for k in cols))
            elif x[0]=="smart_ab":
                _,m,var,h,bp,hp,dlt,wmae,ppused=x
                worst_extra=min(0.0, config.POSITION_SIZE*wmae-bp)
                c.execute("INSERT INTO smart_exit_shadow_v491(ts,mint,variant,horizon_s,baseline_pnl,hypo_pnl,delta,better,worse,wait_mae,worst_extra_loss,pp_used) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(time.time(),m,var,h,bp,hp,dlt,1 if dlt>0 else 0,1 if dlt<0 else 0,wmae,worst_extra,ppused))
                c.execute("INSERT INTO meta(k,v) VALUES('smart_ab_saved','1') ON CONFLICT(k) DO UPDATE SET v=CAST(COALESCE(meta.v,'0') AS REAL)+1")
                c.execute("INSERT INTO meta(k,v) VALUES('smart_ab_delta',?) ON CONFLICT(k) DO UPDATE SET v=CAST(COALESCE(meta.v,'0') AS REAL)+excluded.v",(dlt,))
    if ppa:db.inc("pp_v4_armed",ppa)
    if ppn:db.inc("pp_v2_shadow_hits",ppn);db.inc("pp_v3_triggers",ppn);db.inc("pp_v4_triggered",ppn)
    if fs:db.inc("fast_shadow_started",fs);db.inc("fast_classifier_cases",fs)
    if fd:db.inc("fast_shadow_done",fd);db.inc("fast_shadow_delta",delta)

def execute_pending(m,p,e):
    execution.expire(db.con())
    for side in ("SELL","BUY"):
        order=execution.pending(db.con(),m,side)
        if not order:continue
        allowed=execution.eligible(order,e)
        audit_code="ELIGIBLE" if allowed else time_audit.eligibility_reason(order,e)
        time_audit.add(db.con(),order["order_id"],"eligibility",audit_code,e)
        if side=="SELL":exit_audit.eligibility(order["trade_id"],order,e,audit_code)
        if not allowed:continue
        if side=="BUY":
            frozen=json.loads(order["snapshot"]);frozen["cost_evidence"]=e
            ok,reason=buy(m,p,order["score"],frozen,_fill=True,_order_id=order["order_id"])
            if not ok:
                time_audit.add(db.con(),order["order_id"],"fill_rejection",reason,e)
                time_audit.add(db.con(),order["order_id"],"lifecycle","REJECTED",e)
                db.con().execute("UPDATE paper_orders_v0720 SET status='REJECTED',note=? WHERE order_id=?",(reason,order["order_id"]))
        else:
            sell(m,p,order["reason"],order["signal_price"],order["signal_ts"],_fill=True,_order_id=order["order_id"])

def clock_candidate_availability(m):
    """Read-only snapshot of the actual portfolio, not a counterfactual wallet."""
    c=db.con();enabled=config.EXECUTION_MODEL_ENABLED
    rn,rc=execution.reserve(c) if enabled else (0,0)
    spendable=capital.cash(c) if enabled else float(db.getm("cash"))
    required=capital.budget(c,config.POSITION_SIZE) if enabled else config.POSITION_SIZE
    reasons=[]
    if enabled and execution.pending(c,m,"BUY"):reasons.append("ENTRY_PENDING")
    if getattr(config,"BLOCK_ENTRIES_WHEN_TRADE_STALLED",False) and not transport_entry_ready():reasons.append(transport_block_reason())
    if m in P:reasons.append("ALREADY_OPEN")
    if m in ENTERED and not config.ALLOW_REENTRY:reasons.append("ALREADY_TRADED")
    if len(P)+rn>=config.MAX_POSITIONS:reasons.append("NO_SLOT")
    if spendable-rc<required:reasons.append("CASH_AFTER_COSTS")
    return dict(reasons=reasons,available=spendable-rc,required=required,open_positions=len(P),reserved_positions=rn)

@runtime_control.measured("PROCESS_TRADE")
def process_trade(m,side,p,trader,source,evidence=None):
    if isinstance(__import__('recovery_gate').ACTIVE,__import__('recovery_gate').Coordinator):__import__('recovery_gate').status()
    if not m or m not in S:return
    branch_transport.emit(m,side,p,trader,source,evidence,S[m]['born'])
    _t0=time.perf_counter(); HOT["trade_calls"]+=1
    now=time.time(); _d0=time.perf_counter(); db.hot_trade_tick(now); HOT["db_ms"]+=(time.perf_counter()-_d0)*1000
    if m not in FIRST_TRADE_SEEN:FIRST_TRADE_SEEN.add(m);db.inc("first_trade_mints");db.stream_debug(m,"FIRST_TRADE",f"{side} via {source}")
    replay_log.capture(m,side,p,trader,source,now)
    time_audit.clock(db.con(),evidence,now)
    clock_lab.observe(db.con(),m,p,evidence,context=dict(side=side,trader=trader,born=S[m]["born"],now=now,availability=lambda: clock_candidate_availability(m)))
    s=S[m]
    if config.EXECUTION_MODEL_ENABLED and (p<=0 or not execution.fresh(evidence)):
        entry_diagnostics.event(db.con(),m,entry_diagnostics.rejection(p,evidence),now,evidence)
        if m in P:exit_audit.event(P[m]["trade_id"],side,p,source,evidence,entry_diagnostics.rejection(p,evidence))
        diagnostics.evidence(db.con(),m,"INVALID_OR_STALE",(evidence or {}).get("pool",""));db.inc("execution_rejected_ticks");return
    if config.EXECUTION_MODEL_ENABLED and s.get("cost_evidence",{}).get("provider_ts",0)>evidence["provider_ts"]:
        entry_diagnostics.event(db.con(),m,"OUT_OF_ORDER",now,evidence)
        if m in P:exit_audit.event(P[m]["trade_id"],side,p,source,evidence,"OUT_OF_ORDER")
        diagnostics.evidence(db.con(),m,"OUT_OF_ORDER");db.inc("execution_rejected_ticks");return
    if config.EXECUTION_MODEL_ENABLED:
        recent=s.setdefault("execution_signatures",deque(maxlen=128))
        if evidence["signature"] in recent:
            entry_diagnostics.event(db.con(),m,"DUPLICATE",now,evidence)
            if m in P:exit_audit.event(P[m]["trade_id"],side,p,source,evidence,"DUPLICATE")
            diagnostics.evidence(db.con(),m,"DUPLICATE");db.inc("execution_rejected_ticks");return
        recent.append(evidence["signature"])
        entry_diagnostics.event(db.con(),m,"ACCEPTED",now,evidence)
        if m in P:exit_audit.event(P[m]["trade_id"],side,p,source,evidence,"ACCEPTED")
        diagnostics.evidence(db.con(),m,"ACCEPTED",evidence.get("pool",""))
        diagnostics.tick(db.con(),m,p,now)
    if p>0:
        s["cost_evidence"]=evidence or cost_evidence.metadata({},source,"adapter_price")
        cost_evidence.on_tick(db.con(),m,p,s["cost_evidence"],wall=s["cost_evidence"]["received_ts"],mono=s["cost_evidence"]["received_mono"])
    db.event(m,side,p,trader,source);s["trades"]+=1;s["buys"]+=side=="buy";s["sells"]+=side=="sell";s["sides"].append(side)
    if m in SHADOW_AB:
        SHADOW_AB[m]["post_trades"]=SHADOW_AB[m].get("post_trades",0)+1
        SHADOW_AB[m]["post_buys"]=SHADOW_AB[m].get("post_buys",0)+(1 if side=="buy" else 0)
        SHADOW_AB[m]["post_sells"]=SHADOW_AB[m].get("post_sells",0)+(1 if side=="sell" else 0)
    if m in P:
        P[m]["post_trades"]=P[m].get("post_trades",0)+1
        P[m]["post_buys"]=P[m].get("post_buys",0)+(1 if side=="buy" else 0)
        P[m]["post_sells"]=P[m].get("post_sells",0)+(1 if side=="sell" else 0)
    if side=="buy" and trader and len(s["buyers"])<config.MAX_BUYERS_PER_MINT:s["buyers"].add(trader)
    if p>0:
        db.inc("valid_price_events");s["valid_prices"]+=1;s["price_source"]=source
        if not s["first"]:s["first"]=p
        s["last"]=p;s["prices"].append(p);s["model_events"].append((side,p,time.time()));
        if config.FAST_SHADOW_V3: fast_shadow_v3_tick(m,p)
        if getattr(config,"FAST_RECOVERY30_ENABLED",False): fast_recovery30_tick(m,p)
        if getattr(config,"FAST_RECOVERY_SIGNAL_ENABLED",False): fast_recovery_signal_tick(m,p,side)
        if getattr(config,"FAST_RECOVERY_RISK_ENABLED",False): fast_recovery_risk_tick(m,p,side)
        if m not in FAST_REENTRY: observation.tick(m,p,0.,0.,cost_fn=observation_cost)
        if getattr(config,"FAST_REENTRY_ENABLED",False): fast_reentry_tick(m,p,side)
        micro_tick(m,p,side)
        if config.EXECUTION_MODEL_ENABLED:execute_pending(m,p,s["cost_evidence"])
        _m0=time.perf_counter(); manage(m,p); shadow_ab_manage(m,p); HOT["manage_ms"]+=(time.perf_counter()-_m0)*1000
    else:db.inc("invalid_price_events")
    _s0=time.perf_counter(); sc,g,r,mom=calc(s); HOT["signal_ms"]+=(time.perf_counter()-_s0)*1000
    if g!='PASS' or sc<config.MIN_SCORE:
        entry_diagnostics.decision(db.con(),m,'SIGNAL_'+g if g!='PASS' else 'SIGNAL_SCORE_BELOW_MIN',now)
    if getattr(config,'PREFLIGHT_ENABLED',False) and g=='PASS' and sc>=config.MIN_SCORE and preentry.ready(db.con(),m):
        buy(m,p,sc,s)  # Recheck all entry rules against this fresh tick after approval.
    if s["trades"] in (1,2,3,4,6,8,12,20,30) and s["milestone"]!=s["trades"]:
        dec="PASS" if g=="PASS" and sc>=config.MIN_SCORE else "NO_BUY";db.signal(m,sc,dec,g,s["trades"],len(s["buyers"]),r,mom,s["first"],s["last"],s["valid_prices"],s["price_source"]);s["milestone"]=s["trades"]
        if dec=="PASS":
            db.inc("pass")
            if m not in PASS_SEEN:PASS_SEEN.add(m);db.inc("unique_pass")
            PASS_PIN[m]=time.time()+config.PASS_PIN_SECONDS;db.inc("pass_pinned");db.entry_debug(m,sc,"PASS","PASS",p,float(db.getm("cash")),len(P),s["trades"],len(s["buyers"]),r,mom);buy(m,p,sc,s)
    HOT["trade_ms"]+=(time.perf_counter()-_t0)*1000

def cleanup(force=False):
    global LAST_CLEANUP_TS
    mono=time.monotonic()
    if not force and mono-LAST_CLEANUP_TS < config.CLEANUP_INTERVAL:return
    LAST_CLEANUP_TS=mono
    now=time.time()
    diagnostics.advance(db.con(),now)
    protected={r[0] for r in db.con().execute("SELECT mint FROM paper_orders_v0720 WHERE status='PENDING'")}
    protected.update(r[0] for r in db.con().execute("SELECT mint FROM signal_observations_v0722 WHERE ts+300>?",(now,)))
    # Transport hardening: never age/watchdog positions while the trade feed is disconnected.
    # Completed feed gaps are subtracted from effective holding time, so stale wall-clock time cannot force an exit.
    if TRADE_GAP_START is None:
        for m,q in list(P.items()):
            gap=float(q.get("gap_seconds",0.0))
            effective_age=max(0.0,now-q["ts"]-gap)
            if effective_age>=config.MAX_HOLD and not execution.pending(db.con(),m,"SELL"):
                px=q.get("last",q["entry"])
                db.inc("stale_forced_exits")
                db.entry_debug(m,0,"FORCED_EXIT","MAX_HOLD_WATCHDOG",px,float(db.getm("cash")),len(P),0,0,0,gap);sell(m,px,"MAX_HOLD_WATCHDOG")
    for m,q in list(SHADOW_AB.items()):
        if now-q["ts"]>=config.MAX_HOLD: shadow_ab_sell(m,q.get("last",q["entry"]),"MAX_HOLD_WATCHDOG")
    # v0.7.8: complete every 30s FAST recovery case, even when the mint stops printing trades.
    for m,q in list(FAST_RECOVERY_30.items()):
        if now-q["started"]>=config.FAST_RECOVERY30_SECONDS: fast_recovery30_finalize(m,"WATCHDOG_LAST",now)
    # v0.7.9: signal policy has a hard 30s maximum. A no-print token is still scored
    # at its last observed price and explicitly marked WATCHDOG_LAST.
    for m,q in list(FAST_RECOVERY_SIGNAL.items()):
        if now-q["started"]>=config.FAST_RECOVERY_SIGNAL_MAX_SECONDS: fast_recovery_signal_finalize(m,"TIMEOUT","WATCHDOG_LAST",now)
    for m,q in list(FAST_RECOVERY_RISK.items()):
        if now-q["started"]>=config.FAST_RECOVERY_SIGNAL_MAX_SECONDS: fast_recovery_risk_finalize(m,"TIMEOUT","WATCHDOG_LAST",now)
    for m,q in list(FAST_REENTRY.items()):
        if not q.get("reentered") and now-q["started"]>=config.FAST_REENTRY_WATCH_SECONDS:
            fast_reentry_finalize(m,"NO_SIGNAL","WATCHDOG_LAST",now)
        elif q.get("reentered") and now-float(q.get("reentry_ts") or now)>=config.FAST_REENTRY_MAX_HOLD:
            fast_reentry_finalize(m,"REENTRY_TIMEOUT","WATCHDOG_LAST",now)
    for k in list(PASS_PIN):
        if PASS_PIN[k]<=now:PASS_PIN.pop(k,None)
    for old in [k for k,v in S.items() if k not in P and k not in protected and k not in cost_evidence.WATCH and k not in SHADOW_AB and k not in FAST_SHADOW_V3 and k not in FAST_RECOVERY_30 and k not in FAST_RECOVERY_SIGNAL and k not in FAST_RECOVERY_RISK and k not in FAST_REENTRY and k not in observation.ACTIVE and PASS_PIN.get(k,0)<=now and now-v["born"]>config.TTL]:S.pop(old,None);ACTIVE.pop(old,None);FIRST_TRADE_SEEN.discard(old);SUB_OK_MINTS.discard(old);PASS_SEEN.discard(old);clock_lab.forget(old)
    db.setm("active_subscriptions",len(ACTIVE))

async def pumpportal_discovery(q):
    uri="wss://pumpportal.fun/api/data"+(('?api-key='+config.PUMPPORTAL_API_KEY) if config.PUMPPORTAL_API_KEY else '')
    back=1
    while True:
        try:
            async with websockets.connect(uri,ping_interval=None,close_timeout=3,max_queue=config.WS_MAX_QUEUE) as ws:
                await ws.send(json.dumps({"method":"subscribeNewToken"}));__import__("discovery_gaps_v100").recovered(db.con());db.setm("stream_state","LIVE");back=1;last=time.time()
                while True:
                    try:raw=await asyncio.wait_for(ws.recv(),timeout=5)
                    except asyncio.TimeoutError:
                        if time.time()-last>=getattr(config,"DISCOVERY_STALE_SECONDS",180):raise RuntimeError("discovery stream stale")
                        continue
                    last=time.time();db.inc("ws_messages");db.setm("last_ws_ts",last)
                    try:x=json.loads(raw)
                    except:continue
                    m=x.get("mint")
                    if m and x.get("txType")=="create":
                        db.inc("new");db.setm("last_new_ts",time.time())
                        if config.RECORD_DISCOVERY: db.discovery_event(m,raw)
                        S.setdefault(m,state())
                        if len(S)>config.MAX_CANDIDATES:
                            for oldm in list(S.keys()):
                                if oldm not in P and not execution.pending(db.con(),oldm,"BUY") and oldm not in cost_evidence.WATCH and oldm not in ACTIVE and PASS_PIN.get(oldm,0)<=time.time():
                                    S.pop(oldm,None); FIRST_TRADE_SEEN.discard(oldm); SUB_OK_MINTS.discard(oldm); PASS_SEEN.discard(oldm); clock_lab.forget(oldm)
                                    if len(S)<=config.MAX_CANDIDATES: break
                        try:q.put_nowait(m)
                        except asyncio.QueueFull:db.inc("sub_queue_dropped")
        except Exception as e:
            __import__("discovery_gaps_v100").failed(db.con(),type(e).__name__);db.inc("discovery_ws_errors");db.inc("discovery_reconnects");db.inc("ws_errors");db.inc("reconnects");db.reconnect_event("discovery",repr(e),len(P));db.setm("last_error",f"discovery: {e!r}"[:400]);await asyncio.sleep(back+random.uniform(0,config.RECONNECT_JITTER_MAX));back=min(config.RECONNECT_BACKOFF_MAX,back*2)

async def pumpportal_trades(q):
    db.setm("trade_stream_provider","pumpportal")
    if not config.PUMPPORTAL_API_KEY:
        db.setm("trade_stream_state","BLOCKED");db.setm("trade_stream_error","PumpPortal trade subscriptions require an API key with provider-required funding. Add PUMPPORTAL_API_KEY or BITQUERY_TOKEN.")
        while True: await asyncio.sleep(30)
    uri="wss://pumpportal.fun/api/data?api-key="+config.PUMPPORTAL_API_KEY;back=1
    while True:
        sender=None; ws=None
        try:
            ws=await websockets.connect(uri,ping_interval=None,close_timeout=3,max_queue=config.WS_MAX_QUEUE)
            db.inc("stream_generation"); db.setm("trade_stream_state","CONNECTING");db.setm("trade_stream_error","");back=1
            if ACTIVE:
                keys=list(ACTIVE.keys())
                for i in range(0,len(keys),50):
                    batch=keys[i:i+50];await ws.send(json.dumps({"method":"subscribeTokenTrade","keys":batch}));db.inc("sub_resubscribed",len(batch))
            async def send_subscriptions():
                while True:
                    m=await q.get(); db.setm("sub_queue_size",q.qsize())
                    await ws.send(json.dumps({"method":"subscribeTokenTrade","keys":[m]}));db.inc("sub_requests");db.stream_debug(m,"SUB_REQUEST","PumpPortal subscribeTokenTrade sent");ACTIVE[m]=time.time()
                    while len(ACTIVE)>config.MAX_ACTIVE_SUBSCRIPTIONS:
                        old,_=ACTIVE.popitem(last=False);await ws.send(json.dumps({"method":"unsubscribeTokenTrade","keys":[old]}));db.inc("unsubscribed_total")
                    db.setm("active_subscriptions",len(ACTIVE))
            sender=asyncio.create_task(send_subscriptions())
            while True:
                raw=await asyncio.wait_for(ws.recv(),timeout=max(30,config.STREAM_STALE_SECONDS*2));db.inc("ws_messages")
                try:x=json.loads(raw)
                except:continue
                m=x.get("mint");typ=x.get("txType","")
                if m and typ in ("buy","sell"):
                    if m in ACTIVE:
                        if m not in SUB_OK_MINTS:SUB_OK_MINTS.add(m);db.inc("sub_ok")
                        ACTIVE.pop(m,None);ACTIVE[m]=time.time()
                    p,src=extract_price(x);process_trade(m,typ,p,x.get("traderPublicKey",""),"pumpportal:"+src);cleanup();continue
                txt=str(x);lo=txt.lower()
                if "only available when connecting with an api key funded" in lo:
                    db.inc("sub_rejected");db.setm("trade_stream_state","BLOCKED");db.setm("trade_stream_error",txt[:400]);db.stream_debug("","SUB_REJECTED",txt)
                    if config.PUMPPORTAL_FAIL_FAST:
                        if sender:
                            sender.cancel()
                            try: await sender
                            except BaseException: pass
                            sender=None
                        try: await ws.close()
                        except BaseException: pass
                        ws=None
                        db.setm("trade_stream_state","BLOCKED")
                        db.setm("trade_stream_error","PumpPortal funding/account requirement detected. COST-SAFE fail-fast engaged: reconnect loop stopped.")
                        while True: await asyncio.sleep(config.PUMPPORTAL_BLOCK_COOLDOWN)
                    raise RuntimeError("PumpPortal trade stream blocked by account requirements")
                if "subscrib" in lo or "unsubscrib" in lo:db.stream_debug("","CONTROL",txt[:300])
        except Exception as e:
            db.inc("ws_errors");db.inc("reconnects");db.reconnect_event("pumpportal",repr(e),len(P));db.setm("trade_stream_state","DEGRADED");db.setm("trade_stream_error",repr(e)[:400])
            await asyncio.sleep(back+random.uniform(0,config.RECONNECT_JITTER_MAX));back=min(config.RECONNECT_BACKOFF_MAX,back*2)
        finally:
            if sender:
                sender.cancel()
                try: await sender
                except BaseException: pass
            if ws:
                try: await ws.close()
                except BaseException: pass
            # Yield before constructing a fresh protocol/transport. No cancelled recv task is reused.
            await asyncio.sleep(0.05)



def _free_api_fetch(mint):
    qs=urllib.parse.urlencode({"limit":config.FREE_API_LIMIT,"offset":0,"minimumSize":0})
    url=f"{config.FREE_API_BASE}/trades/all/{urllib.parse.quote(mint)}?{qs}"
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=config.FREE_API_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8","replace"))

def _free_trade_fields(x,mint):
    sig=str(x.get("signature") or x.get("txSignature") or x.get("transactionSignature") or x.get("id") or "")
    trader=str(x.get("user") or x.get("userAddress") or x.get("traderPublicKey") or x.get("trader") or x.get("owner") or "")
    is_buy=x.get("is_buy")
    if is_buy is None:is_buy=x.get("isBuy")
    typ=str(x.get("txType") or x.get("type") or x.get("side") or "").lower()
    side="buy" if is_buy is True or typ in ("buy","b") else "sell" if is_buy is False or typ in ("sell","s") else ""
    p=0.0
    for k in ("price","priceSol","priceSOL","tokenPrice","price_in_sol"):
        p=num(x.get(k))
        if p:break
    if not p:
        sol=num(x.get("sol_amount") or x.get("solAmount") or x.get("sol"))
        tok=num(x.get("token_amount") or x.get("tokenAmount") or x.get("amount"))
        if sol and tok:
            # Pump.fun legacy trade API commonly reports lamports/raw token units.
            if sol>1e5:sol/=1e9
            if tok>1e8:tok/=1e6
            p=sol/tok if tok else 0.0
    ts=num(x.get("timestamp") or x.get("created_timestamp") or x.get("createdTs") or x.get("blockTime"))
    if ts>1e12:ts/=1000.0
    key=sig or f"{ts:.3f}:{side}:{p:.16g}:{trader}"
    return key,side,p,trader,ts

async def free_api_trades(q):
    """Cost-safe polling of Pump.fun public frontend trade history. No API key and no SOL-funded subscription."""
    db.setm("trade_stream_provider","free_pump_frontend")
    db.setm("trade_stream_state","CONNECTING")
    db.setm("trade_stream_error","")
    watch=OrderedDict(); seen={}; consecutive_errors=0
    while True:
        # Drain newly discovered mints without blocking the polling loop.
        while True:
            try:m=q.get_nowait()
            except asyncio.QueueEmpty:break
            watch[m]=time.time();watch.move_to_end(m)
            while len(watch)>config.FREE_API_MAX_MINTS:
                old,_=watch.popitem(last=False);seen.pop(old,None)
        if not watch:
            await asyncio.sleep(0.15);continue
        any_ok=False; any_event=False
        for m in list(watch.keys()):
            if m not in S:
                watch.pop(m,None);seen.pop(m,None);continue
            try:
                rows=await asyncio.to_thread(_free_api_fetch,m)
                if isinstance(rows,dict):rows=rows.get("trades") or rows.get("data") or rows.get("results") or []
                if not isinstance(rows,list):rows=[]
                any_ok=True; db.inc("free_api_requests")
                parsed=[]
                for x in rows:
                    if not isinstance(x,dict):continue
                    key,side,p,trader,ts=_free_trade_fields(x,m)
                    if side and p>0:parsed.append((ts,key,side,p,trader))
                parsed.sort(key=lambda z:z[0] or 0)
                ss=seen.setdefault(m,set())
                for ts,key,side,p,trader in parsed:
                    if key in ss:continue
                    ss.add(key)
                    if len(ss)>200:
                        # bounded dedupe; keeping recent response keys is enough for polling overlap
                        ss=set(list(ss)[-120:]);seen[m]=ss
                    process_trade(m,side,p,trader,"free_pump_frontend");any_event=True
                if parsed:
                    db.setm("trade_stream_last_ts",time.time());db.setm("trade_stream_state","LIVE")
            except Exception as e:
                db.inc("free_api_errors");db.setm("trade_stream_error",f"FREE API {type(e).__name__}: {e}"[:400])
            await asyncio.sleep(0.02)
        if any_ok:
            consecutive_errors=0
            if not any_event and str(db.getm("trade_stream_state"))!="LIVE":db.setm("trade_stream_state","LIVE")
        else:
            consecutive_errors+=1
            if consecutive_errors>=3:db.setm("trade_stream_state","DEGRADED")
        cleanup();await asyncio.sleep(config.FREE_API_POLL_SECONDS)

async def pumpapi_trades(q):
    global TRADE_GAP_START
    """One PumpApi socket with an application-level traffic watchdog.
    CONNECTED means handshake succeeded; RECEIVING means actual market trade messages are arriving.
    Soft silence blocks entries; hard silence rebuilds the socket, including during control traffic.
    """
    db.setm("trade_stream_provider","pumpapi_free")
    db.setm("trade_stream_state","CONNECTING"); db.setm("trade_watchdog_state","STARTING")
    db.setm("trade_stream_error",""); db.setm("pumpapi_socket_active",0)
    back=max(1.0,config.PUMPAPI_MIN_RECONNECT_DELAY)
    async def drain_discovery_queue():
        while True:
            await q.get(); db.setm("sub_queue_size",q.qsize())
    drainer=asyncio.create_task(drain_discovery_queue())
    probe=stream_diagnostics.Probe(db.setm)
    telemetry=runtime_control.MarketTelemetry()
    ws=None
    from market_watchdog import MarketSilence
    watch=None
    def check_market_silence():
        observed=watch.poll();stall=observed["age_s"]
        if observed["publish"]:
            db.setm("trade_max_stall_seconds",max(float(db.getm("trade_max_stall_seconds",0)),stall))
        if observed["state"]=="HARD_STALE":
            db.inc("trade_stall_restarts");db.setm("trade_stream_state","STALLED");db.setm("trade_watchdog_state","STALLED")
            db.setm("trade_stream_error",f"Trade stream hard-stalled: no market message for {stall:.1f}s")
            db.stream_debug("","WATCHDOG_HARD_STALL",f"no market message {stall:.2f}s; forcing socket restart; open={len(P)}")
            raise RuntimeError(f"trade stream hard stall {stall:.1f}s")
        if observed["state"]=="SOFT_STALE" and observed["publish"]:
            db.setm("trade_stream_state","MARKET_STALE");db.setm("trade_watchdog_state","MARKET_STALE")
            db.setm("trade_stream_error",f"Market traffic stale for {stall:.1f}s; entries blocked, socket retained")
            db.inc("trade_soft_stale_ticks")
    try:
        while True:
            connected_at=0.0; stopping=False
            try:
                ping_interval=None if config.PUMPAPI_PING_INTERVAL <= 0 else config.PUMPAPI_PING_INTERVAL
                ws=await websockets.connect(config.PUMPAPI_STREAM_URL,ping_interval=ping_interval,
                    close_timeout=config.PUMPAPI_CLOSE_TIMEOUT,open_timeout=config.PUMPAPI_OPEN_TIMEOUT,
                    max_queue=config.WS_MAX_QUEUE)
                probe.connected()
                __import__('recovery_gate').connection()
                connection_epoch=(__import__('recovery_gate').status() or {}).get('epoch')
                connected_at=time.time()
                watch=MarketSilence(config.PUMPAPI_STALL_SECONDS,config.PUMPAPI_HARD_STALL_SECONDS,config.PUMPAPI_WATCHDOG_TICK_SECONDS)
                db.inc("stream_generation"); db.inc("pumpapi_connects")
                db.setm("pumpapi_socket_active",1); db.setm("trade_stream_state","CONNECTED"); db.setm("trade_watchdog_state","CONNECTED"); db.setm("trade_stream_error","")
                db.stream_debug("","WATCHDOG_CONNECTED",f"PumpApi connected; waiting for market traffic; timeout={config.PUMPAPI_STALL_SECONDS:.0f}s")
                back=max(1.0,config.PUMPAPI_MIN_RECONNECT_DELAY)
                while True:
                    if (__import__('recovery_gate').status() or {}).get('epoch')!=connection_epoch:raise RuntimeError('RECOVERY_RECONNECT_REQUIRED')
                    check_market_silence()
                    recv_started=time.monotonic()
                    try:
                        raw=await asyncio.wait_for(ws.recv(),timeout=config.PUMPAPI_WATCHDOG_TICK_SECONDS)
                    except asyncio.TimeoutError:
                        telemetry.flush(db,force=True)
                        probe.flush()
                        check_market_silence()
                        continue
                    if (__import__('recovery_gate').status() or {}).get('epoch')!=connection_epoch:raise RuntimeError('RECOVERY_RECONNECT_REQUIRED')
                    handler_started=probe.received(ws,recv_started)
                    try:
                        now=time.time(); recv_mono=time.monotonic(); telemetry.received(now);telemetry.flush(db)
                        try:x=json.loads(raw)
                        except Exception: db.inc("pumpapi_parse_errors"); continue
                        if not isinstance(x,dict):continue
                        probe.provider(x,now)
                        action=str(x.get("action") or x.get("txType") or "").lower()
                        if action not in ("buy","sell"):continue
                        if str(x.get("pool") or "").lower()!=config.PUMPAPI_POOL:
                            if str(x.get("mint") or "") in P: diagnostics.evidence(db.con(),str(x.get("mint") or ""),("OTHER_POOL" if x.get("pool") else "MISSING_POOL"),str(x.get("pool") or ""),now)
                            continue
                        # Heartbeat is provider-wide market traffic, not only candidate mints.
                        watch.market();telemetry.market(now)
                        if TRADE_GAP_START is not None:
                            gap=max(0.0,now-TRADE_GAP_START); db.inc("trade_gap_count"); db.inc("trade_gap_seconds",gap); db.setm("last_trade_gap_seconds",gap)
                            if gap>=config.DATA_GAP_MARK_SECONDS:
                                for _q in P.values(): _q["gap_seconds"]=_q.get("gap_seconds",0.0)+gap; _q["gap_count"]=_q.get("gap_count",0)+1
                            db.stream_debug("","GAP_END",f"trade feed recovered after {gap:.2f}s; open={len(P)}")
                            TRADE_GAP_START=None
                        m=str(x.get("mint") or "")
                        if not m:continue
                        if m in S or branch_transport.watches(m):telemetry.flush(db,force=True)
                        if m not in S:
                            if branch_transport.watches(m):
                                bp=num(x.get('price'));bb='provider_price'
                                if not bp:
                                    bb='trade_amount_ratio';qa=num(x.get('quoteAmount'));ta=num(x.get('tokenAmount'));bp=qa/ta if qa and ta else 0.0
                                branch_transport.emit(m,action,bp,str(x.get('txSigner') or x.get('trader') or ''),'pumpapi_free',cost_evidence.metadata(x,'pumpapi_free',bb,now,recv_mono),branch_transport.born(m))
                            continue
                        p=num(x.get("price"));basis="provider_price"
                        if not p:
                            basis="trade_amount_ratio"
                            qa=num(x.get("quoteAmount"));ta=num(x.get("tokenAmount"));p=qa/ta if qa and ta else 0.0
                        trader=str(x.get("txSigner") or x.get("trader") or "")
                        process_trade(m,action,p,trader,"pumpapi_free",cost_evidence.metadata(x,"pumpapi_free",basis,now,recv_mono)); db.setm("trade_stream_last_ts",now); cleanup()
                    finally:
                        telemetry.flush(db)
                        probe.finished(ws,handler_started)
            except asyncio.CancelledError:
                stopping=True;raise
            except (__import__('sqlite3').Error,db.LocalDatabaseFailure) as e:
                # A local database failure is not a provider disconnect. Only telemetry
                # has a separately proven rollback/retry path; never replay a trade here.
                stopping=True
                system_health.failure(config.DB,'TRADE_STREAM_LOCAL_DATABASE',e)
                raise
            except Exception as e:
                probe.error();probe.flush(force=True)
                if TRADE_GAP_START is None: TRADE_GAP_START=time.time(); db.stream_debug("","GAP_START",f"trade feed unavailable; open={len(P)}")
                db.inc("trade_ws_errors");db.inc("trade_reconnects");db.inc("ws_errors");db.inc("reconnects");db.reconnect_event("pumpapi_free",repr(e),len(P))
                if str(db.getm("trade_watchdog_state"))!="STALLED": db.setm("trade_stream_state","DEGRADED"); db.setm("trade_watchdog_state","DEGRADED")
                if "watchdog stall" not in repr(e): db.setm("trade_stream_error",f"PumpApi stream: {e!r}"[:400])
            finally:
                try:
                    telemetry.flush(db,force=True)
                    db.setm("pumpapi_socket_active",0)
                finally:
                    if ws is not None:
                        closing_ws,ws=ws,None
                        await runtime_control.close_market_socket(closing_ws,stopping=stopping)
                    if not stopping:await asyncio.sleep(config.PUMPAPI_CLOSE_GRACE)
            db.inc("pumpapi_reconnect_waits")
            err=str(db.getm("trade_stream_error",""))
            # Cool down aggressively after provider single-WS rejection; hard stalls use a shorter controlled delay.
            wait=max(back, config.PUMPAPI_SERVER_SINGLE_WS_COOLDOWN if "only one websocket connection" in err.lower() else config.PUMPAPI_WATCHDOG_RECONNECT_DELAY if "stall" in err.lower() else 0)
            await asyncio.sleep(wait+random.uniform(0,config.RECONNECT_JITTER_MAX))
            back=min(config.RECONNECT_BACKOFF_MAX,max(config.PUMPAPI_MIN_RECONNECT_DELAY,back*2))
    finally:
        probe.flush(force=True)
        if ws is not None:
            try: await ws.close()
            except Exception: pass
        drainer.cancel()
        try:await drainer
        except BaseException:pass

BITQUERY_QUERY='''subscription RapidPumpTrades { Solana { DEXTrades(where: {Trade: {Dex: {ProtocolName: {is: "pump"}}}, Transaction: {Result: {Success: true}}}) { Trade { Buy { Account { Address } Currency { MintAddress Symbol } Price } Sell { Account { Address } Currency { MintAddress Symbol } Price } } Transaction { Signer Signature } } } }'''

async def bitquery_trades(q):
    db.setm("trade_stream_provider","bitquery")
    if not config.BITQUERY_TOKEN:
        db.setm("trade_stream_state","BLOCKED");db.setm("trade_stream_error","BITQUERY_TOKEN is empty. Add a Bitquery access token or use a funded PumpPortal API key.");
        while True: await asyncio.sleep(30)
    url="wss://streaming.bitquery.io/graphql?token="+config.BITQUERY_TOKEN;back=1
    while True:
        try:
            async with websockets.connect(url,subprotocols=["graphql-ws"],ping_interval=None,close_timeout=3,max_queue=config.WS_MAX_QUEUE) as ws:
                db.setm("trade_stream_state","CONNECTING");db.setm("trade_stream_error","");await ws.send(json.dumps({"type":"connection_init"}));back=1
                while True:
                    msg=json.loads(await asyncio.wait_for(ws.recv(),timeout=20));typ=msg.get("type")
                    if typ=="connection_ack":
                        await ws.send(json.dumps({"type":"start","id":"rapid","payload":{"query":BITQUERY_QUERY}}));db.inc("sub_requests");db.inc("sub_ok");db.setm("trade_stream_state","LIVE");db.stream_debug("","SUB_OK","Bitquery global Pump.fun DEXTrades stream live");break
                    if typ in ("error","connection_error"):raise RuntimeError(str(msg.get("payload")))
                while True:
                    raw=await asyncio.wait_for(ws.recv(),timeout=30);msg=json.loads(raw);db.inc("bitquery_messages")
                    if msg.get("type") in ("ka","pong"):continue
                    if msg.get("type")=="error":raise RuntimeError(str(msg.get("payload")))
                    payload=msg.get("payload",{});data=payload.get("data",payload);rows=((data or {}).get("Solana") or {}).get("DEXTrades") or []
                    for row in rows:
                        tr=row.get("Trade") or {};buy=tr.get("Buy") or {};sell=tr.get("Sell") or {};bc=buy.get("Currency") or {};sc=sell.get("Currency") or {}
                        bm=bc.get("MintAddress") or "";sm=sc.get("MintAddress") or "";signer=(row.get("Transaction") or {}).get("Signer","")
                        # Candidate token on buy side => token buy; candidate token on sell side => token sell.
                        if bm in S:process_trade(bm,"buy",num(buy.get("Price")) or (1/num(sell.get("Price")) if num(sell.get("Price")) else 0),signer,"bitquery")
                        if sm in S:process_trade(sm,"sell",num(sell.get("Price")) or (1/num(buy.get("Price")) if num(buy.get("Price")) else 0),signer,"bitquery")
                    if rows:db.setm("trade_stream_last_ts",time.time())
                    cleanup()
        except Exception as e:
            db.inc("ws_errors");db.inc("reconnects");db.reconnect_event("bitquery",repr(e),len(P));db.setm("trade_stream_state","DEGRADED");db.setm("trade_stream_error",repr(e)[:400]);await asyncio.sleep(back+random.uniform(0,config.RECONNECT_JITTER_MAX));back=min(config.RECONNECT_BACKOFF_MAX,back*2)

async def cost_safe_no_trade_stream(q):
    db.setm("trade_stream_provider","cost_safe_offline")
    db.setm("trade_stream_state","BLOCKED")
    db.setm("trade_stream_error","COST-SAFE: no free trade-stream token configured. Discovery recording stays live; no PumpPortal trade subscription or paid reconnect loop is used. Set BITQUERY_TOKEN for live paper data, or use run_replay.bat for offline tests.")
    while True:
        await asyncio.sleep(60)

def observation_cost(m,entry,price):
    return _shadow_roundtrip_cost(m,entry,price,config.POSITION_SIZE*(price/entry-1),"BASE")[0]

async def observation_timer():
    while True:
        if config.EXECUTION_MODEL_ENABLED:execution.expire(db.con())
        cost_evidence.poll(db.con())
        observation.poll(transport_ok=str(db.getm("trade_watchdog_state","STARTING"))=="RECEIVING",market_ts=float(db.getm("trade_last_market_ts",0)),market_stale_after=config.PUMPAPI_STALL_SECONDS)
        await asyncio.sleep(.025)

async def heartbeat():
    global SHADOW_PAUSED
    try:proc=psutil.Process(os.getpid());proc.cpu_percent(None)
    except psutil.Error:proc=None
    ticks=0
    expected=time.monotonic()+1.0; last_checks=0; last_triggers=0
    while True:
        flush_shadow_queue()
        with db.con() as _oc:
            replay_log.flush(_oc)
            observation.flush(_oc)
        # Expire FAST_STOP shadow watches even if a mint stops producing events.
        wall=time.time()
        for _m,_q in list(FAST_SHADOW_V3.items()):
            if wall-_q["started"] > config.FAST_SHADOW_MAX_SECONDS+5: FAST_SHADOW_V3.pop(_m,None)
        nowm=time.monotonic(); lag_ms=max(0.0,(nowm-expected)*1000.0); expected=nowm+1.0
        __import__('wal_checkpoint_v1014').guard()
        exit_audit.checkpoint(db.con());runtime_status.beat(db.con());run_audit.beat(db.con());db.setm("heartbeat_ts",time.time());cleanup(force=True);ticks+=1
        try:
            if proc is None:proc=psutil.Process(os.getpid());proc.cpu_percent(None)
            cpu=proc.cpu_percent(None);rss=proc.memory_info().rss/(1024*1024)
        except psutil.Error:cpu=None;rss=None;proc=None
        if ((cpu is not None and cpu>=config.CPU_SHADOW_PAUSE_PCT) or lag_ms>=config.EVENT_LOOP_LAG_PAUSE_MS) and not SHADOW_PAUSED:
            SHADOW_PAUSED=True; db.inc("shadow_auto_pauses"); db.stream_debug("","CPU_GUARD",f"shadow paused cpu={cpu}% lag={lag_ms:.1f}ms")
        elif SHADOW_PAUSED and cpu is not None and cpu<=config.CPU_SHADOW_RESUME_PCT and lag_ms<config.EVENT_LOOP_LAG_PAUSE_MS/2:
            SHADOW_PAUSED=False; db.stream_debug("","CPU_GUARD",f"shadow resumed cpu={cpu:.1f}% lag={lag_ms:.1f}ms")
        checks=HOT["shadow_checks"]; triggers=HOT["shadow_triggers"]
        calls=max(1,HOT["trade_calls"])
        db.setmany(dict(process_cpu_pct=round(cpu,1) if cpu is not None else 'UNKNOWN',resource_sample_available=int(cpu is not None),event_loop_lag_ms=round(lag_ms,1),
          shadow_paused=int(SHADOW_PAUSED),pp_engine_enabled=int(config.PP_SHADOW_V2),pp_engine_called=HOT['pp_calls'],
          shadow_checks_sec=checks-last_checks,shadow_triggers_sec=triggers-last_triggers,shadow_state_count=len(PP_V2_HITS),
          perf_trade_ms=HOT['trade_ms']/calls,perf_manage_ms=HOT['manage_ms']/calls,perf_db_ms=HOT['db_ms']/calls,
          perf_signal_ms=HOT['signal_ms']/calls,perf_trade_calls=HOT['trade_calls'],process_ram_mb=round(rss,1) if rss is not None else 'UNKNOWN',
          tracked_mints=len(S),open_positions_mem=len(P),active_subscriptions=len(ACTIVE),pass_pins=len(PASS_PIN)))
        last_checks=checks;last_triggers=triggers
        HOT.update({"trade_calls":0,"trade_ms":0.0,"manage_ms":0.0,"db_ms":0.0,"signal_ms":0.0})
        if rss is not None and rss>=config.MEMORY_SOFT_LIMIT_MB:
            # Aggressive cleanup of non-position candidates, oldest first.
            now=time.time(); target=max(100,config.MAX_CANDIDATES//2)
            for m in sorted([m for m in S if m not in P and not execution.pending(db.con(),m,"BUY") and m not in cost_evidence.WATCH and m not in FAST_RECOVERY_30 and m not in observation.ACTIVE], key=lambda x:S[x].get("born",now)):
                if len(S)<=target: break
                S.pop(m,None);ACTIVE.pop(m,None);PASS_PIN.pop(m,None);FIRST_TRADE_SEEN.discard(m);SUB_OK_MINTS.discard(m);PASS_SEEN.discard(m);clock_lab.forget(m)
            gc.collect();db.inc("memory_cleanups");db.setm("memory_state","CLEANUP")
        else: db.setm("memory_state","OK" if rss is not None else "UNKNOWN")
        if rss is not None and rss>=config.MEMORY_HARD_LIMIT_MB: db.setm("memory_state","HARD_LIMIT")
        if ticks%30==0: gc.collect()
        await asyncio.sleep(1)

async def run():
    import clean_campaign
    clean_campaign.preflight(config.DB)
    global SINGLETON_SOCKET
    if not config.JUPITER_API_KEY:
        raise RuntimeError(f"{config.VERSION}: JUPITER_API_KEY is required. Run 02_SETTINGS.bat first; do not enter a wallet private key.")
    await exit_quotes.refresh_fx()
    if exit_quotes.FX_STATUS!='READY':
        raise RuntimeError("Quote startup check failed: "+exit_quotes.FX_STATUS+". Run 02_SETTINGS.bat and 04_CHECK.bat; no test was started.")
    # Cross-version local process guard: only one hardened PumpApi bot can own the socket.
    SINGLETON_SOCKET=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        SINGLETON_SOCKET.bind(("127.0.0.1",config.PUMPAPI_SINGLETON_PORT)); SINGLETON_SOCKET.listen(1)
    except OSError as e:
        raise RuntimeError("Another PumpApi bot instance is already running on the hardened singleton guard. Close it before starting this copy.") from e
    startup_clock=await time_check.startup()
    db.init();clean_campaign.ensure(db.con(),config.DB);wallet_costs.ensure(db.con());preentry.recover(db.con());shadow_preflight.recover(db.con());auto_simulation.recover(db.con());cost_evidence.restart(db.con());execution.restart(db.con());restore_positions();db.setm("bot_start_ts",time.time());db.setm("bot_pid",os.getpid());db.setm("stream_state","STARTING")
    runtime_status.begin(db.con())
    run_audit.begin(db.con(),startup_clock)
    db.runtime_mode()
    import recovery_gate
    db.setm('recovery_required_v75',1)
    db.setm("single_ws_guard",1)
    q=asyncio.Queue(maxsize=config.SUB_QUEUE_MAX)
    provider=config.DATA_PROVIDER
    if provider=="cost_safe":
        provider="pumpapi"
    elif provider=="auto":
        provider="pumpapi"
    db.setm('matched_mode_v64',1)
    branch_plan=__import__('branch_plan_v101').begin(db.con(),config.DB,run_audit.ACTIVE)
    publisher=await asyncio.to_thread(branch_transport.Fanout,config.DB);branch_transport.ACTIVE=publisher
    __import__('branch_plan_v101').complete(db.con(),branch_plan,publisher)
    recovery_guard=recovery_gate.Coordinator(config.DB,run_audit.ACTIVE)
    recovery_gate.ACTIVE=recovery_guard
    runtime_probe=runtime_control.Probe(config.DB,extra=lambda:publisher.snapshot()).start()
    trade_task=pumpapi_trades(q) if provider=="pumpapi" else free_api_trades(q) if provider=="free_api" else bitquery_trades(q) if provider=="bitquery" else pumpportal_trades(q) if provider=="pumpportal" else cost_safe_no_trade_stream(q)
    tasks=[('recovery_guard',recovery_guard.run()),('clock_reference',clock_lab.references()),('clock_watch',clock_lab.local_watch()),
      ('heartbeat',heartbeat()),('observation',observation_timer()),('quotes',exit_quotes.worker()),
      ('auto_simulation',auto_simulation.worker()),('preentry',preentry.worker()),
      ('shadow_preflight',shadow_preflight.worker()),('discovery',pumpportal_discovery(q)),('trade_stream',trade_task)]
    branch_cleanup=set()
    monitor_watch=__import__('branch_isolation').MonitorWatch(config.DB)
    async def branch_watch():
        while True:
            if runtime_control.stop_requested(config.DB,run_audit.ACTIVE):
                publisher.begin_stop()
                raise runtime_control.RequestedStop()
            await asyncio.to_thread(publisher.refresh_positions)
            if runtime_control.stop_requested(config.DB,run_audit.ACTIVE):
                publisher.begin_stop()
                raise runtime_control.RequestedStop()
            db.setmany(dict(branch_queue=publisher.pending,branch_dropped=publisher.dropped))
            for failed_branch in await asyncio.to_thread(publisher.isolate_failures):
                branch_cleanup.add(asyncio.create_task(asyncio.to_thread(failed_branch.close)))
            for cleanup in list(branch_cleanup):
                if cleanup.done():
                    try:cleanup.result()
                    except Exception as exc:system_health.failure(config.DB,'isolated_branch_cleanup',exc)
                    branch_cleanup.remove(cleanup)
            supervision=await asyncio.to_thread(monitor_watch.sample)
            db.setmany(dict(monitor_supervision_v79=supervision['status'],
                monitor_publication_v99=json.dumps(supervision['publication_v99'])))
            await asyncio.sleep(1)
    tasks.append(('amm_branch_watch',branch_watch()))
    # v0.7.101.8: a SELL on a curve that stays drained is written off as in ROUTE (this module, not a copy of it).
    tasks.append(('primary_write_off',__import__('primary_write_off').worker(__import__('sys').modules[__name__],config.DB)))
    running=[asyncio.create_task(system_health.supervise(name,task,config.DB,allow_return=name=='clock_reference')) for name,task in tasks]
    counter_sink=__import__('counter_writer').start(config.DB,session=run_audit.ACTIVE)
    try:
        await asyncio.gather(*running)
    finally:
        if hasattr(publisher,'begin_stop'):publisher.begin_stop()
        branch_transport.ACTIVE=None
        tasks_stopped=await runtime_control.cancel_tasks(list(zip([name for name,_ in tasks],running)),config.DB)
        try:
            if tasks_stopped:
                with db.atomic():execution.cancel_unfilled_buys_on_stop(db.con())
        finally:
            await __import__('counter_writer').stop(counter_sink)
            try:
                await asyncio.to_thread(publisher.close)
                if branch_cleanup:await asyncio.gather(*branch_cleanup,return_exceptions=True)
            finally:
                await runtime_probe.close()
                recovery_guard.close();recovery_gate.ACTIVE=None
        if not tasks_stopped:raise RuntimeError('TASK_SHUTDOWN_TIMEOUT')

if __name__=="__main__":
    if os.name=='nt':
        import signal
        def request_stop(*_):
            if branch_transport.LAST and hasattr(branch_transport.LAST,'begin_stop'):branch_transport.LAST.begin_stop()
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK,request_stop)
    try:asyncio.run(run())
    except (KeyboardInterrupt,runtime_control.RequestedStop):
        branch_transport.close_last()
        flush_shadow_queue()
        with db.con() as c:
            replay_log.flush(c)
            observation.flush(c)
        exit_audit.checkpoint(db.con(),force=True)
        runtime_status.beat(db.con(),stopped=True)
        run_audit.beat(db.con(),status="STOPPED",force=True)
        db.setm("stream_state","STOPPED");db.setm("trade_stream_state","STOPPED")

    except BaseException as exc:
        branch_transport.close_last()
        system_health.failure(config.DB,"main",exc)
        try:
            if run_audit.ACTIVE:
                exit_audit.checkpoint(db.con(),force=True)
                run_audit.beat(db.con(),status="FAILED",force=True)
                db.setmany(dict(stream_state='FAILED',trade_stream_state='FAILED',runtime_state_v41='FAILED',runtime_updated_ts_v41=time.time()))
        except Exception as storage_error:
            system_health.failure(config.DB,'failure_checkpoint',storage_error)
        raise

    finally:
        __import__('wal_checkpoint_v1014').close()
        __import__('shutdown_audit').closed(config.DB,'primary',run_audit.ACTIVE)
