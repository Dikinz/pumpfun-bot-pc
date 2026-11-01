from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
import sys,time,json,threading,os
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import system_health,strategy_diagnostics,stream_diagnostics
import evidence_report
import run_audit,exit_audit,exit_timeline,entry_pipeline
import runtime_status
import time_check
import clock_lab
import shadow_preflight
import time_order_audit
import entry_diagnostics
import config,database as db,paper_capital,diagnostics,preentry,exit_comparison,position_monitor,wallet_costs,exit_quotes,monitor_service
PAGE=(Path(__file__).parent/'page.html').read_bytes()
DETAILS=(Path(__file__).parent/'details.html').read_bytes()
TERMINAL=(Path(__file__).parent/'terminal.html').read_bytes()

def snap():
    with db.con() as c:
        c.execute('BEGIN')
        diag=diagnostics.summary(c)
        rows=[dict(r) for r in c.execute('SELECT * FROM positions ORDER BY opened_ts')]
        waits={x['mint']:x for x in diag['exits']}
        for r in rows:r['waiting_exit']=waits.get(r['mint'])
        monitor=monitor_service.report(config.DB)
        result=dict(version=config.VERSION,database_identity=system_health.identity(config.DB),generated_ts=time.time(),as_of=diag['as_of'],frozen=diag['frozen'],
          evidence=evidence_report.summary(c,monitor,diag["as_of"]),entry_pipeline=entry_pipeline.summary(c),exit_timeline=exit_timeline.summary(c,monitor),runs=run_audit.summary(c),exit_audit=exit_audit.summary(c),device_time_check=time_check.latest(),shadow_preflight=shadow_preflight.summary(c),runtime=runtime_status.summary(c),clock_lab=clock_lab.summary(c),time_audit=time_order_audit.summary(c),
          entry_audit=entry_diagnostics.summary(c,diag['as_of']),market_feed=entry_diagnostics.feed(c,diag['as_of']),
          capital=paper_capital.summary(c),diagnostics=diag,positions=rows,
          waiting_principal=sum(r['size'] for r in rows if r['mint'] in waits),
          monitor=monitor,wallet=wallet_costs.summary(c),
          preentry=preentry.summary(c),comparison=exit_comparison.summary(c),
          quotes=exit_quotes.summary(c),
          buys=[dict(r) for r in c.execute("SELECT status,COUNT(*) n FROM paper_orders_v0720 WHERE side='BUY' GROUP BY status")],
          sells=[dict(r) for r in c.execute("SELECT status,COUNT(*) n FROM paper_orders_v0720 WHERE side='SELL' GROUP BY status")],
          trades=[dict(r) for r in c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 100')],
          entry_debug=[dict(r) for r in c.execute('SELECT ts,mint,reason,stage FROM entry_debug ORDER BY id DESC LIMIT 100')])

        try:result['parallel_paper']=__import__('paper_branch').report(config.DB)
        except Exception:result['parallel_paper']=dict(status='REPORT_ERROR')
        result['stream_diagnostics']=stream_diagnostics.summary(c)
        result["system_diagnostics"]=strategy_diagnostics.summary(c,result,config.DB)
        try:result['execution_readiness']=__import__('execution_lab').status()
        except Exception:result['execution_readiness']=dict(mode='PAPER',status='READINESS_REPORT_ERROR',main_strategy_live_routing=False)
        result['route_paper']=__import__('route_branch').report(config.DB)
        result['overview']=__import__('campaign_view').overview(config.DB)
        return result

from dashboard.cache import LatestJSON
_FULL_VIEW=LatestJSON(ttl=2.)
_SNAP_LOCK=_FULL_VIEW.lock  # compatibility: the full snapshot still has a single build lock
_OVERVIEW_VIEW=LatestJSON(ttl=2.)

def snap_bytes():return _FULL_VIEW.get(str(Path(config.DB).resolve()),snap)
def overview_bytes():return _OVERVIEW_VIEW.get(str(Path(config.DB).resolve()),lambda:__import__('campaign_view').overview(config.DB))

class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True

class H(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def _reply(self,body,content_type,status=200):
        try:
            self.send_response(status);self.send_header("Content-Type",content_type);self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.send_header("Connection","close");self.end_headers();self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):
            return
        except OSError as e:
            if getattr(e,"winerror",None) in (10053,10054,10038,10060): return
            raise
    def do_GET(self):
        try:
            if self.path=="/health":
                self._reply(json.dumps(dict(version=config.VERSION,database_identity=system_health.identity(config.DB),pid=os.getpid(),launch_id=os.environ.get('PUMPFUN_DASHBOARD_LAUNCH_ID'))).encode(),"application/json")
            elif self.path=="/overview":
                self._reply(overview_bytes(),"application/json")
            elif self.path=="/details":
                self._reply(DETAILS,"text/html;charset=utf-8")
            elif self.path.split("?",1)[0]=="/terminal":
                self._reply(TERMINAL,"text/html;charset=utf-8")
            elif self.path.split("?",1)[0]=="/classic":
                self._reply(PAGE,"text/html;charset=utf-8")
            elif self.path.startswith("/api"):
                self._reply(snap_bytes(),"application/json")
            else:
                # The trading terminal is the main page; the summary page stays at /classic.
                self._reply(TERMINAL,"text/html;charset=utf-8")
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):
            return
        except OSError as e:
            if getattr(e,"winerror",None) in (10053,10054,10038,10060): return
            raise
        except Exception as exc:
            system_health.failure(config.DB,'dashboard_api',exc)
            self._reply(b'{"error":"DASHBOARD_SNAPSHOT_FAILED"}',"application/json",status=500)

if __name__=="__main__":
    db.init()
    print(f"Dashboard http://127.0.0.1:{config.PORT}")
    DashboardHTTPServer(("127.0.0.1",config.PORT),H).serve_forever()
