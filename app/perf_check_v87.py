"""Disposable offline dashboard/I/O benchmark. No working DB, RPC or credentials."""
import json,os,sys,time,tempfile,tracemalloc,types
from pathlib import Path
APP=Path(__file__).resolve().parent
REPORT=APP.parent/'results/performance_v87.json'

def reference(name):
    module=types.ModuleType(name+'_v864')
    source=APP/'benchmarks'/(name+'_v864.txt')
    exec(compile(source.read_text(),str(source),'exec'),module.__dict__)
    return module

def sample(fn,c):
    count=[0]
    def trace(_):count[0]+=1
    c.set_trace_callback(trace);tracemalloc.start();cpu=time.process_time();start=time.perf_counter()
    try:
        result=fn();_,peak=tracemalloc.get_traced_memory()
        return result,dict(elapsed_ms=(time.perf_counter()-start)*1000,cpu_ms=(time.process_time()-cpu)*1000,python_peak_bytes=peak,sql_statements=count[0])
    finally:tracemalloc.stop();c.set_trace_callback(None)

def main():
    import config,database as db,paper_capital as cap,campaign_view as view,clean_campaign,late_spawn
    from contextlib import closing
    old_cap=reference('paper_capital');old_view=reference('campaign_view');old_view.paper_capital=old_cap
    old_registry=reference('late_spawn');original_db=config.DB
    report=dict(schema='PERFORMANCE_V87',scope='OFFLINE_DISPOSABLE_SYNTHETIC',passed=False,windows_verified=os.name=='nt',
       note='Python allocations and process CPU only; not process RSS, VM memory, network latency or prediction of total bot CPU. UI cache never authorizes a trade.',cases=[])
    try:
        with tempfile.TemporaryDirectory(prefix='pump_perf_v87_') as td:
            try:
                config.DB=str(Path(td)/'synthetic.db');db.init();c=db.con();clean_campaign.ensure(c,config.DB)
                for i in range(2000):
                    tid='synthetic-'+str(i)
                    for side,price,pnl in [('BUY',1.,0.),('SELL',1.1,.3)]:
                        c.execute('INSERT INTO trades(trade_id,size,ts,mint,side,price,pnl,reason,hold) VALUES(?,?,?,?,?,?,?,?,?)',(tid,3,1000+i,tid,side,price,pnl,'TEST',1))
                        cap.record(c,tid,side,3 if side=='BUY' else 3.3,{'pool_fee_rate':.01})
                c.execute("UPDATE meta SET v=? WHERE k='cash'",(str(cap.settings(c)['start']+.3*2000),));c.commit()
                # Warm both paths once, then alternate three paired measurements.
                old_view.branch(c,config.DB,'primary',time.time());view.branch(c,config.DB,'primary',time.time())
                measurements_before=[];measurements_after=[]
                for _ in range(3):
                    before,a=sample(lambda:old_view.branch(c,config.DB,'primary',time.time()),c);measurements_before.append(a)
                    after,b=sample(lambda:view.branch(c,config.DB,'primary',time.time()),c);measurements_after.append(b)
                from statistics import median
                a={k:median(x[k] for x in measurements_before) for k in a};b={k:median(x[k] for x in measurements_after) for k in b}
                report['measurement_method']=dict(warmup_each=1,paired_samples=3,aggregation='MEDIAN',tracemalloc=True)
                for key in ('entries','closed','wins','net','gross','costs','closed_drawdown','accounting_missing'):
                    assert before[key]==after[key] or (isinstance(before[key],(float,int)) and abs(before[key]-after[key])<1e-8),(key,before[key],after[key])
                assert before['trades']==after['trades'] and before['capital']==after['capital']
                assert after['capital']['status']=='OK'
                report['cases'].append(dict(name='2000_closed_lots',before=a,after=b,all_lots_accounted=2000,returned_recent_rows=len(after['trades']),accounting_equivalent=True))
                old=old_registry.Registry(str(Path(td)/'old.db'),'s');new=late_spawn.Registry(str(Path(td)/'new.db'),'s')
                writes=[0];write=old_registry.write
                def observed(*a):writes[0]+=1;return write(*a)
                old_registry.write=observed
                for _ in range(300):old.last_save=0;old.poll();new.last_save=0;new.poll()
                assert writes[0]==300 and new.writes_v87==1
                report['cases'].append(dict(name='300_unchanged_empty_registry_polls',before_writes=writes[0],after_writes=new.writes_v87,avoided=new.skipped_writes_v87))
                report['integrity']=c.execute('PRAGMA integrity_check').fetchone()[0];assert report['integrity']=='ok'
                report['passed']=True
            finally:
                conn=getattr(db._LOCAL,'conn',None)
                if conn:conn.close();db._LOCAL.conn=None
    except Exception as exc:report.update(error_type=type(exc).__name__,error=str(exc))
    finally:
        config.DB=original_db
    REPORT.parent.mkdir(parents=True,exist_ok=True);REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('OFFLINE PERFORMANCE:', 'PASS' if report['passed'] else 'FAILED');print('Report:',REPORT)
    return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
