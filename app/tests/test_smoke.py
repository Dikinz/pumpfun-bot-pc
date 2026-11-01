import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pathlib
def test_config():
 import config
 assert config.VERSION=="v0.7.101.2" and config.PORT==8188 and config.DB=="data/clean_0762.db"
 assert config.STRATEGY_FINALIZED and config.FINAL_PP_VARIANT=="PP_10_7"
 assert config.EXECUTION_SIM_ENABLED

def test_strategy_unchanged():
 import config
 assert config.MIN_TRADES==4 and config.MIN_BUYERS==3 and config.MIN_RATIO==1.35
 assert config.STOP_LOSS==-0.12 and config.TP2==0.30 and config.PP_MIN_AGE==8

def test_legacy_labs_off():
 import config
 assert not config.PP_SHADOW_V2 and not config.FAST_SHADOW_V3 and not config.SMART_EXIT_AB_V491

def test_transport_retained():
 import config
 assert config.PUMPAPI_SINGLETON_PORT==49190 and config.PUMPAPI_STALL_SECONDS==20 and config.PUMPAPI_HARD_STALL_SECONDS==60 and config.PUMPAPI_WATCHDOG_TICK_SECONDS==5 and config.BLOCK_ENTRIES_WHEN_TRADE_STALLED
 assert config.PUMPAPI_SERVER_SINGLE_WS_COOLDOWN>=240 and config.PUMPAPI_CLOSE_GRACE>=15

def test_sim_assumptions_valid():
 import config
 assert 0<=config.SIM_FAILED_TX_PROB<1 and config.SIM_BUY_SLIPPAGE_PCT>=0 and config.SIM_SELL_SLIPPAGE_PCT>=0 and config.SIM_TRADE_FEE_PCT>=0


def test_v061_audit_profiles():
 import config
 assert config.EXEC_AUDIT_ENABLED
 assert set(config.EXEC_AUDIT_PROFILES)=={"LOW","BASE","STRESS"}
 assert config.EXEC_AUDIT_PROFILES["LOW"]["buy_slip"] < config.EXEC_AUDIT_PROFILES["BASE"]["buy_slip"] < config.EXEC_AUDIT_PROFILES["STRESS"]["buy_slip"]


def test_execution_edge_lab_shadow_only():
    import config
    assert config.EDGE_LAB_ENABLED is True
    assert config.STRATEGY_FINALIZED is True
    assert config.FINAL_PP_VARIANT == "PP_10_7"
    assert set(config.EDGE_LAB_VARIANTS) == {"ALL","ACTIVITY_8_5","PRESSURE_2X","QUALITY_COMBO"}


def test_v067_offline_model_shadow_only():
 import config
 assert config.OFFLINE_MODEL_LAB_ENABLED
 assert config.OFFLINE_MODEL_WINDOW==20
 assert config.STRATEGY_FINALIZED and config.FINAL_PP_VARIANT=="PP_10_7"
 assert config.OFFLINE_MODEL_TRAIN_FRACTION==0.70


def test_v068_walk_forward_shadow_only():
 import config
 assert config.WALK_FORWARD_ENABLED
 assert config.WALK_FORWARD_MIN_TRAIN==300
 assert config.WALK_FORWARD_TEST_SIZE==100 and config.WALK_FORWARD_STEP==100
 assert config.WALK_FORWARD_THRESHOLDS==(0.55,0.60)
 assert config.STRATEGY_FINALIZED and config.FINAL_PP_VARIANT=="PP_10_7"


def test_v069_model_gate_frozen():
 import config
 assert not config.MODEL_GATE_ENABLED
 assert config.MODEL_GATE_THRESHOLD==0.55
 assert config.MODEL_GATE_MIN_TRAIN==300
 assert config.STRATEGY_FINALIZED and config.FINAL_PP_VARIANT=="PP_10_7"


def test_v0691_model_gate_import_before_runtime():
 import pathlib
 text=pathlib.Path("main.py").read_text(encoding='utf-8')
 assert "import model_gate" in text[:500]
 assert text.find("import model_gate") < text.find("def buy(")

def test_v0691_dashboard_port_label():
 import pathlib
 assert "v0.7.101.2" in pathlib.Path("dashboard/details.html").read_text(encoding='utf-8')


def test_final_gate_validation_schema():
    import database as db
    db.init()
    with db.con() as c:
        cols={r[1] for r in c.execute("PRAGMA table_info(final_gate_validation_v0692)")}
    assert {"gate_decision","model_p","pnl","low_pnl","base_pnl"} <= cols

def test_gate_frozen_after_warmup_flag():
    import config
    assert config.MODEL_GATE_FREEZE_AFTER_WARMUP is True


def test_v0694_cost_edge_shadow_only():
 import config, pathlib
 assert not config.COST_EDGE_LAB_ENABLED
 assert config.COST_EDGE_MODEL_THRESHOLDS==()
 assert config.COST_EDGE_SWEET_THRESHOLDS==(65,)
 assert config.COST_EDGE_TARGET_BE_PER_TRADE==0.20
 text=pathlib.Path("dashboard/details.html").read_text(encoding='utf-8')
 assert "FAST RECOVERY SIGNAL LAB" not in text


def test_v073_feature_lab_broad_entries():
 import config, pathlib
 assert not config.STABLE_VOL_GATE_ENABLED
 assert not config.SWEET70_GATE_ENABLED
 assert not config.MODEL_GATE_ENABLED
 assert not config.COST_EDGE_LAB_ENABLED
 text=pathlib.Path("dashboard/server.py").read_text(encoding='utf-8')
 for name in ("ACCEL_POS","ACCEL_GE_0_5","LAST1_GE_2","VOL_1_TO_4","PRESSURE_POS","WINNER_CORE","WINNER_BALANCED"):
  assert name not in text



def test_v079_fast_recovery_signal_lab():
 import config, pathlib, database as db
 assert config.VERSION == "v0.7.101.2"
 assert config.PORT == 8188
 assert config.DB == "data/clean_0762.db"
 assert config.PRESSURE_GATE_ENABLED is False
 assert config.FAST_SHADOW_V3 is False
 assert config.FAST_RECOVERY30_ENABLED is False
 assert config.FAST_RECOVERY_SIGNAL_ENABLED is True
 assert config.FAST_RECOVERY_SIGNAL_MAX_SECONDS == 30.0
 assert config.FAST_RECOVERY_SIGNAL_TARGET_SAMPLES == 300
 assert config.FAST_RECOVERY_SIGNAL_HARD_ABORT_PCT == -0.06
 assert config.FAST_RECOVERY_SIGNAL_ARM_PCT == 0.05
 assert config.FAST_RECOVERY_SIGNAL_TRAIL_PCT == 0.03
 assert config.FAST_RECOVERY_SIGNAL_MIN_BASE_DELTA_PER_FAST == 0.05
 assert config.FAST_RECOVERY_SIGNAL_BIG_WORSE_USD == 0.30
 page=pathlib.Path("dashboard/details.html").read_text(encoding='utf-8')
 assert "v0.7.101.2" in page and "FAST RECOVERY SIGNAL LAB" not in page
 assert "positionRows" in page and "monitorRows" in page
 main=pathlib.Path("main.py").read_text(encoding='utf-8')
 assert "fast_recovery_signal_start" in main and "fast_recovery_signal_tick" in main
 db.init()
 with db.con() as c:
  cols={r[1] for r in c.execute("PRAGMA table_info(fast_recovery_signal_v079)")}
 assert {"exit_reason","armed","arm_age","peak_recovery_pct","recent_momentum_pct","buy_pressure","base_delta","oracle_base_delta","big_worse","last_event_age"} <= cols

def test_v079_real_fast_stop_still_unchanged():
 import pathlib
 text=pathlib.Path("main.py").read_text(encoding='utf-8')
 # Shadow watcher is started before the real trade is popped, but does not replace sell accounting.
 assert 'fast_recovery_signal_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)' in text
 assert 'q=P.pop(m,None)' in text
 assert text.find('fast_recovery_signal_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)') < text.find('q=P.pop(m,None)')


def test_v0710_risk_control_retired_but_schema_retained():
 import config,database as db
 assert config.FAST_RECOVERY_SIGNAL_ENABLED is True
 assert config.FAST_RECOVERY_RISK_ENABLED is False
 db.init()
 with db.con() as c:
  cols={r[1] for r in c.execute("PRAGMA table_info(fast_recovery_risk_v0710)")}
 assert {"mint","exit_reason","base_delta","big_worse","armed","peak_recovery_pct"} <= cols

def test_v0711_reentry_runs_after_real_fast_exit_signal_only():
 import pathlib
 text=pathlib.Path("main.py").read_text(encoding='utf-8')
 assert 'fast_recovery_signal_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)' in text
 assert 'fast_reentry_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)' in text
 assert text.find('fast_reentry_start(m,q,p,q.get("size",config.POSITION_SIZE)*r)') < text.find("q=P.pop(m,None)")
 assert "REENTRY_STOP" in text and "REENTRY_TRAIL" in text and "REENTRY_FADE" in text

def test_dashboard_socket_and_html_polling_hotfix():
 import pathlib
 server=pathlib.Path("dashboard/server.py").read_text(encoding='utf-8')
 page=pathlib.Path("dashboard/details.html").read_text(encoding='utf-8')
 assert "ConnectionAbortedError" in server and "10053" in server and "10054" in server and "10060" in server
 assert "if(busy)return" in page and "AbortSignal.timeout" in page
 assert "Нет связи с панелью" in page
 assert "_SNAP_LOCK" in server and "snap_bytes" in server and "Content-Length" in server
 assert "walletRows" in page and "monitor_service.report" in server


def test_v0711_config_and_schema():
 import config,database as db
 assert config.VERSION=="v0.7.101.2" and config.PORT==8188 and config.DB=="data/clean_0762.db"
 assert config.FAST_REENTRY_ENABLED is True
 assert config.FAST_REENTRY_TARGET_FAST==300 and config.FAST_REENTRY_MIN_REENTRIES==80
 assert config.FAST_REENTRY_TRIGGER_RECOVERY_PCT==0.05
 assert config.FAST_REENTRY_TRIGGER_MOMENTUM_PCT==0.005
 assert config.FAST_REENTRY_MIN_BUY_PRESSURE==0.55
 assert config.FAST_REENTRY_HARD_STOP_PCT==-0.05
 assert config.FAST_REENTRY_MIN_BASE_LIFT_PER_FAST==0.05
 db.init()
 with db.con() as c:
  cols={r[1] for r in c.execute("PRAGMA table_info(fast_reentry_v0711)")}
 assert {"reentered","reentry_price","signal_recovery_pct","ideal_reentry_pnl","low_reentry_pnl","base_reentry_pnl","stress_reentry_pnl","base_cost","big_loss","last_event_age"} <= cols


def test_v0711_roundtrip_cost_is_incremental_and_profile_ordered():
 import main,config
 ideal=0.30
 low,lc,_,_=main._shadow_roundtrip_cost("mint-test",1.0,1.1,ideal,"LOW")
 base,bc,_,_=main._shadow_roundtrip_cost("mint-test",1.0,1.1,ideal,"BASE")
 stress,sc,_,_=main._shadow_roundtrip_cost("mint-test",1.0,1.1,ideal,"STRESS")
 assert lc>0 and bc>lc and sc>bc
 assert low>base>stress


def test_v0711_shadow_state_can_reenter_and_finalize():
 import time,main,config
 m="unit-v0711"
 main.FAST_REENTRY.pop(m,None)
 q={"entry":1.10,"ts":time.time()-3,"mfe":0.01,"mae":-0.09}
 main.fast_reentry_start(m,q,1.0,config.POSITION_SIZE*(1.0/1.10-1))
 main.FAST_REENTRY[m]["started"]=time.time()-2
 main.fast_reentry_tick(m,1.02,"buy"); main.fast_reentry_tick(m,1.04,"buy"); main.fast_reentry_tick(m,1.06,"buy")
 assert main.FAST_REENTRY[m]["reentered"] is True
 main.fast_reentry_tick(m,1.12,"buy")
 main.fast_reentry_tick(m,1.08,"sell")
 assert m not in main.FAST_REENTRY
 assert any(x[0]=="fast_reentry_done" and x[1]["mint"]==m for x in main.SHADOW_DB_QUEUE)


def test_v0711_env_and_launchers():
 import pathlib
 env=pathlib.Path(".env.example").read_text(encoding='utf-8')
 assert "DATABASE_PATH=data/clean_0762.db" in env and "DASHBOARD_PORT=8188" in env
 assert "FAST_RECOVERY_RISK_ENABLED=false" in env and "FAST_REENTRY_ENABLED=true" in env
 root=pathlib.Path('..')
 assert {p.name for p in root.glob('*.bat')}=={'01_INSTALL.bat','02_SETTINGS.bat','03_START.bat','04_CHECK.bat','05_RESULTS.bat','06_AMM_CHECK.bat','07_REVIEW_SAVED_REPORT.bat','08_PRESERVE_DATA.bat','09_RECOVERY_CHECK.bat','10_TEST_AMM_STOP.bat','11_PERF_CHECK.bat','12_NETWORK_CHECK.bat','13_SPAWN_DIAGNOSTICS.bat','14_RPC_STATUS.bat','15_PAPER_FAULT_TEST.bat'}
 assert not list(pathlib.Path('.').glob('*.bat'))



def test_v0712_false_bounce_lab():
 import pathlib,database as db,config
 db.init()
 import sqlite3
 con=sqlite3.connect(config.DB)
 assert con.execute("SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=\'false_bounce_v0712\'").fetchone()
 con.close()
 page=pathlib.Path("dashboard/details.html").read_text(encoding='utf-8')
 assert "FALSE-BOUNCE CLASSIFIER LAB" not in page and "positionRows" in page
 main=pathlib.Path("main.py").read_text(encoding='utf-8')
 assert "fb_snaps" in main and "false_bounce_done" in main
