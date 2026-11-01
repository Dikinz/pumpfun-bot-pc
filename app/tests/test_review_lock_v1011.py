"""Deterministic concurrency proof for priority-read retry lock ownership."""
import threading
import types
from pathlib import Path

import pytest
import rpc_budget as rpc
import rpc_priority as priority


def test_review1011_retry_releases_metadata_and_all_slots_before_wait(tmp_path, monkeypatch):
    root = tmp_path / 'control'
    b = rpc.Budget(root, slots=4, rate=6, wait=.5)
    directory = root / 'exit_priority_v94'
    directory.mkdir(parents=True)
    original_scandir = priority.os.scandir
    entered_retry = threading.Event()
    locks_checked = threading.Event()
    errors = []
    clock = types.SimpleNamespace(now=100.)
    original_time = rpc.time

    def unreadable(path):
        if Path(path) == directory:
            raise PermissionError(13, 'controlled priority read denial')
        return original_scandir(path)

    def retry_wait(_delay):
        # Permit reaches this only after the denied priority read. Freeze it
        # here while another owner tries every lock before the deadline.
        entered_retry.set()
        assert locks_checked.wait(5), 'concurrent owner never completed'
        clock.now = 101.

    def concurrent_owner():
        held = []
        try:
            assert entered_retry.wait(5), 'Permit never entered its retry wait'
            for name in ['metadata.lock', *['slot_' + str(i) + '.lock' for i in range(b.slots)]]:
                fd = rpc.lock_slot(root / name)
                assert fd is not None, name + ' retained during priority retry'
                held.append(fd)
        except BaseException as exc:
            errors.append(exc)
        finally:
            for fd in reversed(held):
                rpc.unlock_slot(fd)
            locks_checked.set()

    monkeypatch.setattr(priority.os, 'scandir', unreadable)
    monkeypatch.setattr(rpc, 'time', types.SimpleNamespace(
        monotonic=lambda: clock.now, time=original_time.time, sleep=retry_wait))
    observer = threading.Thread(target=concurrent_owner, daemon=True)
    observer.start()
    try:
        with pytest.raises(rpc.Deferred) as caught:
            with b.request('https://rpc.invalid'):
                pytest.fail('HTTP was admitted during priority read denial')
        assert caught.value.rpc_budget_reason == 'PRIORITY_READ_BUSY'
        observer.join(5)
        assert not observer.is_alive()
        assert not errors, repr(errors)
        assert entered_retry.is_set() and locks_checked.is_set()
        assert not b.failed.exists()
        with b.session() as c:
            assert sum(r[0] for r in c.execute('SELECT started FROM endpoints')) == 0
    finally:
        b.close()
