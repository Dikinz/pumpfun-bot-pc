"""Read-only report. Historical mode is exploratory; default uses logged entry decisions."""
import argparse,json,sqlite3
from pathlib import Path
from contextlib import closing
import entry_selection as lab

def historical(c):
 rows=[]
 for r in c.execute('SELECT mint,ts,reentry_hold,signal_recovery_pct,signal_momentum_pct,signal_buy_pressure,reentry_age,base_reentry_pnl,low_reentry_pnl,stress_reentry_pnl FROM fast_reentry_v0711 WHERE reentered=1 ORDER BY ts-reentry_hold,id'):
  m,ts,hold,rec,mom,bp,age,base,low,stress=r
  rows.append(dict(ts=ts-hold,mint=m,gate=lab.packet(rec,mom,bp,age),result=dict(base_reentry_pnl=base,low_reentry_pnl=low,stress_reentry_pnl=stress)))
 return dict(mode='HISTORICAL_EXPLORATORY_NOT_PROSPECTIVE',plan=lab.PLAN,cases=len(rows),rows=lab.aggregate(rows),thirds=[lab.aggregate(rows[len(rows)*i//3:len(rows)*(i+1)//3]) for i in range(3)])

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('database',nargs='?');p.add_argument('--historical',action='store_true');p.add_argument('--output');a=p.parse_args()
 if a.database:db=a.database
 else:
  import config
  db=config.DB
 with closing(sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True)) as c:
  if c.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise RuntimeError('Database integrity failed')
  result=historical(c) if a.historical else lab.summarize(c)
 if a.output:Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result,ensure_ascii=True,indent=2))
 raise SystemExit(1 if result.get('invalid') else 0)
