import json,math
import pytest
import entry_selection as lab
from replay_research import verify
from test_v16 import recorded

def test_boundaries_and_nonfinite():
 assert lab.decide(10,5)['R10_M5']
 assert not lab.decide(10.00001,5)['R10']
 assert not lab.decide(10,5.00001)['M5']
 with pytest.raises(ValueError):lab.decide(float('nan'),1)
 with pytest.raises(ValueError):lab.decide(1,float('inf'))

@pytest.mark.parametrize('price,accepted',[(.95,True),(1.10,False)])
def test_online_decision_and_no_trade_change(recorded,price,accepted):
 _,begin,tick,flush=recorded;begin();tick(100.3,.91);tick(100.7,.93);tick(101.1,price)
 # Decision already exists while outcome is unknown; every baseline entry still occurs.
 assert __import__('main').FAST_REENTRY['m']['reentered']
 c=flush();p=json.loads(c.execute("select payload from replay_v0716 where kind='REENTRY'").fetchone()[0])
 assert p['entry_gate']['decisions']['R10_M5']==accepted
 assert lab.summarize(c)['pending']==1 and lab.summarize(c)['complete']==0
 tick(101.3,price*.90,'sell');__import__('replay_log').flush(c)
 report=lab.summarize(c);assert report['complete']==1 and report['invalid']==0
 row=next(r for r in report['rows'] if r['variant']=='R10_M5')
 assert row['kept']==int(accepted)
 assert row['base']==(report['rows'][0]['base'] if accepted else 0)
 assert verify(c)['matched']==1

@pytest.mark.parametrize('damage',['flip','remove','feature','drop'])
def test_corrupt_gate_never_in_totals(recorded,damage):
 _,begin,tick,flush=recorded;begin();tick(100.3,.91);tick(100.7,.93);tick(101.1,.95);tick(101.3,.85,'sell');c=flush()
 rid,p=c.execute("select id,payload from replay_v0716 where kind='REENTRY'").fetchone();d=json.loads(p)
 if damage=='flip':d['entry_gate']['decisions']['R10_M5']=False
 elif damage=='remove':del d['entry_gate']
 elif damage=='feature':d['entry_gate']['features']['recovery_pct']=9.9
 elif damage=='drop':c.execute("delete from replay_v0716 where kind='EVENT' and seq=2")
 c.execute('update replay_v0716 set payload=? where id=?',(json.dumps(d),rid))
 r=lab.summarize(c);assert r['invalid']==1 and r['complete']==0
 if damage!='drop':assert verify(c)['mismatched']==1


def test_accounting_includes_skip_all_and_lost_winners():
 cases=[]
 for recovery,momentum,pnl in [(8,3,.5),(12,6,-1.),(15,7,.3)]:
  cases.append(dict(gate=lab.packet(recovery,momentum,.8,2),result=dict(base_reentry_pnl=pnl,low_reentry_pnl=pnl+.1,stress_reentry_pnl=pnl-.1)))
 rows={r['variant']:r for r in lab.aggregate(cases)}
 r=rows['R10_M5'];assert r['kept']==1 and r['skipped']==2
 assert r['base']==pytest.approx(.5);assert r['lift']==pytest.approx(.7)
 assert r['avoided_loss']==pytest.approx(1.) and r['lost_profit']==pytest.approx(.3)
 assert r['lift']==pytest.approx(r['avoided_loss']-r['lost_profit'])
 assert rows['NO_REENTRY']['base']==0 and rows['NO_REENTRY']['base_per_kept'] is None
