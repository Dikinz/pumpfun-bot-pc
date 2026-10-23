"""Read-only full-cohort diagnostics. A warning never changes an order or balance."""
import math, time
import system_health

def summary(c, snapshot, primary):
    user_stopped=system_health.apply_lifecycle(snapshot,primary)
    issues=[]
    def issue(severity,code,detail):issues.append(dict(severity=severity,code=code,detail=detail))
    capital=snapshot.get('capital',{})
    if capital.get('status')=='ERROR':issue('CRITICAL','CAPITAL_RECONCILIATION','Помилка звірки капіталу: '+str(len(capital.get('errors',[]))))
    monitor=snapshot.get('monitor',{});health=monitor.get('health',{});connection=monitor.get('connection',{})
    if health.get('technical_errors',0):issue('WARNING','MONITOR_TECHNICAL_ERRORS',str(health['technical_errors']))
    if connection.get('warning'):issue('CRITICAL','MONITOR_ERROR_STREAK','Послідовні технічні помилки монітора')
    if connection.get('gaps',{}).get('total_position_seconds',0)>0:
        issue('WARNING','MONITOR_COVERAGE_GAPS','Частина часу позицій не мала своєчасної перевірки продажу')
    if snapshot.get('positions') and monitor.get('service',{}).get('status') not in ('RUNNING','STOPPED'):
        issue('CRITICAL','OPEN_POSITIONS_WITHOUT_MONITOR','Є відкриті позиції; активність монітора не підтверджена')
    if user_stopped:
        issue('INFO','USER_STOP','Бот остановлен пользователем')

    runtime=snapshot.get('runtime',{})
    if runtime.get('state')=='STOPPED' and snapshot.get('positions'):
        issue('CRITICAL','STOPPED_WITH_OPEN_POSITIONS','Бот остановлен; открытые позиции не управляются')
    if runtime.get('state') in ('UNKNOWN','FAILED'):issue('CRITICAL','PRIMARY_HEARTBEAT_UNKNOWN','Немає свіжого heartbeat основного процесу')
    state=system_health.read_state(primary)
    age=time.time()-state.get('heartbeat_ts',0) if state else None
    if state and state.get('status')=='RUNNING' and (age>15 or age < -2):
        issue('WARNING','RECORDER_HEARTBEAT_INVALID','Немає свіжого heartbeat незалежного реєстратора')
    for code in state.get('alerts',[]):issue('WARNING',code,'Остання вибірка незалежного реєстратора')
    if state.get('status')=='FAILED':issue('CRITICAL','PREVIOUS_LAUNCH_FAILED',state.get('stop_reason',''))
    # Aggregate the complete ledger once, then join to SELL; never duplicate a
    # sell through two fee rows. Missing costs remain unknown, not zero.
    rows=[dict(r) for r in c.execute('''
      WITH fees AS (SELECT trade_id,COUNT(*) n,SUM(total_cost) cost
                    FROM capital_ledger_v0721 GROUP BY trade_id)
      SELECT t.trade_id,t.mint,t.reason,t.pnl gross,t.hold,t.size,f.n fee_rows,
             f.cost,t.pnl-f.cost net
      FROM trades t LEFT JOIN fees f ON f.trade_id=t.trade_id
      WHERE t.side='SELL' ORDER BY t.id''')]
    valid=[r for r in rows if r['fee_rows']==2 and all(isinstance(r[x],(int,float)) and math.isfinite(r[x]) for x in ('gross','net','cost'))]
    if len(valid)!=len(rows):issue('CRITICAL','MISSING_OR_INVALID_CLOSED_COSTS',str(len(rows)-len(valid)))
    groups={}
    for r in valid:
        g=groups.setdefault(r['reason'],dict(reason=r['reason'],count=0,wins=0,gross=0.,cost=0.,net=0.,hold_total_s=0.))
        for key in ('gross','cost','net'):g[key]+=r[key]
        g['count']+=1;g['wins']+=r['net']>0;g['hold_total_s']+=r['hold'] or 0
    net=sum(r['net'] for r in valid);gross=sum(r['gross'] for r in valid);cost=sum(r['cost'] for r in valid)
    positives=sum(max(0,r['net']) for r in valid);losses=-sum(min(0,r['net']) for r in valid)
    curve=0.;peak=0.;drawdown=0.
    for r in valid:curve+=r['net'];peak=max(peak,curve);drawdown=max(drawdown,peak-curve)
    if rows and len(rows)<100:issue('INFO','SMALL_SAMPLE','Мала вибірка: '+str(len(rows))+' закритих угод; поріг 100 є лише діагностичним')
    if valid and gross>0 and net<0:issue('WARNING','COSTS_ERASE_GROSS','Модельні витрати перевищують торговий результат')
    pipeline=snapshot.get('entry_pipeline',{});signal=monitor.get('signal_exit',{}).get('cohort',{})
    delta=signal.get('matched_closed',{}).get('delta')
    if isinstance(delta,(int,float)) and math.isfinite(delta) and delta<0:
        issue('WARNING','SIGNAL_EXIT_WORSE','Експериментальний вихід погіршив результат спільної вибірки')
    audit=monitor.get('cost_price_audit',{});pairs=[]
    for row in audit.get('rows',[]):
        p=row.get('pool',{});before=p.get('sale_before');after=p.get('sale_after')
        pairs.append(dict(mint=row['mint'],before_gap_s=before.get('delta_to_tick_s') if before else None,
                          after_gap_s=after.get('delta_to_tick_s') if after else None))
    report=dict(schema='SYSTEM_STRATEGY_DIAGNOSTICS_V1',diagnostic_only=True,
      status='CRITICAL' if any(i['severity']=='CRITICAL' for i in issues) else 'WARNING' if any(i['severity']=='WARNING' for i in issues) else 'OK',
      issues=issues,recorder=state,recorder_age_s=age,recorder_available=bool(state),
      strategy=dict(closed=len(rows),accounted=len(valid),wins=sum(r['net']>0 for r in valid),
        losses=sum(r['net']<0 for r in valid),gross=gross,cost=cost,net=net,
        mean_net=net/len(valid) if valid else None,profit_factor=positives/losses if losses else None,
        max_closed_drawdown=drawdown,by_reason=list(groups.values()),orders=pipeline.get('status_counts',{}),
        preflight=pipeline.get('preflight_counts',{}),exit_pair_coverage=pairs),
      note='Уся база: лише закриті угоди з двома записами витрат. Просідання — за послідовністю закриттів, без оцінки відкритих позицій. Моделі PAPER та BASE різні. Діагностика не є дозволом live.')
    return report
