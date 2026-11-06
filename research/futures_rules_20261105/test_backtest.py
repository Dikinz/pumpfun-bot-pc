import numpy as np
import backtest as B

def mk(prices_o,prices_h,prices_l,prices_c,t0=0,ft=(),fr=()):
    n=len(prices_o);t=np.arange(n,dtype=np.int64)*B.M5+t0
    return B.Sym('X',t,np.array(prices_o,float),np.array(prices_h,float),np.array(prices_l,float),np.array(prices_c,float),
                 np.array(ft,dtype=np.int64),np.array(fr,float))

def flat(n,p=100.0):return mk([p]*n,[p]*n,[p]*n,[p]*n)

def test_resample_ohlc_and_drop_incomplete():
    n=12*2+5  # two full hours and 5 bars of a third
    o=np.arange(n)+100.0;s=mk(o,o+1,o-1,o+.5)
    b=B.resample(s,'1h')
    assert len(b['t'])==2
    assert b['o'][0]==100 and b['h'][0]==111+1 and b['l'][0]==99 and b['c'][0]==111.5
    assert b['close_t'][0]==B.H and b['o'][1]==112

def test_week_bars_start_monday():
    s=flat(12*24*14,1.0);s.t=s.t+B.MON_OFFSET-2*B.D  # start on a Saturday
    b=B.resample(s,'1W')
    assert all(((x-B.MON_OFFSET)%B.W)==0 for x in b['t'][1:])

def test_roll_prev_excludes_current():
    x=np.arange(30.0)
    r=B.roll_prev(x,20,np.max)
    assert np.isnan(r[19]) and r[20]==19 and r[29]==28

def test_no_lookahead_in_signals():
    rng=np.random.default_rng(0);n=12*200
    c=100+np.cumsum(rng.normal(0,.3,n));s=mk(c,c+.2,c-.2,c)
    b=B.resample(s,'1h');sig=B.signals('T',b)
    s2=mk(c,c+.2,c-.2,c);s2.c[12*150:]+=50;s2.h[12*150:]+=50;s2.l[12*150:]+=50;s2.o[12*150:]+=50
    b2=B.resample(s2,'1h');sig2=B.signals('T',b2)
    assert (sig['long_in'][:149]==sig2['long_in'][:149]).all() and (sig['short_in'][:149]==sig2['short_in'][:149]).all()

def test_long_stop_gap_fills_at_open():
    n=12*30;s=flat(n,100.0);b=B.resample(s,'1h')
    sig=dict(atr=np.full(len(b['t']),1.0),nt_exit={1:np.full(len(b['t'])+1,len(b['t'])),-1:np.full(len(b['t'])+1,len(b['t']))})
    j=12*5+3;s.o[j:]=97.0;s.h[j:]=97.0;s.l[j:]=97.0;s.c[j:]=97.0  # gap from 100 to 97 through a 98 stop
    tr=B.trade(s,b,sig,'T','1h',2,1)
    assert tr['entry']==100 and tr['d']==2.0 and tr['kind']=='stop' and tr['exit']==97.0 and tr['exit_t']==s.t[j]

def test_short_stop_intrabar_fills_at_stop():
    n=12*30;s=flat(n,100.0);b=B.resample(s,'1h')
    sig=dict(atr=np.full(len(b['t']),1.0),nt_exit={1:np.full(len(b['t'])+1,len(b['t'])),-1:np.full(len(b['t'])+1,len(b['t']))})
    j=12*6;s.h[j]=103.0
    tr=B.trade(s,b,sig,'T','1h',2,-1)
    assert tr['kind']=='stop' and tr['exit']==102.0

def test_rule_exit_and_max_hold():
    n=12*100;s=flat(n,100.0);b=B.resample(s,'1h');nb=len(b['t'])
    sig=dict(atr=np.full(nb,1.0),nt_exit={1:np.full(nb+1,nb),-1:np.full(nb+1,nb)})
    tr=B.trade(s,b,sig,'M','1h',5,1)  # no exit signal: max hold 20 bars after entry
    assert tr['kind']=='rule' and tr['exit_t']-tr['entry_t']==20*B.H
    mask=np.zeros(nb,bool);mask[9]=True;sig['nt_exit'][1]=B.next_true(mask)
    tr=B.trade(s,b,sig,'M','1h',5,1)
    assert tr['exit_t']==b['close_t'][9]

def test_r_net_costs_and_funding_signs():
    tr=dict(entry=100.0,exit=102.0,d=2.0,dir=1,kind='rule',fund=0.001)
    gross=1.0;cost=(100*(B.FEE+B.SLIP)+102*(B.FEE+B.SLIP))/2.0;fund=-0.001*100/2.0
    assert abs(B.r_net(tr)-(gross-cost+fund))<1e-12
    short=dict(tr,dir=-1)
    assert abs(B.r_net(short)-(-gross-cost-fund))<1e-12
    stop=dict(tr,kind='stop')
    assert B.r_net(stop)<B.r_net(tr)
    assert abs(B.r_net(tr,2.0,funding=False)-(gross-2*cost))<1e-12

def test_funding_window_is_open_closed():
    s=mk([1.0]*3,[1.0]*3,[1.0]*3,[1.0]*3,ft=[100,200,300],fr=[0.1,0.2,0.4])
    assert abs(s.funding_sum(100,300)-0.6)<1e-12 and s.funding_sum(300,400)==0

def test_run_rule_one_position_at_a_time():
    rng=np.random.default_rng(1);n=12*24*60
    c=100*np.exp(np.cumsum(rng.normal(0,.002,n)));s=mk(c,c*1.001,c*.999,c)
    b=B.resample(s,'1h');sig=B.signals('T',b);trs=B.run_rule(s,b,sig,'T','1h')
    assert trs
    for a,b2 in zip(trs,trs[1:]):assert b2['entry_t']>=a['exit_t']

def test_bootstrap_p_extremes():
    w=B.W;base=B.MON_OFFSET+w*3000
    pos=[dict(entry_t=base+k*w,r=1.0+0.01*k) for k in range(40)]
    assert B.bootstrap_p(pos,(base,base+40*w),n_boot=200)==0.0
    neg=[dict(entry_t=base+k*w,r=-1.0) for k in range(40)]
    assert B.bootstrap_p(neg,(base,base+40*w),n_boot=200)==1.0
