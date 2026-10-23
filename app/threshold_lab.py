"""Fixed exploratory grid. Never selects a trading policy."""
import math
GRID=(-5.0,-2.0,0.0,2.0,5.0)
MAX_DELAY=0.25

def finite(v):
    return isinstance(v,(int,float)) and math.isfinite(v)

def evaluate(cases,timing,fast=0,big_loss=.30):
    out={'samples':len(cases),'fast':fast,'coverage':[],'rows':[], 'observations':cases}
    original=sum(r['base_pnl'] for r in cases if finite(r.get('base_pnl')))
    for sec in (1,2):
        usable=[]; cov=dict(sec=sec,observed=0,usable=0,legacy=0,late=0,exit_tick=0,missing=0)
        for r in cases:
            meta=timing.get((r['mint'],sec),{})
            if not all(finite(r.get(f's{sec}_{k}')) for k in ('ret','mom')):
                cov['missing']+=1;continue
            cov['observed']+=1
            h=meta.get('actual_hold'); end=r.get('reentry_hold')
            if not all(finite(v) for v in (h,end,meta.get('base_abort'),r.get('base_pnl'))):
                cov['legacy']+=1;continue
            # Natural exit wins a tie; the snapshot taken on the exit tick is not preventative.
            if h>=end-1e-6:
                cov['exit_tick']+=1;continue
            if not sec<=h<=sec+MAX_DELAY:
                cov['late']+=1;continue
            usable.append((r,meta))
        cov['usable']=len(usable);out['coverage'].append(cov)
        rules=[('RET',x,None) for x in GRID]+[('MOM',None,y) for y in GRID]+[('AND',x,y) for x in GRID for y in GRID]
        for rule,x,y in rules:
            affected=[]
            for r,m in usable:
                a=x is None or r[f's{sec}_ret']<x
                b=y is None or r[f's{sec}_mom']<y
                if a and b:affected.append((r,m))
            replacements={r['mint']:m['base_abort'] for r,m in affected}
            net=original+sum(m['base_abort']-r['base_pnl'] for r,m in affected)
            valid=[r for r in cases if finite(r.get('base_pnl'))]
            out['rows'].append(dict(sec=sec,rule=rule,x=x,y=y,usable=len(usable),
                false_total=sum(r['label']=='FALSE_BOUNCE' for r,m in usable),
                winner_total=sum(r['label']=='WINNER' for r,m in usable),
                false_caught=sum(r['label']=='FALSE_BOUNCE' for r,m in affected),
                winner_killed=sum(r['label']=='WINNER' for r,m in affected),
                other_affected=sum(r['label']=='OTHER' for r,m in affected),
                diagnostic_false=sum(r['label']=='FALSE_BOUNCE' and finite(r.get(f's{sec}_ret')) and finite(r.get(f's{sec}_mom')) and (x is None or r[f's{sec}_ret']<x) and (y is None or r[f's{sec}_mom']<y) for r in cases),
                diagnostic_winner=sum(r['label']=='WINNER' and finite(r.get(f's{sec}_ret')) and finite(r.get(f's{sec}_mom')) and (x is None or r[f's{sec}_ret']<x) and (y is None or r[f's{sec}_mom']<y) for r in cases),
                original_base=original,probation_base=net,
                lift_per_fast=(net-original)/fast if fast else None,
                big_loss_pct=100*sum(replacements.get(r['mint'],r['base_pnl'])<=-big_loss for r in valid)/len(valid) if valid else 0))
    return out

if __name__=='__main__':
    import argparse,sqlite3,json,csv
    from pathlib import Path
    parser=argparse.ArgumentParser(description='Read-only analysis of v0.7.12/v0.7.13 DB; output JSON and sweep CSV')
    parser.add_argument('database');parser.add_argument('--output',default='threshold_report')
    args=parser.parse_args()
    with sqlite3.connect(Path(args.database).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        rows=[dict(r) for r in c.execute('SELECT * FROM false_bounce_v0712 ORDER BY ts,id')]
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='probation_snaps_v0713'").fetchone()
        meta={(r['mint'],r['sec']):dict(r) for r in c.execute('SELECT * FROM probation_snaps_v0713')} if exists else {}
        n=c.execute('SELECT COUNT(*) FROM fast_reentry_v0711').fetchone()[0]
    report=evaluate(rows,meta,n)
    Path(args.output+'.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    with open(args.output+'.csv','w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(report['rows'][0]));w.writeheader();w.writerows(report['rows'])
    print('Saved report. Historical rows without timing/cost metadata are diagnostic only.')
