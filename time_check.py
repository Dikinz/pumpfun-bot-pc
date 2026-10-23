"""Read-only device time precheck. Never corrects time or changes entry tolerance."""
import asyncio,json,math,os,subprocess,time,uuid,statistics,datetime,sys,shutil,zipfile
from pathlib import Path
import clock_lab,config
APP=Path(__file__).resolve().parent
SAMPLES=3
DIAGNOSTIC_OFFSET_MS=50.
MAX_UNCERTAINTY_MS=50.
REPORT_MAX_AGE_S=900.
COMMAND_TIMEOUT_S=5
LABELS={'PASS':'Проверка часов пройдена в пределах диагностических ±50 мс',
 'OFFSET_DETECTED':'Обнаружено расхождение часов больше 50 мс',
 'UNCERTAIN':'Точности или согласованности измерений недостаточно',
 'UNAVAILABLE':'Внешний источник времени недоступен',
 'NOT_CHECKED':'Проверка перед запуском ещё не выполнялась',
 'INVALID_REPORT':'Сохранённый отчёт не удалось прочитать'}
POWERSHELL="[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; try { $s=Get-Service -Name W32Time -ErrorAction Stop; @{status=$s.Status.ToString();start_type=$s.StartType.ToString()} | ConvertTo-Json -Compress } catch { @{status='UNKNOWN';error_type=$_.Exception.GetType().Name} | ConvertTo-Json -Compress; exit 1 }"

def finite(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
def data_dir():
    p=Path(config.DB)
    if not p.is_absolute():p=APP/p
    return p.resolve().parent

def decode_output(raw,encoding=None):
    if isinstance(raw,str):return raw
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')):return raw.decode('utf-16',errors='replace')
    if encoding is None:
        if os.name=='nt':
            import ctypes
            encoding='cp'+str(ctypes.windll.kernel32.GetOEMCP())
        else:encoding='utf-8'
    return raw.decode(encoding,errors='replace').lstrip('\ufeff')

def command(args,encoding=None):
    try:
        p=subprocess.run(args,capture_output=True,timeout=COMMAND_TIMEOUT_S,check=False,shell=False)
        return dict(status='OK' if p.returncode==0 else 'COMMAND_FAILED',returncode=p.returncode,
          stdout=decode_output(p.stdout,encoding).strip(),stderr=decode_output(p.stderr,encoding).strip())
    except subprocess.TimeoutExpired:return dict(status='TIMEOUT',stdout='',stderr='')
    except OSError as e:return dict(status='UNAVAILABLE',error_type=type(e).__name__,stdout='',stderr='')

def last_sync_text(text):
    # Keep locale-specific date text verbatim. Never guess date order or timezone.
    names=('last successful sync time','время последней успешной синхронизации','час останньої успішної синхронізації')
    for line in text.splitlines():
        if ':' in line:
            key,value=line.split(':',1)
            if key.strip().casefold() in names:return value.strip()
    return None

def windows_info(platform_name=None):
    if (os.name if platform_name is None else platform_name)!='nt':return dict(status='NOT_WINDOWS',service={'status':'NOT_WINDOWS'},last_successful_sync_text=None)
    svc=command(['powershell.exe','-NoProfile','-NonInteractive','-Command',POWERSHELL],'utf-8')
    try:service=json.loads(svc['stdout']) if svc['status']=='OK' else {'status':'UNKNOWN'}
    except (ValueError,TypeError):service={'status':'UNKNOWN'}
    state=command(['w32tm.exe','/query','/status','/verbose'])
    source=command(['w32tm.exe','/query','/source'])
    return dict(status='OK' if svc['status']==state['status']==source['status']=='OK' else 'PARTIAL_OR_UNAVAILABLE',
      service=service,service_query=svc,w32tm_status=state,source=source.get('stdout') if source['status']=='OK' else None,
      source_query=source,last_successful_sync_text=last_sync_text(state['stdout']) if state['status']=='OK' else None,
      last_sync_note='Original localized date text, not parsed. Unknown labels remain in raw w32tm output. A previous sync does not prove present millisecond accuracy.')

def assess(samples,clock_step_ms=0):
    good=[r for r in samples if r.get('status')=='MEASURED' and finite(r.get('offset_ms')) and finite(r.get('uncertainty_ms')) and 0<=r['uncertainty_ms']<=MAX_UNCERTAINTY_MS]
    metrics=dict(usable=len(good),requested=SAMPLES,offset_median_ms=statistics.median(r['offset_ms'] for r in good) if good else None,
      uncertainty_max_ms=max((r['uncertainty_ms'] for r in good),default=None),diagnostic_offset_ms=DIAGNOSTIC_OFFSET_MS,uncertainty_limit_ms=MAX_UNCERTAINTY_MS,local_step_ms=clock_step_ms)
    if not samples or all(r.get('status') in ('UNAVAILABLE','SERVER_REFUSAL') for r in samples):status='UNAVAILABLE'
    elif len(good)!=SAMPLES or not finite(clock_step_ms) or abs(clock_step_ms)>20:status='UNCERTAIN'
    else:
        bounds=[(r['offset_ms']-r['uncertainty_ms'],r['offset_ms']+r['uncertainty_ms']) for r in good]
        metrics.update(offset_envelope_low_ms=min(a for a,b in bounds),offset_envelope_high_ms=max(b for a,b in bounds))
        # Every usable interval must satisfy the decision; no averaging away noise.
        if max(a for a,b in bounds)>min(b for a,b in bounds):status='UNCERTAIN'
        elif all(-DIAGNOSTIC_OFFSET_MS<=a and b<=DIAGNOSTIC_OFFSET_MS for a,b in bounds):status='PASS'
        elif all(a>DIAGNOSTIC_OFFSET_MS for a,b in bounds) or all(b< -DIAGNOSTIC_OFFSET_MS for a,b in bounds):status='OFFSET_DETECTED'
        else:status='UNCERTAIN'
    return dict(status=status,label=LABELS[status],**metrics)

async def collect():
    start=clock_lab.pair();win_task=asyncio.create_task(asyncio.to_thread(windows_info));samples=[]
    for i in range(SAMPLES):
        try:r=await asyncio.wait_for(clock_lab.probe(),12)
        except asyncio.TimeoutError:r={'status':'UNAVAILABLE','error_type':'ProbeTimeout'}
        r=dict(r,measured_ts=time.time());samples.append(r)
        # Respect refusal/rate limiting; no additional probes this run.
        if r.get('status')=='SERVER_REFUSAL':break
        if i+1<SAMPLES:await asyncio.sleep(1)
    end=clock_lab.pair();step=((end[0]-start[0])-(end[1]-start[1]))*1000
    windows=await win_task;decision=assess(samples,step)
    return dict(schema='device_time_check_v0744',id=uuid.uuid4().hex,created_ts=time.time(),local_time=datetime.datetime.now().astimezone().isoformat(),
      version=config.VERSION,windows=windows,reference=decision,samples=samples,source=clock_lab.HOST,clock_modified=False,
      active_tolerance_ms=0,market_stream_checked=False,
      note='Diagnostic ±50 ms only; does not change trading tolerance. SNTP uncertainty is an estimate, not a guaranteed bound; one unauthenticated source. PASS does not prove provider timestamps are valid. Market event freshness is checked separately after connection.')

def atomic_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)

def format_report(r):
    ref=r['reference'];w=r['windows'];lines=[f"TIME CHECK {r['version']}",r['local_time'],ref['label'],
      f"Источник: {r['source']}; пригодных измерений: {ref['usable']}/{SAMPLES}",
      f"Медиана смещения: {ref['offset_median_ms']} мс; максимальная оценка погрешности: ±{ref['uncertainty_max_ms']} мс",
      'Плюс: внешний источник опережает местные часы.',
      'W32Time: '+str(w.get('service',{}).get('status','UNKNOWN'))+'; тип запуска: '+str(w.get('service',{}).get('start_type','UNKNOWN')),
      'Источник Windows: '+str(w.get('source') or 'не удалось получить отдельным запросом; см. исходный статус Windows ниже'),
      'Последняя успешная синхронизация (исходный текст Windows): '+str(w.get('last_successful_sync_text') or 'не удалось определить; см. вывод ниже')]
    for i,s in enumerate(r['samples'],1):lines.append(f"Замер {i}: {s.get('status')}; offset={s.get('offset_ms')} ms; uncertainty={s.get('uncertainty_ms')} ms; RTT={s.get('rtt_ms')} ms")
    if w.get('status')!='OK':lines.append('Данные службы Windows получены не полностью. Ошибка доступа/отсутствие службы не доказывает неверное время.')
    for k in ('w32tm_status','source_query','service_query'):
        if k in w:lines.extend([k+': '+w[k]['status'],w[k].get('stdout',''),w[k].get('stderr','')])
    lines+=['Часы не изменялись. Торговый допуск остаётся 0 мс. Поток рынка ещё не проверен.',
      'PASS — диагностическая оценка момента проверки, не гарантия свежести будущих событий.',
      'Если результат не PASS: проверьте синхронизацию Windows при остановленном боте, затем повторите 04_CHECK.']
    return '\n'.join(lines)

def save(r):
    directory=data_dir();atomic_json(directory/'time_check_history'/f"{r['id']}.json",r);atomic_json(directory/'time_check.json',r)
    (directory/'time_check.txt').write_text(format_report(r),encoding='utf-8')

def latest(now=None):
    now=time.time() if now is None else now
    try:r=json.loads((data_dir()/'time_check.json').read_text(encoding='utf-8'))
    except FileNotFoundError:return dict(state='NOT_CHECKED',label=LABELS['NOT_CHECKED'],stale=True)
    except (ValueError,OSError):return dict(state='INVALID_REPORT',label=LABELS['INVALID_REPORT'],stale=True)
    if not isinstance(r,dict) or r.get('schema')!='device_time_check_v0744' or not finite(r.get('created_ts')) or not isinstance(r.get('reference'),dict):return dict(state='INVALID_REPORT',label=LABELS['INVALID_REPORT'],stale=True)
    age=now-r['created_ts'];return dict(state=r['reference'].get('status','UNCERTAIN'),label=r['reference'].get('label',''),age_s=age,stale=not 0<=age<=REPORT_MAX_AGE_S,report=r)

def capture_start():
    data=latest();atomic_json(data_dir()/'time_check_at_start.json',dict(captured_ts=time.time(),check=data));return data

async def startup():
    """Always probe now. Old PASS reports cannot authorize a new process."""
    print('Checking device time NOW, before starting trading...',flush=True)
    r=await collect();save(r);capture_start();print(format_report(r),flush=True)
    attempt=dict(id=r['id'],created_ts=r['created_ts'],status='READY' if r['reference']['status']=='PASS' else 'BLOCKED_TIME',check=r)
    atomic_json(data_dir()/'startup_attempts'/f"{r['id']}.json",attempt)
    if r['reference']['status']!='PASS':
        raise RuntimeError('START BLOCKED: time check '+r['reference']['status']+'. Existing positions are NOT managed by this stopped bot. Correct/check Windows synchronization, then run 03_START again. See app/data/time_check.txt.')
    return r


def export(folder):
    folder=Path(folder);directory=data_dir()
    for name in ('time_check.json','time_check.txt','time_check_at_start.json'):
        if (directory/name).exists():shutil.copy2(directory/name,folder/name)
    attempts=directory/'startup_attempts'
    if attempts.exists():
        with zipfile.ZipFile(folder/'startup_attempts.zip','w',zipfile.ZIP_DEFLATED) as z:
            for p in sorted(attempts.glob('*.json')):z.write(p,p.name)
    history=directory/'time_check_history'
    if history.exists():
        with zipfile.ZipFile(folder/'time_check_history.zip','w',zipfile.ZIP_DEFLATED) as z:
            for p in sorted(history.glob('*.json')):z.write(p,p.name)


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    r=asyncio.run(collect());save(r);print(format_report(r));print('Report:',data_dir()/'time_check.txt')
    return 0 if r['reference']['status']=='PASS' else 2

if __name__=='__main__':
    import sys
    raise SystemExit(main())
