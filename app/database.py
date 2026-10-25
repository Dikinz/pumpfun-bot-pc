import run_audit,exit_audit
import clock_lab
import shadow_preflight
import sqlite3,time,threading
from contextlib import contextmanager
from pathlib import Path
import config,position_monitor,wallet_costs,price_exit_trial
import entry_diagnostics
import diagnostics,exit_quotes,execution_verification,auto_simulation,exit_comparison,preentry
import replay_log,cost_evidence,execution_model,paper_capital,entry_activity

class Connection(sqlite3.Connection):
    def logged(self,operation,fn,*args):
        try:return fn(*args)
        except sqlite3.Error as exc:
            __import__('sqlite_diagnostics').record(config.DB,operation,exc,in_transaction=self.in_transaction)
            raise
    def execute(self,sql,parameters=(),/):
        import runtime_control as rt
        op="SQL_"+(sql.lstrip().split(None,1)[0].upper() if sql.strip() else 'EMPTY')
        if rt.ACTIVE is None:return self.logged(op,super().execute,sql,parameters)
        with rt.scope(op):return self.logged(op,super().execute,sql,parameters)
    def executemany(self,sql,parameters,/):return self.logged('EXECUTEMANY',super().executemany,sql,parameters)
    def executescript(self,sql,/):return self.logged('EXECUTESCRIPT',super().executescript,sql)
    def commit(self):return self.logged('COMMIT',super().commit)
    def rollback(self):return self.logged('ROLLBACK',super().rollback)
    atomic_depth=0
    def __exit__(self,*exc):
        if self.atomic_depth:return False
        return self.logged('TRANSACTION_EXIT',super().__exit__,*exc)

@contextmanager
def atomic():
    c=con();outer=c.atomic_depth==0
    if outer:c.execute('BEGIN IMMEDIATE')
    c.atomic_depth+=1
    try:
        yield c
    except BaseException:
        c.atomic_depth-=1
        if outer:c.rollback()
        raise
    else:
        c.atomic_depth-=1
        if outer:c.commit()

_LOCAL=threading.local()
def con():
    # One persistent SQLite connection per process/thread. The old build opened
    # a new connection and re-issued journal_mode=WAL for almost every metric.
    c=getattr(_LOCAL,"conn",None)
    if c is None:
        Path(config.DB).parent.mkdir(parents=True, exist_ok=True)
        c=sqlite3.connect(config.DB,timeout=30,isolation_level=None,factory=Connection)
        c.row_factory=sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        __import__('sqlite_diagnostics').record(config.DB,'OPEN',journal_mode=c.execute('PRAGMA journal_mode').fetchone()[0],synchronous=c.execute('PRAGMA synchronous').fetchone()[0],busy_timeout=c.execute('PRAGMA busy_timeout').fetchone()[0])
        _LOCAL.conn=c
    return c

def runtime_mode():
    # Runtime lock contention must fail visibly instead of blocking every callback for 30s.
    # Schema migrations/startup keep their original budget; financial atomic rollback unchanged.
    con().execute('PRAGMA busy_timeout=250')


def init():
    with con() as c:
        check=[x[0] for x in c.execute('PRAGMA quick_check(20)')]
        if check!=['ok']:
            __import__('sqlite_diagnostics').record(config.DB,'STARTUP_INTEGRITY_FAILED',findings=check)
            raise RuntimeError('SQLITE_INTEGRITY_FAILED: stop and preserve data with 08_PRESERVE_DATA.bat; no repair performed')
        cols={r[1] for r in c.execute('PRAGMA table_info(trades)')}
        if cols and ('trade_id' not in cols or not c.execute("SELECT 1 FROM sqlite_master WHERE name='capital_ledger_v0721'").fetchone()):
            raise RuntimeError('v0.7.52 requires a NEW database. Keep old data; use data/rapid_07520.db.')
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(run_audit.SCHEMA)
        c.executescript(__import__('shared_lots').SCHEMA)
        c.executescript(exit_audit.SCHEMA)
        c.executescript(replay_log.SCHEMA)
        c.executescript(cost_evidence.SCHEMA)
        c.executescript(execution_model.SCHEMA)
        c.executescript(paper_capital.SCHEMA)
        c.executescript(entry_activity.SCHEMA)
        c.executescript(diagnostics.SCHEMA)
        c.executescript(exit_quotes.SCHEMA)
        c.executescript(execution_verification.SCHEMA)
        c.executescript(auto_simulation.SCHEMA)
        c.executescript(exit_comparison.SCHEMA)
        c.executescript(shadow_preflight.SCHEMA)
        shadow_preflight.freeze(c)
        c.executescript(clock_lab.SCHEMA)
        clock_lab.freeze(c)
        c.executescript(preentry.SCHEMA)
        c.executescript(entry_diagnostics.SCHEMA)
        c.executescript(position_monitor.SCHEMA)
        c.executescript(wallet_costs.SCHEMA)
        c.executescript(price_exit_trial.SCHEMA)
        paper_capital.init(c,config.START_BALANCE)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,side TEXT,price REAL,trader TEXT,price_source TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS discovery_events(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,raw TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS signals(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,score INT,decision TEXT,gate TEXT,
          trades INT,buyers INT,ratio REAL,momentum REAL,
          first_price REAL DEFAULT 0,current_price REAL DEFAULT 0,valid_prices INT DEFAULT 0,price_source TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS entry_debug(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,score INT,stage TEXT,reason TEXT,
          price REAL DEFAULT 0,cash REAL DEFAULT 0,open_positions INT DEFAULT 0,
          trades INT DEFAULT 0,buyers INT DEFAULT 0,ratio REAL DEFAULT 0,momentum REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stream_debug(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,stage TEXT,detail TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS trades(trade_id TEXT,size REAL,
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,side TEXT,price REAL,pnl REAL DEFAULT 0,
          reason TEXT,mfe REAL DEFAULT 0,mae REAL DEFAULT 0,hold REAL DEFAULT 0,entry_kind TEXT DEFAULT 'PRIMARY', ab_variant TEXT DEFAULT 'BASELINE', stop_trigger_price REAL DEFAULT 0, exit_latency_ms REAL DEFAULT 0, slippage_gap_pct REAL DEFAULT 0
        , UNIQUE(trade_id,side));
        CREATE TABLE IF NOT EXISTS positions(trade_id TEXT,
          mint TEXT PRIMARY KEY,entry REAL,current REAL,opened_ts REAL,last_update_ts REAL,
          high REAL,mfe REAL DEFAULT 0,mae REAL DEFAULT 0,entry_kind TEXT DEFAULT 'PRIMARY', ab_variant TEXT DEFAULT 'BASELINE', stop_trigger_price REAL DEFAULT 0, exit_latency_ms REAL DEFAULT 0, slippage_gap_pct REAL DEFAULT 0
        , size REAL, cost_profiles TEXT, gap_seconds REAL DEFAULT 0, gap_count INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS entry_features(trade_id TEXT,
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,score INT,trades INT,buyers INT,ratio REAL,momentum REAL,price REAL
        , UNIQUE(trade_id));
        CREATE TABLE IF NOT EXISTS entry_trajectory_v064(trade_id TEXT,
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,samples INT,price_accel REAL,pressure_delta REAL,pullback REAL,
          volatility REAL,last_return REAL,buy_streak INT,buy_pressure_recent REAL,buy_pressure_early REAL
        , UNIQUE(trade_id));
        CREATE TABLE IF NOT EXISTS entry_model_snapshot_v067(trade_id TEXT,
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,samples INT,ret_mean REAL,ret_vol REAL,accel REAL,last1 REAL,last3 REAL,pullback REAL,
          bp5 REAL,bp10 REAL,bp20 REAL,pressure_trend REAL,buy_streak INT,entry_score INT,entry_trades INT,buyers INT,ratio REAL,momentum REAL
        , UNIQUE(trade_id));
        CREATE TABLE IF NOT EXISTS model_gate_v069(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,model_p REAL,threshold REAL,decision TEXT,train_n INT,price REAL,score INT
        );
        CREATE TABLE IF NOT EXISTS final_gate_validation_v0692(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT UNIQUE,gate_decision TEXT,model_p REAL,entry REAL,exit REAL,reason TEXT,
          pnl REAL,mfe REAL,mae REAL,hold REAL,low_pnl REAL,base_pnl REAL
        );
        CREATE TABLE IF NOT EXISTS micro_confirm_v074(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,variant TEXT,signal_price REAL,entry REAL,exit REAL,reason TEXT,
          pnl REAL,mfe REAL,mae REAL,hold REAL,low_pnl REAL,base_pnl REAL,signal_return REAL,buy_pressure REAL,post_events INT,
          UNIQUE(mint,variant)
        );
        CREATE TABLE IF NOT EXISTS reconnect_events(
          id INTEGER PRIMARY KEY,ts REAL,stream TEXT,error TEXT,open_positions INT DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS entry_quality(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,decision TEXT,pullback REAL,recent_slope REAL,buy_pressure REAL,momentum REAL,score INT
        );
        CREATE TABLE IF NOT EXISTS profit_protection_v2(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,variant TEXT,trigger_ts REAL,trigger_price REAL,hypo_pnl REAL,mfe_at_trigger REAL,floor_return REAL,
          actual_reason TEXT,actual_exit REAL,actual_pnl REAL,delta_vs_actual REAL
        );
        CREATE TABLE IF NOT EXISTS fast_stop_shadow_v3(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,horizon_s INT,entry REAL,fast_exit REAL,fast_pnl REAL,shadow_price REAL,shadow_pnl REAL,delta_vs_fast REAL,observed_age REAL
        );
        CREATE TABLE IF NOT EXISTS fast_stop_recovery_v078(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT UNIQUE,entry REAL,fast_exit REAL,fast_pnl REAL,fast_mfe REAL,fast_mae REAL,fast_hold REAL,mfe_bin TEXT,
          final_price REAL,final_pnl REAL,delta_vs_fast REAL,low_fast_pnl REAL,low_recovery_pnl REAL,low_delta REAL,
          base_fast_pnl REAL,base_recovery_pnl REAL,base_delta REAL,max_price REAL,max_pnl REAL,max_delta REAL,max_base_pnl REAL,max_base_delta REAL,time_to_max REAL,
          min_price REAL,min_pnl REAL,min_delta REAL,time_to_min REAL,events_after_fast INT,final_source TEXT,last_event_age REAL,observed_age REAL
        );
        CREATE TABLE IF NOT EXISTS fast_recovery_signal_v079(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT UNIQUE,entry REAL,fast_exit REAL,fast_pnl REAL,fast_mfe REAL,fast_mae REAL,fast_hold REAL,mfe_bin TEXT,
          exit_price REAL,exit_pnl REAL,exit_reason TEXT,exit_age REAL,events_after_fast INT,armed INT,arm_age REAL,arm_price REAL,
          peak_price REAL,peak_recovery_pct REAL,max_delta REAL,time_to_max REAL,min_price REAL,min_delta REAL,time_to_min REAL,
          recent_momentum_pct REAL,buy_pressure REAL,delta_vs_fast REAL,low_fast_pnl REAL,low_signal_pnl REAL,low_delta REAL,
          base_fast_pnl REAL,base_signal_pnl REAL,base_delta REAL,oracle_base_delta REAL,big_worse INT,final_source TEXT,last_event_age REAL
        );
        CREATE TABLE IF NOT EXISTS fast_recovery_risk_v0710(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT UNIQUE,entry REAL,fast_exit REAL,fast_pnl REAL,fast_mfe REAL,fast_mae REAL,fast_hold REAL,mfe_bin TEXT,
          exit_price REAL,exit_pnl REAL,exit_reason TEXT,exit_age REAL,events_after_fast INT,armed INT,arm_age REAL,arm_price REAL,
          peak_price REAL,peak_recovery_pct REAL,max_delta REAL,time_to_max REAL,min_price REAL,min_delta REAL,time_to_min REAL,
          recent_momentum_pct REAL,buy_pressure REAL,delta_vs_fast REAL,low_fast_pnl REAL,low_signal_pnl REAL,low_delta REAL,
          base_fast_pnl REAL,base_signal_pnl REAL,base_delta REAL,oracle_base_delta REAL,big_worse INT,final_source TEXT,last_event_age REAL
        );
        CREATE TABLE IF NOT EXISTS fast_reentry_v0711(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT UNIQUE,entry REAL,fast_exit REAL,fast_pnl REAL,fast_mfe REAL,fast_mae REAL,fast_hold REAL,mfe_bin TEXT,
          reentered INT,reentry_age REAL,reentry_price REAL,signal_recovery_pct REAL,signal_momentum_pct REAL,signal_buy_pressure REAL,
          exit_price REAL,exit_reason TEXT,reentry_hold REAL,events_after_fast INT,peak_gain_pct REAL,mae_reentry_pct REAL,recent_momentum_pct REAL,buy_pressure REAL,
          ideal_reentry_pnl REAL,low_reentry_pnl REAL,base_reentry_pnl REAL,stress_reentry_pnl REAL,low_cost REAL,base_cost REAL,stress_cost REAL,
          base_fast_pnl REAL,combined_base_pnl REAL,big_loss INT,final_source TEXT,last_event_age REAL
        );
        CREATE TABLE IF NOT EXISTS observation_v0714(id INTEGER PRIMARY KEY,case_id TEXT,mint TEXT,ts REAL,kind TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS observation_case ON observation_v0714(case_id,kind);
        CREATE TABLE IF NOT EXISTS probation_snaps_v0713(
          mint TEXT,sec INT,actual_hold REAL,base_abort REAL,PRIMARY KEY(mint,sec)
        );
        CREATE TABLE IF NOT EXISTS false_bounce_v0712(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT UNIQUE,label TEXT,base_pnl REAL,signal_age REAL,signal_recovery_pct REAL,signal_momentum_pct REAL,signal_buy_pressure REAL,
          s1_ret REAL,s1_mom REAL,s1_bp REAL,s1_events INT,s2_ret REAL,s2_mom REAL,s2_bp REAL,s2_events INT,s3_ret REAL,s3_mom REAL,s3_bp REAL,s3_events INT,
          s5_ret REAL,s5_mom REAL,s5_bp REAL,s5_events INT,peak_gain_pct REAL,mae_reentry_pct REAL,reentry_hold REAL,exit_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS fast_stop_cases_v49(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT UNIQUE,entry REAL,fast_exit REAL,fast_pnl REAL,
          mfe REAL,mae REAL,hold REAL,post_trades INT,post_buys INT,post_sells INT,post_ratio REAL,
          drawdown_from_high REAL,class_score INT,class_band TEXT
        );
        CREATE TABLE IF NOT EXISTS smart_exit_shadow_v491(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,variant TEXT,horizon_s INT,baseline_pnl REAL,hypo_pnl REAL,
          delta REAL,better INT,worse INT,wait_mae REAL,worst_extra_loss REAL,pp_used INT DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS execution_sim_v060(trade_id TEXT,
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,reason TEXT,ideal_entry REAL,ideal_exit REAL,ideal_pnl REAL,
          sim_entry REAL,sim_exit REAL,sim_pnl REAL,total_cost REAL,buy_slip REAL,sell_slip REAL,fee_cost REAL,priority_cost REAL,
          latency_ms REAL,failed_tx INT DEFAULT 0,delta_vs_ideal REAL
        , UNIQUE(trade_id));

        CREATE TABLE IF NOT EXISTS execution_audit_v061(trade_id TEXT,
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,profile TEXT,reason TEXT,ideal_pnl REAL,sim_pnl REAL,total_cost REAL,
          buy_slippage_cost REAL,sell_slippage_cost REAL,trade_fee_cost REAL,priority_fee_cost REAL,latency_impact_cost REAL,failed_tx_penalty REAL,
          latency_ms REAL,failed_tx INT DEFAULT 0, UNIQUE(trade_id,profile)
        );        CREATE TABLE IF NOT EXISTS profit_protection(
          id INTEGER PRIMARY KEY,ts REAL,mint TEXT,entry REAL,protect_exit REAL,protect_pnl REAL,mfe REAL,mae REAL,hold REAL,
          shadow_done INT DEFAULT 0,original_reason TEXT DEFAULT '',original_exit REAL DEFAULT 0,original_pnl REAL DEFAULT 0,saved_vs_original REAL DEFAULT 0
        );
        """)
        defaults={"cash":config.START_BALANCE,"realized":0,"new":0,"events":0,"pass":0,"entries":0,"exits":0,
          "ws_messages":0,"ws_errors":0,"reconnects":0,"last_ws_ts":0,"heartbeat_ts":0,"bot_pid":0,
          "bot_start_ts":0,"stream_state":"STARTING","last_error":"","valid_price_events":0,
          "invalid_price_events":0,"active_subscriptions":0,"subscribed_total":0,"unsubscribed_total":0,
          "stale_reconnects":0,"sub_requests":0,"sub_ack_like":0,"first_trade_mints":0,"last_new_ts":0,"last_trade_ts":0,"last_sub_request_ts":0,"last_ack_ts":0,"unique_pass":0,"entry_attempts":0,"entry_blocked":0,"entry_ok":0,"pass_pinned":0,
          "sub_ok":0,"sub_rejected":0,"sub_resubscribed":0,"stale_forced_exits":0,"trade_stream_state":"STARTING","trade_stream_provider":"none","trade_stream_error":"","trade_stream_last_ts":0,"bitquery_messages":0,"process_ram_mb":0,"tracked_mints":0,"open_positions_mem":0,"pass_pins":0,"sub_queue_size":0,"sub_queue_dropped":0,"memory_cleanups":0,"memory_state":"OK","early_fail_exits":0,"fast_stop_exits":0,"fast_stop_triggered":0,"stream_generation":0,"trade_reconnects":0,"trade_ws_errors":0,"discovery_reconnects":0,"discovery_ws_errors":0,"trade_gap_count":0,"trade_gap_seconds":0,"last_trade_gap_seconds":0,"data_gap_exits":0,"eq_would_block":0,"eq_would_block_pullback":0,"eq_would_block_deceleration":0,"eq_would_block_pressure_fade":0,"profit_protect_exits":0,"profit_protect_shadow_done":0,"profit_protect_saved":0,"pp_v2_shadow_hits":0,"pp_v2_shadow_done":0,"pp_v2_delta":0,"process_cpu_pct":0,"event_loop_lag_ms":0,"shadow_paused":0,"shadow_checks_sec":0,"shadow_triggers_sec":0,"shadow_state_count":0,"shadow_auto_pauses":0,"perf_trade_ms":0,"perf_manage_ms":0,"perf_db_ms":0,"perf_signal_ms":0,"perf_trade_calls":0,"pp_v3_triggers":0,"pp_engine_enabled":0,"pp_engine_called":0,"pp_v4_armed":0,"pp_v4_triggered":0,"pp_v4_baseline_closed":0,"pp_v4_comparison_saved":0,"fast_shadow_started":0,"fast_shadow_done":0,"fast_shadow_delta":0,"fast_classifier_cases":0,"smart_ab_saved":0,"smart_ab_delta":0,"pp_real_exits":0,"pp_real_pp_6_7":0,"pp_real_pp_10_7":0,"single_ws_guard":0,"trade_stall_restarts":0,"trade_watchdog_state":"STARTING","trade_last_raw_ts":0,"trade_last_market_ts":0,"trade_max_stall_seconds":0,"entries_blocked_stall":0,"sim_trades":0,"sim_failed_tx":0,"sim_total_cost":0,"sim_realistic_pnl":0,"fast_signal_started":0,"fast_signal_completed":0,"fast_signal_base_delta":0,"fast_signal_ideal_delta":0,"fast_signal_big_worse":0,"fast_risk_started":0,"fast_risk_completed":0,"fast_risk_base_delta":0,"fast_risk_ideal_delta":0,"fast_risk_big_worse":0,"fast_reentry_started":0,"fast_reentry_completed":0,"fast_reentry_executed":0,"fast_reentry_signals":0,"fast_reentry_base_delta":0,"fast_reentry_ideal_delta":0,"fast_reentry_big_loss":0}
        cols={r[1] for r in c.execute("PRAGMA table_info(trades)")}
        for name,decl in (("stop_trigger_price","REAL DEFAULT 0"),("exit_latency_ms","REAL DEFAULT 0"),("slippage_gap_pct","REAL DEFAULT 0")):
            if name not in cols: c.execute(f"ALTER TABLE trades ADD COLUMN {name} {decl}")
        if "ab_variant" not in cols: c.execute("ALTER TABLE trades ADD COLUMN ab_variant TEXT DEFAULT 'BASELINE'")
        pcols={r[1] for r in c.execute("PRAGMA table_info(positions)")}
        if "ab_variant" not in pcols: c.execute("ALTER TABLE positions ADD COLUMN ab_variant TEXT DEFAULT 'BASELINE'")
        for k,v in defaults.items():
            c.execute("INSERT OR IGNORE INTO meta VALUES(?,?)",(k,str(v)))

def setm(k,v):
    with con() as c:c.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(k,str(v)))
def setmany(values):
    # One short transaction for related telemetry; never spans await or network IO.
    with atomic() as c:
        c.executemany("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      [(k,str(v)) for k,v in values.items()])

def getm(k,d="0"):
    with con() as c:
        r=c.execute("SELECT v FROM meta WHERE k=?",(k,)).fetchone()
        return r["v"] if r else d
def inc(k,n=1):
    # Only these three diagnostic totals may lag by the batch interval.
    # Cash, realized PNL, entries/exits and every other key keep prior semantics.
    import counter_writer
    if k in counter_writer.META_KEYS and counter_writer.ACTIVE is not None:
        counter_writer.ACTIVE.add(k,'',table='meta',amount=n)
        return
    # Startup/offline callers retain the synchronous path; no failed-writer fallback.
    with con() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=CAST(COALESCE(meta.v,'0') AS REAL)+excluded.v",(k,float(n)))

def hot_trade_tick(now, valid=None):
    setmany(dict(last_trade_ts=now,trade_stream_last_ts=now,trade_stream_state='LIVE'))

def event(mint,side,p,trader,source):
    if getattr(config,'RECORD_RAW_EVENTS',False):
        with con() as c:c.execute("INSERT INTO events(ts,mint,side,price,trader,price_source) VALUES(?,?,?,?,?,?)",(time.time(),mint,side,p,trader,source))
    inc("events")
def signal(mint,sc,dec,gate,t,b,r,mom,first,last,valid,source):
    with con() as c:c.execute("""INSERT INTO signals
      (ts,mint,score,decision,gate,trades,buyers,ratio,momentum,first_price,current_price,valid_prices,price_source)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (time.time(),mint,sc,dec,gate,t,b,r,mom,first,last,valid,source))

def entry_debug(mint,score,stage,reason,price,cash,open_positions,trades,buyers,ratio,momentum):
    with con() as c:
        c.execute("""INSERT INTO entry_debug
          (ts,mint,score,stage,reason,price,cash,open_positions,trades,buyers,ratio,momentum)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
          (time.time(),mint,score,stage,reason,price,cash,open_positions,trades,buyers,ratio,momentum))


def stream_debug(mint,stage,detail=""):
    with con() as c:
        c.execute("INSERT INTO stream_debug(ts,mint,stage,detail) VALUES(?,?,?,?)",(time.time(),mint or "",stage,str(detail)[:300]))


def reconnect_event(stream,error,open_positions=0):
    with con() as c:
        c.execute("INSERT INTO reconnect_events(ts,stream,error,open_positions) VALUES(?,?,?,?)",
                  (time.time(),stream,str(error)[:400],int(open_positions)))


def entry_feature(mint,score,trades,buyers,ratio,momentum,price,trade_id=None):
    with con() as c:
        c.execute("INSERT INTO entry_features(trade_id,ts,mint,score,trades,buyers,ratio,momentum,price) VALUES(?,?,?,?,?,?,?,?,?)",
                  (trade_id,time.time(),mint,int(score),int(trades),int(buyers),float(ratio),float(momentum),float(price)))


def discovery_event(mint, raw=""):
    with con() as c:
        c.execute("INSERT INTO discovery_events(ts,mint,raw) VALUES(?,?,?)",(time.time(),mint,str(raw)[:2000]))


def entry_quality(mint,decision,pullback,recent_slope,buy_pressure,momentum,score):
    with con() as c:
        c.execute("INSERT INTO entry_quality(ts,mint,decision,pullback,recent_slope,buy_pressure,momentum,score) VALUES(?,?,?,?,?,?,?,?)",
                  (time.time(),mint,decision,float(pullback),float(recent_slope),float(buy_pressure),float(momentum),int(score)))


def entry_trajectory_v064(mint,samples,price_accel,pressure_delta,pullback,volatility,last_return,buy_streak,buy_pressure_recent,buy_pressure_early,trade_id=None):
    with con() as c:
        c.execute("""INSERT OR REPLACE INTO entry_trajectory_v064(trade_id,ts,mint,samples,price_accel,pressure_delta,pullback,volatility,last_return,buy_streak,buy_pressure_recent,buy_pressure_early) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
          (trade_id,time.time(),mint,int(samples),float(price_accel),float(pressure_delta),float(pullback),float(volatility),float(last_return),int(buy_streak),float(buy_pressure_recent),float(buy_pressure_early)))


def entry_model_snapshot_v067(mint,samples,ret_mean,ret_vol,accel,last1,last3,pullback,bp5,bp10,bp20,pressure_trend,buy_streak,entry_score,entry_trades,buyers,ratio,momentum,trade_id=None):
    with con() as c:
        c.execute("""INSERT OR REPLACE INTO entry_model_snapshot_v067(trade_id,ts,mint,samples,ret_mean,ret_vol,accel,last1,last3,pullback,bp5,bp10,bp20,pressure_trend,buy_streak,entry_score,entry_trades,buyers,ratio,momentum) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (trade_id,time.time(),mint,samples,ret_mean,ret_vol,accel,last1,last3,pullback,bp5,bp10,bp20,pressure_trend,buy_streak,entry_score,entry_trades,buyers,ratio,momentum))


def model_gate_v069(mint,model_p,threshold,decision,train_n,price,score):
    with con() as c:
        c.execute("INSERT INTO model_gate_v069(ts,mint,model_p,threshold,decision,train_n,price,score) VALUES(?,?,?,?,?,?,?,?)",(time.time(),mint,model_p,threshold,decision,train_n,price,score))
    inc("model_gate_checked")
    inc("model_gate_allowed" if decision in ("ALLOW","WARMUP") else "model_gate_blocked")
