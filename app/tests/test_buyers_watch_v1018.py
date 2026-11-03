"""v0.7.101.8: the buyers-at-entry check is observation only and has a criterion fixed in advance."""
import json,sqlite3
import pytest
import buyers_watch_v1018 as bw

def db(rows):
    c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE paper_orders_v0720(trade_id TEXT,side TEXT,status TEXT,snapshot TEXT);
      CREATE TABLE trades(id INTEGER PRIMARY KEY,trade_id TEXT,side TEXT,pnl REAL);
      CREATE TABLE capital_ledger_v0721(trade_id TEXT,side TEXT,total_cost REAL);''')
    for i,(buyers,pnl) in enumerate(rows):
        tid='t%d'%i
        c.execute("INSERT INTO paper_orders_v0720 VALUES(?,'BUY','FILLED',?)",(tid,json.dumps(dict(buyers=['w%d'%k for k in range(buyers)])) if buyers is not None else '{}'))
        c.execute("INSERT INTO trades(trade_id,side,pnl) VALUES(?,'SELL',?)",(tid,pnl))
        for side in ('BUY','SELL'):c.execute('INSERT INTO capital_ledger_v0721 VALUES(?,?,.05)',(tid,side))
    return c

def test_groups_use_net_after_costs_and_the_fixed_threshold():
    x=bw.summary(db([(9,-.4),(10,.5),(3,.2),(None,1.)]))
    assert x['observation_only'] and x['threshold']==10 and x['unknown']==1
    assert x['below']['closed']==2 and x['below']['net']==pytest.approx(-.4)  # (-.5)+(.1)
    assert x['at_or_above']==dict(closed=1,net=pytest.approx(.4),mean=pytest.approx(.4),wins=1)
    assert x['verdict']=='TOO_FEW'

@pytest.mark.parametrize('below,above,verdict',[(-.3,.1,'SUPPORTED'),(.3,.1,'NOT_SUPPORTED'),(-.3,-.5,'NOT_SUPPORTED')])
def test_verdict_after_forty_closes(below,above,verdict):
    rows=[(5,below+.1)]*20+[(20,above+.1)]*20
    assert bw.summary(db(rows))['verdict']==verdict

def test_fork_excludes_carried_trades():
    c=db([(5,-1.)]*40);assert bw.summary(c,fork=40)['below']['closed']==0
