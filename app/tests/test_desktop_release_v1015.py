"""WP4 desktop release integration: 16_DESKTOP.bat (spec 7), BUILD_MANIFEST coverage of every desktop file
(spec 5), 04_CHECK plan (spec 6.4), no external URL/CDN in the window, PAPER only, the window never opens a
listening socket. Static checks plus cmd.exe runs of 16_DESKTOP.bat in a temporary folder that never reach
pip, venv creation or the window (nothing is installed or started)."""
import ast,hashlib,json,os,re,shutil,subprocess,tempfile
from pathlib import Path
import pytest
import check_plan_v972 as plan,release_guard
APP=Path(__file__).resolve().parents[1]
ROOT=APP.parent
DESKTOP=ROOT/'desktop'
BAT='16_DESKTOP.bat'
ROOT_MAX=150  # 16_DESKTOP.bat: %~dp0 (folder + '\') may have at most 151 characters
GUI_MODULES=('webview','pystray','PIL','clr','pythonnet','clr_loader')
SERVER_MODULES=('socket','socketserver','http.server','asyncio','selectors','xmlrpc','wsgiref','multiprocessing','bottle')
SERVER_CALLS=('bind','create_server','serve_forever','start_server','HTTPServer','ThreadingHTTPServer')

def manifest():return json.loads((ROOT/'BUILD_MANIFEST.json').read_text(encoding='utf-8'))
def shipped(path):return not any(p=='__pycache__' or p.startswith('.') for p in path.relative_to(ROOT).parts)
def desktop_files():return sorted(p.relative_to(ROOT).as_posix() for p in DESKTOP.rglob('*') if p.is_file() and shipped(p))
def window_modules():return sorted(p for p in DESKTOP.glob('*.py'))  # shipped window code (tests live in desktop/tests)
def text(path):return path.read_text(encoding='utf-8')
def window_constants():
    """desktop/constants.py by path (it has no relative import); app tests do not put ROOT on sys.path."""
    import importlib.util
    spec=importlib.util.spec_from_file_location('pfdesk_release_constants',DESKTOP/'constants.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

# ---- 16_DESKTOP.bat (spec 7) ----
def test_desktop_bat_follows_the_bat_frame_and_spec():
    t=text(ROOT/BAT)
    lines=[l.strip() for l in t.splitlines()]
    assert lines[:5]==['@echo off','setlocal','set "PYTHONPATH="','set "PYTHONHOME="','cd /d "%~dp0"']
    # test_win314_release / test_clean_release_v62 rules: .venv required, isolated bootstrap, failure propagates
    assert 'if not exist ".venv\\Scripts\\python.exe"' in t and 'Run 01_INSTALL.bat first.' in t
    assert 'goto failed' in t and 'exit /b 1' in t and ':failed' in t
    assert '"%~dp0.venv\\Scripts\\python.exe" -I "%~dp0app\\bootstrap.py" verify-release || goto failed' in t
    assert '"%~dp0.venv\\Scripts\\python.exe" -I -m venv "%~dp0.venv-ui"' in t
    assert '-I -m pip install --disable-pip-version-check --require-hashes -r "%~dp0desktop\\requirements-desktop.txt"' in t
    assert 'start "" "%~dp0.venv-ui\\Scripts\\pythonw.exe" -I "%~dp0desktop\\app_main.py"' in t
    # order: .venv -> path length -> installed? -> release check -> venv -> pip -> marker -> start
    order=[t.index(s) for s in ('if not exist ".venv\\Scripts\\python.exe"','%DESKTOP_ROOT:~151,1%','fc /b','bootstrap.py" verify-release',
        '-m venv','-m pip install','copy /y','start ""')]
    assert order==sorted(order)
    assert t.count('|| goto failed')==5 and 'pip install' not in t.split(':ready')[1]
    assert re.findall(r'bootstrap\.py" (\S+)',t)==['verify-release']  # never start/check/results from this bat
    assert 'python.exe" -I "%~dp0desktop\\app_main.py"' not in t  # the window never runs with a console
    for word in ('8188','49190','launcher.py','EXECUTION_MODE','.env','taskkill','reg ','schtasks','runas'):assert word not in t,word

def test_desktop_bat_is_the_sixteenth_launcher():
    assert sorted(p.name for p in ROOT.glob('*.bat'))[-1]==BAT and len(list(ROOT.glob('*.bat')))==16
    assert "'16_DESKTOP.bat'" in text(APP/'tests'/'test_smoke.py')

def bat_root(length):
    """Temporary installation folder with exactly `length` characters (a short base, padded)."""
    base=Path(tempfile.mkdtemp(prefix='pfd'))
    pad=length-len(str(base))-1
    assert pad>0,'temporary folder is too long for this check'
    root=base/('r'*pad);root.mkdir()
    assert len(str(root))==length
    (root/BAT).write_bytes((ROOT/BAT).read_bytes().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n'))  # as shipped in the desktop ZIP (CRLF)
    (root/'desktop').mkdir();shutil.copy(DESKTOP/'requirements-desktop.txt',root/'desktop')
    return base,root

def touch(path,data=b''):path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)

def run_bat(root,*args):
    return subprocess.run(['cmd','/d','/c',str(root/BAT),*args],stdin=subprocess.DEVNULL,capture_output=True,timeout=60,cwd=root)

@pytest.fixture
def bat_case():
    if os.name!='nt':pytest.skip('cmd.exe BAT execution is Windows-only; static checks run everywhere')
    made=[]
    def make(length=60):made.append(bat_root(length));return made[-1][1]
    yield make
    for base,_ in made:shutil.rmtree(base,ignore_errors=True)

def test_desktop_bat_without_venv_asks_for_01_install(bat_case):
    root=bat_case()
    r=run_bat(root)
    assert r.returncode==1 and b'Run 01_INSTALL.bat first.' in r.stdout and not (root/'.venv-ui').exists()

def test_desktop_bat_refuses_a_long_folder_before_creating_anything(bat_case):
    root=bat_case(ROOT_MAX+1);touch(root/'.venv'/'Scripts'/'python.exe')
    r=run_bat(root)
    assert r.returncode==1 and b'too long' in r.stdout and b'Failed.' in r.stdout
    assert not (root/'.venv-ui').exists()

def test_desktop_bat_accepts_the_longest_allowed_folder_and_skips_a_current_install(bat_case):
    # .venv-ui present and its installed requirements equal the pinned file: no venv, no pip, no window
    root=bat_case(ROOT_MAX);touch(root/'.venv'/'Scripts'/'python.exe');touch(root/'.venv-ui'/'Scripts'/'pythonw.exe')
    shutil.copy(root/'desktop'/'requirements-desktop.txt',root/'.venv-ui'/'requirements-desktop.installed')
    r=run_bat(root,'--prepare')
    assert r.returncode==0 and b'too long' not in r.stdout and b'Failed' not in r.stdout

def test_desktop_bat_reinstalls_changed_requirements_and_keeps_failure(bat_case):
    # stale marker -> release check and pip would run; a broken interpreter (a folder named python.exe, so
    # nothing is executed) fails -> marker unchanged, exit 1
    root=bat_case();(root/'.venv'/'Scripts'/'python.exe').mkdir(parents=True);touch(root/'.venv-ui'/'Scripts'/'pythonw.exe')
    (root/'.venv-ui'/'Scripts'/'python.exe').mkdir()
    touch(root/'.venv-ui'/'requirements-desktop.installed',b'old\n')
    r=run_bat(root,'--prepare')
    assert r.returncode==1 and b'Failed.' in r.stdout
    assert (root/'.venv-ui'/'requirements-desktop.installed').read_bytes()==b'old\n'

# ---- BUILD_MANIFEST.json (spec 5) ----
def test_manifest_lists_every_shipped_desktop_file():
    files=manifest()['files']
    required=[BAT,'app/desktop_engine.py','app/tests/test_desktop_engine_v1015.py','app/tests/test_desktop_release_v1015.py',
        'docs/DESKTOP_APP_USER_RU.md','docs/DESKTOP_APP_SPEC_RU.md','docs/DESKTOP_SPIKE_RU.md']+desktop_files()
    assert [n for n in required if n not in files]==[]
    for name in ('desktop/__init__.py','desktop/app_main.py','desktop/requirements-desktop.txt','desktop/ui/status.html',
        'desktop/ui/log.html','desktop/assets/app_idle.ico','desktop/tests/fakes.py','desktop/tests/test_desktop_gui.py'):
        assert name in files,name

def test_manifest_has_no_excluded_zone_and_keeps_the_release_identity():
    m=manifest();files=m['files']
    assert m['version']==release_guard.VERSION=='v0.7.102.0' and m['build_revision']=='AMM_OBSERVER_V10200'
    for name in files:
        parts=name.split('/')
        assert parts[0] not in ('packaging','results','dist') and not parts[0].startswith('.venv'),name
        assert parts[:2]!=['app','data'] and '.env' not in parts and '__pycache__' not in parts and not name.endswith('.pyc'),name
    assert len(files)==len(set(files))

def test_manifest_hashes_are_lf_normalised_and_the_tree_verifies():
    files=manifest()['files']
    for name in [n for n in files if n.startswith('desktop/')]+[BAT,'app/desktop_engine.py','docs/DESKTOP_APP_USER_RU.md','README_RU.txt']:
        path=ROOT/name;raw=path.read_bytes()
        expected=hashlib.sha256(raw.replace(b'\r\n',b'\n') if path.suffix.lower() in release_guard.TEXT_SUFFIXES else raw).hexdigest()
        assert files[name]==expected,name
    assert release_guard.verify(ROOT)['version']=='v0.7.102.0'
    # desktop/release_check rule: no desktop/**/*.py outside the manifest
    extra=[p.relative_to(ROOT).as_posix() for p in DESKTOP.rglob('*.py') if '__pycache__' not in p.parts and p.relative_to(ROOT).as_posix() not in files]
    assert extra==[]

# ---- 04_CHECK plan (spec 6.4) ----
def test_check_plan_runs_the_desktop_tests_once():
    planned=plan.RECOVERY_TEST_FILES+plan.technical_remainder()
    assert len(planned)==len(set(planned))
    desktop=['../desktop/tests/'+p.name for p in sorted((DESKTOP/'tests').glob('test_desktop_*.py'))]
    assert len(desktop)==7
    for name in desktop+['tests/test_desktop_engine_v1015.py','tests/test_desktop_release_v1015.py']:
        assert name in plan.technical_remainder(),name
    assert all((APP/p).is_file() for p in planned)
    assert not any('packaging' in p for p in planned)  # packaging tests are not shipped

def top_level_imports(path):
    """Module names imported at import time (module body, including top-level if/try/with blocks)."""
    names=[];todo=list(ast.parse(text(path)).body)
    while todo:
        node=todo.pop()
        if isinstance(node,ast.Import):names+=[a.name for a in node.names]
        elif isinstance(node,ast.ImportFrom) and node.module and not node.level:names.append(node.module)
        elif isinstance(node,(ast.If,ast.Try,ast.With)):
            todo+=node.body+getattr(node,'orelse',[])+getattr(node,'finalbody',[])
            for handler in getattr(node,'handlers',[]):todo+=handler.body
    return names

def test_planned_desktop_tests_and_window_code_import_no_gui_library_at_import_time():
    # 04_CHECK runs in .venv, which has no pywebview/pystray/Pillow/pythonnet
    files=list(window_modules())+sorted((DESKTOP/'tests').glob('*.py'))+[APP/'desktop_engine.py']
    for path in files:
        bad=[n for n in top_level_imports(path) if n.split('.')[0] in GUI_MODULES]
        assert bad==[],(path.name,bad)

# ---- no external URL / CDN, PAPER only, no listening socket ----
URL=re.compile(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s\'"<>)]*')

def test_window_pages_load_nothing_external():
    pages=sorted((DESKTOP/'ui').glob('*'))
    assert {p.name for p in pages}=={'status.html','log.html'}
    for p in pages:
        t=text(p)
        assert URL.findall(t)==[],p.name
        assert "default-src 'none'" in t,p.name
        assert not re.search(r'(?i)(src|href)\s*=\s*["\']?(//|[a-z]+:)',t),p.name
        assert not re.search(r'(?i)\bcdn|googleapis|jsdelivr|unpkg|cloudflare|@import|fetch\(|XMLHttpRequest|WebSocket|EventSource',t),p.name

def test_window_code_names_only_the_local_dashboard():
    C=window_constants()
    assert C.HOST=='127.0.0.1' and C.FORBIDDEN_PORTS==(49190,)
    for path in window_modules():
        for url in URL.findall(text(path)):
            assert url=='http://' or url.startswith('http://127.0.0.1:'),(path.name,url)

def test_window_is_paper_only():
    status=text(DESKTOP/'ui'/'status.html')
    assert '<span class="paper">PAPER</span>' in status
    for path in window_modules()+sorted((DESKTOP/'ui').glob('*.html')):
        assert not re.search(r'(?i)\blive\b',text(path)),path.name
        if path.name!='constants.py':assert "'EXECUTION_MODE'" not in text(path),path.name
    C=window_constants()
    assert 'EXECUTION_MODE' in C.ENGINE_CLEAR_ENV  # the host never inherits a mode; it refuses non-paper itself
    engine=text(APP/'desktop_engine.py')
    assert "config.EXECUTION_MODE!='paper'" in engine and 'PAPER_ONLY' in engine
    req=text(DESKTOP/'requirements-desktop.txt').lower()
    for name in ('solana','solders','websockets','aiohttp'):assert name not in req,name

def calls(tree):
    for node in ast.walk(tree):
        if isinstance(node,ast.Call):
            f=node.func;yield node,(f.attr if isinstance(f,ast.Attribute) else getattr(f,'id',None))

def test_window_never_opens_a_listening_socket():
    seen_start=seen_create=0
    for path in window_modules()+[APP/'desktop_engine.py']:
        tree=ast.parse(text(path))
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):mods=[a.name for a in node.names]
            elif isinstance(node,ast.ImportFrom):mods=[node.module or '']
            else:continue
            for m in mods:assert not any(m==s or m.startswith(s+'.') for s in SERVER_MODULES),(path.name,m)
        for node,name in calls(tree):
            assert name not in SERVER_CALLS,(path.name,name)
            if name=='start' and isinstance(node.func,ast.Attribute) and ast.unparse(node.func.value).endswith('webview'):
                seen_start+=1;kw={k.arg:ast.unparse(k.value) for k in node.keywords}
                assert kw.get('private_mode')=='False' and 'http_server' not in kw and 'ssl' not in kw,kw
            if name=='create_window':
                seen_create+=1;kw={k.arg:ast.unparse(k.value) for k in node.keywords}
                assert kw.get('url',"self.layout.url")=='self.layout.url' and 'http_port' not in kw and 'server' not in kw,kw
            if name=='load_url':assert [ast.unparse(a) for a in node.args]==['self.layout.url'],path.name
        for node in ast.walk(tree):  # kw['url']=... before create_window(**kw)
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Subscript) and ast.unparse(t.slice)=="'url'" for t in node.targets):
                assert ast.unparse(node.value)=='self.layout.url',path.name
    assert seen_start==1 and seen_create>=2  # main window (url/html) and the log window (html)

# ---- user documentation ----
def test_readme_and_user_guide_cover_start_stop_logoff_and_update():
    readme=text(ROOT/'README_RU.txt')
    assert '16_DESKTOP' in readme and 'docs/DESKTOP_APP_USER_RU.md' in readme and '150' in readme
    guide=text(ROOT/'docs'/'DESKTOP_APP_USER_RU.md')
    for heading in ('## Установка','## Запуск','## Остановка','## Крестик, трей и выход из окна','## Выход из Windows и выключение ПК','## Обновление'):
        assert heading in guide,heading
    for phrase in ('Всё равно завершить','09_RECOVERY_CHECK','08_PRESERVE_DATA','app\\data','app\\.env','results','EXTRA_MODULE','150 символов','только в режиме PAPER'):
        assert phrase in guide,phrase
    # review fixes: new window states/notices (texts as the window shows them), installer, pip and dev notes
    c=window_constants()
    flat=' '.join(guide.split())
    for phrase in (c.TEXT['orphan'],c.STATE_TEXT['ORPHAN_RUNNING'],c.TEXT['self_stopped'].replace('{code}','N'),c.TEXT['start_slow'],
        'Папка уже содержит другие файлы. Выберите пустую папку или прежнюю папку установки.','proxy-tools','setuptools','js_api',
        'PFDESK_DEV_*','.git','Restart Manager'):
        assert ' '.join(phrase.split()) in flat,phrase
    assert 'бот работает без управляющего процесса' in readme and 'Бот остановился сам' in readme

def test_user_guide_covers_round2_fixes():
    # round 2: Yes/No dialog, orphan only for window-started bots, session-mode TTL, stop before launch on logoff,
    # git working copy refused by the installer, run files the installer checks (pumpfun_desktop.iss BotRunning)
    flat=' '.join(text(ROOT/'docs'/'DESKTOP_APP_USER_RU.md').split())
    window=text(DESKTOP/'window.py');session=text(DESKTOP/'session_end.py');engine=text(APP/'desktop_engine.py')
    asking=re.search(r"asking='([^']+)'",window).group(1)
    assert 'MODE_TTL_S=60.' in session and "say('STOP_REQUESTED before launch: the launcher was not started')" in engine
    for phrase in (asking,'«Да»','«Нет»','По умолчанию выбрана «Нет»','только у бота, запущенного **из окна**','работает (03_START)',
        'через 60 секунд','STOP_REQUESTED before launch: the launcher was not started','`.git`',
        'app\\data\\clean_0762_launcher.lock','app\\data\\clean_0762_monitor.lock','app\\data\\clean_0762.db','engine_host.lock'):
        assert phrase in flat,phrase
