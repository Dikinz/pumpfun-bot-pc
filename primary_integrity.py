"""Read-only reconciliation of PRIMARY paper trades. Costs remain assumptions."""
import json, math, sqlite3, argparse
from pathlib import Path

PROFILES = ('LOW', 'BASE', 'STRESS')
COMPONENTS = ('buy_slippage_cost','sell_slippage_cost','trade_fee_cost','priority_fee_cost','latency_impact_cost','failed_tx_penalty')

def summarize(c):
    errors=[]
    def issue(message): errors.append(message)
    def close(a,b):
        return a is not None and b is not None and math.isfinite(float(a)) and math.isfinite(float(b)) and math.isclose(float(a),float(b),rel_tol=1e-8,abs_tol=1e-8)
    trades=[dict(r) for r in c.execute('SELECT * FROM trades')]
    positions=[dict(r) for r in c.execute('SELECT * FROM positions')]
    groups={}
    for r in trades:
        tid=r.get('trade_id')
        if not tid: issue('Trade without trade_id');continue
        groups.setdefault(tid,[]).append(r)
    linked={}
    for table in ('entry_features','entry_trajectory_v064','entry_model_snapshot_v067','execution_sim_v060','execution_audit_v061'):
        by={}
        for row in c.execute('SELECT * FROM '+table):
            r=dict(row);tid=r.get('trade_id');by.setdefault(tid,[]).append(r)
            if not tid or tid not in groups: issue(table+': orphan record')
        linked[table]=by
    pos={}
    for r in positions:
        tid=r.get('trade_id');pos.setdefault(tid,[]).append(r)
        if not tid or tid not in groups:issue('Orphan open position')
    closed=0;ideal=0.;totals={p:{'count':0,'ideal':0.,'net':0.,'cost':0.} for p in PROFILES}
    for tid,rows in groups.items():
        b=[r for r in rows if r['side']=='BUY'];s=[r for r in rows if r['side']=='SELL']
        if len(b)!=1 or len(s)>1 or len(rows)!=len(b)+len(s):issue(tid+': invalid BUY/SELL pairing');continue
        b=b[0]
        for table in ('entry_features','entry_trajectory_v064','entry_model_snapshot_v067'):
            records=linked[table].get(tid,[])
            if len(records)!=1:issue(tid+': expected one '+table)
            for r in records:
                if r['mint']!=b['mint'] or r['ts']>b['ts']+0.01:issue(tid+': mismatched/late '+table)
        audits=linked['execution_audit_v061'].get(tid,[])
        sims=linked['execution_sim_v060'].get(tid,[])
        if not s:
            opens=pos.get(tid,[])
            if len(opens)!=1:issue(tid+': open BUY without one position')
            elif opens[0]['mint']!=b['mint'] or not close(opens[0]['entry'],b['price']) or not close(opens[0]['size'],b.get('size')):issue(tid+': open position mismatch')
            if audits or sims:issue(tid+': costs recorded before SELL')
            continue
        s=s[0];closed+=1;ideal+=s['pnl']
        if pos.get(tid):issue(tid+': closed trade still open')
        if s['mint']!=b['mint'] or s['ts']<b['ts']:issue(tid+': mint/time mismatch')
        if not b.get('size') or not b['price'] or not close(s.get('size'),b['size']) or not close(s['pnl'],b['size']*(s['price']/b['price']-1)):issue(tid+': price/size/PnL mismatch')
        if len(sims)!=1:issue(tid+': expected one execution simulation')
        elif not close(sims[0]['ideal_pnl'],s['pnl']) or not close(sims[0]['sim_pnl']+sims[0]['total_cost'],s['pnl']):issue(tid+': simulation PnL mismatch')
        if sorted(r['profile'] for r in audits)!=sorted(PROFILES):issue(tid+': expected LOW/BASE/STRESS costs')
        for r in audits:
            if r['mint']!=b['mint'] or not close(r['ideal_pnl'],s['pnl']) or not close(sum(r[k] for k in COMPONENTS),r['total_cost']) or not close(r['sim_pnl']+r['total_cost'],s['pnl']):issue(tid+': cost reconciliation mismatch')
            if r['profile'] in totals:
                t=totals[r['profile']];t['count']+=1;t['ideal']+=r['ideal_pnl'];t['net']+=r['sim_pnl'];t['cost']+=r['total_cost']
    return {'status':'ERROR' if errors else ('OK' if trades else 'NO_DATA'),'buys':sum(r['side']=='BUY' for r in trades),'closed':closed,'open':len(positions),'errors':len(errors),'details':errors[:50],'ideal':ideal,'profiles':totals,'gross_per_closed':ideal/closed if closed else None,'base_cost_per_closed':totals['BASE']['cost']/closed if closed else None,'scope':'Default v0.7.29 primary PAPER accounting; modeled costs; not a live-readiness or profitability verdict.'}

def check(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        c.execute('BEGIN')
        result=summarize(c)
        result['sqlite_integrity']=[r[0] for r in c.execute('PRAGMA integrity_check')]
        if result['sqlite_integrity']!=['ok']:
            result['errors']+=1;result['status']='ERROR'
        return result

if __name__=='__main__':
    import config
    parser=argparse.ArgumentParser();parser.add_argument('--db',default=config.DB);parser.add_argument('--out',default='primary_integrity_results.json');args=parser.parse_args()
    result=check(args.db)
    Path(args.out).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(1 if result['errors'] else 0)
