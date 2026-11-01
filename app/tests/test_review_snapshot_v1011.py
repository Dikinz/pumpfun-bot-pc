"""A denial starting after early snapshot reads must retain unknown-state evidence."""
import pytest
import rpc_budget as rpc

@pytest.mark.parametrize('blocked',[False,True])
def test_review1011_late_snapshot_denial_keeps_unknown_and_blocked(tmp_path,monkeypatch,blocked):
    b=rpc.Budget(tmp_path.resolve())
    with b.session():pass
    if blocked:b.block(RuntimeError('LOCAL_TEST'),stage='LOCAL_TEST')
    original=b._pending
    count=0
    # Initial construction skips _pending when already blocked; the failure is
    # always the directory read inside _snapshot's database-reading section.
    fail_at=2 if blocked else 3
    def pending():
        nonlocal count
        count+=1
        if count==fail_at:
            try:raise PermissionError(13,'SECRET_DENIAL')
            except PermissionError as exc:raise rpc.OutcomeReadBusy('DIRECTORY') from exc
        return original()
    monkeypatch.setattr(b,'_pending',pending)
    try:
        data=b.snapshot()
        assert data['status']==('BLOCKED' if blocked else 'OUTCOME_READ_BUSY')
        assert data['reason']=='OUTCOME_READ_BUSY'
        assert data['pending_outcomes'] is None
        assert data['failure_chain'][0]['reason']=='OUTCOME_READ_BUSY'
        assert data['failure_chain'][1]['errno']==13
        assert 'SECRET_DENIAL' not in str(data)
    finally:b.close()
