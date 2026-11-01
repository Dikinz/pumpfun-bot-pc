"""Persist the actual startup branch set, tied to this primary run and sessions."""
import json,math
SCHEMA='BRANCH_PLAN_V101'
NAMES=('paper_branch','route_branch')

def store(c,plan):c.execute("INSERT OR REPLACE INTO meta VALUES('branch_plan_v101',?)",(json.dumps(plan),))

def begin(c,primary,run_id):
    import branch_mode_v100
    modules=branch_mode_v100.modules(primary)
    row=c.execute("SELECT v FROM meta WHERE k='runtime_started_ts_v41'").fetchone()
    plan=dict(schema=SCHEMA,modules=list(modules),run_id=run_id,started_ts=float(row[0]),sessions={},complete=False)
    store(c,plan);return plan

def complete(c,plan,publisher):
    sessions={p.module:p.session for p in getattr(publisher,'publishers',[])}
    plan=dict(plan,sessions=sessions,complete=set(sessions)==set(plan['modules']))
    store(c,plan);return plan

def selected(c):
    row=c.execute("SELECT v FROM meta WHERE k='branch_plan_v101'").fetchone()
    if not row:return None
    try:
        plan=json.loads(row[0]);modules=plan['modules'];sessions=plan['sessions'];started=plan['started_ts']
        runtime=c.execute("SELECT v FROM meta WHERE k='runtime_started_ts_v41'").fetchone()
        latest=c.execute('SELECT id FROM runs_v45 ORDER BY started_ts DESC,rowid DESC LIMIT 1').fetchone()
        if plan.get('schema')!=SCHEMA or plan.get('complete') is not True or not isinstance(modules,list) or modules not in (['route_branch'],list(NAMES)):return None
        if not isinstance(sessions,dict) or set(sessions)!=set(modules):return None
        if any(not isinstance(s,str) or len(s)!=32 or any(x not in '0123456789abcdef' for x in s) for s in sessions.values()):return None
        if type(started) not in (int,float) or not math.isfinite(started) or not runtime or float(runtime[0])!=started or not latest or latest[0]!=plan['run_id']:return None
        return plan
    except (ValueError,TypeError,KeyError):return None
