"""Windows 10+ process containment. No PID-based kill and no event-loop OS calls.

Job-list startup assignment prevents the venv redirector from starting outside
the job. A daemon owner thread retains all handles through creation and cleanup.
"""
import asyncio,ctypes,json,os,subprocess,threading,time
from pathlib import Path
C=ctypes
DWORD=C.c_uint32;BOOL=C.c_int32;HANDLE=C.c_void_p;SIZE=C.c_size_t;LPVOID=C.c_void_p

class STARTUPINFO(C.Structure):
    _fields_=[('cb',DWORD),('lpReserved',C.c_wchar_p),('lpDesktop',C.c_wchar_p),('lpTitle',C.c_wchar_p)]+[(n,DWORD) for n in ('dwX','dwY','dwXSize','dwYSize','dwXCountChars','dwYCountChars','dwFillAttribute','dwFlags')]+[('wShowWindow',C.c_uint16),('cbReserved2',C.c_uint16),('lpReserved2',LPVOID),('hStdInput',HANDLE),('hStdOutput',HANDLE),('hStdError',HANDLE)]
class STARTUPINFOEX(C.Structure):_fields_=[('StartupInfo',STARTUPINFO),('lpAttributeList',LPVOID)]
class PROCESSINFO(C.Structure):_fields_=[('hProcess',HANDLE),('hThread',HANDLE),('dwProcessId',DWORD),('dwThreadId',DWORD)]
class BASICLIMIT(C.Structure):
    _fields_=[('PerProcessUserTimeLimit',C.c_int64),('PerJobUserTimeLimit',C.c_int64),('LimitFlags',DWORD),('MinimumWorkingSetSize',SIZE),('MaximumWorkingSetSize',SIZE),('ActiveProcessLimit',DWORD),('Affinity',SIZE),('PriorityClass',DWORD),('SchedulingClass',DWORD)]
class IOLIMIT(C.Structure):_fields_=[(n,C.c_uint64) for n in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
class EXTENDEDLIMIT(C.Structure):_fields_=[('BasicLimitInformation',BASICLIMIT),('IoInfo',IOLIMIT)]+[(n,SIZE) for n in ('ProcessMemoryLimit','JobMemoryLimit','PeakProcessMemoryUsed','PeakJobMemoryUsed')]
class ACCOUNTING(C.Structure):_fields_=[(n,C.c_int64) for n in ('TotalUserTime','TotalKernelTime','ThisPeriodTotalUserTime','ThisPeriodTotalKernelTime')]+[(n,DWORD) for n in ('TotalPageFaultCount','TotalProcesses','ActiveProcesses','TotalTerminatedProcesses')]
class FILETIME(C.Structure):_fields_=[('low',DWORD),('high',DWORD)]

class NativeGroup:
    proof='WINDOWS_JOB_ACCOUNTING'
    atomic_assignment=True
    def __init__(self,args,cwd,logpath,trace=None,identity_callback=None):
        if os.name!='nt':raise RuntimeError('WINDOWS_CHILD_REQUIRES_WINDOWS')
        self.trace=trace;self.identity_callback=identity_callback;self.constructing=True
        if trace:trace.event('SPAWN_NATIVE_SETUP_ENTER_V82')
        import msvcrt
        self.k=C.WinDLL('kernel32',use_last_error=True);self.job=None;self.process=None;self.log=None
        signatures={
          'CreateJobObjectW':([LPVOID,C.c_wchar_p],HANDLE),
          'SetInformationJobObject':([HANDLE,C.c_int,LPVOID,DWORD],BOOL),
          'QueryInformationJobObject':([HANDLE,C.c_int,LPVOID,DWORD,LPVOID],BOOL),
          'TerminateJobObject':([HANDLE,C.c_uint32],BOOL),
          'CloseHandle':([HANDLE],BOOL),'GetCurrentProcess':([],HANDLE),
          'DuplicateHandle':([HANDLE,HANDLE,HANDLE,C.POINTER(HANDLE),DWORD,BOOL,DWORD],BOOL),
          'InitializeProcThreadAttributeList':([LPVOID,DWORD,DWORD,C.POINTER(SIZE)],BOOL),
          'UpdateProcThreadAttribute':([LPVOID,DWORD,SIZE,LPVOID,SIZE,LPVOID,LPVOID],BOOL),
          'DeleteProcThreadAttributeList':([LPVOID],None),
          'CreateProcessW':([C.c_wchar_p,C.c_wchar_p,LPVOID,LPVOID,BOOL,DWORD,LPVOID,C.c_wchar_p,LPVOID,C.POINTER(PROCESSINFO)],BOOL),
          'WaitForSingleObject':([HANDLE,DWORD],DWORD),
          'GetExitCodeProcess':([HANDLE,C.POINTER(DWORD)],BOOL),
          'GetProcessTimes':([HANDLE,C.POINTER(FILETIME),C.POINTER(FILETIME),C.POINTER(FILETIME),C.POINTER(FILETIME)],BOOL)}
        for name,(argspec,result) in signatures.items():f=getattr(self.k,name);f.argtypes=argspec;f.restype=result
        attr=None;initialized=False;duplicates=[]
        try:
            self.job=self.native('CreateJobObjectW',None,None);self.check(self.job)
            limits=EXTENDEDLIMIT();limits.BasicLimitInformation.LimitFlags=0x2000 # KILL_ON_JOB_CLOSE
            self.check(self.native('SetInformationJobObject',self.job,9,C.byref(limits),C.sizeof(limits)))
            self.log=open(logpath,'wb',buffering=0)
            with open(os.devnull,'rb',buffering=0) as null:
                current=self.native('GetCurrentProcess')
                for f in (null,self.log):
                    h=HANDLE();self.check(self.native('DuplicateHandle',current,HANDLE(msvcrt.get_osfhandle(f.fileno())),current,C.byref(h),0,True,2));duplicates.append(h)
            size=SIZE();self.native('InitializeProcThreadAttributeList',None,2,0,C.byref(size))
            if not size.value:raise C.WinError(C.get_last_error())
            attr=C.create_string_buffer(size.value)
            self.check(self.native('InitializeProcThreadAttributeList',attr,2,0,C.byref(size)));initialized=True
            jobs=(HANDLE*1)(self.job);handles=(HANDLE*2)(*duplicates)
            self.check(self.native('UpdateProcThreadAttribute',attr,0,0x2000D,jobs,C.sizeof(jobs),None,None)) # JOB_LIST
            self.check(self.native('UpdateProcThreadAttribute',attr,0,0x20002,handles,C.sizeof(handles),None,None)) # HANDLE_LIST
            si=STARTUPINFOEX();si.StartupInfo.cb=C.sizeof(si);si.StartupInfo.dwFlags=0x100
            si.StartupInfo.hStdInput=duplicates[0];si.StartupInfo.hStdOutput=duplicates[1];si.StartupInfo.hStdError=duplicates[1];si.lpAttributeList=C.cast(attr,LPVOID)
            info=PROCESSINFO();command=C.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in args]))
            if trace:trace.event('SPAWN_NATIVE_CREATEPROCESS_ENTER_V78')
            self.check(self.native('CreateProcessW',str(args[0]),command,None,None,True,0x80000|0x200,None,str(cwd),C.byref(si),C.byref(info)))
            self.process=info.hProcess;self.pid=info.dwProcessId;self.native('CloseHandle',info.hThread)
            birth,end,kernel,user=FILETIME(),FILETIME(),FILETIME(),FILETIME()
            self.check(self.native('GetProcessTimes',self.process,C.byref(birth),C.byref(end),C.byref(kernel),C.byref(user)))
            self.created_ts=((birth.high<<32)|birth.low)/10000000-11644473600
            if self.identity_callback:self.identity_callback(self.pid,self.created_ts,'GET_PROCESS_TIMES_RETURN')
            if trace:trace.identify(self.pid,self.created_ts)
        except BaseException:
            self.close();raise
        finally:
            if initialized:self.native('DeleteProcThreadAttributeList',attr)
            for h in duplicates:self.native('CloseHandle',h)
            self.constructing=False
    def native(self,api,*args):
        trace=getattr(self,'trace',None)
        seq=trace.native_enter(api,expected_probe=api=='InitializeProcThreadAttributeList' and args[0] is None) if trace else None
        function=getattr(self.k,api)
        C.set_last_error(0)
        if trace:trace.native_started(seq)
        try:result=function(*args)
        except BaseException as exc:
            if trace:trace.error('native_call_exception_v82',exc)
            raise
        error=C.get_last_error();ended=time.monotonic();wall=time.time()
        try:
            if api=='CreateProcessW' and result:
                info=C.cast(args[-1],C.POINTER(PROCESSINFO)).contents
                self.process=info.hProcess;self.pid=info.dwProcessId;self.created_ts=None
                if self.identity_callback:self.identity_callback(self.pid,None,'CREATE_PROCESS_RETURN')
            if trace:trace.native_return(seq,api,bool(result) if result is not None else True,error if not result and result is not None else None,ended,wall,identity=(self.pid,None) if api=='CreateProcessW' and result else None)
        finally:C.set_last_error(error) # Diagnostics must not overwrite the API failure code.
        return result
    def check(self,value):
        if not value:raise C.WinError(C.get_last_error())
        return value
    def poll(self):
        status=self.k.WaitForSingleObject(self.process,0)
        if status==258:return None
        if status!=0:raise C.WinError(C.get_last_error())
        code=DWORD();self.check(self.k.GetExitCodeProcess(self.process,C.byref(code)));return code.value
    def active(self):
        a=ACCOUNTING();self.check(self.k.QueryInformationJobObject(self.job,1,C.byref(a),C.sizeof(a),None));return a.ActiveProcesses
    def diagnostic(self):
        # Read through retained kernel handles, never reopen by a possibly reused PID.
        state=dict(launcher_pid=self.pid,launcher_created_ts=self.created_ts,proof=self.proof)
        status=self.k.WaitForSingleObject(self.process,0);state['wait_status']=status
        state['wait_state']={0:'SIGNALED',258:'WAIT_TIMEOUT',0xffffffff:'WAIT_FAILED'}.get(status,'UNKNOWN')
        if status==0xffffffff:state['wait_winerror']=C.get_last_error()
        code=DWORD()
        if self.k.GetExitCodeProcess(self.process,C.byref(code)):state['raw_exit_code']=code.value
        else:state['exit_code_winerror']=C.get_last_error()
        try:state['job_active_processes']=self.active()
        except OSError as exc:state['job_query_winerror']=getattr(exc,'winerror',None)
        return state
    def terminate(self):self.check(self.k.TerminateJobObject(self.job,1))
    def close(self):
        for name in ('job','process'):
            h=getattr(self,name,None)
            if h:
                self.native('CloseHandle',h) if getattr(self,'constructing',False) else self.k.CloseHandle(h)
                setattr(self,name,None)
        if self.log:self.log.close();self.log=None

class BackgroundChild:
    STOP_WAIT_SECONDS=5.2
    """Creation may block in Windows; only this daemon thread enters that API."""
    def __init__(self,args,cwd,logpath,trace,factory=None,retain_cleanup=False):
        self.pid=None;self.returncode=None;self.created_ts=None;self.error=None;self.cleanup_error=None
        self.cleanup_pending=threading.Event();self.cleanup_pending_reason=None
        # Only a persistent monitor registry can safely hand off a timed-out
        # check. Standalone auxiliary callers retain their prior fatal behavior.
        self.retain_delayed_cleanup=retain_cleanup
        self.trace=trace;self.stop_requested=threading.Event();self.spawn_done=threading.Event();self.done=threading.Event();self.factory=factory or (lambda args,cwd,logpath:NativeGroup(args,cwd,logpath,trace,identity_callback=self.observe_identity))
        self.thread=threading.Thread(target=self._run,args=(args,cwd,logpath),daemon=True,name='simulation-owner-v78');self.thread.start()
    def observe_identity(self,pid,created_ts,source):
        self.pid=pid;self.created_ts=created_ts
        # Publish to the owner before any diagnostic file I/O can delay return.
        self.identity_source=source
    def observe_group(self,group,stage):
        try:
            snapshot=group.diagnostic() if hasattr(group,'diagnostic') else dict(launcher_returncode=group.poll(),job_active_processes=group.active(),proof=getattr(group,'proof','INJECTED_TEST_GROUP'))
            self.trace.event(stage,snapshot=snapshot)
        except BaseException as exc:self.trace.event(stage,observation_error=__import__('child_diagnostics').exception(exc))
    def retained_read(self,read):
        last_report=0
        while True:
            try:return read()
            except OSError as exc:
                if not self.cleanup_pending.is_set():raise
                # A transient observation failure cannot resolve uncertainty or
                # authorize closing retained handles. No PID-based recovery.
                if time.monotonic()-last_report>=1:
                    self.trace.error('cleanup_observation_error_v93',exc);last_report=time.monotonic()
                time.sleep(.1)
    def _run(self,args,cwd,logpath):
        group=None;spawn_started=time.monotonic()
        try:
            if self.stop_requested.is_set():return
            if hasattr(self.trace,'spawn_context'):
                try:self.trace.spawn_context(args,cwd)
                except Exception as diagnostic_error:self.trace.event('SPAWN_CONTEXT_UNAVAILABLE_V89',error_type=type(diagnostic_error).__name__)
            group=self.factory(args,cwd,logpath)
            self.pid=group.pid;self.created_ts=group.created_ts
            self.trace.identify(self.pid,self.created_ts)
            self.trace.event('SPAWN_RETURNED_V78',job_assigned_at_creation=getattr(group,'atomic_assignment',False),stop_already_requested=self.stop_requested.is_set(),spawn_ms=(time.monotonic()-spawn_started)*1000)
            self.spawn_done.set()
            while not self.stop_requested.is_set():
                self.returncode=group.poll()
                if self.returncode is not None:break
                self.stop_requested.wait(.02)
        except BaseException as exc:self.error=exc;self.trace.error('worker_exception',exc)
        finally:
            try:
                if group is not None:
                    self.trace.event('CLEANUP_JOB_INSPECT_V78',active_processes=group.active())
                    # Termination is scoped to our kernel job, including exited/reparented wrappers.
                    self.observe_group(group,'JOB_BEFORE_TERMINATE_V80')
                    if group.active():
                        self.trace.event('JOB_TERMINATE_CALL_V80');group.terminate();self.trace.event('JOB_TERMINATE_RETURNED_V80')
                    self.observe_group(group,'JOB_AFTER_TERMINATE_V80')
                    self.trace.event('CLEANUP_WAIT_LAUNCHER',timeout_s=3);start=time.monotonic()
                    deadline=start+3
                    while group.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                    self.returncode=group.poll()
                    if self.returncode is None:
                        if not self.retain_delayed_cleanup:
                            self.observe_group(group,'JOB_LAUNCHER_TIMEOUT_V80')
                            raise RuntimeError('SIMULATION_CHILD_CLEANUP_TIMEOUT')
                        self.cleanup_pending_reason='LAUNCHER_HANDLE_UNSIGNALED';self.cleanup_pending.set()
                        self.observe_group(group,'JOB_LAUNCHER_TIMEOUT_V80')
                        self.trace.event('CLEANUP_OWNERSHIP_PENDING_V93',reason=self.cleanup_pending_reason,original_result_ineligible=True)
                        # Keep the exact process/job handles, not a PID lookup or a
                        # longer success deadline. The event loop may leave this
                        # check, while the registry retains its owner until proof.
                        while self.returncode is None:
                            time.sleep(.05);self.returncode=self.retained_read(group.poll)
                    self.trace.event('CLEANUP_LAUNCHER_EXIT',returncode=self.returncode,wait_ms=(time.monotonic()-start)*1000)
                    self.trace.event('CLEANUP_WAIT_DESCENDANTS',timeout_s=2);deadline=time.monotonic()+2
                    while self.retained_read(group.active) and time.monotonic()<deadline:time.sleep(.02)
                    active=self.retained_read(group.active)
                    if active:
                        if not self.retain_delayed_cleanup:
                            self.observe_group(group,'JOB_DESCENDANTS_TIMEOUT_V80')
                            self.trace.event('CLEANUP_DESCENDANTS_REMAIN',active_processes=active)
                            raise RuntimeError('SIMULATION_CHILD_CLEANUP_TIMEOUT')
                        self.cleanup_pending_reason='JOB_DESCENDANTS_REMAIN';self.cleanup_pending.set()
                        self.observe_group(group,'JOB_DESCENDANTS_TIMEOUT_V80')
                        self.trace.event('CLEANUP_DESCENDANTS_REMAIN',active_processes=active)
                        self.trace.event('CLEANUP_OWNERSHIP_PENDING_V93',reason=self.cleanup_pending_reason,original_result_ineligible=True)
                        while active:
                            time.sleep(.05);active=self.retained_read(group.active)
                    self.observe_group(group,'JOB_CLEANUP_FINAL_V80')
                    self.trace.event('CLEANUP_CONFIRMED',returncode=self.returncode,active_processes=0,proof=getattr(group,'proof','INJECTED_TEST_GROUP'))
                    self.cleanup_pending.clear()
                elif self.error is not None:self.trace.event('SPAWN_FAILED_V78',no_process_returned=True,spawn_ms=(time.monotonic()-spawn_started)*1000)
                else:self.trace.event('SPAWN_CANCELLED_BEFORE_CALL_V78')
            except BaseException as exc:self.cleanup_error=exc;self.trace.error('cleanup_exception',exc)
            finally:
                try:
                    if group is not None:group.close()
                except BaseException as exc:
                    self.cleanup_error=exc;self.trace.event('CLEANUP_HANDLE_CLOSE_FAILED_V78');self.trace.error('cleanup_exception',exc)
                finally:self.spawn_done.set();self.done.set()
    async def spawned(self):
        while not self.spawn_done.is_set():await asyncio.sleep(.02)
        if self.error is not None:raise self.error
    async def wait(self):
        while not self.done.is_set():await asyncio.sleep(.02)
        if self.cleanup_error is not None:raise self.cleanup_error
        if self.error is not None:raise self.error
        return self.returncode
    async def stop(self):
        self.stop_requested.set()
        try:await asyncio.wait_for(self._await_done(),self.STOP_WAIT_SECONDS)
        except asyncio.TimeoutError:
            self.trace.event('CLEANUP_SPAWN_OR_WAIT_PENDING_V78',child_pid=self.pid,thread_alive=self.thread.is_alive())
            raise RuntimeError('SIMULATION_CHILD_CLEANUP_UNCONFIRMED') from None
        if self.cleanup_error is not None:raise self.cleanup_error
    async def _await_done(self):
        while not self.done.is_set():
            if self.cleanup_pending.is_set():raise RuntimeError('SIMULATION_CHILD_CLEANUP_UNCONFIRMED')
            await asyncio.sleep(.02)
