"""One immutable JSON response per view; never used for trading permissions."""
import json,threading,time
class LatestJSON:
    def __init__(self,ttl=2.,clock=None):
        self.ttl=ttl;self.clock=clock or time.monotonic;self.lock=threading.Lock();self.key=None;self.body=b'';self.built=0.;self.builds=0
    def get(self,key,build):
        with self.lock:
            now=self.clock()
            if self.body and self.key==key and 0<=now-self.built<self.ttl:return self.body
            value=build();body=json.dumps(value,separators=(',',':'),allow_nan=False).encode()
            # Retain one response, never a growing history or per-client objects.
            self.key=key;self.body=body;self.built=self.clock();self.builds+=1
            return body
