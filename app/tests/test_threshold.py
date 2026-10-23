from threshold_lab import evaluate

def case(m,label='FALSE_BOUNCE',ret=-3,mom=-2,base=-.5,hold=3):
 return dict(mint=m,label=label,s1_ret=ret,s1_mom=mom,s2_ret=None,s2_mom=None,base_pnl=base,reentry_hold=hold)

def test_no_leakage_and_costs():
 rows=[case('good'),case('tie',hold=1.1),case('late'),case('legacy'),case('winner','WINNER',ret=10,mom=3,base=.4)]
 meta={(m,1):dict(actual_hold=h,base_abort=-.2) for m,h in [('good',1.1),('tie',1.1),('late',1.5),('winner',1.1)]}
 out=evaluate(rows,meta,10)
 r=next(r for r in out['rows'] if r['sec']==1 and r['rule']=='AND' and r['x']==0 and r['y']==0)
 assert r['false_caught']==1 and r['winner_killed']==0
 assert abs(r['probation_base']-(r['original_base']+.3))<1e-8
 assert abs(r['lift_per_fast']-.03)<1e-8
 assert out['coverage'][0]['exit_tick']==1 and out['coverage'][0]['late']==1 and out['coverage'][0]['legacy']==1

def test_missing_and_strict_threshold():
 r=case('zero',ret=0,mom=0)
 out=evaluate([r],{('zero',1):dict(actual_hold=1,base_abort=-.1)},0)
 z=next(x for x in out['rows'] if x['sec']==1 and x['rule']=='AND' and x['x']==0 and x['y']==0)
 assert z['false_caught']==0 and z['lift_per_fast'] is None
 assert len(out['rows'])==70
 assert out['coverage'][1]['missing']==1

def test_real_snapshot_metadata(monkeypatch):
 import main,config
 monkeypatch.setattr(main.time,'time',lambda:100.0)
 m='threshold-metadata-unit'
 main.fast_reentry_start(m,{'entry':1,'ts':97,'mfe':0,'mae':0},1,0)
 q=main.FAST_REENTRY[m]
 q.update(reentered=True,reentry_ts=98.9,reentry_price=1,peak_price=1,min_price=1)
 main.fast_reentry_tick(m,1.01,'buy')
 snap=q['fb_snaps']['1']
 assert 1<=snap['actual_hold']<=1.25
 assert abs(snap['base_abort']-main._shadow_roundtrip_cost(m,1,1.01,config.POSITION_SIZE*.01,'BASE')[0])<1e-9
 main.fast_reentry_finalize(m,'REENTRY_TIMEOUT',now=102)
 payload=next(x[1] for x in reversed(main.SHADOW_DB_QUEUE) if x[0]=='false_bounce_done' and x[1]['mint']==m)
 assert payload['snap_meta']['1']==snap
