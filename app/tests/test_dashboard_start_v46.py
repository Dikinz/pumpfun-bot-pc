"""Start the same entry point used by the Windows launcher, in a separate process."""
import json,os,socket,subprocess,sys,time,urllib.request
from pathlib import Path

def test_dashboard_standalone(tmp_path):
    app=Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=dict(os.environ,DATABASE_PATH=str(tmp_path/'test.db'),DASHBOARD_PORT=str(port));env.pop('PYTHONPATH',None)
    with (tmp_path/'dashboard.log').open('w+') as log:
        p=subprocess.Popen([sys.executable,str(app/'dashboard/server.py')],cwd=tmp_path,env=env,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if p.poll() is not None:log.seek(0);raise AssertionError(log.read())
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api',timeout=.5) as response:data=json.load(response)
                    break
                except OSError:time.sleep(.1)
            else:raise AssertionError('Dashboard API timeout')
            assert data['version']=='v0.7.101.10' and 'exit_timeline' in data
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/details',timeout=2) as response:assert 'timelinePending' in response.read().decode()
        finally:p.terminate();p.wait(timeout=10)
