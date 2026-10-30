"""Read-only launch evidence. Optional Windows event metadata, no messages.
PowerShell timeouts start after OS process creation; they cannot cancel a
CreateProcessW call blocked in the Windows kernel. Never changes privileges.
"""
import argparse,json,math,os,subprocess,time
from datetime import datetime,timezone,timedelta
from pathlib import Path
APP=Path(__file__).resolve().parent
LOGS=('Microsoft-Windows-Windows Defender/Operational','Microsoft-Windows-CodeIntegrity/Operational','Microsoft-Windows-AppLocker/EXE and DLL')

def read_traces(app=APP):
    rows=[];errors=[];scanned=0
    for name in ('monitor_evidence','preentry_evidence','shadow_preflight_evidence','auto_simulations'):
        directory=Path(app)/name
        if not directory.exists():continue
        for p in directory.glob('*/child_lifecycle_v73.json'):
            scanned+=1
            if scanned>2000:break
            try:
                if p.stat().st_size>262144:continue
                d=json.loads(p.read_text(encoding='utf-8'))
                native=next((r for r in reversed(d.get('native_calls_v82',[])) if isinstance(r,dict) and r.get('api')=='CreateProcessW'),None)
                if native is None:continue
                if native.get('state')=='RETURNED' and native.get('ok') is True and native.get('elapsed_ms',0)<1000:continue
                rows.append(dict(check_id=d.get('check_id'),source=name,monitor_pid=d.get('monitor_pid'),child_pid=d.get('child_pid'),phase=d.get('phase'),ts=d.get('ts'),native_call=native,context=d.get('spawn_context_v89'),observation=d.get('native_create_v89')))
            except (OSError,ValueError,TypeError,AttributeError) as exc:errors.append(dict(source=name,error_type=type(exc).__name__))
        if scanned>2000:break
    rows.sort(key=lambda r:r['ts'] if type(r.get('ts')) in (int,float) and math.isfinite(r['ts']) else 0,reverse=True)
    return dict(rows=rows[:40],scan_limit=2000,scanned=min(scanned,2000),partial=scanned>2000,read_errors=errors[:10])

def windows_events(start,end):
    if os.name!='nt':return dict(status='NOT_WINDOWS',logs=[])
    logs=[]
    for name in LOGS:
        script="$ErrorActionPreference='Stop'; try { $r=@(Get-WinEvent -FilterHashtable @{LogName='"+name+"';StartTime=[datetime]'"+start+"';EndTime=[datetime]'"+end+"'} -MaxEvents 20 | Select-Object Id,@{Name='utc';Expression={$_.TimeCreated.ToUniversalTime().ToString('o')}},ProviderName); ConvertTo-Json -InputObject $r -Compress } catch { @{status='UNAVAILABLE';category=$_.CategoryInfo.Category.ToString()} | ConvertTo-Json -Compress }"
        try:
            proc=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
            if len(proc.stdout)>32768:raise ValueError('oversize')
            data=json.loads(proc.stdout.decode('utf-8-sig'))
            if isinstance(data,list):logs.append(dict(log=name,status='QUERIED',events=[{k:r.get(k) for k in ('Id','utc','ProviderName')} for r in data if isinstance(r,dict)]))
            else:logs.append(dict(log=name,status='UNAVAILABLE',category=data.get('category') if isinstance(data,dict) else None))
        except Exception as exc:logs.append(dict(log=name,status='UNAVAILABLE',error_type=type(exc).__name__))
    return dict(status='METADATA_ONLY',logs=logs,timeout_scope='5_SECONDS_PER_LOG_AFTER_POWERSHELL_CREATION')

def write_report(path,include_events=True,app=APP):
    traces=read_traces(app);now=datetime.now(timezone.utc)
    latest=next((r['native_call'].get('entered_ts') for r in traces['rows'] if type(r['native_call'].get('entered_ts')) in (int,float) and math.isfinite(r['native_call']['entered_ts']) and 0<=r['native_call']['entered_ts']<=time.time()+86400),None)
    center=datetime.fromtimestamp(latest,timezone.utc) if latest is not None else now
    start=(center-timedelta(minutes=2)).isoformat();end=(center+timedelta(minutes=5)).isoformat()
    report=dict(schema='SPAWN_DIAGNOSTICS_V89',created_utc=now.isoformat(),read_only=True,root_cause_fixed=False,security_product_cause='UNKNOWN',traces=traces,events=windows_events(start,end) if include_events else dict(status='NOT_REQUESTED'),note='WinError 5 is access denied; no attribution to antivirus, ACL or policy without corroborating evidence. No privilege elevation, exclusions, retries or process flags are changed.')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path);return report

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=APP.parent/'results/spawn_diagnostics_v89.json');p.add_argument('--no-events',action='store_true');args=p.parse_args(argv)
    report=write_report(args.output,include_events=not args.no_events)
    print('SPAWN DIAGNOSTICS: recorded incidents',len(report['traces']['rows']));print('Report:',args.output);return 0
if __name__=='__main__':raise SystemExit(main())
