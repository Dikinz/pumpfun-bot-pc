"""Window controller without GUI: state detection (spec 2.1), action matrix (2.4), start/stop progress,
background 05/04, close/tray policy. The GUI (WP3) calls refresh() every POLL_S and the action methods.

Never: kill a process, write anything but a stop file / logs, or contact any port but GET /health.
Dropping a Controller leaves the engine untouched (it was started detached).

- ORPHAN_RUNNING (M1): the ENGINE_LOST conditions while /health answers with this installation's identity AND the
  dashboard process descends from the recorded host (EngineClient.lineage: ppid chain + create_time). The bot
  processes outlived their host and the window cannot stop them: only status, logs, folders and the terminal.
  A dashboard that does not descend from the host (a stale engine_state.json, then 03_START.bat) is EXTERNAL_SAME
  (Ctrl+C in 03_START); so is an undeterminable lineage (AccessDenied...), logged once to stderr/window.log,
  because EXTERNAL_SAME never tells the user to reboot.
- Hysteresis (MINOR 13): EXTERNAL_SAME/ORPHAN_RUNNING fall back to STOPPED/ENGINE_LOST only after
  C.HEALTH_MISS_LIMIT failed /health polls in a row, so one slow answer never enables Start.
- A run that ends by itself (STOPPED, exit_code != 0, no stop requested) is reported once and keeps the tray red
  until acknowledged (MINOR 2). A start is never failed on time while the host lives (MINOR 3)."""
import sys,threading,time
from dataclasses import dataclass
from pathlib import Path
from . import constants as C,actions,logs
from .engine_client import EngineClient,new_token

@dataclass
class Status:
    kind:str
    state:dict
    alive:bool=False
    health:dict|None=None
    busy:str|None=None
    lost_acked:bool=False
    start:dict|None=None
    stop:dict|None=None
    action:dict|None=None
    warning:str|None=None
    self_stop:dict|None=None  # {token, code, text} of an unacknowledged run that ended by itself
    @property
    def own(self):return self.kind.startswith(C.OWN)
    @property
    def phase(self):return self.state.get('phase')
    @property
    def terminal(self):return self.kind in C.TERMINAL_STATES
    @property
    def text(self):return C.STATE_TEXT.get(self.kind,self.kind)
    @property
    def failed(self):return self.kind==C.STOPPED and self.state.get('phase')=='FAILED'

ORPHAN_ALLOWED=('open_results','open_logs','show_log','show_terminal')

def allowed(status,action):
    """Matrix 2.4 -> (True, None) or (False, reason in Russian)."""
    k=status.kind;busy=status.busy
    if action not in C.ACTIONS:return False,'Неизвестное действие.'
    if k==C.ORPHAN_RUNNING:
        # The launcher lived inside the dead host: no graceful stop, no start, no 05/04/02/08 over running bot processes.
        return (True,None) if action in ORPHAN_ALLOWED else (False,C.TEXT['orphan'])
    if action in ('preserve','open_results','open_logs','show_log'):return True,None
    if action=='show_terminal':return (True,None) if status.terminal else (False,'Терминал доступен, когда бот работает.')
    if action=='stop':
        if k==C.OWN_RUNNING:
            return (False,C.TEXT['stopping']) if busy=='stop' else (True,None)
        if k==C.EXTERNAL_SAME:return False,C.TEXT['external_same']
        if k==C.OWN_STOP_REQUESTED:return False,C.TEXT['stopping']
        if k in (C.OWN_PREFLIGHT,C.OWN_STARTING):return False,C.TEXT['starting']
        return False,C.TEXT['not_running']
    def blocked():
        if k==C.EXTERNAL_SAME:return C.TEXT['external_same']
        if k==C.EXTERNAL_FOREIGN:return C.TEXT['external_foreign']
        if status.own:return C.TEXT['running']
        if busy:return C.TEXT['starting'] if busy=='start' else C.TEXT['busy'].format(action=busy)
        return None
    reason=blocked()
    if reason:return False,reason.format(port=status.health.get('_port','') if status.health else '')
    if action=='start' and k==C.ENGINE_LOST and not status.lost_acked:return False,C.TEXT['lost_confirm']
    return True,None

def title(status,version):return C.TITLE.format(version=version or '?',state=status.text)
def tray_tip(status):return C.TRAY_TIP.format(state=status.text)
def tray_state(status):
    """grey stopped, green running, yellow start/stop/action, red FAILED/ENGINE_LOST/ORPHAN_RUNNING/EXTERNAL_FOREIGN
    and an unacknowledged run that ended by itself."""
    if status.kind in (C.ENGINE_LOST,C.ORPHAN_RUNNING,C.EXTERNAL_FOREIGN) or status.failed or status.self_stop:return 'red'
    if status.busy or status.kind in (C.OWN_PREFLIGHT,C.OWN_STARTING,C.OWN_STOP_REQUESTED):return 'yellow'
    if status.kind in (C.OWN_RUNNING,C.EXTERNAL_SAME):return 'green'
    return 'grey'
def close_action(status):
    """X button: hide to tray while bot processes run (own, 03_START, orphans) or an action runs, else quit."""
    return 'hide' if status.own or status.kind in (C.EXTERNAL_SAME,C.ORPHAN_RUNNING) or status.busy else 'quit'
def quit_needs_confirm(status):return status.own or status.kind==C.EXTERNAL_SAME or bool(status.busy)

class Controller:
    def __init__(self,layout,client=None,clock=time.monotonic,engine_cmd=None,popen=None,startfile=None):
        self.layout=layout;self.client=client or EngineClient(layout,popen=popen);self.clock=clock
        self.engine_cmd=engine_cmd or (lambda token:actions.engine_argv(layout,token))
        self._popen=popen;self._startfile=startfile;self._lock=threading.RLock()
        self._start=None;self._stop=None;self._bg=None;self._lost_ack=None;self._lost_seen=None;self._orphan_seen=None
        self._misses=0;self._last_health=None;self._self_stop_acked=set();self._self_stop_seen=None;self._lineage={}
        self.lineage_log=[]
        self.last_action=None;self.events=[];self.status=None

    # ---- detection (2.1) ----
    def refresh(self):
        with self._lock:
            state=self.client.state();health=None;alive=False;polled=True
            if C.engine.active(state):
                alive=self.client.alive(state)
                if alive:
                    kind=C.OWN+state['phase'];polled=self._pending()
                    if polled:health=self.client.health()
                else:
                    health=self.client.health()
                    if self.client.health_kind(health)==C.EXTERNAL_SAME:
                        kind=C.ORPHAN_RUNNING if self._descends(state,health) else C.EXTERNAL_SAME
                    else:kind=C.ENGINE_LOST
            else:
                health=self.client.health()
                kind=self.client.health_kind(health) or C.STOPPED
            if polled:kind,health=self._hysteresis(kind,health)
            if isinstance(health,dict):health=dict(health,_port=self.layout.port)
            status=Status(kind,state,alive,health)
            key=(state.get('token'),state.get('pid'))
            if kind==C.ENGINE_LOST:
                status.lost_acked=self._lost_ack==key
                if self._lost_seen!=key:self._lost_seen=key;self._event('engine_lost',C.TEXT['lost'])
            if kind==C.ORPHAN_RUNNING and self._orphan_seen!=key:self._orphan_seen=key;self._event('orphan',C.TEXT['orphan'])
            self._track_start(status);self._track_stop(status);self._track_action(status);self._track_self_stop(status)
            status.busy=('start' if self._start and not self._start['done'] else 'stop' if self._stop and not self._stop['done'] else
                self._bg.kind if self._bg and self._bg.poll() is None else None)
            name=state.get('log_name')
            if name and logs.too_big(self.layout.logs/name):status.warning=C.TEXT['log_big'].format(name=name)
            self.status=status;return status

    def _descends(self,state,health):
        """Cached per (token, host pid, host create_time, dashboard pid): the answer of one run never changes.
        Undeterminable answers are not cached (retried next poll) but logged only once per key."""
        key=(state.get('token'),state.get('pid'),state.get('create_time'),health.get('pid') if isinstance(health,dict) else None)
        if key in self._lineage and self._lineage[key] is not None:return self._lineage[key]
        verdict,detail=self.client.lineage(state,health)
        if key not in self._lineage:
            line='DESKTOP LINEAGE: '+('orphan' if verdict else 'external (03_START)' if verdict is False else 'undetermined -> external')+': '+str(detail)
            self.lineage_log.append(line)
            if verdict is not True:
                try:print(time.strftime('%Y-%m-%d %H:%M:%S')+' '+line,file=sys.stderr,flush=True)
                except Exception:pass
        self._lineage[key]=verdict if verdict is not None else self._lineage.get(key)
        return verdict is True

    def _hysteresis(self,kind,health):
        """A failed /health keeps EXTERNAL_SAME/ORPHAN_RUNNING until C.HEALTH_MISS_LIMIT misses in a row."""
        prev=self.status.kind if self.status is not None else None
        if health is not None:
            self._misses=0;self._last_health=health;return kind,health
        if prev in C.HEALTH_HELD and kind in C.HEALTH_HELD[prev] and self._last_health is not None:
            self._misses+=1
            if self._misses<C.HEALTH_MISS_LIMIT:return prev,self._last_health
        else:self._misses=0
        return kind,health

    def _pending(self):return any(op and not op['done'] for op in (self._start,self._stop))
    def _event(self,name,payload=None):self.events.append((name,payload))
    def drain_events(self):
        with self._lock:events,self.events=self.events,[];return events

    def _track_start(self,status):
        op=self._start
        if not op or op['done']:status.start=op;return
        st=status.state if status.state.get('token')==op['token'] else {}
        op['elapsed']=self.clock()-op['t0'];op['phase']=st.get('phase')
        op['health_ok']=self.client.health_kind(status.health)==C.EXTERNAL_SAME
        def finish(ok,message,error=None):
            op.update(done=True,ok=ok,message=message,error_code=error,log_tail=None if ok else logs.tail_lines(op['log']))
            self._event('start_done' if ok else 'start_failed',message)
        if st:
            if st['phase']=='FAILED':finish(False,'Запуск не удался: '+str(st.get('error_code')),st.get('error_code'))
            elif st['phase']=='STOPPED':finish(False,'Движок остановился до готовности (код '+str(st.get('exit_code'))+').')
            elif status.kind==C.ENGINE_LOST:finish(False,C.TEXT['lost'])
            elif status.kind==C.ORPHAN_RUNNING:finish(False,C.TEXT['orphan'])
            elif st['phase']=='RUNNING' and op['health_ok']:finish(True,'Бот работает.')
        if not op['done'] and not st:
            code=op['spawned'].exited()
            if code is not None and self.client.state().get('token')==op['token']:code=None  # its state arrived meanwhile: next poll
            if code is not None:finish(False,C.TEXT['already_running'] if code==C.EXIT_ALREADY_RUNNING else C.TEXT['start_exited'].format(code=code))
        # MINOR 3: checks on a big database may take longer; only a dead host, FAILED or STOPPED end the start.
        if not op['done'] and op['elapsed']>C.START_TIMEOUT_S and not op['slow']:
            op['slow']=True;op['message']=C.TEXT['start_slow'];self._event('start_slow',op['message'])
        status.start=op

    def _track_stop(self,status):
        op=self._stop
        if not op or op['done']:status.stop=op;return
        st=status.state if status.state.get('token')==op['token'] else {}
        op['elapsed']=self.clock()-op['t0']
        if st.get('phase') in ('STOP_REQUESTED','STOPPED','FAILED') or st.get('stop_requested_ts'):op['accepted']=True
        if op['accepted'] and status.health is None:op['panel_down']=True
        if st.get('phase') in ('STOPPED','FAILED'):
            code=st.get('exit_code');log=self.layout.logs/st['log_name'] if st.get('log_name') else None
            op.update(done=True,ok=code==0,exit_code=code,panel_down=True,audit=logs.audit_line(log) if log else None,
                message=C.TEXT['stop_ok'] if code==0 else C.TEXT['stop_bad'].format(code=code))
            self._event('stop_done',op['message'])
        elif status.kind in (C.ENGINE_LOST,C.ORPHAN_RUNNING) or (not st and op['accepted']):
            message=C.TEXT['orphan'] if status.kind==C.ORPHAN_RUNNING else C.TEXT['lost']
            op.update(done=True,ok=False,exit_code=None,message=message);self._event('stop_done',op['message'])
        elif op['elapsed']>C.STOP_SLOW_S and not op['slow']:
            op['slow']=True;op['message']=C.TEXT['stop_slow'];self._event('stop_slow',op['message'])  # never kill
        status.stop=op

    def _track_action(self,status):
        if self._bg is None:status.action=self.last_action;return
        result=self._bg.poll()
        if result is None:
            status.action=dict(kind=self._bg.kind,done=False,log=str(self._bg.log),elapsed=self.clock()-self._bg.started)
        else:
            self.last_action=dict(result,done=True);self._bg=None;self._event('action_done',self.last_action);status.action=self.last_action

    def _track_self_stop(self,status):
        """STOPPED with a non-zero code nobody asked for (no stop file of this window, no stop_requested_ts, not a
        start this window already reported as failed): one 'self_stopped' event, red until acknowledged."""
        st=status.state;token=st.get('token');code=st.get('exit_code')
        if status.kind!=C.STOPPED or st.get('phase')!='STOPPED' or code in (0,None) or st.get('stop_requested_ts'):return
        if token in self._self_stop_acked or (self._stop and self._stop['token']==token):return
        if self._start and self._start['token']==token and not self._start.get('ok'):return
        status.self_stop=dict(token=token,code=code,text=C.TEXT['self_stopped'].format(code=code))
        if self._self_stop_seen!=token:self._self_stop_seen=token;self._event('self_stopped',status.self_stop['text'])
    def ack_self_stop(self,token=None):
        """The user has seen it (window shown, status/log opened, new start). Returns the acknowledged token."""
        with self._lock:
            current=self.status.self_stop if self.status is not None else None
            if token is None:token=current['token'] if current else None
            if not C.engine.valid_token(token):return None
            self._self_stop_acked.add(token)
            if current and current['token']==token:self.status.self_stop=None
            return token

    def _current(self):return self.status or self.refresh()
    def can(self,action):
        with self._lock:return allowed(self.refresh(),action)

    # ---- actions (2.3) ----
    def confirm_lost(self):
        with self._lock:
            s=self.refresh()
            if s.kind!=C.ENGINE_LOST:return dict(ok=False,message='Нет предупреждения для подтверждения.')
            self._lost_ack=(s.state.get('token'),s.state.get('pid'));s.lost_acked=True;return dict(ok=True,message=None)

    def start(self):
        with self._lock:
            s=self.refresh();ok,reason=allowed(s,'start')
            if not ok:return dict(ok=False,message=reason)
            if not self.layout.venv_python.is_file():return dict(ok=False,message=C.TEXT['no_venv'])
            token=new_token()
            spawned=self.client.spawn(token,self.engine_cmd(token),actions.engine_env())
            acked=self.ack_self_stop()
            self._start=dict(token=token,spawned=spawned,log=spawned.log,t0=self.clock(),elapsed=0.,phase=None,health_ok=False,
                done=False,ok=None,slow=False,message=None,error_code=None,log_tail=None)
            self._stop=None;self._lost_ack=None
            return dict(ok=True,message=None,token=token,log=str(spawned.log),self_stop_acked=acked)

    def stop(self):
        """Caller has already shown the confirmation (C.TEXT['stop_confirm']). Writes exactly one stop file."""
        with self._lock:
            s=self.refresh();ok,reason=allowed(s,'stop')
            if not ok:return dict(ok=False,message=reason)
            return self._request_stop(s)
    def _request_stop(self,s):
        token=s.state['token'];path=self.client.request_stop(token)
        self._stop=dict(token=token,t0=self.clock(),elapsed=0.,requested=True,accepted=False,panel_down=False,done=False,ok=None,
            exit_code=None,audit=None,slow=False,message=None,file=str(path))
        return dict(ok=True,message=None,file=str(path))
    def stop_for_session_end(self):
        """WM_QUERYENDSESSION (log-off/shutdown): True when Windows must be held, i.e. the own campaign starts,
        runs or stops (C.SESSION_HOLD_STATES). One stop file is written in PREFLIGHT/STARTING/RUNNING (MINOR 4)."""
        with self._lock:
            s=self.refresh()
            if s.kind in C.SESSION_STOP_STATES:
                if not (self._stop and self._stop['token']==s.state.get('token') and not self._stop['done']):self._request_stop(s)
                return True
            return s.kind in C.SESSION_HOLD_STATES

    def run(self,kind):
        """05 (results) or 04 (check) in the background; one at a time."""
        with self._lock:
            s=self.refresh();ok,reason=allowed(s,kind)
            if not ok:return dict(ok=False,message=reason)
            if not self.layout.venv_python.is_file():return dict(ok=False,message=C.TEXT['no_venv'])
            self._bg=actions.Background(self.layout,kind,popen=self._popen,clock=self.clock).start();self.last_action=None
            return dict(ok=True,message=None,log=str(self._bg.log))
    def open_console(self,kind):
        with self._lock:
            ok,reason=allowed(self.refresh(),kind)
            if not ok:return dict(ok=False,message=reason)
            if not self.layout.venv_python.is_file():return dict(ok=False,message=C.TEXT['no_venv'])
            actions.open_console(self.layout,kind,popen=self._popen);return dict(ok=True,message=None)
    def open_folder(self,which):
        folder={'open_results':self.layout.results,'open_logs':self.layout.logs}[which]
        error=actions.open_folder(folder,self._startfile);return dict(ok=error is None,message=error)
    def latest_log(self):
        """Log shown by 'Журнал': running/last action first, else the engine log of the state file."""
        with self._lock:
            if self._bg is not None:return self._bg.log
            if self._start and not self._start['done']:return self._start['log']
            if self.last_action:return Path(self.last_action['log'])
            name=self._current().state.get('log_name')
            return self.layout.logs/name if name else None

    def snapshot(self):
        """JSON-safe view for status.html (js_api get_status)."""
        with self._lock:
            s=self.refresh();state=s.state
            clean=lambda op:None if op is None else {k:v for k,v in op.items() if k not in ('spawned',)} | ({'log':str(op['log'])} if op.get('log') else {})
            return dict(kind=s.kind,text=s.text,phase=s.phase,paper=True,version=self.layout.version,port=self.layout.port,busy=s.busy,
                tray=tray_state(s),title=title(s,self.layout.version),lost=C.TEXT['lost'] if s.kind==C.ENGINE_LOST else None,lost_acked=s.lost_acked,
                orphan=C.TEXT['orphan'] if s.kind==C.ORPHAN_RUNNING else None,self_stop=s.self_stop['text'] if s.self_stop else None,
                last=dict(phase=state.get('phase'),exit_code=state.get('exit_code'),error_code=state.get('error_code'),
                    finished_ts=state.get('finished_ts'),started_ts=state.get('started_ts'),log_name=state.get('log_name')),
                allowed={a:list(allowed(s,a)) for a in C.ACTIONS},start=clean(s.start),stop=clean(s.stop),action=s.action,warning=s.warning)
