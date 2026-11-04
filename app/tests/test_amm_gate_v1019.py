"""v0.7.101.9, path B (user's choice 04.11.2026): stage 1 needs a verified AMM exit or proof that none is needed.

178 PRIMARY and 148 ROUTE positions 02–04.11.2026 never migrated to an AMM pool, so the old checkpoint could not pass."""
import sqlite3
import pytest
import config,roadmap_progress as rp,route_branch,campaign_view

STAGES=[dict(id=i,title=str(i)) for i in range(1,5)]
RPC=dict(status='READY',endpoints=[dict(started=10)])
NATIVE=dict(version=config.VERSION,passed=True,native_windows_verified=True)

def route(**kw):
    r=dict(verified_routes={'PUMP_CURVE':100},entries=100,closed=100,new_closed=100,positions=[],input_gap=False,
        unknown_route_exits=0,exit_routes_v1019={'PUMP_CURVE':230})
    r.update(kw);return r

def gate(r):
    stage=rp.build(STAGES,{},{},r,RPC,NATIVE)['stages'][0]
    return next(c for c in stage['checkpoints'] if c['title']=='AMM-выход (или не нужен)'),stage

def test_hundred_curve_only_closes_prove_amm_exit_not_needed():
    c,stage=gate(route())
    assert c==dict(title='AMM-выход (или не нужен)',passed=True,basis='NOT_NEEDED_NO_MIGRATION') and stage['percent']==100

def test_a_verified_amm_exit_still_passes_without_the_sample():
    c,_=gate(route(new_closed=3,verified_routes={'PUMP_CURVE':2,'PUMP_AMM':1},exit_routes_v1019={'PUMP_CURVE':5,'PUMP_AMM':2}))
    assert c['passed'] and c['basis']=='VERIFIED'

@pytest.mark.parametrize('change',[dict(new_closed=99),dict(exit_routes_v1019={'PUMP_CURVE':200,'PUMP_AMM':1}),
    dict(unknown_route_exits=1),dict(positions=[{}]),dict(exit_routes_v1019=None)])
def test_no_proof_no_pass(change):
    c,stage=gate(route(**change))
    assert c==dict(title='AMM-выход (или не нужен)',passed=False) and stage['percent']<=80

def test_exit_routes_ignore_checks_without_a_job():
    c=sqlite3.connect(':memory:');c.executescript(route_branch.SCHEMA)
    c.executemany('INSERT INTO route_exit_decisions_v59 VALUES(?,?,?,?,?,?)',[('a','t','m',1,'SETTLED','PUMP_CURVE'),('b','t','m',2,'INVALID_PAYLOAD',None),('c','t','m',3,'STALE_OR_PRE_SIGNAL','PUMP_CURVE')])
    assert campaign_view.exit_routes(c)=={'PUMP_CURVE':2}
