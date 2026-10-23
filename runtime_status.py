"""Runtime display; never controls orders or connectivity."""
import time,math
ORIGIN=None

def put(c,k,v):c.execute('INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,str(v)))

def begin(c):
    global ORIGIN
    ORIGIN=time.monotonic()
    put(c,'runtime_started_ts_v41',time.time());put(c,'runtime_elapsed_s_v41',0)
    put(c,'runtime_state_v41','STARTING');put(c,'runtime_updated_ts_v41',time.time())

def beat(c,stopped=False):
    if ORIGIN is None:return
    put(c,'runtime_elapsed_s_v41',max(0,time.monotonic()-ORIGIN))
    put(c,'runtime_updated_ts_v41',time.time());put(c,'runtime_state_v41','STOPPED' if stopped else 'RUNNING')

def summary(c,now=None):
    now=time.time() if now is None else now;m=dict(c.execute('SELECT k,v FROM meta'))
    started=float(m.get('runtime_started_ts_v41',0));updated=float(m.get('runtime_updated_ts_v41',0))
    elapsed=float(m.get('runtime_elapsed_s_v41',0));state=m.get('runtime_state_v41','NOT_STARTED')
    if not started:state='NOT_STARTED'
    elif state not in ('STOPPED','FAILED') and not 0<=now-updated<=15:state='UNKNOWN'
    return dict(state=state,label={'NOT_STARTED':'Ещё не запущен','STARTING':'Запускается','RUNNING':'Работает','STOPPED':'Остановлен','FAILED':'Завершён с ошибкой','UNKNOWN':'Нет сигнала активности — состояние неизвестно'}.get(state,'Состояние неизвестно'),
      elapsed_s=elapsed if math.isfinite(elapsed) and elapsed>=0 else 0,started_ts=started or None,updated_ts=updated or None,
      heartbeat_age_s=now-updated if updated else None,frozen=state not in ('RUNNING','STARTING'),
      note='Elapsed monotonic time recorded by bot heartbeat, refreshed with dashboard data. Stop freezes final duration; missing heartbeat freezes last confirmed duration. Process activity does not mean market data are eligible.')
