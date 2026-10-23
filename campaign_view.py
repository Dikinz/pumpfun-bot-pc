"""Small read-only operator view. Full diagnostics remain on a separate page."""
import json,sqlite3,time
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
    for r in c.execute('SELECT trade_id,mint,size,opened_ts FROM positions'):
        order=by_mint.get(r['mint']);wait=max(0,at-order['signal_ts']) if order else None
        decision=c.execute('SELECT decision FROM route_exit_decisions_v59 WHERE trade_id=? ORDER BY available_ts DESC LIMIT 1',(r['trade_id'],)).fetchone() if role=='route' else None
        state=decision[0] if decision else ('WAITING_VERIFIED_ROUTE_RESULT' if role=='route' else 'WAITING_VERIFIED_AMM_OR_TICK' if role=='amm' else 'WAITING_FRESH_TICK') if order else 'NO_EXIT_SIGNAL'
        positions.append(dict(r,exit_decision=state,exit_executor='VERIFIED_ROUTE' if role=='route' else 'AMM_OR_TICK' if role=='amm' else 'TICK_BASELINE',age_s=max(0,at-r['opened_ts']),exit_reason=order['reason'] if order else None,exit_wait_s=wait,
            exit_overdue=wait is not None and wait>config.MAX_HOLD))
    new_closed=0;carried_closed=0
    trades=[];all_net=[];missing=0;closed=0;gross=0.;fees=0.
    for r in c.execute("SELECT trade_id,mint,ts,pnl,reason,hold FROM trades WHERE side='SELL' AND id>? ORDER BY id",(fork,)):
        r=dict(r)
        if r['trade_id'] in carried:carried_closed+=1
        else:new_closed+=1
        cost=c.execute('SELECT COUNT(*),SUM(total_cost) FROM capital_ledger_v0721 WHERE trade_id=?',(r['trade_id'],)).fetchone()
        net=r['pnl']-cost[1] if cost[0]==2 and cost[1] is not None else None
        r.update(net=net,cost=cost[1] if net is not None else None);trades.append(r);closed+=1;gross+=r['pnl']
        if net is None:missing+=1
        else:all_net.append(net);fees+=cost[1]
    entries=c.execute("SELECT COUNT(*) FROM trades WHERE side='BUY' AND id>?",(fork,)).fetchone()[0]
    pending=dict(c.execute("SELECT status,COUNT(*) FROM preentry_v0729 GROUP BY status"))
    # Aggregate queue columns only: do not parse full RPC/VM logs for the front page.
    import rpc_timing
    timing={}
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='preentry_timing_v56'").fetchone():
        rows=list(c.execute('SELECT queue_ms,run_ms FROM preentry_timing_v56'))
        timing=dict(queue=rpc_timing.distribution(r[0] for r in rows),runtime=rpc_timing.distribution(r[1] for r in rows))
    settlements=c.execute('SELECT COUNT(*) FROM branch_amm_settlements').fetchone()[0] if c.execute("SELECT 1 FROM sqlite_master WHERE name='branch_amm_settlements'").fetchone() else 0
    cash=campaign['start_balance'] if campaign else config.START_BALANCE;peak=cash;equity=cash;drawdown=0.
    for net in all_net:equity+=net;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
    engine=__import__('route_branch') if role=='route' else paper_branch
    stop=engine.read_lifecycle(primary);transport=engine.read_lifecycle(primary,True)
    if role not in ('amm','route'):stop={};transport={}
    else:
        if stop.get('session')!=meta.get('branch_session'):stop={}
        if transport.get('session')!=meta.get('branch_session'):transport={}
    blocks=diag['entry_blocks'];last=meta.get('last_entry_result_v0722','NO_SIGNAL')
    return dict(runtime_perf=__import__('runtime_control').read(primary if role=='primary' else engine.path(primary)),first_gap=json.loads(meta.get('branch_first_gap_v60','null')),resources=dict(cpu=meta.get('process_cpu_pct'),ram_mb=meta.get('process_ram_mb')),role=role,campaign=campaign,runtime=runtime,blocks=blocks,last_entry_result=last,capital=dict(available=cap['available'],cash=cap['net_cash'],principal=cap['open_principal'],status=cap['status']),
        new_closed=new_closed,carried_closed=carried_closed,entries=entries,closed=closed,wins=sum(x>0 for x in all_net),net=None if missing else sum(all_net),gross=gross,costs=None if missing else fees,
        accounting_missing=missing,closed_drawdown=None if missing else drawdown,positions=positions,trades=trades[-12:][::-1],preentry=pending,timing=timing,
        amm_exits=settlements,stop=stop,transport=transport,input_gap=meta.get('branch_input_gap')=='1',
        stream=dict(state=meta.get('trade_stream_state'),age_s=max(0,at-float(meta.get('trade_last_market_ts',0))) if meta.get('trade_last_market_ts') else None))

def roadmap(primary,amm,route=None):
    a=amm if amm and amm.get('role')=='amm' else {};closed=a.get('closed',0)
    r=route or {};counts=r.get('verified_routes',{})
    return [dict(id=1,title='Самостоятельный проверенный выход',status='IN_PROGRESS',progress=f"Curve: {counts.get('PUMP_CURVE',0)}; AMM: {counts.get('PUMP_AMM',0)}; новые закрытия: {r.get('new_closed',0)}; перенесённые: {r.get('carried_closed',0)}",
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
    return dict(version=config.VERSION,generated_ts=now,campaign=p['campaign'],primary=p,amm=a,route=route,alerts=alerts,roadmap=roadmap(p,a,route),
        resources=__import__('system_health').read_state(primary),note='Три независимых PAPER-сценария. Капитал не складывается; это не реальные средства. Итоги относятся к новой кампании, время работы — к текущему запуску.')

def from_snapshot(snapshot):
    """Embedded file exports use the exact small view saved at export time."""
    return snapshot.get('overview')
