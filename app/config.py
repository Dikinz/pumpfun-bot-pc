import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().with_name(".env"))

VERSION="v0.7.101.8"
EXECUTION_MODE=os.getenv("EXECUTION_MODE","paper").strip().lower()
if EXECUTION_MODE!="paper":raise RuntimeError("Main strategy live adapter is not validated; EXECUTION_MODE must remain paper.")
DB=os.getenv("DATABASE_PATH","data/clean_0762.db")
PUMPPORTAL_API_KEY=os.getenv("PUMPPORTAL_API_KEY","").strip()
BITQUERY_TOKEN=os.getenv("BITQUERY_TOKEN","").strip()
DATA_PROVIDER=os.getenv("TRADE_DATA_PROVIDER","pumpapi").strip().lower()  # pumpapi|free_api|cost_safe|auto|bitquery|pumpportal
PORT=int(os.getenv("DASHBOARD_PORT","8188"))
START_BALANCE=float(os.getenv("START_BALANCE","30"))
POSITION_SIZE=float(os.getenv("POSITION_SIZE","3"))
MAX_POSITIONS=int(os.getenv("MAX_POSITIONS","2"))
MIN_TRADES=int(os.getenv("MIN_TRADES_FOR_ENTRY","4"))
MIN_BUYERS=int(os.getenv("MIN_UNIQUE_BUYERS","3"))
MIN_RATIO=float(os.getenv("MIN_BUY_SELL_RATIO","1.35"))
MIN_MOMENTUM=float(os.getenv("MIN_MOMENTUM_PCT","2.0"))
MIN_SCORE=int(os.getenv("MIN_SCORE","70"))
MAX_ENTRY_MOMENTUM=float(os.getenv("MAX_ENTRY_MOMENTUM_PCT","18.0"))
MAX_ENTRY_LAST_JUMP=float(os.getenv("MAX_ENTRY_LAST_JUMP_PCT","10.0"))
MIN_ENTRY_AGE=float(os.getenv("MIN_ENTRY_AGE_SECONDS","1.5"))
STOP_LOSS=float(os.getenv("STOP_LOSS_PCT","-0.12"))
TP1=float(os.getenv("TP1_PCT","0.15"))
TP2=float(os.getenv("TP2_PCT","0.30"))
TRAIL=float(os.getenv("TRAILING_STOP_PCT","0.08"))
MAX_HOLD=float(os.getenv("MAX_HOLD_SECONDS","120"))
TTL=float(os.getenv("CANDIDATE_TTL_SECONDS","180"))
ALLOW_REENTRY=os.getenv("ALLOW_REENTRY","false").lower()=="true"
MAX_ACTIVE_SUBSCRIPTIONS=int(os.getenv("MAX_ACTIVE_SUBSCRIPTIONS","24"))
STREAM_STALE_SECONDS=float(os.getenv("STREAM_STALE_SECONDS","20"))
PASS_PIN_SECONDS=float(os.getenv("PASS_PIN_SECONDS","90"))
Path(DB).parent.mkdir(parents=True, exist_ok=True)

STALE_POSITION_SECONDS=float(os.getenv("STALE_POSITION_SECONDS",str(MAX_HOLD)))

# v0.4.8.2 memory guardrails
MAX_CANDIDATES=int(os.getenv("MAX_CANDIDATES","750"))
MAX_BUYERS_PER_MINT=int(os.getenv("MAX_BUYERS_PER_MINT","64"))
WS_MAX_QUEUE=int(os.getenv("WS_MAX_QUEUE","256"))
SUB_QUEUE_MAX=int(os.getenv("SUB_QUEUE_MAX","512"))
MEMORY_SOFT_LIMIT_MB=float(os.getenv("MEMORY_SOFT_LIMIT_MB","350"))
MEMORY_HARD_LIMIT_MB=float(os.getenv("MEMORY_HARD_LIMIT_MB","600"))
HISTORY_SET_MAX=int(os.getenv("HISTORY_SET_MAX","10000"))

# v0.4.8.6 early failure detector
PROBATION_MIN_SECONDS=float(os.getenv("PROBATION_MIN_SECONDS","8"))
PROBATION_MAX_SECONDS=float(os.getenv("PROBATION_MAX_SECONDS","20"))
EARLY_FAIL_MAX_MFE=float(os.getenv("EARLY_FAIL_MAX_MFE_PCT","2.5"))/100.0
EARLY_FAIL_RETURN=float(os.getenv("EARLY_FAIL_RETURN_PCT","-3.0"))/100.0
EARLY_FAIL_HARD_RETURN=float(os.getenv("EARLY_FAIL_HARD_RETURN_PCT","-6.0"))/100.0
EARLY_FAIL_HARD_MAX_MFE=float(os.getenv("EARLY_FAIL_HARD_MAX_MFE_PCT","1.5"))/100.0
EARLY_FAIL_MIN_POST_TRADES=int(os.getenv("EARLY_FAIL_MIN_POST_TRADES","3"))
EARLY_FAIL_MAX_POST_RATIO=float(os.getenv("EARLY_FAIL_MAX_POST_BUY_SELL_RATIO","1.0"))
RECONNECT_BACKOFF_MAX=float(os.getenv("RECONNECT_BACKOFF_MAX_SECONDS","30"))
RECONNECT_JITTER_MAX=float(os.getenv("RECONNECT_JITTER_MAX_SECONDS","0.75"))

# v0.4.8.8 fast loss protection + stream hardening
FAST_PROTECT_SECONDS=float(os.getenv("FAST_PROTECT_SECONDS","12"))
FAST_STOP=float(os.getenv("FAST_STOP_PCT","-8.0"))/100.0
FAST_STOP_MIN_EVENTS=int(os.getenv("FAST_STOP_MIN_EVENTS","1"))

# v0.4.8.8 cost-safe controls
PUMPPORTAL_FAIL_FAST=os.getenv("PUMPPORTAL_FAIL_FAST","true").lower()=="true"
PUMPPORTAL_BLOCK_COOLDOWN=float(os.getenv("PUMPPORTAL_BLOCK_COOLDOWN_SECONDS","3600"))
RECORD_DISCOVERY=os.getenv("RECORD_DISCOVERY","true").lower()=="true"

# v0.4.8.8.1 free HTTP trade feed (no funded PumpPortal subscription)
FREE_API_BASE=os.getenv("FREE_API_BASE","https://frontend-api.pump.fun").rstrip("/")
FREE_API_POLL_SECONDS=float(os.getenv("FREE_API_POLL_SECONDS","1.25"))
FREE_API_MAX_MINTS=int(os.getenv("FREE_API_MAX_MINTS","12"))
FREE_API_LIMIT=int(os.getenv("FREE_API_LIMIT","20"))
FREE_API_TIMEOUT=float(os.getenv("FREE_API_TIMEOUT_SECONDS","6"))

# v0.4.8.8.2 free PumpApi websocket feed
PUMPAPI_STREAM_URL=os.getenv("PUMPAPI_STREAM_URL","wss://stream.pumpapi.io/").strip()
PUMPAPI_POOL=os.getenv("PUMPAPI_POOL","pump").strip().lower()

# v0.4.8.8.3 stream stability
PUMPAPI_PING_INTERVAL=float(os.getenv("PUMPAPI_PING_INTERVAL_SECONDS","0"))
PUMPAPI_OPEN_TIMEOUT=float(os.getenv("PUMPAPI_OPEN_TIMEOUT_SECONDS","12"))
PUMPAPI_CLOSE_TIMEOUT=float(os.getenv("PUMPAPI_CLOSE_TIMEOUT_SECONDS","2"))
PUMPAPI_MIN_RECONNECT_DELAY=float(os.getenv("PUMPAPI_MIN_RECONNECT_DELAY_SECONDS","2"))

# v0.4.8.8.4 stream resilience / gap-aware analytics
DATA_GAP_MARK_SECONDS=float(os.getenv("DATA_GAP_MARK_SECONDS","5"))
DATA_GAP_EXIT_SECONDS=float(os.getenv("DATA_GAP_EXIT_SECONDS","12"))


# v0.4.8.8.5 ENTRY QUALITY V2
ENTRY_QUALITY_V2=os.getenv("ENTRY_QUALITY_V2","true").lower()=="true"
EQ_MAX_PULLBACK_PCT=float(os.getenv("EQ_MAX_PULLBACK_PCT","5.0"))
EQ_MIN_RECENT_SLOPE_PCT=float(os.getenv("EQ_MIN_RECENT_SLOPE_PCT","-1.5"))
EQ_MIN_BUY_PRESSURE_TREND=float(os.getenv("EQ_MIN_BUY_PRESSURE_TREND","0.55"))
EQ_MIN_SAMPLES=int(os.getenv("EQ_MIN_SAMPLES","4"))

# v0.4.8.8.7 PROFIT PROTECTION V2 — SHADOW FIRST
# Real exits stay identical to v0.4.8.8.5 baseline. These variants are diagnostic only.
PROFIT_PROTECTION_V1=False
PP_SHADOW_V2=False  # v0.5 frozen strategy: legacy PP shadow disabled
PP_SHADOW_MIN_LOCK=float(os.getenv("PP_SHADOW_MIN_LOCK_PCT","1.0"))/100.0
PP_SHADOW_VARIANTS=(("PP_6_7",0.06,0.07),("PP_10_7",0.10,0.07),("PP_15_8",0.15,0.08),("PP_20_10",0.20,0.10))
PP_ACTIVATE_MFE=float(os.getenv("PP_ACTIVATE_MFE_PCT","6.0"))/100.0
PP_MAX_GIVEBACK=float(os.getenv("PP_MAX_GIVEBACK_PCT","7.0"))/100.0
PP_MIN_LOCK=float(os.getenv("PP_MIN_LOCK_PCT","1.0"))/100.0
PP_MIN_AGE=float(os.getenv("PP_MIN_AGE_SECONDS","8"))
PP_SHADOW_MAX_SECONDS=float(os.getenv("PP_SHADOW_MAX_SECONDS","180"))

# v0.4.8.8.7.1 CPU HOTFIX
SHADOW_CHECK_INTERVAL=float(os.getenv("SHADOW_CHECK_INTERVAL_SECONDS","0.25"))
CLEANUP_INTERVAL=float(os.getenv("CLEANUP_INTERVAL_SECONDS","0.50"))
CPU_SHADOW_PAUSE_PCT=float(os.getenv("CPU_SHADOW_PAUSE_PCT","85"))
CPU_SHADOW_RESUME_PCT=float(os.getenv("CPU_SHADOW_RESUME_PCT","65"))
EVENT_LOOP_LAG_PAUSE_MS=float(os.getenv("EVENT_LOOP_LAG_PAUSE_MS","250"))

# v0.4.8.8.7.2 PERFORMANCE FIX
RECORD_RAW_EVENTS=os.getenv("RECORD_RAW_EVENTS","false").lower()=="true"
PERF_SAMPLE_EVERY=int(os.getenv("PERF_SAMPLE_EVERY","100"))

# v0.4.8.8.7.3 ASYNC SHADOW ANALYTICS
FAST_SHADOW_V3=False  # v0.7.7 multi-horizon lab retired after analysis
FAST_SHADOW_HORIZONS=(1,2,3,5,10,20,30)
FAST_SHADOW_MAX_SECONDS=30.0
SHADOW_FLUSH_INTERVAL=1.0

# v0.7.8 FAST_STOP 30S INDEPENDENT VALIDATION — SHADOW ONLY.
# Predeclared before the run: wait exactly 30s after each real FAST_STOP in shadow,
# with no threshold tuning. The real PAPER exit remains unchanged.
FAST_RECOVERY30_ENABLED=False
FAST_RECOVERY30_SECONDS=30.0
FAST_RECOVERY30_TARGET_SAMPLES=300
FAST_RECOVERY30_MIN_BASE_DELTA_PER_FAST=0.05
FAST_RECOVERY30_FRESH_SECONDS=5.0

# v0.7.9 FAST RECOVERY SIGNAL LAB — SHADOW ONLY.
# Real FAST_STOP still exits immediately. The counterfactual recovery policy below
# uses only information available after the trigger: fresh trade ticks, price recovery,
# recent momentum, buy pressure, a hard-abort guard and a trailing lock.
FAST_RECOVERY_SIGNAL_ENABLED=os.getenv("FAST_RECOVERY_SIGNAL_ENABLED","true").lower()=="true"
FAST_RECOVERY_SIGNAL_MAX_SECONDS=float(os.getenv("FAST_RECOVERY_SIGNAL_MAX_SECONDS","30"))
FAST_RECOVERY_SIGNAL_TARGET_SAMPLES=int(os.getenv("FAST_RECOVERY_SIGNAL_TARGET_SAMPLES","300"))
FAST_RECOVERY_SIGNAL_MIN_AGE=float(os.getenv("FAST_RECOVERY_SIGNAL_MIN_AGE","1.0"))
FAST_RECOVERY_SIGNAL_HARD_ABORT_PCT=float(os.getenv("FAST_RECOVERY_SIGNAL_HARD_ABORT_PCT","-6.0"))/100.0
FAST_RECOVERY_SIGNAL_ARM_PCT=float(os.getenv("FAST_RECOVERY_SIGNAL_ARM_PCT","5.0"))/100.0
FAST_RECOVERY_SIGNAL_ARM_MOMENTUM_PCT=float(os.getenv("FAST_RECOVERY_SIGNAL_ARM_MOMENTUM_PCT","0.5"))/100.0
FAST_RECOVERY_SIGNAL_MIN_BUY_PRESSURE=float(os.getenv("FAST_RECOVERY_SIGNAL_MIN_BUY_PRESSURE","0.55"))
FAST_RECOVERY_SIGNAL_TRAIL_PCT=float(os.getenv("FAST_RECOVERY_SIGNAL_TRAIL_PCT","3.0"))/100.0
FAST_RECOVERY_SIGNAL_LOCK_PCT=float(os.getenv("FAST_RECOVERY_SIGNAL_LOCK_PCT","1.0"))/100.0
FAST_RECOVERY_SIGNAL_MOMENTUM_FADE_PCT=float(os.getenv("FAST_RECOVERY_SIGNAL_MOMENTUM_FADE_PCT","-1.0"))/100.0
FAST_RECOVERY_SIGNAL_PRESSURE_FADE=float(os.getenv("FAST_RECOVERY_SIGNAL_PRESSURE_FADE","0.40"))
FAST_RECOVERY_SIGNAL_NO_RECOVERY_SECONDS=float(os.getenv("FAST_RECOVERY_SIGNAL_NO_RECOVERY_SECONDS","8.0"))
FAST_RECOVERY_SIGNAL_NO_RECOVERY_PEAK_PCT=float(os.getenv("FAST_RECOVERY_SIGNAL_NO_RECOVERY_PEAK_PCT","3.0"))/100.0
FAST_RECOVERY_SIGNAL_FRESH_SECONDS=float(os.getenv("FAST_RECOVERY_SIGNAL_FRESH_SECONDS","5.0"))
FAST_RECOVERY_SIGNAL_MIN_BASE_DELTA_PER_FAST=float(os.getenv("FAST_RECOVERY_SIGNAL_MIN_BASE_DELTA_PER_FAST","0.05"))
FAST_RECOVERY_SIGNAL_BIG_WORSE_USD=float(os.getenv("FAST_RECOVERY_SIGNAL_BIG_WORSE_USD","0.30"))
FAST_RECOVERY_SIGNAL_MAX_BIG_WORSE_RATE=float(os.getenv("FAST_RECOVERY_SIGNAL_MAX_BIG_WORSE_RATE","0.10"))

# v0.7.10 RECOVERY RISK CONTROL LAB — SHADOW ONLY.
# v0.7.9 stays enabled as a frozen control on the same FAST_STOP cases.
FAST_RECOVERY_RISK_ENABLED=os.getenv("FAST_RECOVERY_RISK_ENABLED","false").lower()=="true"
FAST_RECOVERY_RISK_TARGET_SAMPLES=int(os.getenv("FAST_RECOVERY_RISK_TARGET_SAMPLES","300"))
FAST_RECOVERY_RISK_EARLY_MIN_AGE=float(os.getenv("FAST_RECOVERY_RISK_EARLY_MIN_AGE","1.5"))
FAST_RECOVERY_RISK_EARLY_MIN_EVENTS=int(os.getenv("FAST_RECOVERY_RISK_EARLY_MIN_EVENTS","3"))
FAST_RECOVERY_RISK_COLLAPSE_PCT=float(os.getenv("FAST_RECOVERY_RISK_COLLAPSE_PCT","-3.5"))/100.0
FAST_RECOVERY_RISK_COLLAPSE_MOM_PCT=float(os.getenv("FAST_RECOVERY_RISK_COLLAPSE_MOM_PCT","-2.0"))/100.0
FAST_RECOVERY_RISK_COLLAPSE_MAX_BUY_PRESSURE=float(os.getenv("FAST_RECOVERY_RISK_COLLAPSE_MAX_BUY_PRESSURE","0.40"))
FAST_RECOVERY_RISK_NO_BOUNCE_AGE=float(os.getenv("FAST_RECOVERY_RISK_NO_BOUNCE_AGE","3.0"))
FAST_RECOVERY_RISK_NO_BOUNCE_MIN_EVENTS=int(os.getenv("FAST_RECOVERY_RISK_NO_BOUNCE_MIN_EVENTS","4"))
FAST_RECOVERY_RISK_NO_BOUNCE_PEAK_PCT=float(os.getenv("FAST_RECOVERY_RISK_NO_BOUNCE_PEAK_PCT","1.5"))/100.0
FAST_RECOVERY_RISK_NO_BOUNCE_LOSS_PCT=float(os.getenv("FAST_RECOVERY_RISK_NO_BOUNCE_LOSS_PCT","-2.0"))/100.0
FAST_RECOVERY_RISK_NO_BOUNCE_MOM_PCT=float(os.getenv("FAST_RECOVERY_RISK_NO_BOUNCE_MOM_PCT","-1.0"))/100.0
FAST_RECOVERY_RISK_NO_BOUNCE_MAX_BUY_PRESSURE=float(os.getenv("FAST_RECOVERY_RISK_NO_BOUNCE_MAX_BUY_PRESSURE","0.40"))
FAST_RECOVERY_RISK_MIN_BASE_DELTA_PER_FAST=float(os.getenv("FAST_RECOVERY_RISK_MIN_BASE_DELTA_PER_FAST","0.05"))
FAST_RECOVERY_RISK_MAX_BIG_WORSE_RATE=float(os.getenv("FAST_RECOVERY_RISK_MAX_BIG_WORSE_RATE","0.10"))


# v0.7.11 FAST EXIT + RECOVERY RE-ENTRY LAB — SHADOW ONLY.
# Real PAPER still exits FAST_STOP immediately. A fresh post-FAST confirmation may open
# one independent counterfactual re-entry, with its own round-trip execution costs.
FAST_REENTRY_ENABLED=os.getenv("FAST_REENTRY_ENABLED","true").lower()=="true"
FAST_REENTRY_TARGET_FAST=int(os.getenv("FAST_REENTRY_TARGET_FAST","300"))
FAST_REENTRY_MIN_REENTRIES=int(os.getenv("FAST_REENTRY_MIN_REENTRIES","80"))
FAST_REENTRY_WATCH_SECONDS=float(os.getenv("FAST_REENTRY_WATCH_SECONDS","30"))
FAST_REENTRY_MIN_AGE=float(os.getenv("FAST_REENTRY_MIN_AGE","1.0"))
FAST_REENTRY_MIN_EVENTS=int(os.getenv("FAST_REENTRY_MIN_EVENTS","3"))
FAST_REENTRY_TRIGGER_RECOVERY_PCT=float(os.getenv("FAST_REENTRY_TRIGGER_RECOVERY_PCT","5.0"))/100.0
FAST_REENTRY_TRIGGER_MOMENTUM_PCT=float(os.getenv("FAST_REENTRY_TRIGGER_MOMENTUM_PCT","0.5"))/100.0
FAST_REENTRY_MIN_BUY_PRESSURE=float(os.getenv("FAST_REENTRY_MIN_BUY_PRESSURE","0.55"))
FAST_REENTRY_MAX_HOLD=float(os.getenv("FAST_REENTRY_MAX_HOLD_SECONDS","20"))
FAST_REENTRY_HARD_STOP_PCT=float(os.getenv("FAST_REENTRY_HARD_STOP_PCT","-5.0"))/100.0
FAST_REENTRY_TRAIL_ARM_PCT=float(os.getenv("FAST_REENTRY_TRAIL_ARM_PCT","5.0"))/100.0
FAST_REENTRY_TRAIL_PCT=float(os.getenv("FAST_REENTRY_TRAIL_PCT","3.0"))/100.0
FAST_REENTRY_LOCK_PCT=float(os.getenv("FAST_REENTRY_LOCK_PCT","1.0"))/100.0
FAST_REENTRY_FADE_MIN_AGE=float(os.getenv("FAST_REENTRY_FADE_MIN_AGE","2.0"))
FAST_REENTRY_FADE_MOMENTUM_PCT=float(os.getenv("FAST_REENTRY_FADE_MOMENTUM_PCT","-1.0"))/100.0
FAST_REENTRY_FADE_MAX_BUY_PRESSURE=float(os.getenv("FAST_REENTRY_FADE_MAX_BUY_PRESSURE","0.40"))
FAST_REENTRY_FRESH_SECONDS=float(os.getenv("FAST_REENTRY_FRESH_SECONDS","5.0"))
FAST_REENTRY_BIG_LOSS_USD=float(os.getenv("FAST_REENTRY_BIG_LOSS_USD","0.30"))
FAST_REENTRY_MIN_BASE_LIFT_PER_FAST=float(os.getenv("FAST_REENTRY_MIN_BASE_LIFT_PER_FAST","0.05"))
FAST_REENTRY_MAX_BIG_LOSS_RATE=float(os.getenv("FAST_REENTRY_MAX_BIG_LOSS_RATE","0.10"))

# v0.4.8.8.7.4 DUAL SHADOW LAB
MFE_REVERSAL_LEVELS=(0.05,0.08,0.10,0.15,0.20)

# v0.4.9 FAST_STOP CLASSIFIER / RECOVERY SHADOW
FAST_CLASSIFIER_V49=False  # legacy diagnostic disabled
FAST_CLASSIFIER_HORIZONS=(5,10,20,30)

# v0.4.9.1 SMART EXIT SHADOW A/B
SMART_EXIT_AB_V491=False  # legacy diagnostic disabled
SMART_EXIT_HORIZONS=(10,20,30)
SMART_EXIT_PRIMARY_HORIZON=20
SMART_EXIT_PP_VARIANT="PP_6_7"

# v0.4.9.3 TRANSPORT HARDENING + PP_10_7 CONFIRMATION
PP_REAL_AB_V492=False
PP_REAL_VARIANTS=("PP_10_7",)
# v0.5 frozen strategy: every new position uses PP_10_7.
STRATEGY_FINALIZED=True
FINAL_PP_VARIANT="PP_10_7"
# Fixed guard port is intentionally version-independent for all future builds.
PUMPAPI_SINGLETON_PORT=int(os.getenv("PUMPAPI_SINGLETON_PORT","49190"))
PUMPAPI_SERVER_SINGLE_WS_COOLDOWN=float(os.getenv("PUMPAPI_SERVER_SINGLE_WS_COOLDOWN_SECONDS","240"))
PUMPAPI_CLOSE_GRACE=float(os.getenv("PUMPAPI_CLOSE_GRACE_SECONDS","15"))

# v0.4.9.4 TRADE STREAM WATCHDOG
PUMPAPI_STALL_SECONDS=float(os.getenv("PUMPAPI_STALL_SECONDS","20"))  # market-stale threshold: block new entries, do not reconnect yet
PUMPAPI_HARD_STALL_SECONDS=float(os.getenv("PUMPAPI_HARD_STALL_SECONDS","60"))  # only then rebuild socket
PUMPAPI_WATCHDOG_TICK_SECONDS=float(os.getenv("PUMPAPI_WATCHDOG_TICK_SECONDS","5"))
PUMPAPI_WATCHDOG_RECONNECT_DELAY=float(os.getenv("PUMPAPI_WATCHDOG_RECONNECT_DELAY_SECONDS","8"))
BLOCK_ENTRIES_WHEN_TRADE_STALLED=os.getenv("BLOCK_ENTRIES_WHEN_TRADE_STALLED","true").lower()=="true"

# v0.6 REALISTIC EXECUTION SIMULATOR — strategy remains frozen PP_10_7.
EXECUTION_SIM_ENABLED=os.getenv("EXECUTION_SIM_ENABLED","true").lower()=="true"
SIM_BUY_SLIPPAGE_PCT=float(os.getenv("SIM_BUY_SLIPPAGE_PCT","1.0"))/100.0
SIM_SELL_SLIPPAGE_PCT=float(os.getenv("SIM_SELL_SLIPPAGE_PCT","1.5"))/100.0
SIM_TRADE_FEE_PCT=float(os.getenv("SIM_TRADE_FEE_PCT","1.0"))/100.0
SIM_PRIORITY_FEE_USD=float(os.getenv("SIM_PRIORITY_FEE_USD","0.005"))
SIM_BASE_LATENCY_MS=float(os.getenv("SIM_BASE_LATENCY_MS","450"))
SIM_LATENCY_JITTER_MS=float(os.getenv("SIM_LATENCY_JITTER_MS","550"))
SIM_FAILED_TX_PROB=float(os.getenv("SIM_FAILED_TX_PROB","0.02"))
SIM_FAILED_EXIT_EXTRA_SLIPPAGE_PCT=float(os.getenv("SIM_FAILED_EXIT_EXTRA_SLIPPAGE_PCT","3.0"))/100.0

# v0.6.1 EXECUTION COST AUDIT — three deterministic audit profiles; strategy unchanged.
EXEC_AUDIT_ENABLED=os.getenv("EXEC_AUDIT_ENABLED","true").lower()=="true"
EXEC_AUDIT_PROFILES={
 "LOW": {"buy_slip":0.003,"sell_slip":0.005,"fee":0.003,"priority":0.002,"latency_base":200,"latency_jitter":250,"failed_prob":0.005,"failed_extra":0.010,"latency_slip_per_s":0.002},
 "BASE":{"buy_slip":0.010,"sell_slip":0.015,"fee":0.010,"priority":0.005,"latency_base":450,"latency_jitter":550,"failed_prob":0.020,"failed_extra":0.030,"latency_slip_per_s":0.004},
 "STRESS":{"buy_slip":0.020,"sell_slip":0.030,"fee":0.015,"priority":0.010,"latency_base":800,"latency_jitter":1200,"failed_prob":0.050,"failed_extra":0.060,"latency_slip_per_s":0.008},
}

# v0.6.3 EXECUTION EDGE LAB — diagnostic shadow only; real paper strategy remains frozen PP_10_7.
EDGE_LAB_ENABLED=os.getenv("EDGE_LAB_ENABLED","true").lower()=="true"
EDGE_LAB_VARIANTS={
 "ALL": {},
 "ACTIVITY_8_5": {"min_trades":8,"min_buyers":5},
 "PRESSURE_2X": {"min_ratio":2.0},
 "QUALITY_COMBO": {"min_trades":8,"min_buyers":5,"min_ratio":2.0,"min_momentum":4.0},
}

# v0.6.4 ENTRY TRAJECTORY LAB — SHADOW ONLY. Frozen PP_10_7 paper strategy is unchanged.
TRAJECTORY_LAB_ENABLED=os.getenv("TRAJECTORY_LAB_ENABLED","true").lower()=="true"
TRAJECTORY_VARIANTS={
 "ALL": {},
 "ACCEL_POS": {"min_accel":0.0},
 "PRESSURE_RISING": {"min_pressure_delta":0.0},
 "LOW_PULLBACK": {"min_pullback":-2.0},
 "LOW_VOL": {"max_volatility":8.0},
 "TRAJECTORY_COMBO": {"min_accel":0.0,"min_pressure_delta":0.0,"min_pullback":-2.0,"max_volatility":8.0},
}

# v0.6.5 MULTIVARIATE TRAJECTORY CLASSIFIER LAB — SHADOW ONLY.
# Fixed diagnostic score uses only features known at entry; it never changes paper entries/exits.
CLASSIFIER_LAB_ENABLED=os.getenv("CLASSIFIER_LAB_ENABLED","true").lower()=="true"
CLASSIFIER_THRESHOLDS=(60,65,70,75,80)
SWEET_SPOT_LAB_ENABLED=os.getenv("SWEET_SPOT_LAB_ENABLED","true").lower()=="true"


# v0.6.7 OFFLINE ENTRY MODEL LAB — SHADOW ONLY.
# The model is trained only on chronologically earlier closed paper trades and evaluated on later trades.
OFFLINE_MODEL_LAB_ENABLED=os.getenv("OFFLINE_MODEL_LAB_ENABLED","true").lower()=="true"
OFFLINE_MODEL_TRAIN_FRACTION=float(os.getenv("OFFLINE_MODEL_TRAIN_FRACTION","0.70"))
OFFLINE_MODEL_MIN_TRAIN=int(os.getenv("OFFLINE_MODEL_MIN_TRAIN","150"))
OFFLINE_MODEL_THRESHOLDS=(0.45,0.50,0.55,0.60,0.65,0.70)
OFFLINE_MODEL_WINDOW=20


# v0.6.8 WALK-FORWARD VALIDATION — SHADOW ONLY.
# Frozen v0.6.7 feature set/model family; every fold trains strictly on past closes and tests the next unseen block.
WALK_FORWARD_ENABLED=os.getenv("WALK_FORWARD_ENABLED","true").lower()=="true"
WALK_FORWARD_MIN_TRAIN=int(os.getenv("WALK_FORWARD_MIN_TRAIN","300"))
WALK_FORWARD_TEST_SIZE=int(os.getenv("WALK_FORWARD_TEST_SIZE","100"))
WALK_FORWARD_STEP=int(os.getenv("WALK_FORWARD_STEP","100"))
WALK_FORWARD_THRESHOLDS=(0.55,0.60)
WALK_FORWARD_MIN_TEST_REMAINDER=int(os.getenv("WALK_FORWARD_MIN_TEST_REMAINDER","50"))


# v0.6.9 MODEL-GATED PAPER A/B — PAPER ONLY.
# Gate frozen before this run from v0.6.8 walk-forward result. PP_10_7 exits remain unchanged.
MODEL_GATE_ENABLED=os.getenv("MODEL_GATE_ENABLED","false").lower()=="true"
MODEL_GATE_THRESHOLD=float(os.getenv("MODEL_GATE_THRESHOLD","0.55"))
MODEL_GATE_MIN_TRAIN=int(os.getenv("MODEL_GATE_MIN_TRAIN","300"))
MODEL_GATE_RETRAIN_EVERY=int(os.getenv("MODEL_GATE_RETRAIN_EVERY","25"))

TRUE_SHADOW_AB_ENABLED=os.getenv("TRUE_SHADOW_AB_ENABLED","true").lower()=="true"
MODEL_GATE_FREEZE_AFTER_WARMUP=os.getenv("MODEL_GATE_FREEZE_AFTER_WARMUP","true").lower()=="true"

# v0.7.3 FAST-vs-WINNER FEATURE LAB — PAPER ONLY.
# Broad baseline entries: no research feature is allowed to block real PAPER entries.
STABLE_VOL_GATE_ENABLED=False
STABLE_VOL_MAX_RET_VOL=3.5
STABLE_VOL_TARGET_BE_PER_TRADE=0.20
# SWEET gate is disabled in this build.
SWEET70_GATE_ENABLED=False
SWEET70_GATE_THRESHOLD=65
SWEET70_TARGET_BE_PER_TRADE=0.20
COST_EDGE_LAB_ENABLED=False
COST_EDGE_MODEL_THRESHOLDS=()
COST_EDGE_SWEET_THRESHOLDS=(65,)
COST_EDGE_TARGET_BE_PER_TRADE=0.20

# v0.7.4 MICRO-CONFIRMATION ENTRY LAB — PAPER/SHADOW ONLY.
MICRO_CONFIRM_LAB_ENABLED=False
MICRO_CONFIRM_DELAYS=(1.0,2.0,3.0)
MICRO_CONFIRM_MIN_RETURN_PCT=float(os.getenv("MICRO_CONFIRM_MIN_RETURN_PCT","0.0"))
MICRO_CONFIRM_MIN_BUY_PRESSURE=float(os.getenv("MICRO_CONFIRM_MIN_BUY_PRESSURE","0.55"))
DISCOVERY_STALE_SECONDS=float(os.getenv("DISCOVERY_STALE_SECONDS","180"))

# v0.7.5 LAST1 >= 2% INDEPENDENT VALIDATION — PAPER ONLY.
# Frozen prospectively from the v0.7.4 same-run research signal; no threshold tuning in this run.
LAST1_GATE_ENABLED=False
LAST1_GATE_MIN_RETURN_PCT=2.0
LAST1_GATE_TARGET_BE_PER_TRADE=0.20

# v0.7.6 PRESSURE_RISING INDEPENDENT VALIDATION — PAPER ONLY.
# Frozen prospectively from the v0.7.5 same-run trajectory cohort.
# Exact feature semantics: pressure_delta = recent-half buy fraction - early-half buy fraction.
# ALLOW when pressure_delta >= 0.0; BLOCK when pressure_delta < 0.0. No threshold tuning in this run.
PRESSURE_GATE_ENABLED=False
PRESSURE_GATE_MIN_DELTA=0.0
PRESSURE_GATE_TARGET_BE_PER_TRADE=0.20

# Production default; tests may isolate the underlying accounting without the order model.
EXECUTION_MODEL_ENABLED=True

# Quote-only observer. Never accepts a wallet or signing key.
JUPITER_API_KEY=os.getenv("JUPITER_API_KEY","").strip()
QUOTE_RPC_URL=os.getenv("QUOTE_RPC_URL","https://api.mainnet-beta.solana.com").strip()
QUOTE_MAX_LOSS_BPS=int(os.getenv("QUOTE_MAX_LOSS_BPS","1000"))
QUOTE_SLIPPAGE_BPS=int(os.getenv("QUOTE_SLIPPAGE_BPS","100"))
if not 0<=QUOTE_MAX_LOSS_BPS<10000 or not 0<=QUOTE_SLIPPAGE_BPS<=1000:
    raise ValueError("Invalid quote policy: max loss 0..9999 bps; slippage 0..1000 bps")
if not QUOTE_RPC_URL.startswith("https://"):
    raise ValueError("QUOTE_RPC_URL must use HTTPS")

# Optional PUBLIC address for read-only RPC simulation; no private key.
SIMULATION_PUBLIC_KEY=os.getenv("SIMULATION_PUBLIC_KEY","").strip()
if SIMULATION_PUBLIC_KEY:
    from solders.pubkey import Pubkey
    Pubkey.from_string(SIMULATION_PUBLIC_KEY)

# Mandatory pre-entry local roundtrip gate in v0.7.60.
PREFLIGHT_ENABLED=True

# Execution infrastructure only; no strategy or clock threshold.
PROGRAM_CACHE_ENABLED=os.getenv("PROGRAM_CACHE_ENABLED","1")=="1"

# v0.7.101.1: common admission for PAPER RPC readers; not a trading threshold.
RPC_CONTROL_DIR=Path(os.getenv('RPC_CONTROL_DIR',str(Path(__file__).resolve().parent/'data/rpc_control_v89')))
RPC_MAX_REQUESTS_PER_SECOND=float(os.getenv('RPC_MAX_REQUESTS_PER_SECOND','6'))
RPC_MAX_INFLIGHT=int(os.getenv('RPC_MAX_INFLIGHT','4'))
RPC_ADMISSION_WAIT_SECONDS=float(os.getenv('RPC_ADMISSION_WAIT_SECONDS','0.75'))

# v99: direct HTTPS keep-alive preserves common RPC admission and 6s deadlines.
RPC_KEEPALIVE=os.getenv("RPC_KEEPALIVE","1")=="1"
# Optional endpoint identity cache. OFF preserves live genesis verification.
GENESIS_CACHE_TTL_S=float(os.getenv("GENESIS_CACHE_TTL_S","0"))
if not 0<=GENESIS_CACHE_TTL_S<=21600:raise ValueError("GENESIS_CACHE_TTL_S must be 0..21600")

# Optional independent AMM PAPER process; targeted AMM check is separate.
AMM_BRANCH_ENABLED=os.getenv("AMM_BRANCH_ENABLED","0")=="1"
