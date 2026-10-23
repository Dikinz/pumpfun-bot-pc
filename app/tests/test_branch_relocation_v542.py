import asyncio,sqlite3,shutil
from pathlib import Path
import pytest
import paper_branch as branch
import config
from test_primary_integrity import fresh

def snapshot(p):
    with sqlite3.connect(p) as c:return {t:list(c.execute('SELECT * FROM '+t)) for t in ('trades','positions','capital_ledger_v0721')}
def legacy(fresh):
    import run_audit
    run_audit.begin(fresh,None);fresh.commit()
    src=Path(config.DB);dst=branch.seed(src)
    for p in (src,dst):
        with sqlite3.connect(p) as c:
            c.execute("DELETE FROM meta WHERE k='paper_source_identity'")
            if p==dst:c.execute("DELETE FROM branch_info WHERE k='source_identity'")
    return src,dst
def move(src,dst,tmp_path):
    root=tmp_path/'moved';root.mkdir();new=root/src.name
    
    for old,dest in ((src,new),(dst,branch.path(new))):
        with sqlite3.connect(old) as a,sqlite3.connect(dest) as b:a.backup(b)
    return new

def test_legacy_move_preserves_financial_history(fresh,tmp_path):
    src,dst=legacy(fresh);new=move(src,dst,tmp_path)
    before=snapshot(new),snapshot(branch.path(new))
    assert branch.seed(new)==branch.path(new)
    assert before==(snapshot(new),snapshot(branch.path(new)))
    with sqlite3.connect(branch.path(new)) as c:
        info=dict(c.execute('SELECT * FROM branch_info'))
        assert info['original_source']==str(src.resolve())
        assert info['source_identity'] and info['source']==str(new.resolve())
    assert branch.seed(new)==branch.path(new)
def test_identity_empty_move(fresh,tmp_path):
    src=Path(config.DB);dst=branch.seed(src);new=move(src,dst,tmp_path)
    assert branch.seed(new)==branch.path(new)
def test_mismatched_identity_same_path(fresh):
    src=Path(config.DB);dst=branch.seed(src)
    with sqlite3.connect(dst) as c:c.execute("UPDATE branch_info SET v='other' WHERE k='source_identity'")
    before=snapshot(dst)
    with pytest.raises(RuntimeError,match='IDENTITY_MISMATCH'):branch.seed(src)
    assert snapshot(dst)==before
def test_legacy_no_anchor_rejected(fresh,tmp_path):
    src,dst=legacy(fresh);new=move(src,dst,tmp_path)
    with sqlite3.connect(new) as c:c.execute('DELETE FROM runs_v45')
    with pytest.raises(RuntimeError,match='ancestry unproven'):branch.seed(new)
def test_conflicting_buy_rejected(fresh,tmp_path):
    src,dst=legacy(fresh);new=move(src,dst,tmp_path)
    for p,price in ((new,1),(branch.path(new),2)):
        with sqlite3.connect(p) as c:c.execute("INSERT INTO trades(trade_id,side,ts,mint,size,price) VALUES('same','BUY',1,'m',1,?)",(price,))
    with pytest.raises(RuntimeError,match='ancestry unproven'):branch.seed(new)
def test_failed_branch_creates_no_stream_coroutines(fresh,monkeypatch):
    import main
    called=[]
    async def noop(*a):pass
    async def clock():return {}
    class Sock:
        def bind(self,*a):pass
        def listen(self,*a):pass
    def fail(*a):raise RuntimeError('BRANCH_START_FAILED')
    monkeypatch.setattr(config,'JUPITER_API_KEY','test')
    monkeypatch.setattr(main.exit_quotes,'refresh_fx',noop)
    monkeypatch.setattr(main.exit_quotes,'FX_STATUS','READY')
    from types import SimpleNamespace
    monkeypatch.setattr(main,'socket',SimpleNamespace(socket=lambda *a:Sock(),AF_INET=2,SOCK_STREAM=1))
    monkeypatch.setattr(main.time_check,'startup',clock)
    monkeypatch.setattr(main.branch_transport,'Fanout',fail)
    def stream(*a):called.append(True);return noop()
    for name in ('pumpapi_trades','free_api_trades','bitquery_trades','pumpportal_trades','cost_safe_no_trade_stream'):
        monkeypatch.setattr(main,name,stream)
    with pytest.raises(RuntimeError,match='BRANCH_START_FAILED'):asyncio.run(main.run())
    assert not called
