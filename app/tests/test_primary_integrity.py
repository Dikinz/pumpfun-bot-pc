import copy
import pytest
import config, database as db, main, primary_integrity

@pytest.fixture
def fresh(tmp_path,monkeypatch):
    old=getattr(db._LOCAL,'conn',None)
    if old:old.close()
    db._LOCAL.conn=None
    monkeypatch.setattr(config,'DB',str(tmp_path/'test.db'))
    monkeypatch.setattr(config,'PREFLIGHT_ENABLED',False) # legacy accounting tests; gate tested separately
    monkeypatch.setattr(config,'MIN_ENTRY_AGE',0)
    monkeypatch.setattr(config,'EXECUTION_MODEL_ENABLED',False) # isolate accounting; dedicated execution tests enable it
    monkeypatch.setattr(config,'BLOCK_ENTRIES_WHEN_TRADE_STALLED',False)
    monkeypatch.setattr(config,'ALLOW_REENTRY',False)
    monkeypatch.setattr(config,'PRESSURE_GATE_ENABLED',False)
    main.P.clear();main.ENTERED.clear();db.init()
    yield db.con()
    db.con().close();db._LOCAL.conn=None;main.P.clear();main.ENTERED.clear()

def test_round_trip_and_repeat_mint(fresh,monkeypatch):
    assert main.buy('same',1)[0]
    assert primary_integrity.summarize(fresh)['status']=='OK'
    first=main.P['same']['trade_id'];main.sell('same',1.1,'TEST')
    main.P.clear();main.ENTERED.clear();main.restore_positions()
    assert main.buy('same',1)[1]=='ALREADY_TRADED'
    monkeypatch.setattr(config,'ALLOW_REENTRY',True)
    assert main.buy('same',1)[0]
    assert main.P['same']['trade_id']!=first
    main.sell('same',.9,'TEST')
    r=primary_integrity.summarize(fresh)
    assert r['status']=='OK',r
    assert r['closed']==2 and r['profiles']['BASE']['count']==2
    assert fresh.execute('SELECT COUNT(*) FROM entry_features').fetchone()[0]==2
    fresh.execute("DELETE FROM execution_audit_v061 WHERE trade_id=? AND profile='BASE'",(first,))
    assert primary_integrity.summarize(fresh)['status']=='ERROR'

def test_restore_freezes_size_and_cost_profiles(fresh,monkeypatch):
    main.buy('open',2);q=copy.deepcopy(main.P['open']);cash=float(db.getm('cash'))
    main.P.clear();main.ENTERED.clear()
    monkeypatch.setattr(config,'POSITION_SIZE',999)
    monkeypatch.setattr(config,'EXEC_AUDIT_PROFILES',{})
    main.restore_positions()
    assert main.P['open']['trade_id']==q['trade_id']
    assert main.P['open']['cost_profiles']==q['cost_profiles']
    main.sell('open',2.2,'TEST')
    assert float(db.getm('cash'))==pytest.approx(cash+q['size']*1.1)
    assert primary_integrity.summarize(fresh)['status']=='OK'

def test_failed_sell_rolls_back_all_primary_records(fresh,monkeypatch):
    main.buy('rollback',1);cash=db.getm('cash');q=copy.deepcopy(main.P)
    original=main.execution_audit_record
    def broken(*args):
        original(*args)
        raise RuntimeError('injected disk/write error')
    monkeypatch.setattr(main,'execution_audit_record',broken)
    with pytest.raises(SystemExit,match='rolled back'):main.sell('rollback',1.2,'TEST')
    assert main.P==q and db.getm('cash')==cash
    assert fresh.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]==0
    assert fresh.execute('SELECT COUNT(*) FROM execution_sim_v060').fetchone()[0]==0
    assert fresh.execute('SELECT COUNT(*) FROM execution_audit_v061').fetchone()[0]==0
    assert primary_integrity.summarize(fresh)['status']=='OK'

def test_failed_buy_rolls_back_cash_and_position(fresh,monkeypatch):
    cash=db.getm('cash')
    def broken(*args,**kwargs):raise RuntimeError('injected feature failure')
    monkeypatch.setattr(db,'entry_feature',broken)
    with pytest.raises(SystemExit):main.buy('rollback',1)
    assert not main.P and not main.ENTERED and db.getm('cash')==cash
    assert fresh.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==0

def test_legacy_database_rejected(tmp_path,monkeypatch):
    import sqlite3
    old=getattr(db._LOCAL,'conn',None)
    if old:old.close()
    db._LOCAL.conn=None
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE trades(mint TEXT)')
    monkeypatch.setattr(config,'DB',str(path))
    try:
        with pytest.raises(RuntimeError,match='NEW database'):db.init()
    finally:
        db.con().close();db._LOCAL.conn=None
