"""Offline identical-message telemetry component comparison, not full-bot speed."""
import json,sqlite3,tempfile,time,platform
from contextlib import contextmanager
from pathlib import Path
import runtime_control as rt

class DB:
    def __init__(self,path):
        self.c=sqlite3.connect(path,isolation_level=None);self.c.execute('PRAGMA journal_mode=WAL');self.c.execute('PRAGMA synchronous=NORMAL');self.c.execute('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT)');self.depth=0;self.writes=0
    @contextmanager
    def atomic(self):
        outer=self.depth==0
        if outer:self.c.execute('BEGIN IMMEDIATE')
        self.depth+=1
        try:yield
        except BaseException:
            if outer:self.c.rollback()
            raise
        else:
            if outer:self.c.commit()
        finally:self.depth-=1
    def inc(self,k,n=1):
        self.writes+=1;self.c.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=CAST(v AS REAL)+excluded.v",(k,n))
    def setm(self,k,v):
        self.writes+=1;self.c.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,v))
    def setmany(self,d):
        with self.atomic():
            for k,v in d.items():self.setm(k,v)

def run(messages=10000,rate=500):
    result=dict(scope='UNTRACKED_MARKET_TELEMETRY_COMPONENT_ONLY',messages=messages,simulated_messages_per_s=rate,platform=platform.system(),python=platform.python_version(),network=False)
    with tempfile.TemporaryDirectory() as td:
        for mode in ('v59','v60'):
            db=DB(str(Path(td)/(mode+'.db')));clock=[0.];original=rt.time
            if mode=='v60':rt.time=type('Clock',(),{'monotonic':staticmethod(lambda:clock[0])})()
            aggregate=rt.MarketTelemetry();wall=time.perf_counter();cpu=time.process_time()
            try:
                for i in range(messages):
                    stamp=1000+i/rate;clock[0]=i/rate
                    if mode=='v59':
                        db.inc('pumpapi_messages');db.setm('pumpapi_last_message_ts',stamp);db.setm('trade_last_raw_ts',stamp)
                        db.setm('trade_last_market_ts',stamp);db.setm('trade_stream_state','RECEIVING');db.setm('trade_watchdog_state','RECEIVING')
                    else:aggregate.received(stamp);aggregate.market(stamp);aggregate.flush(db)
                if mode=='v60':aggregate.flush(db,True)
            finally:rt.time=original
            result[mode]=dict(cpu_s=time.process_time()-cpu,elapsed_s=time.perf_counter()-wall,sql_write_statements=db.writes,meta=dict(db.c.execute('SELECT k,v FROM meta')))
            db.c.close()
    a,b=result['v59'],result['v60'];assert a['meta']==b['meta'];result['same_final_counters']=True
    result['write_reduction_pct']=100*(1-b['sql_write_statements']/a['sql_write_statements'])
    result['cpu_reduction_pct']=100*(1-b['cpu_s']/a['cpu_s'])
    result['limitations']='Only untracked telemetry at simulated arrival times, local Linux SQLite. Does not measure total bot CPU/RSS, Windows, RPC performance or hours-long stability.'
    return result
if __name__=='__main__':
    data=run();target=Path(__file__).parent/'data/runtime_benchmark_v60.json';target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(data,indent=2));print(json.dumps(data))
