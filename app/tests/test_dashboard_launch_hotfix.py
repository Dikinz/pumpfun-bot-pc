import io,json,os,socket,subprocess,sys,time
from pathlib import Path
from contextlib import contextmanager
import pytest
import config,system_health

def test_redirected_pid_with_valid_launch_token_is_accepted(monkeypatch):
    import launcher
    class Redirector:
        pid=101
        def poll(self):return None
    @contextmanager
    def response(*a,**k):
        yield io.BytesIO(json.dumps(dict(version=config.VERSION,
            database_identity=system_health.identity(config.DB),pid=202,launch_id='our-launch')).encode())
    monkeypatch.setattr('urllib.request.urlopen',response)
    launcher.wait_dashboard(Redirector(),launch_id='our-launch')

def test_same_version_database_and_pid_cannot_replace_launch_token(monkeypatch,tmp_path):
    import launcher
    monkeypatch.setattr(config,'DB',str(tmp_path/'test.db'))
    class Child:
        pid=101
        def poll(self):return None
    @contextmanager
    def response(*a,**k):
        yield io.BytesIO(json.dumps(dict(version=config.VERSION,
            database_identity=system_health.identity(config.DB),pid=101,launch_id='previous-launch')).encode())
    monkeypatch.setattr('urllib.request.urlopen',response)
    with pytest.raises(RuntimeError,match=r'identity mismatch \(launch_token\)'):
        launcher.wait_dashboard(Child(),launch_id='our-launch')
    event=system_health.history(config.DB)[-1]
    assert event['fields']==['launch_token'] and 'our-launch' not in json.dumps(event)

def test_real_dashboard_health_token_with_redirector_pid(tmp_path,monkeypatch):
    import launcher
    app=Path(__file__).resolve().parents[1]
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    monkeypatch.setattr(config,'DB',str(tmp_path/'test.db'))
    monkeypatch.setattr(config,'PORT',port)
    env=dict(os.environ,DATABASE_PATH=config.DB,DASHBOARD_PORT=str(port),PUMPFUN_DASHBOARD_LAUNCH_ID='integration-launch')
    with (tmp_path/'dashboard.log').open('w+') as log:
        p=subprocess.Popen([sys.executable,str(app/'dashboard/server.py')],cwd=app,env=env,stdout=log,stderr=log)
        class Redirector:
            pid=p.pid+100000
            def poll(self):return p.poll()
        try:
            launcher.wait_dashboard(Redirector(),timeout=15,launch_id='integration-launch')
        finally:p.terminate();p.wait(timeout=10)
