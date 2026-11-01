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
