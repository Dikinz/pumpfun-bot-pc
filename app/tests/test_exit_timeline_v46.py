import json,copy
import pytest
import exit_audit as ea,exit_timeline as et,execution_model as ex,main
from test_primary_integrity import fresh
from test_execution_model import env

def test_decomposition_includes_costs_once():
    a=et.attribution({'price':10.,'size':3.},{'price':5.,'pnl':-1.5},{'signal_price':9.},[{'side':'BUY','total_cost':.05},{'side':'SELL','total_cost':.03}])
    assert a['before_signal']==pytest.approx(-.3)
    assert a['after_signal']==pytest.approx(-1.2)
    assert a['net']==pytest.approx(-1.58) and a['reconciled']
    assert et.attribution({'price':10.,'size':3.},{},{'signal_price':9.},[])['net'] is None
    assert et.attribution({}, {}, {}, [])['status']=='MISSING_ENTRY_OR_SIGNAL'

def test_pending_persisted_without_new_tick_and_after_restart(env,monkeypatch):
    c,clock,tick,entry=env;entry();tick(p=1.,dt=.3)
    tid=main.P['a']['trade_id'];main.sell('a',1.,'TEST')
    tick(p=.9,dt=.1);tick(p=.85,dt=.13)
    before=[tuple(r) for r in c.execute('SELECT * FROM capital_ledger_v0721')]
    ea.checkpoint(c,force=True)
    d=json.loads(c.execute('SELECT data_json FROM exit_audit_v45 WHERE trade_id=?',(tid,)).fetchone()[0])
    cp=d['checkpoint'];assert cp['buffer_reason']=='ONLY_EVENTS_BEFORE_250MS_IN_BUFFER'
    assert cp['eligibility_counts']['DELAY_NOT_ELAPSED']==2 and cp['events_after_delay_in_buffer']==0
    clock['m']+=20;clock['t']+=20;ea.checkpoint(c,force=True)
    d=json.loads(c.execute('SELECT data_json FROM exit_audit_v45 WHERE trade_id=?',(tid,)).fetchone()[0])
    assert d['checkpoint']['wait_wall_s']>20 and d['checkpoint']['events_seen_this_process']==cp['events_seen_this_process']
    assert [tuple(r) for r in c.execute('SELECT * FROM capital_ledger_v0721')]==before
    monkeypatch.setattr(ea,'PROCESS_ID','restarted');ea.BUFFERS.clear();ea.TOTAL.clear();ex.restart(c)
    ea.checkpoint(c,force=True)
    d=json.loads(c.execute('SELECT data_json FROM exit_audit_v45 WHERE trade_id=?',(tid,)).fetchone()[0])
    assert d['previous_process_checkpoint']['events_seen_this_process']==cp['events_seen_this_process']
    assert d['checkpoint']['process_restarted'] and d['checkpoint']['events_seen_this_process']==0
    tick(p=.5,dt=.3)
    d=json.loads(c.execute('SELECT data_json FROM exit_audit_v45 WHERE trade_id=?',(tid,)).fetchone()[0])
    assert d['signal_to_fill_monotonic_ms'] is None
    assert d['loss_attribution']['reconciled']

def test_data_rejection_distinct_from_no_tick(env):
    c,clock,tick,entry=env;entry();tick(p=1.,dt=.3);tid=main.P['a']['trade_id'];main.sell('a',1.,'TEST')
    tick(p=.4,dt=.4,timestamp=(clock['t']-10)*1000)
    ea.checkpoint(c,force=True)
    cp=json.loads(c.execute('SELECT data_json FROM exit_audit_v45 WHERE trade_id=?',(tid,)).fetchone()[0])['checkpoint']
    assert cp['buffer_reason']=='DATA_REJECTED_AFTER_DELAY_IN_BUFFER'
    assert cp['events'][-1]['validation']=='PROVIDER_STALE' and 'a' in main.P

def test_summary_keeps_monitor_and_tick_status_separate(env):
    c,clock,tick,entry=env;entry();tick(p=1.,dt=.3);tid=main.P['a']['trade_id'];main.sell('a',1.,'TEST');ea.checkpoint(c,force=True)
    mon={'positions':[{'trade_id':tid,'status':'INSUFFICIENT_RESERVES','finished_ts':clock['t']+3,'health':{'first_problem_ts':clock['t']+3},'result':{'sale':{'status':'LOCAL_SWAP_FAILED'}}}]}
    r=et.summary(c,mon)['rows'][0]
    assert r['monitor_status']=='INSUFFICIENT_RESERVES' and r['attribution']['net'] is None
    assert r['monitor_finished_ts']>r['signal_ts'] and r['status']=='WAITING_FILL'
