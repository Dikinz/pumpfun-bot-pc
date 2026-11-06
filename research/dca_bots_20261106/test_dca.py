import numpy as np
import dca as Dm

def path(prices,spread=0.0):
    p=np.array(prices,float);n=len(p);t=np.arange(n,dtype=np.int64)*300_000
    return t,p.copy(),p*(1+spread),p*(1-spread),p.copy()

NOF=(np.zeros(0,dtype=np.int64),np.zeros(0))

def cfg(**k):
    base=dict(fee_taker=0.0,fee_maker=0.0,slip=0.0,funding=False)
    base.update(k);return Dm.Cfg(**base)

def test_levels_scale():
    c=cfg(step=0.01,step_scale=2.0,n_so=3,so_first=10,vol_scale=1.5)
    dev,size=c.levels()
    assert np.allclose(dev,[0.01,0.03,0.07]) and np.allclose(size,[10,15,22.5])
    assert abs(c.max_notional()-(c.base+47.5))<1e-9

def test_take_profit_cycle_without_costs():
    t,o,h,l,c=path([100,100,101.5,101.5])
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,tp=0.01,n_so=0),capital=1000)
    assert r.wins>=1 and abs(r.cycle_log[0][1]-1.0)<1e-9  # 1% of 100 notional

def test_safety_order_lowers_average_and_tp():
    # entry 100, SO at 99 (1%), equal size -> avg 99.4975, tp 1% -> ~100.49
    t,o,h,l,c=path([100,99,99,100.6,100.6])
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,so_first=100,n_so=1,step=0.01,tp=0.01),capital=1000)
    assert r.cycle_log[0][2]==1 and r.cycle_log[0][3]=='tp' and r.cycle_log[0][1]>0

def test_conservative_intrabar_order_tp_before_so():
    # one bar spanning both the TP at 101 and the SO at 99: TP of the pre-bar average is taken first
    t=np.arange(3,dtype=np.int64)*300_000;o=np.array([100.,100.,100.]);h=np.array([100.,101.,100.]);l=np.array([100.,99.,100.]);c=o.copy()
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,so_first=100,n_so=1,step=0.01,tp=0.01),capital=1000)
    assert r.cycle_log[0][2]==0 and r.cycle_log[0][3]=='tp'

def test_liquidation_wipes_bot():
    prices=[100]+list(np.linspace(100,60,40))
    t,o,h,l,c=path(prices)
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,so_first=100,n_so=2,step=0.05,tp=0.01,leverage=5),capital=50)
    assert r.liquidated_t is not None and r.equity==0.0

def test_no_liquidation_spot_like_holds_bag_and_closes_at_end():
    prices=[100]+list(np.linspace(100,80,20))
    t,o,h,l,c=path(prices)
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,n_so=0,tp=0.01,leverage=1),capital=1000)
    assert r.liquidated_t is None and r.cycle_log[-1][3]=='end' and abs(r.equity-(1000-20))<1e-6

def test_stop_loss_from_average():
    prices=[100,100,94,94]
    t,o,h,l,c=path(prices)
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,n_so=0,tp=0.05,sl=0.05),capital=1000)
    assert r.stops==1 and abs(r.cycle_log[0][1]-(-6.0))<1e-9  # gapped through 95: filled at the 94 open

def test_fees_reduce_profit():
    t,o,h,l,c=path([100,100,101.5,101.5])
    a=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,tp=0.01,n_so=0),capital=1000)
    b=Dm.run(t,o,h,l,c,*NOF,Dm.Cfg(base=100,tp=0.01,n_so=0,funding=False),capital=1000)
    assert b.equity<a.equity

def test_funding_long_pays_positive_rate():
    t,o,h,l,c=path([100]*10)
    ft=np.array([t[5]],dtype=np.int64);fr=np.array([0.01])
    r=Dm.run(t,o,h,l,c,ft,fr,cfg(base=100,tp=0.5,n_so=0,funding=True),capital=1000)
    assert abs(r.equity-(1000-1.0))<1e-9

def test_short_mirror():
    t,o,h,l,c=path([100,100,98.5,98.5])
    r=Dm.run(t,o,h,l,c,*NOF,cfg(direction=-1,base=100,tp=0.01,n_so=0),capital=1000)
    assert r.wins>=1 and r.cycle_log[0][1]>0

def test_insufficient_margin_skips_so():
    t,o,h,l,c=path([100,98,98,98])
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,so_first=1000,n_so=1,step=0.01,tp=0.5,leverage=1),capital=150)
    assert r.max_so_hits==0

def test_veles_stop_only_after_full_grid():
    # grid 100 / 95 (5% overlap, 2 orders), SL 5% beyond last -> 90 from the 100 open
    t,o,h,l,c=path([100,96,91,89,89])
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=50,so_first=50,n_so=1,step=0.05,tp=0.5,sl=0.05,sl_mode='beyond_last'),capital=1000)
    assert r.stops==1 and r.cycle_log[0][2]==1
    q=50/100+50/95;exp=89*q-100  # gapped through 90: filled at the 89 open
    assert abs(r.cycle_log[0][1]-exp)<1e-9
    # without the second order filled the stop never fires
    t,o,h,l,c=path([100,96,96,96])
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=50,so_first=50,n_so=1,step=0.10,tp=0.5,sl=0.01,sl_mode='beyond_last'),capital=1000)
    assert r.stops==0

def test_start_dir_switches_direction_per_deal():
    # first deal short (price falls 1% -> TP), then the array says long; flat bars are waits
    t,o,h,l,c=path([100,100,98.9,98.9,98.9,100.0,100.0])
    sd=np.array([-1,-1,0,0,1,1,1])
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,tp=0.01,n_so=0,start_dir=sd),capital=1000)
    kinds=[(x[3]) for x in r.cycle_log]
    assert r.cycle_log[0][3]=='tp' and r.cycle_log[0][1]>0  # short won
    assert r.wins>=2  # long from 98.9 to 100 won too

def test_daily_marks_recorded_while_waiting():
    n=288*3;t,o,h,l,c=path([100.0]*n)
    r=Dm.run(t,o,h,l,c,*NOF,cfg(base=100,tp=0.01,n_so=0,start_dir=np.zeros(n,dtype=int)),capital=1000)
    assert len(r.daily)==3 and all(v==1000 for _,v in r.daily)
