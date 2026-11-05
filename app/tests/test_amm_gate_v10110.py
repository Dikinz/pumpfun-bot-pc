"""v0.7.101.10 (user's choice 05.11.2026): stage 1 "AMM exit (or not needed)" may be proven by the PRIMARY monitor.

05.11.2026: ROUTE ran out of PAPER cash at 63 closes, so the v0.7.101.9 proof (100 ROUTE closes) became unreachable,
while the PRIMARY monitor had checked all 77 PRIMARY positions (542 checks, all on the curve, none completed)."""
import time
import pytest
import config,roadmap_progress as rp,route_branch as route,primary_write_off as pw,campaign_view,main
from test_primary_integrity import fresh
from test_parallel_paper_v54 import opened
from test_route_exit_v59 import save_check
from test_curve_floor_v10110 import drained

STAGES=[dict(id=i,title=str(i)) for i in range(1,5)]
RPC=dict(status='READY',endpoints=[dict(started=10)])
NATIVE=dict(version=config.VERSION,passed=True,native_windows_verified=True)
TITLE='AMM-выход (или не нужен)'

def primary(**kw):
    p=dict(new_closed=100,positions=[],monitor_routes_v10110=dict(closed=100,checked=100,routes={'PUMP_CURVE':700},completed_checks=0))
    p.update(kw);return p

def route_stuck(**kw):
    r=dict(verified_routes={'PUMP_CURVE':58},entries=63,closed=63,new_closed=63,positions=[],input_gap=False,unknown_route_exits=0,exit_routes_v1019={'PUMP_CURVE':400})
    r.update(kw);return r

def gate(p,r):
    stage=rp.build(STAGES,p,{},r,RPC,NATIVE)['stages'][0]
    return next(c for c in stage['checkpoints'] if c['title']==TITLE)

def test_primary_monitor_proves_no_migration_while_route_is_stuck():
    assert gate(primary(),route_stuck())==dict(title=TITLE,passed=True,basis='NOT_NEEDED_NO_MIGRATION_PRIMARY_MONITOR')

def test_primary_monitor_proof_without_a_route_branch():
    assert gate(primary(),{})['basis']=='NOT_NEEDED_NO_MIGRATION_PRIMARY_MONITOR'

def test_route_proof_and_verified_amm_still_pass():
    assert gate(primary(new_closed=10),route_stuck(new_closed=100))['basis']=='NOT_NEEDED_NO_MIGRATION'
    assert gate({},route_stuck(verified_routes={'PUMP_CURVE':2,'PUMP_AMM':1}))['basis']=='VERIFIED'

def monitor(**kw):
    m=dict(closed=100,checked=100,routes={'PUMP_CURVE':700},completed_checks=0);m.update(kw);return m

@pytest.mark.parametrize('p,r',[
    (primary(new_closed=99,monitor_routes_v10110=monitor(closed=99,checked=99)),route_stuck()),
    (primary(monitor_routes_v10110=monitor(checked=99)),route_stuck()),          # a closed position never checked
    (primary(monitor_routes_v10110=monitor(closed=101)),route_stuck()),          # evidence from another sample
    (primary(monitor_routes_v10110=monitor(routes={'PUMP_CURVE':700,'PUMP_AMM':1})),route_stuck()),
    (primary(monitor_routes_v10110=monitor(completed_checks=1)),route_stuck()),
    (primary(monitor_routes_v10110=None),route_stuck()),
    (primary(positions=[{}]),route_stuck()),
    (primary(),route_stuck(exit_routes_v1019={'PUMP_CURVE':400,'PUMP_AMM':1})),  # a ROUTE position migrated
    (primary(),route_stuck(unknown_route_exits=1)),
    (primary(),route_stuck(positions=[{}])),
    (primary(),dict(status='BRANCH_REPORT_ERROR')),                               # ROUTE DB busy: nothing checked
    (primary(),dict(status='CAMPAIGN_MISMATCH')),
    (primary(),route_stuck(exit_routes_v1019=None))])
def test_no_proof_no_pass(p,r):
    assert gate(p,r)==dict(title=TITLE,passed=False)

def test_monitor_routes_cover_closed_positions_only(fresh):
    tid=opened(fresh);fresh.executescript(route.SCHEMA)
    row,j,r,inv=drained(tid);save_check(fresh,row,j,r,inv)
    assert campaign_view.monitor_routes(fresh,config.DB)==dict(closed=0,checked=0,routes={},completed_checks=0)
    pw.poll(main)  # closes the position at the model value
    assert campaign_view.monitor_routes(fresh,config.DB)==dict(closed=1,checked=1,routes={'PUMP_CURVE':1},completed_checks=0)
    # A check that never decoded the pool (failed, cancelled, running) is no evidence for the position.
    import monitor_service
    from contextlib import closing
    with closing(monitor_service.connect()) as m:m.execute("UPDATE position_checks_v0730 SET status='REQUEST_ERROR',result_json='{}'")
    assert campaign_view.monitor_routes(fresh,config.DB)==dict(closed=1,checked=0,routes={'PUMP_CURVE':1},completed_checks=0)
