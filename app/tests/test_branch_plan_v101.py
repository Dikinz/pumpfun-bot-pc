import json,types,time
import pytest
import branch_plan_v101 as plan,runtime_control as rt,paper_branch,route_branch,config,run_audit,database as db
from test_primary_integrity import fresh


def prepare(c,monkeypatch,enabled):
 monkeypatch.setattr(config,'AMM_BRANCH_ENABLED',enabled)
 now=time.time();c.execute("INSERT OR REPLACE INTO meta VALUES('runtime_started_ts_v41',?)",(str(now),));c.execute("INSERT OR REPLACE INTO meta VALUES('runtime_state_v41','STOPPED')")
 rid=run_audit.begin(c);data=plan.begin(c,config.DB,rid)
 publishers=[types.SimpleNamespace(module=m,session=('a' if m=='paper_branch' else 'b')*32) for m in data['modules']]
 data=plan.complete(c,data,types.SimpleNamespace(publishers=publishers))
 for p in publishers:
  engine=__import__(p.module)
  engine.write_lifecycle(config.DB,dict(status='STOPPED',session=p.session,monitor_forced=False))
  engine.write_lifecycle(config.DB,dict(status='STOPPED',session=p.session,exit_code=0,forced=False),True)
 return data

@pytest.mark.parametrize('enabled',[False,True])
def test_branch101_only_current_planned_branches_confirm(fresh,monkeypatch,enabled):
 data=prepare(fresh,monkeypatch,enabled)
 # Configuration changes after startup cannot change the expected owners.
 monkeypatch.setattr(config,'AMM_BRANCH_ENABLED',not enabled)
 result=rt.shutdown_status(config.DB,dict(exit_code=0,forced=False))
 assert result['confirmed'] and set(result['branches'])==set(data['modules'])
 assert result['branch_plan_v101']=='CURRENT_RUN_BOUND'

@pytest.mark.parametrize('enabled',[False,True])
@pytest.mark.parametrize('fault',['missing','wrong_session','forced','active_monitor','new_run','incomplete_plan','corrupt_plan'])
def test_branch101_missing_or_foreign_ack_never_confirmed(fresh,monkeypatch,fault,enabled):
 data=prepare(fresh,monkeypatch,enabled)
 if fault=='missing':route_branch.lifecycle_path(config.DB).unlink()
 elif fault=='wrong_session':route_branch.write_lifecycle(config.DB,dict(status='STOPPED',session='c'*32))
 elif fault=='forced':route_branch.write_lifecycle(config.DB,dict(status='STOPPED',session='b'*32,exit_code=0,forced=True),True)
 elif fault=='active_monitor':route_branch.write_lifecycle(config.DB,dict(status='STOPPED',session='b'*32,monitor_forced=True))
 elif fault=='new_run':run_audit.begin(fresh)
 elif fault=='corrupt_plan':fresh.execute("UPDATE meta SET v='bad' WHERE k='branch_plan_v101'")
 elif fault=='incomplete_plan':data['complete']=False;plan.store(fresh,data)
 assert rt.shutdown_status(config.DB,dict(exit_code=0,forced=False))['confirmed'] is False
