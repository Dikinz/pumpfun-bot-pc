"""v0.7.101.9: a stop delivered as KeyboardInterrupt is not an unhandled error.

04.11.2026 16:19: the user's stop reached amm_branch_watch while it wrote a state file and was logged as
UNHANDLED_ERROR/KeyboardInterrupt, although the branch acknowledged the stop and every process exited with 0."""
import asyncio
import pytest
import system_health

def test_keyboard_interrupt_is_re_raised_without_a_failure_record(monkeypatch):
    recorded=[];monkeypatch.setattr(system_health,'failure',lambda *a:recorded.append(a))
    async def stopped():raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):asyncio.run(system_health.supervise('amm_branch_watch',stopped(),'db'))
    assert recorded==[]

def test_real_errors_are_still_recorded(monkeypatch):
    recorded=[];monkeypatch.setattr(system_health,'failure',lambda primary,name,exc:recorded.append((name,type(exc).__name__)))
    async def broken():raise ValueError('x')
    with pytest.raises(ValueError):asyncio.run(system_health.supervise('amm_branch_watch',broken(),'db'))
    assert recorded==[('amm_branch_watch','ValueError')]
