"""/terminal: offline trading-terminal page over the existing dashboard JSON (PAPER only)."""
import json,os,re,socket,subprocess,sys,time,urllib.request
from pathlib import Path
import pytest
import config

APP=Path(__file__).resolve().parents[1]
DASH=APP/'dashboard'
TERMINAL=DASH/'terminal.html'
ENDPOINTS={'/overview','/api','/health'}
LINKS={'/details','/'}
# JSON paths the page reads; every one must exist in the empty-database responses.
OVERVIEW_PATHS=('version','generated_ts','note','alerts','campaign','route','amm','amm_branch_enabled_v100',
    'resources','rpc_control_v89.status','rpc_control_v89.rate_per_origin',
    'rpc_control_v89.max_inflight','rpc_control_v89.endpoints','rpc_control_v89.pending_outcomes',
    'rpc_control_v89.exit_priority_pending_v94','rpc_control_v89.failure_recovered_v97',
    'roadmap_progress.percent','roadmap_progress.stages','roadmap_progress.basis',
    'primary.runtime.state','primary.runtime.label','primary.runtime.elapsed_s','primary.runtime.frozen',
    'primary.live_v87.loop_heartbeat_age_s','primary.live_v87.market_data_age_s','primary.live_v87.monitor_status',
    'primary.live_v87.monitor_heartbeat_age_s','primary.live_v87.active_checks','primary.live_v87.worker_limit',
    'primary.live_v87.oldest_check_age_s','primary.live_v87.ownership_pending','primary.stream.state',
    'primary.resources.cpu','primary.resources.ram_mb','primary.capital.cash','primary.capital.available',
    'primary.capital.principal','primary.capital.status','primary.net','primary.gross','primary.costs',
    'primary.closed','primary.entries','primary.wins','primary.accounting_missing','primary.closed_drawdown',
    'primary.new_closed','primary.carried_closed','primary.positions','primary.trades','primary.blocks',
    'primary.last_entry_result','primary.timing.queue','primary.timing.runtime','primary.preentry',
    'primary.cost_basis_v89')
API_PATHS=('version','generated_ts','as_of','frozen','waiting_principal','positions','trades','entry_debug',
    'buys','sells','capital.reserved','capital.errors','capital.total_cost','capital.pool_cost','capital.route_cost',
    'capital.fixed_cost','capital.open_entry_cost','capital.missing_exit_rate_count',
    'capital.marked_equity_before_exit_cost','capital.valuation.max_tick_age_s','capital.valuation.priced_count',
    'capital.valuation.excluded_count','capital.valuation.excluded_principal','execution_readiness.mode',
    'execution_readiness.main_strategy_live_routing','market_feed.last_market_ts','execution_readiness.mainnet_execution_verified',
    'device_time_check.state','device_time_check.label','market_feed.state','market_feed.market_age_s',
    'system_diagnostics.status','system_diagnostics.issues','diagnostics.exits','diagnostics.observations',
    'diagnostics.observation_total','diagnostics.entry_status','diagnostics.last_entry_ts','evidence.stale_ticks',
    'evidence.unknown_ticks','entry_pipeline.total_orders','entry_pipeline.outcome_counts','runs.current',
    'entry_audit.labels')
# resources is {} until the recorder heartbeat exists; the page guards these with ||[].
OPTIONAL_TEXT=('resources.alerts','resources.processes')


def page():return TERMINAL.read_text(encoding='utf-8')


def lookup(data,path):
    for key in path.split('.'):
        assert isinstance(data,dict) and key in data,path
        data=data[key]
    return data


def test_terminal1011_page_is_self_contained_and_offline():
    text=page()
    assert len(TERMINAL.read_bytes())<=64*1024
    assert not re.search(r'https?://|//cdn|@import|@font-face|url\((?!#)',text)
    assert not re.search(r'<(script|link|img|iframe)\b[^>]*\b(src|href)=',text)
    assert text.count('<script>')==1 and '<html lang="ru">' in text


def test_terminal1011_uses_only_existing_endpoints():
    text=page()
    fetched=set(re.findall(r"\['(/[a-z]+)',\d+,\d+\]",text))
    assert fetched==ENDPOINTS
    linked=set(re.findall(r'href="([^"]*)"',text))
    assert linked<=LINKS
    literals=set(re.findall(r"'(/[a-z/]*)'",text))
    assert literals<=ENDPOINTS|LINKS
    assert 'XMLHttpRequest' not in text and 'WebSocket' not in text and 'EventSource' not in text


def test_terminal1011_paper_honesty_and_secret_free_rendering():
    text=page()
    assert 'PAPER · без реальных денег' in text
    assert 'доля контрольных пунктов · не вероятность прибыли · не разрешение live' in text
    assert 'доля прошлых, не вероятность' in text and 'малая выборка' in text
    assert 'Итога по всем веткам нет намеренно' in text and 'капитал не складывается' in text
    assert 'включает покупки — не PNL' in text
    # Never render paths, executables, wallet data or exception chains.
    assert not re.search(r"\b(wallet|executable|exception)\b",text)
    assert not re.search(r"\.database\b|'database'|state_read_error\.path",text)
    # Version is read from the API, never hard-coded (so it cannot drift from config.VERSION).
    assert config.VERSION not in text and not re.search(r'v0\.\d+\.\d+',text)


def test_terminal1011_server_route_is_additive():
    src=(DASH/'server.py').read_text(encoding='utf-8')
    assert "TERMINAL=(Path(__file__).parent/'terminal.html').read_bytes()" in src
    assert 'elif self.path.split("?",1)[0]=="/terminal":' in src
    for route in ('if self.path=="/health":','elif self.path=="/overview":','elif self.path=="/details":','elif self.path.startswith("/api"):'):
        assert route in src
    assert src.index('=="/terminal"')<src.index('self.path.startswith("/api")')


def get(port,path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}',timeout=20) as r:
        return r.status,r.headers.get('Content-Type'),r.read()


@pytest.fixture
def dashboard(tmp_path):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port!=8188
    env=dict(os.environ,DATABASE_PATH=str(tmp_path/'terminal.db'),DASHBOARD_PORT=str(port));env.pop('PYTHONPATH',None)
    with (tmp_path/'server.log').open('w+') as log:
        child=subprocess.Popen([sys.executable,str(DASH/'server.py')],cwd=APP,env=env,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                try:get(port,'/health');break
                except OSError:time.sleep(.1)
            else:log.seek(0);raise AssertionError(log.read())
            yield port
        finally:child.terminate();child.wait(timeout=10)


def test_terminal1011_route_and_existing_routes_with_empty_database(dashboard):
    port=dashboard
    status,ctype,body=get(port,'/terminal')
    assert status==200 and ctype.startswith('text/html') and body==TERMINAL.read_bytes() and b'PAPER' in body
    assert get(port,'/terminal?demo=empty')[2]==body
    assert get(port,'/')[2]==(DASH/'page.html').read_bytes()
    assert get(port,'/terminalx')[2]==(DASH/'page.html').read_bytes()
    assert get(port,'/details')[2]==(DASH/'details.html').read_bytes()
    health=json.loads(get(port,'/health')[2])
    status,ctype,raw=get(port,'/overview');overview=json.loads(raw)
    assert ctype=='application/json'
    status,ctype,raw=get(port,'/api');api=json.loads(raw)
    assert ctype=='application/json'
    assert health['version']==overview['version']==api['version']==config.VERSION
    text=page()
    missing=[]
    for data,paths in ((overview,OVERVIEW_PATHS),(api,API_PATHS)):
        for path in paths:
            try:lookup(data,path)
            except AssertionError:missing.append(path)
            assert path.split('.')[-1] in text,path
    assert missing==[]
    assert isinstance(overview['resources'],dict)
    for path in OPTIONAL_TEXT:assert path in text


def test_terminal1011_fix_round1_regressions():
    text=page()
    # Open-position marks use the same base as paper_capital (wall clock at snapshot), never the frozen as_of.
    assert 'A.generated_ts-a.last_update_ts' in text and 'A.as_of-a.last_update_ts' not in text
    assert "A.frozen!==true&&isN(A.generated_ts)" in text
    # "Возраст рынка" in the System panel follows the top-bar chip (live_v87), never a frozen-relative age.
    assert "const mAge=isN(l.market_data_age_s)?l.market_data_age_s:A.frozen===true?null" in text
    assert 'на момент heartbeat' in text
    # Feed SELL amounts: net joined by trade_id, otherwise a visible uncoloured gross label.
    assert "nm[t.trade_id]" in text and 'бр. ' in text
    # Exponential back-off on errors, capped at 60 s, and a throttled manual refresh.
    assert 'Math.min(EP[k][2]*2**S.ec[k],60000)' in text and 'n-S.lastForce<5000' in text and 'e.repeat' in text
    # Tabs/branch buttons are built once (focus survives refreshes) and carry ARIA state.
    assert 'role=tablist' in text and 'role=tab ' in text and 'aria-selected' in text and 'aria-pressed' in text
    # Dark native controls, AA-contrast muted text, reduced motion.
    assert ':root{color-scheme:dark}' in text and '--mut:#8a93a0' in text and 'prefers-reduced-motion' in text
    for code in ('TP2','TRAIL','MAX_HOLD_WATCHDOG','PROFIT_PROTECT_EXIT','ALREADY_OPEN','NO_PRICE'):
        assert code+':' in text

def test_terminal1011_fix_round2_regressions():
    text=page()
    # Marks and "Оценка с открытыми" are hidden when the /api snapshot is old or late, never shown as current.
    assert 'const AMAX=25,aAge=()=>' in text and 'const aOld=()=>!S.A||(!S.demo&&(aLate()||aAge()>AMAX));' in text
    assert 'const old=aOld(),anyPriced=A&&!old&&A.frozen!==true' in text and "p.exit_decision!=='WAITING_FRESH_TICK'" in text
    assert 'const vOk=A&&!aOld(),g2=' in text and 'снимок /api устарел' in text and 'Снимок /api устарел (' in text
    assert "A.frozen===true&&g(O,'primary.runtime.frozen')!==false" in text and "fr=g(p,'runtime.frozen')===true" in text
    assert 'A.as_of,aOld()]' in text and "start_balance'),P?[ag,aOld()]" in text and 'conn(),aLate(),aOld(),' in text
    assert 'без расходов входа и выхода' in text and 'до расходов выхода;' not in text
    # Feed rows wrap to two lines and carry the full text in a tooltip; BUY size is not repeated.
    assert '-webkit-line-clamp:2' in text and '<span class=x title="${esc(' in text and "'покупка @ '+px(t.price)" in text
    # Forced refresh respects back-off and the /api period.
    assert 'if(!S.ec.O)S.next.O=0;if(!S.ec.A&&n-(S.okA||0)>=15000)S.next.A=0;' in text and 'S.next.O=S.next.A=0' not in text
    # NOT_STARTED shows no elapsed time; banner count uses the same alert set as the counter.
    assert "rs==='NOT_STARTED'||!isN(rt.elapsed_s)?chip('Работает',D)" in text and 'const al=O?alerts():[];' in text
    # Position cards below 1000 px; accessibility.
    assert '@media(max-width:1000px){#pPos thead{display:none}' in text
    assert '.card .bd.dn{background:rgba(246,70,93,.08)}' in text and 'id=alc aria-label=' in text and text.count('role=img')>=2
    assert '??D' not in text.replace('esc(pe[k]??D)','')


def test_terminal1011_critic_final_regressions():
    import re
    text=page()
    js='\n'.join(re.findall(r'<script>(.*?)</script>',text,re.S))
    # Every bare function call refers to a function/constant defined in the page or to a browser built-in
    # (a bulk ??D -> nv() rewrite once produced an undefined escnv() that broke the diagnostics tab).
    called=set(re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(',js))
    defined=set(re.findall(r'function\s+([A-Za-z_$][\w$]*)',js))|set(re.findall(r'(?:const|let|var|,)\s*([A-Za-z_$][\w$]*)\s*=',js))
    builtins={'AbortController','Date','Error','Set','String','URLSearchParams','addEventListener','catch','clearTimeout','fetch','for','function','if','isFinite','parseFloat','parseInt','setInterval','setTimeout','var','fn','s','url','snapshot'}
    assert called-defined-builtins==set()
    assert "'pid '+esc(nv(p.pid))" in text
    # An old (not only late) /api snapshot raises the banner and warn status; its current price is hidden.
    assert "if(A&&!S.demo&&aOld())h.push(['a','Снимок /api устарел (" in text and "!S.demo&&aOld()?'warn':''" in text
    assert '${old?D:px(a.current)}' in text and '(S.A&&aOld()))rBans()' in text
