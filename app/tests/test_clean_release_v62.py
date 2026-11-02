import hashlib,json,socket,subprocess,sys
from pathlib import Path
import pytest,release_guard

def release(root):
    app=root/'app';app.mkdir()
    p=app/'paper_branch.py';p.write_text('async def worker(primary,scenario=None):pass\n')
    (root/'BUILD_MANIFEST.json').write_text(json.dumps(dict(version='v0.7.101.5',files={'app/paper_branch.py':hashlib.sha256(p.read_bytes()).hexdigest()})))
    return p

def test_mixed_module_is_blocked_before_import(tmp_path):
    p=release(tmp_path);assert release_guard.verify(tmp_path)
    p.write_text('async def worker(primary):pass\n')
    with pytest.raises(RuntimeError,match='FILE_CHANGED'):release_guard.verify(tmp_path)

def test_missing_file_is_blocked(tmp_path):
    release(tmp_path).unlink()
    with pytest.raises(RuntimeError,match='FILE_MISSING'):release_guard.verify(tmp_path)

def test_extra_old_module_is_blocked(tmp_path):
    release(tmp_path);(tmp_path/'app/old_module.py').write_text('raise Exception()')
    with pytest.raises(RuntimeError,match='EXTRA_MODULE'):release_guard.verify(tmp_path)

def test_user_data_and_credentials_are_not_reset_or_inspected(tmp_path):
    release(tmp_path);data=tmp_path/'app/data';data.mkdir();(data/'user.db').write_bytes(b'keep')
    env=tmp_path/'app/.env';env.write_text('JUPITER_API_KEY=local-data-key')
    assert release_guard.verify(tmp_path)
    assert (data/'user.db').read_bytes()==b'keep' and env.read_text()=='JUPITER_API_KEY=local-data-key'

def test_database_cannot_redirect_to_old_folder(tmp_path):
    assert release_guard.local_database(tmp_path,'data/new.db')==tmp_path/'data/new.db'
    with pytest.raises(RuntimeError,match='OUTSIDE_FOLDER'):release_guard.local_database(tmp_path,'../old/data.db')

def test_occupied_panel_port_is_explicit_and_owner_survives():
    with socket.socket() as owner:
        owner.bind(('127.0.0.1',0));owner.listen();port=owner.getsockname()[1]
        with pytest.raises(RuntimeError,match='PORT_IN_USE'):release_guard.available(port)
        assert owner.fileno()>=0

def test_all_windows_buttons_use_local_isolated_bootstrap():
    root=Path(__file__).parents[2]
    for p in root.glob('*.bat'):
        text=p.read_text()
        assert '-I "%~dp0app\\bootstrap.py"' in text,p.name
        assert 'PYTHONPATH=' in text and 'PYTHONHOME=' in text
        assert 'app\\launcher.py' not in text
