"""Regressions from the 01.11.2026 20:00-22:05 PAPER run (v0.7.101.2 -> v0.7.101.3)."""
import json,sqlite3,uuid
from pathlib import Path
import pytest
import config,paper_branch as branch,run_audit,exit_audit
from test_primary_integrity import fresh

ROOT=Path(__file__).resolve().parents[1]

def statuses(p):
    with sqlite3.connect(p) as c:return dict(c.execute('SELECT id,status FROM runs_v45'))

@pytest.fixture
def audit_state(monkeypatch):
    for k in ('ACTIVE','ORIGIN','LAST'):monkeypatch.setattr(run_audit,k,getattr(run_audit,k))

def test_branch_seed_does_not_turn_running_primary_run_into_interrupted(fresh,audit_state):
    primary=run_audit.begin(fresh,None);fresh.commit()
    dst=branch.seed(Path(config.DB))
    assert statuses(dst)=={primary:'SOURCE_RUN'}
    with sqlite3.connect(dst) as c:
        c.row_factory=sqlite3.Row
        own=run_audit.begin(c,None)
    s=statuses(dst)
    assert s[primary]=='SOURCE_RUN' and s[own]=='RUNNING' and 'INTERRUPTED' not in s.values()
    assert statuses(config.DB)[primary]=='RUNNING'

def test_branch_seed_repairs_copied_run_but_keeps_real_branch_interruptions(fresh,audit_state):
    primary=run_audit.begin(fresh,None);fresh.commit()
    dst=branch.seed(Path(config.DB))
    real=uuid.uuid4().hex
    with sqlite3.connect(dst) as c:
        # State written by v0.7.101.2: the copy was flipped to INTERRUPTED by the branch's begin().
        c.execute("UPDATE runs_v45 SET status='INTERRUPTED' WHERE id=?",(primary,))
        c.execute("INSERT INTO runs_v45 SELECT ?,started_ts+1,updated_ts,elapsed_s,'INTERRUPTED',first_id,last_id,start_json,result_json,clock_json FROM runs_v45 WHERE id=?",(real,primary))
    fresh.execute("UPDATE runs_v45 SET status='STOPPED' WHERE id=?",(primary,));fresh.commit()
    branch.seed(Path(config.DB))
    s=statuses(dst)
    assert s[primary]=='SOURCE_RUN' and s[real]=='INTERRUPTED'

def test_tick_proxy_exit_latency_is_labelled_as_waiting_for_market_trade():
    assert exit_audit.latency_basis(dict(note='tick proxy; no live order'))=='NEXT_MARKET_TRADE'
    assert exit_audit.latency_basis(dict(note='LOCAL_AMM_VERIFIED_NO_BROADCAST')) is None
    assert exit_audit.latency_basis(None) is None

def test_exit_audit_summary_reports_latency_basis(fresh):
    data=dict(order=dict(note='tick proxy; no live order'),fill=dict(reason='MAX_HOLD_WATCHDOG',exit_latency_ms=174482.0))
    fresh.execute('INSERT INTO exit_audit_v45 VALUES(?,?,?,?,?)',('t1','m','CLOSED',0,json.dumps(data)))
    row=exit_audit.summary(fresh)['rows'][0]
    assert row['latency_basis']=='NEXT_MARKET_TRADE' and row['latency_ms']==174482.0

def test_pages_show_market_wait_source_run_and_rent_free_model():
    details=(ROOT/'dashboard'/'details.html').read_text(encoding='utf-8')
    assert "x.latency_basis==='NEXT_MARKET_TRADE'&&x.latency_ms>5000" in details and 'ждали следующую сделку рынка' in details
    terminal=(ROOT/'dashboard'/'terminal.html').read_text(encoding='utf-8')
    assert "SOURCE_RUN:'копия основного'" in terminal
    assert 'b.persistent_wallet_model_v100' in terminal and 'm.retain_token_accounts_model_net' in terminal and 'm.assumed_ata_close_model_net' in terminal
