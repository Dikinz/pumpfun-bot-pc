"""Regressions from the 01.11-02.11.2026 night PAPER run (v0.7.101.3 -> v0.7.101.4)."""
import json,sqlite3,threading,time
from pathlib import Path
import pytest
import config,database as db,system_health,risk_watch_v1014 as rw,wal_checkpoint_v1014 as wc
from test_primary_integrity import fresh

ROOT=Path(__file__).resolve().parents[1]

def wait_for(cond,timeout=5):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if cond():return True
        time.sleep(.05)
    return False

def test_runtime_mode_moves_wal_checkpoints_off_the_market_connection(fresh):
    db.runtime_mode()
    assert fresh.execute('PRAGMA wal_autocheckpoint').fetchone()[0]==0
    assert wc.ACTIVE is not None and wc.ACTIVE.connection is fresh and wc.ACTIVE.thread.name=='wal-checkpoint-v1014'
    fresh.execute('CREATE TABLE IF NOT EXISTS t_v1014(x BLOB)')
    for _ in range(300):fresh.execute('INSERT INTO t_v1014 VALUES(randomblob(2000))')
    assert wait_for(lambda:(wc.status() or {}).get('checkpoints',0)>=1 and wc.status()['checkpointed_frames']==wc.status()['wal_frames'])
    s=wc.status()
    assert s['status']=='RUNNING' and s['thread_alive'] and s['errors']==0 and not s['fallback_autocheckpoint']

def test_dead_checkpoint_thread_restores_automatic_checkpoint_once(fresh):
    db.runtime_mode();active=wc.ACTIVE
    dead=threading.Thread(target=lambda:None);dead.start();dead.join()
    active.thread=dead
    assert wc.guard() is True
    assert fresh.execute('PRAGMA wal_autocheckpoint').fetchone()[0]==wc.AUTOCHECKPOINT_PAGES
    assert wc.guard() is False and wc.status()['fallback_autocheckpoint'] is True
    rows=[json.loads(l) for l in Path(config.DB+'.sqlite_diagnostics.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(r['operation']=='WAL_CHECKPOINT_FALLBACK' for r in rows)

def test_checkpointer_follows_a_new_database(fresh,tmp_path):
    db.runtime_mode();first=wc.ACTIVE
    other=sqlite3.connect(tmp_path/'other.db',isolation_level=None);other.execute('PRAGMA journal_mode=WAL')
    try:
        second=wc.start(tmp_path/'other.db',other)
        assert second is not first and not first.thread.is_alive() and second.path==str(tmp_path/'other.db')
    finally:
        wc.ACTIVE.close();wc.ACTIVE=None;other.close()

def test_runtime_status_reports_checkpoint_state(fresh):
    import runtime_control
    db.runtime_mode()
    probe=runtime_control.Probe(config.DB)
    assert probe.inspect()['wal_checkpoint_v1014']['schema']=='WAL_CHECKPOINT_V1014'

def test_monitor_heartbeat_journal_is_compact_between_full_minutes():
    import monitor_service
    late=dict(pending=False,owners=[1,2],history=[dict(check_id=str(i),state='CONFIRMED') for i in range(40)])
    state=dict(status='RUNNING',pid=7,active_check_ids=['a'],late_creation_v85=late)
    small=monitor_service.compact_heartbeat(state)
    assert small['compact_v1014'] is True and small['status']=='RUNNING' and small['active_check_ids']==['a']
    assert small['late_creation_v85']==dict(pending=False,owners=2,history=40)
    assert len(json.dumps(small))*5<len(json.dumps(state))
    src=(ROOT/'monitor_service.py').read_text(encoding='utf-8')
    assert "full=now-last_full_log>=60 or now<last_full_log" in src and "if now-last_log>=5:" in src

def test_system_journal_keeps_a_night_of_samples():
    # 02.11: ~1.9 KB/s of SAMPLE records; 9 x 2 MiB held only the last 2 h 40 min.
    assert (system_health.BACKUPS+1)*system_health.MAX_LOG_BYTES>=10*3600*1900

def test_would_halt_flags_mark_limits_and_result_after_them():
    nets=[1.0,-0.5,-0.5,-0.5,-0.5,-0.5,-3.0,0.2,-2.0,0.4]
    f=rw.flags(list(enumerate(nets,1)),10)
    codes={h['code']:h for h in f['would_halt']}
    assert codes['WOULD_HALT_STREAK5']['after_closed']==6
    assert codes['WOULD_HALT_DD20']['after_closed']==6 and codes['WOULD_HALT_DD30']['after_closed']==7
    assert codes['WOULD_HALT_DD30']['net_after']==pytest.approx(0.2-2.0+0.4)
    assert f['max_loss_streak']==6 and f['current_loss_streak']==0 and f['closed']==10
    assert rw.flags([],30)==dict(closed=0,max_drawdown_pct=0.,current_drawdown_pct=0.,max_loss_streak=0,current_loss_streak=0,would_halt=[])

def test_would_halt_reports_branch_trade_numbers_when_accounting_has_gaps():
    # Trade 3 has no complete ledger: flags keep the branch's own closed-trade numbers.
    f=rw.flags([(1,-.5),(2,-.5),(4,-.5),(5,-.5),(6,-.5)],30)
    assert [h['after_closed'] for h in f['would_halt'] if h['code']=='WOULD_HALT_STREAK5']==[6]

def test_sell_waits_cover_filled_and_pending_exits_after_the_fork(fresh):
    now=time.time()
    for i,tid in ((1,'t0'),(2,'t1'),(3,'t2')):
        fresh.execute("INSERT INTO trades(id,trade_id,ts,mint,side,price,size,pnl,reason) VALUES(?,?,?,?, 'SELL',1,3,0,'X')",(i,tid,now,'m'+tid))
    for oid,tid,status,wait,sig in (('z','t0','FILLED',99999.,now-200),('a','t1','FILLED',800.,now-100),('b','t2','FILLED',174482.,now-90),('c','t3','PENDING',None,now-12)):
        fresh.execute("INSERT INTO paper_orders_v0720(order_id,mint,side,status,signal_ts,signal_price,size,reason,trade_id,wait_ms) VALUES(?,?,'SELL',?,?,1,3,'X',?,?)",(oid,'m'+oid,status,sig,tid,wait))
    s=rw.sell_waits(fresh,1,now)
    assert s['filled']==2 and s['slow']==1 and s['max_ms']==174482. and s['pending']==1 and s['oldest_pending_s']==pytest.approx(12,abs=.5)
    assert rw.sell_waits(fresh,3,now)['filled']==0

def test_repeated_checkpoint_errors_hand_the_checkpoint_back(fresh,monkeypatch):
    class Failing:
        def __init__(self,*a,**k):pass
        def execute(self,sql,*a):
            if 'wal_checkpoint' in sql:raise sqlite3.OperationalError('disk I/O error')
            return self
        def close(self):pass
    with monkeypatch.context() as m:
        m.setattr(wc,'INTERVAL_S',.01);m.setattr(wc.sqlite3,'connect',Failing)
        active=wc.start(config.DB,fresh)
        assert wait_for(lambda:not active.thread.is_alive())
    s=wc.status();assert s['status']=='FAILED' and s['errors']==wc.MAX_CONSECUTIVE_ERRORS
    assert wc.guard() is True and fresh.execute('PRAGMA wal_autocheckpoint').fetchone()[0]==wc.AUTOCHECKPOINT_PAGES

def test_shutdown_closes_checkpointer_and_restores_automatic_checkpoint(fresh):
    db.runtime_mode();active=wc.ACTIVE
    wc.close()
    assert wc.ACTIVE is None and not active.thread.is_alive()
    assert fresh.execute('PRAGMA wal_autocheckpoint').fetchone()[0]==wc.AUTOCHECKPOINT_PAGES
    for name in ('main.py','paper_branch.py'):
        src=(ROOT/name).read_text(encoding='utf-8')
        assert src.index("__import__('wal_checkpoint_v1014').close()")<src.index("__import__('shutdown_audit').closed(")

def test_overview_branches_carry_observation_only_risk_watch(fresh):
    import clean_campaign as campaign,campaign_view as view
    campaign.ensure(fresh,config.DB)
    o=view.overview(config.DB)
    r=o['primary']['risk_watch_v1014']
    assert r['observation_only'] is True and r['closed']==0 and r['would_halt']==[] and r['sell_wait']['filled']==0

def test_terminal_shows_risk_flags_and_sell_wait():
    t=(ROOT/'dashboard'/'terminal.html').read_text(encoding='utf-8')
    assert 'b.risk_watch_v1014' in t and "'Стоп-флаги'" in t and "'Ожидание продажи'" in t and 'входы не блокируются' in t
    assert len(t.encode('utf-8'))<64*1024
