"""v0.7.101.8: the clock lab forgets a mint together with the bot.

03–04.11.2026: PRIMARY grew 67 -> 161 MB and ROUTE 58 -> 141 MB in 10 h. A synthetic run of 96 000 ticks showed the
growth in clock_lab.STATES (about 10 KB per mint, kept until the 8192 LRU cap) for mints main had already dropped."""
import time
from collections import deque
import config,main,clock_lab
from test_primary_integrity import fresh

def test_cleanup_drops_clock_lab_state_of_evicted_mints(fresh):
    main.S.clear();clock_lab.STATES.clear()
    for m,age in (('old',config.TTL+60),('young',1)):
        main.S[m]=main.state();main.S[m]['born']=time.time()-age
        for v in ('STRICT_0','SHADOW_100'):clock_lab.STATES[(v,m)]=dict(last=None,signatures=deque(maxlen=128))
    clock_lab.STATES[('STRICT_0','other')]=dict(last=None,signatures=deque(maxlen=128))
    main.cleanup(force=True)
    assert 'old' not in main.S and 'young' in main.S
    assert set(clock_lab.STATES)=={('STRICT_0','young'),('SHADOW_100','young'),('STRICT_0','other')}

def test_forget_is_safe_for_unknown_mints():
    clock_lab.STATES.clear();clock_lab.forget('never-seen');assert not clock_lab.STATES
