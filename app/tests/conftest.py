"""Isolate operational RPC state for every test, including spawned workers."""
import pytest

@pytest.fixture(autouse=True)
def rpc_control_state(tmp_path,monkeypatch):
    import config
    root=tmp_path/'rpc_control'
    monkeypatch.setattr(config,'RPC_CONTROL_DIR',root)
    monkeypatch.setattr(config,'RPC_MAX_REQUESTS_PER_SECOND',1000.)
    monkeypatch.setattr(config,'RPC_ADMISSION_WAIT_SECONDS',.1)
    monkeypatch.setenv('RPC_CONTROL_DIR',str(root))
    monkeypatch.setenv('RPC_MAX_REQUESTS_PER_SECOND','1000')
    monkeypatch.setenv('RPC_ADMISSION_WAIT_SECONDS','.1')

@pytest.fixture(autouse=True)
def legacy_two_branch_coverage(monkeypatch):
    # Preserve all legacy two-branch failure/cleanup integration coverage. New
    # branch-mode tests explicitly select OFF and verify the production default.
    import config
    monkeypatch.setattr(config,"AMM_BRANCH_ENABLED",True)
    monkeypatch.setenv("AMM_BRANCH_ENABLED","1")

_NATIVE_READER_BINDINGS={}

@pytest.fixture(autouse=True)
def native_reader_bindings_isolation():
    # CR1-04: monitor_file_reader.bindings() is lru_cached and captures ctypes.WinDLL and
    # ctypes.get_last_error at first use. A test that monkeypatches them (fake_last_error,
    # held CreateProcessW proxy) must not leave its fakes cached for later tests.
    # The real cached function is kept here, so a test that replaces the attribute itself
    # cannot hide the cache from the teardown.
    import monitor_file_reader
    original=_NATIVE_READER_BINDINGS.setdefault("bindings",monitor_file_reader.bindings)
    original.cache_clear()
    yield
    original.cache_clear()

@pytest.fixture(autouse=True)
def wal_checkpointer_isolation():
    # v0.7.101.4: db.runtime_mode() starts a background WAL checkpoint thread with its own
    # connection to config.DB; close it so no test keeps a temporary database open.
    yield
    import wal_checkpoint_v1014
    if wal_checkpoint_v1014.ACTIVE is not None:
        wal_checkpoint_v1014.ACTIVE.close();wal_checkpoint_v1014.ACTIVE=None
