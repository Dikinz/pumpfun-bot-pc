"""Monotonic market-silence guard. Control traffic never refreshes it."""
import math,time

class MarketSilence:
    def __init__(self,soft,hard,interval,clock=None):
        if not all(math.isfinite(x) for x in (soft,hard,interval)) or not 0<interval<=soft<=hard:
            raise ValueError('INVALID_MARKET_WATCHDOG_LIMITS')
        self.soft,self.hard,self.interval=soft,hard,interval
        self.clock=clock or time.monotonic
        self.last_market=self.clock();self.last_report=None;self.previous='FRESH'
    def market(self):
        self.last_market=self.clock();self.previous='FRESH';self.last_report=None
    def poll(self):
        now=self.clock();age=max(0.,now-self.last_market)
        state='HARD_STALE' if age>=self.hard else 'SOFT_STALE' if age>=self.soft else 'FRESH'
        publish=state!='FRESH' and (state!=self.previous or self.last_report is None or now-self.last_report>=self.interval)
        if publish:self.last_report=now
        self.previous=state
        return dict(state=state,age_s=age,publish=publish)
