"""Evidence for the offline acceptance wrapper; no trading/process policy changes."""
import hashlib,json,subprocess,threading,time,traceback,uuid,zipfile
from pathlib import Path


def text(value):
    return value.decode('utf-8',errors='replace') if isinstance(value,bytes) else value or ''


HEARTBEAT_S=20  # Console progress cadence only; the suite deadline is the caller's timeout.


def progress_summary(root):
    """Completed tests and last nodeid from the pytest progress journal (partial lines tolerated)."""
    completed=0;last=None
    try:lines=(Path(root)/'progress.jsonl').read_text(encoding='utf-8',errors='replace').splitlines()
    except OSError:return dict(completed=0,last_nodeid=None)
    for line in lines:
        try:x=json.loads(line)
        except (ValueError,RecursionError):continue
        if not isinstance(x,dict):continue
        if x.get('nodeid'):last=x['nodeid']
        if x.get('event')=='TEST_PHASE_END' and x.get('phase')=='teardown':completed+=1
    return dict(completed=completed,last_nodeid=last)


def heartbeat_line(root,elapsed,timeout):
    p=progress_summary(root)
    return ('Recovery check running: %ds / %ds budget, tests completed: %d, last: %s'%(int(elapsed),int(timeout),p['completed'],p['last_nodeid'] or '-')).encode('ascii','replace').decode('ascii')


def _heartbeat(root,start,timeout,stop,interval,out):
    while not stop.wait(interval):
        try:print(heartbeat_line(root,time.monotonic()-start,timeout),file=out(),flush=True)
        except Exception:pass  # Progress output must never change the suite result.


def run(command,*,cwd,env,root,report,timeout=120,heartbeat_s=None,out=None):
    import sys
    start=time.monotonic()
    report.update(suite_timeout_s_v86_1=timeout,timeout_scope_v86_1='ENTIRE_OFFLINE_SUITE',command_v86_1=command)
    stream=out or (lambda:sys.stdout)
    print('Recovery check started: usually about 4-5 minutes, budget %d s. Progress is printed every %d s. Do not press Ctrl+C: it interrupts the check (to copy text, select it first).'%(int(timeout),int(heartbeat_s or HEARTBEAT_S)),file=stream(),flush=True)
    stop=threading.Event()
    beat=threading.Thread(target=_heartbeat,args=(root,start,timeout,stop,heartbeat_s or HEARTBEAT_S,stream),daemon=True,name='recovery-heartbeat')
    beat.start()
    try:
        result=subprocess.run(command,cwd=cwd,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
        report.update(child_returncode=result.returncode,stdout=result.stdout,stderr=result.stderr)
        return result
    except KeyboardInterrupt:
        # subprocess.run has already killed and reaped the direct child; its exit
        # code and captured pipes are not returned. Grandchildren are not certified.
        report.update(child_returncode=None,child_returncode_unavailable_v1012='KILLED_AND_REAPED_BY_SUBPROCESS_RUN_ON_INTERRUPT',
                      interrupted_v1012=True,descendant_cleanup_verified_v86_1=False,
                      progress_at_interrupt_v1012=progress_summary(root))
        raise
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has waited for the direct child after killing it. It does
        # not certify the fate of grandchildren: do not label their cleanup PASS.
        report.update(stdout=text(exc.stdout),stderr=text(exc.stderr),child_returncode=None,
                      timeout_v86_1=True,descendant_cleanup_verified_v86_1=False,
                      timeout_classification_v86_1='SUITE_DEADLINE_EXCEEDED_TEST_CAUSE_UNKNOWN')
        raise
    finally:
        stop.set()
        report['suite_elapsed_s_v86_1']=time.monotonic()-start
        for field in ('stdout','stderr'):
            if field in report:(Path(root)/(field+'.log')).write_text(report[field],encoding='utf-8')


def preserve(root,report,destination):
    """Collect every readable partial artifact independently, including on timeout."""
    root=Path(root);destination=Path(destination)
    progress=[];errors=[];partial={}
    for p in root.iterdir():
        if p.suffix not in ('.json','.jsonl'):continue
        try:
            if p.suffix=='.json':partial[p.name]=json.loads(p.read_text(encoding='utf-8'))
            else:
                values=[]
                for index,line in enumerate(p.read_text(encoding='utf-8').splitlines()):
                    try:values.append(json.loads(line))
                    except (ValueError,RecursionError) as exc:errors.append(dict(path=p.name,line=index+1,error_type=type(exc).__name__,error=str(exc)))
                partial[p.name]=values
            if p.name=='progress.jsonl':progress=partial[p.name]
        except Exception as exc:errors.append(dict(path=p.name,error_type=type(exc).__name__,error=str(exc)))
    report['partial_artifacts_v86_1']=partial
    report['evidence_read_errors_v86_1']=errors
    report['last_test_phase_v86_1']=next((x for x in reversed(progress) if x.get('nodeid')),None)
    report['test_progress_is_partial_v86_1']=not any(x.get('event')=='SESSION_FINISH' for x in progress)
    if report.get('passed'):return
    try:
        destination.mkdir(parents=True,exist_ok=True)
        path=destination/('recovery_failure_v89_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]+'.zip')
        files=[]
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(root.iterdir()):
                if not p.is_file() or (p.suffix not in ('.json','.jsonl','.xml','.log') and p.name!='rpc_concurrency_v972.zip'):continue
                try:
                    raw=p.read_bytes();archive.writestr(p.name,raw)
                    files.append(dict(path=p.name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
                except Exception as exc:errors.append(dict(path=p.name,error_type=type(exc).__name__,error=str(exc)))
            archive.writestr('MANIFEST.json',json.dumps(dict(scope='OFFLINE_TEMPORARY_TEST_EVIDENCE',complete=False,
                suite_timeout_s=report.get('suite_timeout_s_v86_1'),error_type=report.get('error_type'),error=report.get('error'),
                interrupted=report.get('interrupted_v1012') is True,interruption=report.get('interruption_v1012'),
                suite_elapsed_s=report.get('suite_elapsed_s_v86_1'),child_returncode=report.get('child_returncode'),
                descendant_cleanup_verified=report.get('descendant_cleanup_verified_v86_1',False),last_test_phase=report['last_test_phase_v86_1'],files=files,errors=errors),indent=2))
        with zipfile.ZipFile(path) as archive:
            bad=archive.testzip()
            if bad:raise RuntimeError('EVIDENCE_ZIP_CRC_ERROR: '+bad)
        report['failure_evidence_zip_v86_1']=str(path)
    except Exception as exc:
        report['evidence_export_error_v86_1']=dict(error_type=type(exc).__name__,error=str(exc))
