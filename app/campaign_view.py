"""Small read-only operator view. Full diagnostics remain on a separate page."""
import json,sqlite3,time
from collections import deque
from pathlib import Path
from contextlib import closing
import config,clean_campaign,runtime_status,paper_capital,diagnostics,system_health,paper_branch

def branch(c,primary,role,now):
    c.row_factory=sqlite3.Row
    runtime=runtime_status.summary(c,now);diag=diagnostics.summary(c,now)
    snap=dict(runtime=runtime,diagnostics=diag)
    if role=='primary':system_health.apply_lifecycle(snap,primary)
    meta=dict(c.execute('SELECT k,v FROM meta'));campaign=clean_campaign.marker(c)
    cap=paper_capital.summary(c)
    info=dict(c.execute('SELECT k,v FROM branch_info')) if role=='route' else {}
    fork=int(info.get('route_fork_trade_id',0));carried=set(json.loads(info.get('route_carried_ids','[]')))
    orders=[dict(r) for r in c.execute("SELECT * FROM paper_orders_v0720 WHERE side='SELL' AND status='PENDING'")]
    at=diag['as_of'];by_mint={r['mint']:r for r in orders}
    positions=[]
    wait_details={x['trade_id']:x for x in __import__('exit_wait_v65').rows(c,at)}
    for r in c.execute('SELECT trade_id,mint,size,opened_ts FROM positions'):
        order=by_mint.get(r['mint']);wait=max(0,at-order['signal_ts']) if order else None
        decision=c.execute('SELECT decision FROM route_exit_decisions_v59 WHERE trade_id=? ORDER BY available_ts DESC LIMIT 1',(r['trade_id'],)).fetchone() if role=='route' else None
        state=decision[0] if decision else ('WAITING_VERIFIED_ROUTE_RESULT' if role=='route' else 'WAITING_VERIFIED_AMM_OR_TICK' if role=='amm' else 'WAITING_FRESH_TICK') if order else 'NO_EXIT_SIGNAL'
        remainder=__import__('route_remainder_v96').state(order['signal_ts'] if order else None,at,state) if role=='route' else {}
        positions.append(dict(r,**remainder,wait_diagnostic=wait_details.get(r['trade_id']),exit_decision=state,exit_executor='VERIFIED_ROUTE' if role=='route' else 'AMM_OR_TICK' if role=='amm' else 'TICK_BASELINE',age_s=max(0,at-r['opened_ts']),exit_reason=order['reason'] if order else None,exit_wait_s=wait,
            exit_overdue=wait is not None and wait>config.MAX_HOLD))
    new_closed=0;carried_closed=0
    trades=deque(maxlen=12);missing=0;closed=0;gross=0.;fees=0.;net_total=0.;wins=0;nets=[]
    cash=campaign['start_balance'] if campaign else config.START_BALANCE;peak=cash;equity=cash;drawdown=0.
    for r in c.execute("""SELECT t.trade_id,t.mint,t.ts,t.pnl,t.reason,t.hold,COALESCE(l.legs,0) legs,l.fees fees
      FROM trades t LEFT JOIN (SELECT trade_id,COUNT(*) legs,SUM(total_cost) fees FROM capital_ledger_v0721 GROUP BY trade_id) l
      ON l.trade_id=t.trade_id WHERE t.side='SELL' AND t.id>? ORDER BY t.id""",(fork,)):
        r=dict(r)
        if r['trade_id'] in carried:carried_closed+=1
        else:new_closed+=1
        cost=(r.pop('legs'),r.pop('fees'))
        net=r['pnl']-cost[1] if cost[0]==2 and cost[1] is not None else None
        r.update(net=net,cost=cost[1] if net is not None else None);trades.append(r);closed+=1;gross+=r['pnl']
        if net is None:missing+=1
        else:
            net_total+=net;fees+=cost[1];wins+=net>0;nets.append((closed,net))
            equity+=net;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
    entries=c.execute("SELECT COUNT(*) FROM trades WHERE side='BUY' AND id>?",(fork,)).fetchone()[0]
    pending=dict(c.execute("SELECT status,COUNT(*) FROM preentry_v0729 GROUP BY status"))
    # Aggregate queue columns only: do not parse full RPC/VM logs for the front page.
    import rpc_timing
    timing={}
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='preentry_timing_v56'").fetchone():
        rows=list(c.execute('SELECT queue_ms,run_ms FROM preentry_timing_v56 ORDER BY rowid DESC LIMIT 256'))
        timing=dict(scope='RECENT_256_UI_ONLY',queue=rpc_timing.distribution(r[0] for r in rows),runtime=rpc_timing.distribution(r[1] for r in rows))
    route_counts={}
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='branch_amm_settlements'").fetchone():
        for row in c.execute("SELECT json_extract(details,'$.route') route,COUNT(*) n FROM branch_amm_settlements GROUP BY json_extract(details,'$.route')"):
            route_counts[row['route'] or 'UNKNOWN']=row['n']
    settlements=sum(route_counts.values())
    engine=__import__('route_branch') if role=='route' else paper_branch
    stop=engine.read_lifecycle(primary);transport=engine.read_lifecycle(primary,True)
    if role not in ('amm','route'):stop={};transport={}
    else:
        if stop.get('session')!=meta.get('branch_session'):stop={}
        if transport.get('session')!=meta.get('branch_session'):transport={}
    blocks=list(diag['entry_blocks']);last=meta.get('last_entry_result_v0722','NO_SIGNAL')
    branch_failure=transport.get('failure') or {}
    failed=transport.get('status')=='FAILED' or stop.get('status')=='FAILED'
    transport_gap=bool(branch_failure)
    cause=__import__('failure_cause').branch(stop,transport) if failed else None
    if failed:
        runtime=dict(runtime,state='FAILED',label='Ветка аварийно остановлена',frozen=True)
        blocks.insert(0,'Отказ ветки: '+str(cause['primary_cause']))
        if cause['transport_cause'] and cause['transport_cause']!=cause['primary_cause']:blocks.insert(1,'Состояние транспорта: '+cause['transport_cause'])
    first_gap=json.loads(meta.get('branch_first_gap_v60','null'))
    if transport_gap and first_gap is None:
        first_gap=dict(reason=branch_failure.get('reason','BRANCH_FAILED'),ts=branch_failure.get('ts'),source='PARENT_TRANSPORT')
    matched=dict(c.execute('SELECT status,COUNT(*) FROM comparison_receipts_v64 GROUP BY status')) if c.execute("SELECT 1 FROM sqlite_master WHERE name='comparison_receipts_v64'").fetchone() else {}
    import monitor_service
    monitor=monitor_service.read_state(primary if role=='primary' else engine.path(primary))
    if monitor.get('status') in ('CLEANUP_PENDING','STOP_WAITING_CHILD_CLEANUP') and (monitor.get('late_creation_v85') or {}).get('pending'):
        blocks.insert(0,'Ожидается подтверждение завершения дочернего процесса; новые проверки и входы заблокированы')
    observed_wall=time.time();heartbeat=monitor.get('heartbeat_ts');oldest=monitor.get('oldest_active_started_ts');active_ids=monitor.get('active_check_ids');ownership=(monitor.get('late_creation_v85') or {}).get('pending')
    # Default meta value '0' means no market data yet, not data from 1970.
    last_market=float(meta.get('trade_last_market_ts') or 0)
    age=observed_wall-last_market if last_market>0 else None
    monitor_status=monitor.get('status')
    # A missing state file before the first start is not a read failure; the raw
    # diagnostic stays in state_read_error. After any start it remains an error.
    if (monitor_status=='STATE_READ_ERROR' and runtime_status.summary(c,now)['state']=='NOT_STARTED' and
            (monitor.get('state_read_error_v86') or {}).get('reason')=='MONITOR_STATE_MISSING'):monitor_status='NOT_STARTED'
    monitor_heartbeat_age=observed_wall-heartbeat if isinstance(heartbeat,(int,float)) else None
    raw_monitor_status=monitor_status
    # Same rule as monitor_service.service_status: a RUNNING claim without a fresh
    # heartbeat (>=16 s or none) is not shown as RUNNING in a frozen/stopped report.
    if monitor_status=='RUNNING' and (monitor_heartbeat_age is None or monitor_heartbeat_age>=16):monitor_status='NO_HEARTBEAT'
    live=dict(loop_heartbeat_age_s=runtime.get('heartbeat_age_s'),market_data_age_s=age,monitor_status=monitor_status,
        monitor_status_raw_v1012=raw_monitor_status,
        monitor_heartbeat_age_s=monitor_heartbeat_age,
        active_checks=len(active_ids) if isinstance(active_ids,list) else None,worker_limit=2,
        oldest_check_age_s=observed_wall-oldest if isinstance(oldest,(int,float)) else None,
        ownership_pending=ownership if type(ownership)==bool else None,
        state_read_error=monitor.get('state_read_error_v86'))
    stressed=None
    if role=='route':stressed=__import__('remainder_reporting_v100').stress(c,__import__('monitor_service').paths(engine.path(primary))['db'],at,cap)
    model=None
    if role=='route':
        wallet_settlements=(dict(r) for r in c.execute('SELECT details FROM branch_amm_settlements ORDER BY settled_ts'))
        model=__import__('cost_classes_v100').wallet_model(wallet_settlements,cap['closed_net'])
        # v0.7.101.6: strict isolated sales before the switch, persistent-wallet sales after it; reported apart.
        split={}
        for r in c.execute("SELECT COALESCE(json_extract(details,'$.accounting.cost_model'),'STRICT_ISOLATED_V59') m,COUNT(*) n,"
                "SUM(CAST(json_extract(details,'$.accounting.pnl_units') AS REAL)) pnl,SUM(CAST(COALESCE(json_extract(details,'$.accounting.strict_pnl_units'),json_extract(details,'$.accounting.pnl_units')) AS REAL)) strict "
                "FROM branch_amm_settlements GROUP BY m"):
            split[r['m']]=dict(closed=r['n'],booked_pnl=r['pnl'],strict_pnl=r['strict'])
        model=dict(model,cost_models_v1016=split)
    risk=__import__('risk_watch_v1014').summary(c,nets,campaign['start_balance'] if campaign else config.START_BALANCE,fork,at,missing)
    return dict(risk_watch_v1014=risk,persistent_wallet_model_v100=model,stressed_equity_v100=stressed,remainder_slot_exclusions_v100=__import__('remainder_reporting_v100').slot_exclusions(c),failure_v93=cause,cost_basis_v89=(__import__('route_branch').COST_MODEL if role=='route' else 'TICK_FEE_MODEL_WITHOUT_ACCOUNT_SETUP'),live_v87=live,runtime_perf=__import__('runtime_control').read(primary if role=='primary' else engine.path(primary)),first_gap=first_gap,resources=dict(cpu=meta.get('process_cpu_pct'),ram_mb=meta.get('process_ram_mb')),role=role,campaign=campaign,runtime=runtime,blocks=blocks,last_entry_result=last,capital=dict(available=cap['available'],cash=cap['net_cash'],principal=cap['open_principal'],status=cap['status']),
        new_closed=new_closed,carried_closed=carried_closed,entries=entries,closed=closed,wins=wins,net=None if missing else net_total,gross=gross,costs=None if missing else fees,
        accounting_missing=missing,closed_drawdown=None if missing else drawdown,positions=positions,trades=list(reversed(trades)),preentry=pending,timing=timing,
        matched_entry_counts=matched,verified_exits=settlements,verified_routes=route_counts,curve_exits=route_counts.get('PUMP_CURVE',0),amm_exits=route_counts.get('PUMP_AMM',0),unknown_route_exits=sum(n for route,n in route_counts.items() if route not in ('PUMP_CURVE','PUMP_AMM')),stop=stop,transport=transport,input_gap=meta.get('branch_input_gap')=='1' or transport_gap,
        stream=dict(state=meta.get('trade_stream_state'),age_s=max(0,at-last_market) if last_market>0 else None))

def roadmap(primary,amm,route=None):
    a=amm if amm and amm.get('role')=='amm' else {};baseline=primary or a;closed=baseline.get('new_closed',baseline.get('closed',0))
    r=route or {};counts=r.get('verified_routes',{})
    return [dict(id=1,title='Самостоятельный проверенный выход',status='IN_PROGRESS',estimate_percent=65,estimate_kind='ENGINEERING_ESTIMATE_NOT_ACCEPTANCE',progress=f"Оценка ~65%; Curve: {counts.get('PUMP_CURVE',0)}; AMM: {counts.get('PUMP_AMM',0)}; новые закрытия: {r.get('new_closed',0)}; перенесённые: {r.get('carried_closed',0)}",
        criterion='В отдельной ROUTE-ветке: свежая симуляция после сигнала, точный объём, расходы и освобождённый слот. Без тика SELL. Нехватка ликвидности остаётся открытым остатком. AMM — отдельная целевая RPC-проверка; историческая проверка не заменяет текущий пул.'),
        dict(id=2,title='Стратегия выдерживает проверку',status='REVIEW' if closed>=100 else 'WAITING',progress=f'{closed}/100 закрытий; результат '+('неизвестен' if a.get('net') is None else f"{a['net']:.4f} PAPER"),
            criterion='После механики: минимум 100 новых закрытий без подгонки порогов, раздельный разбор первых/следующих 50, расходов, просадки и открытых остатков. Отрицательный результат — пересмотр стратегии, а не бесконечное продление теста. 100 сделок не доказывают прибыльность.'),
        dict(id=3,title='Подтверждено реальное исполнение',status='NOT_TESTED',progress='Реальные buy/sell и Jito landing не проверены',criterion='Отдельно согласованный ограниченный тест: подпись, landing/finalized, фактические остатки и расходы, защита от повтора и остановка по лимитам. PAPER не открывает этот этап автоматически.'),
        dict(id=4,title='Ограниченный пилот',status='BLOCKED',progress='До прохождения этапов 1–3',criterion='Только после разбора результатов: заранее согласованные капитал и лимит убытка, малый объём, сверка каждого исполнения. Увеличение депозита не является целью теста механики.')]

def overview(primary=None):
    primary=primary or config.DB;now=time.time()
    with closing(sqlite3.connect(Path(primary).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as c:
        c.execute('BEGIN');p=branch(c,primary,'primary',now)
    target=paper_branch.path(primary);a=None
    if target.exists():
        try:
            with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True,timeout=.5)) as c:
                c.execute('BEGIN');a=branch(c,primary,'amm',now)
            if not p['campaign'] or not a['campaign'] or a['campaign']['id']!=p['campaign']['id']:a=dict(status='CAMPAIGN_MISMATCH')
        except (sqlite3.Error,clean_campaign.CampaignError,ValueError):a=dict(status='BRANCH_REPORT_ERROR')
    route=None
    import route_branch
    if route_branch.path(primary).exists():
        try:
            with closing(sqlite3.connect(route_branch.path(primary).as_uri()+'?mode=ro',uri=True,timeout=.5)) as c:
                c.execute('BEGIN');route=branch(c,primary,'route',now)
                route['verified_routes']=dict(c.execute("SELECT json_extract(details,'$.route'),COUNT(*) FROM branch_amm_settlements GROUP BY json_extract(details,'$.route')"))
                route['exit_decisions']=dict(c.execute('SELECT decision,COUNT(*) FROM route_exit_decisions_v59 GROUP BY decision'))
            if not p['campaign'] or not route['campaign'] or route['campaign']['id']!=p['campaign']['id']:route=dict(status='CAMPAIGN_MISMATCH')
        except (sqlite3.Error,clean_campaign.CampaignError,ValueError):route=dict(status='BRANCH_REPORT_ERROR')
    alerts=[]
    for label,b in [('Основной PAPER',p),('AMM-ветка',a),('Самостоятельный выход',route)]:
        if not b:continue
        if b.get('status')=='BRANCH_REPORT_ERROR':alerts.append(label+': не удалось прочитать базу, откройте диагностику');continue
        if b.get('status')=='CAMPAIGN_MISMATCH':alerts.append(label+': база относится к другому тесту');continue
        if b['positions'] and b['runtime']['state'] in ('STOPPED','FAILED','UNKNOWN'):alerts.append(label+': открытые позиции сейчас не управляются')
        if any(x['exit_overdue'] for x in b['positions']):alerts.append(label+': выход ожидает исполнения — открыть диагностику')
        if b['capital']['status']!='OK' or b['accounting_missing']:alerts.append(label+': требуется сверка учёта')
        if b['input_gap']:alerts.append(label+': потеря локальных данных, новые входы заблокированы')
        if b['runtime']['state'] in ('UNKNOWN','FAILED'):alerts.append(label+': состояние процесса '+b['runtime']['state'])
    rpc_control=__import__('rpc_budget').snapshot()
    if rpc_control['status']=='OUTCOME_READ_BUSY':alerts.append('RPC: список результатов временно недоступен; новые запросы отложены до подтверждённого чтения')
    if rpc_control['status']=='PRIORITY_READ_BUSY':alerts.append('RPC: файл приоритета временно недоступен; запросы отложены до успешного чтения')
    if rpc_control['status']=='PENDING':alerts.append('RPC: результаты ожидают записи; новые запросы ждут обработки журнала')
    if rpc_control['status'] in ('BLOCKED','UNAVAILABLE'):alerts.append('RPC: служебное состояние не подтверждено, новые запросы заблокированы — открыть диагностику')
    progress=__import__('roadmap_progress').build(roadmap(p,a,route),p,a,route,rpc_control)
    # Criteria are rendered from roadmap; repeating the full text in the
    # progress cards costs >4K escaped characters even on an empty campaign.
    stages=progress['stages']
    progress=dict(progress,stages=[{k:v for k,v in stage.items() if k!='criterion'} for stage in stages])
    return dict(amm_branch_enabled_v100=config.AMM_BRANCH_ENABLED,roadmap_progress=progress,rpc_control_v89=rpc_control,version=config.VERSION,generated_ts=now,campaign=p['campaign'],primary=p,amm=a,route=route,alerts=alerts,roadmap=stages,
        resources=__import__('system_health').read_state(primary),note='Три независимых PAPER-сценария. Капитал не складывается; это не реальные средства. Итоги относятся к новой кампании, время работы — к текущему запуску.')

def from_snapshot(snapshot):
    """Embedded file exports use the exact small view saved at export time."""
    return snapshot.get('overview')
