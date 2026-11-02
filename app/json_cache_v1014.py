"""Per-snapshot JSON parse cache for dashboard reports (v0.7.101.4).

The /api snapshot parsed the same 13-24 KB position-check results in five or six
independent summaries: ~3600 json.loads per snapshot, ~1 s on the 02.11 night database,
growing with every trade. Inside shared() each distinct string is parsed once per
snapshot and the parsed object is reused; readers must not mutate it (pinned by tests).
Outside shared(), including every trading path in the bot process, loads() is plain json.loads.
"""
import json,threading
from contextlib import contextmanager

_LOCAL=threading.local()

@contextmanager
def shared():
    outer=getattr(_LOCAL,'cache',None) is None
    if outer:_LOCAL.cache={}
    try:yield
    finally:
        if outer:_LOCAL.cache=None

def loads(raw):
    cache=getattr(_LOCAL,'cache',None)
    if cache is None or not isinstance(raw,str):return json.loads(raw)
    hit=cache.get(raw)
    if hit is None:hit=cache[raw]=json.loads(raw)
    return hit
