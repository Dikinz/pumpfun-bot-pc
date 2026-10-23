import math, config, database as db
FEATS=["ret_mean","ret_vol","accel","last1","last3","pullback","bp5","bp10","bp20","pressure_trend","buy_streak","entry_score","entry_trades","buyers","ratio","momentum"]
_CACHE={"n":-1,"means":None,"stds":None,"w":None}
def _label(r): return 1 if r["reason"] in ("TP2","TRAIL","PP_REAL_10_7") else 0
def _fit():
    with db.con() as c:
        rows=[dict(r) for r in c.execute("""SELECT t.reason,x.* FROM trades t JOIN entry_model_snapshot_v067 x ON x.mint=t.mint WHERE t.side='SELL' AND t.reason NOT IN ('TIME') ORDER BY t.id""")]
    n=len(rows)
    if n < config.MODEL_GATE_MIN_TRAIN: return n,None
    # v0.6.9.3: freeze the gate once the warm-up model exists. This prevents
    # accepted-only post-gate outcomes from feeding back into the model.
    if getattr(config,"MODEL_GATE_FREEZE_AFTER_WARMUP",False) and _CACHE["w"] is not None:
        return _CACHE["n"],_CACHE
    if _CACHE["w"] is not None and n-_CACHE["n"] < config.MODEL_GATE_RETRAIN_EVERY: return n,_CACHE
    means=[];stds=[]
    for f in FEATS:
        xs=[float(r.get(f) or 0) for r in rows];m=sum(xs)/n;sd=(sum((x-m)**2 for x in xs)/n)**.5 or 1.;means.append(m);stds.append(sd)
    def X(r): return [1.]+[(float(r.get(f) or 0)-means[i])/stds[i] for i,f in enumerate(FEATS)]
    w=[0.]*(len(FEATS)+1);lr=.035;reg=.002
    for _ in range(260):
        g=[0.]*len(w)
        for r in rows:
            x=X(r);z=max(-25,min(25,sum(a*b for a,b in zip(w,x))));pr=1/(1+math.exp(-z));e=pr-_label(r)
            for j in range(len(w)):g[j]+=e*x[j]
        for j in range(len(w)):w[j]-=lr*(g[j]/n+(reg*w[j] if j else 0))
    _CACHE.update(n=n,means=means,stds=stds,w=w);return n,_CACHE
def predict(vals):
    n,m=_fit()
    if not m:return None,n
    r=dict(zip(FEATS,vals));x=[1.]+[(float(r.get(f) or 0)-m["means"][i])/m["stds"][i] for i,f in enumerate(FEATS)]
    z=max(-25,min(25,sum(a*b for a,b in zip(m["w"],x))));return 1/(1+math.exp(-z)),n
