"""WP4 packaging (not shipped): update_manifest.py, build_portable.py on synthetic trees and on the real tree
(manifest check; one SLOW test builds the real ZIP and runs the 04_CHECK desktop tests unpacked, about a minute),
and a static parse of the Inno Setup script pumpfun_desktop.iss (Inno is not installed).
Run: .venv\\Scripts\\python.exe -I -m pytest -q packaging/desktop/tests (from ROOT)."""
import hashlib,importlib.util,json,os,re,shutil,subprocess,sys,zipfile
from pathlib import Path
import pytest
HERE=Path(__file__).resolve().parents[1]
ROOT=HERE.parents[1]
ISS=HERE/'pumpfun_desktop.iss'

def load(name):
    spec=importlib.util.spec_from_file_location('pfpack_'+name,HERE/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
um=load('update_manifest');bp=load('build_portable')
G=um.guard(ROOT)

def sha(b):return hashlib.sha256(b).hexdigest()

def tree(tmp_path,files=None,extras=True):
    """Small release: real app/release_guard.py, CRLF and LF text, a binary, plus files that must never ship."""
    root=tmp_path/'src';files=files or {
        '01_INSTALL.bat':b'@echo off\r\nexit /b 0\r\n','app/bootstrap.py':b'print(1)\n','app/sub/mod.py':b'x=1\r\n',
        'desktop/__init__.py':b'',"desktop/ui/status.html":'<b>PAPER</b>\r\n'.encode(),'desktop/assets/a.ico':b'\x00\r\n\x01',
        'docs/Р.md':'Тест\r\n'.encode()}
    for name,data in files.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    shutil.copy(ROOT/'app'/'release_guard.py',root/'app'/'release_guard.py')
    names=list(files)+['app/release_guard.py']
    manifest=dict(version=G.VERSION,build_revision='TEST_REV',files={n:sha((root/n).read_bytes().replace(b'\r\n',b'\n') if Path(n).suffix in G.TEXT_SUFFIXES else (root/n).read_bytes()) for n in names})
    um.write(root,manifest)
    if extras:
        for name,data in {'README.md':b'# r\r\n','requirements.txt':b'pytest\r\n','app/data/clean_0762.db':b'DB','app/.env':b'KEY=secret\n',
            'results/r.json':b'{}','packaging/desktop/x.py':b'','.venv/Scripts/python.exe':b'','.venv-ui/x.pyd':b'',
            'app/__pycache__/bootstrap.cpython-314.pyc':b'pyc','desktop/__pycache__/x.pyc':b''}.items():
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    return root

# ---- update_manifest.py ----
@pytest.mark.parametrize('name',['packaging/desktop/build_portable.py','results/x.json','app/data/clean_0762.db','app/data/desktop/engine_state.json',
    'app/.env','.venv/Scripts/python.exe','.venv-ui/pyvenv.cfg','dist/a.zip','app/__pycache__/a.pyc','desktop/x.pyc','BUILD_MANIFEST.json','../x','C:/x'])
def test_excluded_zones(name):
    assert um.excluded(name)

@pytest.mark.parametrize('name',['16_DESKTOP.bat','app/.env.example','desktop/ui/status.html','desktop/assets/app_idle.ico','docs/DESKTOP_APP_USER_RU.md','README_RU.txt'])
def test_shipped_paths_are_not_excluded(name):
    assert um.excluded(name) is None

def test_update_recomputes_adds_and_keeps_identity(tmp_path):
    root=tree(tmp_path)
    (root/'app'/'sub'/'mod.py').write_bytes(b'x=2\r\n');(root/'desktop'/'new.py').write_bytes(b'y=1\r\n')
    (root/'desktop'/'ui'/'log.html').write_bytes(b'<p>\n');(root/'16_DESKTOP.bat').write_bytes(b'@echo off\r\n')
    before=um.load(root)
    new,ch=um.update(root,['desktop','16_DESKTOP.bat'])
    assert ch['changed']==['app/sub/mod.py'] and ch['revision'] is None
    assert ch['added']==['desktop/new.py','desktop/ui/log.html','16_DESKTOP.bat']  # folder sorted, caches skipped, known kept
    assert list(new['files'])[:len(before['files'])]==list(before['files']) and new['version']==G.VERSION and new['build_revision']=='TEST_REV'
    assert new['files']['app/sub/mod.py']==sha(b'x=2\n') and new['files']['desktop/new.py']==sha(b'y=1\n')
    um.write(root,new)
    raw=(root/'BUILD_MANIFEST.json').read_bytes()
    assert raw==(json.dumps(new,indent=2,ensure_ascii=False)+'\n').encode() and b'\r\n' not in raw
    assert G.verify(root)['build_revision']=='TEST_REV'

def test_update_binary_is_hashed_raw(tmp_path):
    root=tree(tmp_path);(root/'desktop'/'assets'/'a.ico').write_bytes(b'\x00\r\n\x02')
    new,ch=um.update(root)
    assert ch['changed']==['desktop/assets/a.ico'] and new['files']['desktop/assets/a.ico']==sha(b'\x00\r\n\x02')

@pytest.mark.parametrize('bad',['app/data','app/.env','results','packaging','.venv','app/__pycache__/bootstrap.cpython-314.pyc','../outside.txt','missing.txt'])
def test_update_refuses_excluded_or_missing(tmp_path,bad):
    root=tree(tmp_path);(tmp_path/'outside.txt').write_text('x')
    with pytest.raises(ValueError,match='MANIFEST_'):um.update(root,[bad])

def test_update_check_mode_and_revision(tmp_path,capsys):
    root=tree(tmp_path);raw=(root/'BUILD_MANIFEST.json').read_bytes()
    assert um.main(['--root',str(root),'--check'])==0 and 'MANIFEST CURRENT' in capsys.readouterr().out
    (root/'app'/'bootstrap.py').write_bytes(b'print(2)\n')
    assert um.main(['--root',str(root),'--check'])==1 and (root/'BUILD_MANIFEST.json').read_bytes()==raw
    assert um.main(['--root',str(root)])==0 and 'release_guard.verify PASSED' in capsys.readouterr().out
    assert um.main(['--root',str(root),'--build-revision','NEXT'])==0 and um.load(root)['build_revision']=='NEXT'
    (root/'app'/'stray.py').write_text('')  # EXTRA_MODULE: the tool reports the failed verify
    assert um.main(['--root',str(root)])==1 and 'CLEAN_RELEASE_EXTRA_MODULE' in capsys.readouterr().out

def test_real_manifest_is_current():
    new,ch=um.update(ROOT)
    assert ch=={'changed':[],'added':[],'revision':None},ch
    assert new['version']=='v0.7.101.5' and new['build_revision']=='RUN_REVIEW_V1015'
    assert (ROOT/'BUILD_MANIFEST.json').read_bytes()==um.render(new)

# ---- build_portable.py ----
def test_portable_zip_layout_lf_and_verify(tmp_path):
    root=tree(tmp_path);out=tmp_path/'out'
    path,top,names=bp.build(root,out)
    assert path==out/('pumpfun_bot_'+G.VERSION+'_desktop.zip') and top=='pumpfun_bot_'+G.VERSION+'_desktop'
    m=um.load(root)
    with zipfile.ZipFile(path) as z:
        got=z.namelist();info=z.infolist()
        assert got==[top+'/'+n for n in list(m['files'])+['BUILD_MANIFEST.json','README.md','requirements.txt']]
        assert {i.date_time for i in info}=={bp.ZIP_DATE} and {i.compress_type for i in info}=={zipfile.ZIP_DEFLATED}
        assert z.read(top+'/01_INSTALL.bat')==b'@echo off\r\nexit /b 0\r\n' and z.read(top+'/docs/Р.md')=='Тест\n'.encode()  # .bat CRLF
        assert z.read(top+'/desktop/assets/a.ico')==b'\x00\r\n\x01' and z.read(top+'/README.md')==b'# r\n'
        assert z.read(top+'/BUILD_MANIFEST.json')==(root/'BUILD_MANIFEST.json').read_bytes()
        for n in m['files']:
            data=z.read(top+'/'+n)
            assert sha(data.replace(b'\r\n',b'\n') if n.endswith('.bat') else data)==m['files'][n],n
    for never in ('app/data','.env','results/','packaging/','.venv','__pycache__','.pyc'):
        assert not any(never in n for n in got),never
    r=bp.verify_zip(path,top,tmp_path)
    assert r['release_guard_verify']=='PASSED' and r['unpacked_files']==len(got)
    assert not list(tmp_path.glob('pfzip_*'))  # temporary unpack folder removed
    again,_,_=bp.build(root,tmp_path/'out2')
    assert again.read_bytes()==path.read_bytes()  # reproducible

def test_portable_refuses_a_tree_a_clean_start_would_refuse(tmp_path):
    root=tree(tmp_path);(root/'app'/'bootstrap.py').write_bytes(b'print(3)\n')
    with pytest.raises(RuntimeError,match='CLEAN_RELEASE_FILE_CHANGED'):bp.build(root,tmp_path/'o1')
    root=tree(tmp_path/'b');(root/'desktop'/'extra.py').write_bytes(b'')
    with pytest.raises(RuntimeError,match='DESKTOP_EXTRA_MODULE: desktop/extra.py'):bp.build(root,tmp_path/'o2')
    root=tree(tmp_path/'c',files={'app/bootstrap.py':b'print(1)\n','results/r.json':b'{}'})
    with pytest.raises(RuntimeError,match='PORTABLE_EXCLUDED_RESULTS'):bp.build(root,tmp_path/'o3')
    root=tree(tmp_path/'d',extras=False)
    with pytest.raises(RuntimeError,match='PORTABLE_FILE_MISSING: README.md'):bp.build(root,tmp_path/'o4')
    assert not list(tmp_path.rglob('*.zip'))

def test_portable_verify_detects_a_damaged_zip(tmp_path):
    root=tree(tmp_path);path,top,names=bp.build(root,tmp_path/'out')
    bad=tmp_path/'bad.zip'
    with zipfile.ZipFile(path) as src,zipfile.ZipFile(bad,'w') as dst:
        for i in src.infolist():dst.writestr(i,src.read(i).replace(b'print(1)',b'print(9)'))
    with pytest.raises(RuntimeError,match='PORTABLE_ZIP_HASH_MISMATCH: app/bootstrap.py'):bp.verify_zip(bad,top,tmp_path)
    with zipfile.ZipFile(path) as src,zipfile.ZipFile(bad,'w') as dst:
        for i in src.infolist():dst.writestr(i,src.read(i))
        dst.writestr(top+'/app/data/x.db',b'x')
    with pytest.raises(RuntimeError,match='PORTABLE_ZIP_BAD_ENTRY'):bp.verify_zip(bad,top,tmp_path)

# ---- Inno Setup script (static) ----
def iss():
    raw=ISS.read_bytes();assert raw.startswith(b'\xef\xbb\xbf')  # UTF-8 BOM: Inno reads the Russian texts as Unicode
    sections={};name=None
    for line in raw.decode('utf-8-sig').splitlines():
        s=line.strip()
        m=re.fullmatch(r'\[(\w+)\]',s)
        if m:name=m.group(1);sections.setdefault(name,[]);continue
        if name and s and not (name!='Code' and s.startswith(';')):sections[name].append(s if name!='Code' else line)
    return raw.decode('utf-8-sig'),sections

def entries(lines):
    """Inno entry lines (Param: value; Param: "quoted, "" inside") -> dicts; fails on unbalanced quotes."""
    out=[]
    for line in lines:
        d={};pos=0
        while pos<len(line):
            m=re.match(r'\s*(\w+):\s*',line[pos:]);assert m,line
            key=m.group(1);pos+=m.end()
            if line[pos:pos+1]=='"':
                m=re.match(r'"((?:[^"]|"")*)"',line[pos:]);assert m,line
                value=m.group(1).replace('""','"')
            else:
                m=re.match(r'[^;]*',line[pos:]);value=m.group(0).strip()
            pos+=m.end();d[key]=value
            m=re.match(r'\s*(;|$)',line[pos:]);assert m,line;pos+=m.end()
        out.append(d)
    return out

def test_iss_setup_is_per_user_in_localappdata():
    text,sec=iss()
    setup=dict(l.split('=',1) for l in sec['Setup'])
    expected=dict(PrivilegesRequired='lowest',DefaultDirName='{localappdata}\\Programs\\PumpfunPaperBot',UsePreviousAppDir='yes',
        DisableDirPage='auto',ArchitecturesInstallIn64BitMode='x64compatible',CloseApplications='no')
    for k,v in expected.items():assert setup.get(k)==v,k
    assert setup['AppId']=='{{{#AppGuid}}' and re.search(r'#define AppGuid "[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}"',text)
    assert '#define AppVersion "0.7.101.5"' in text
    low=text.lower()
    for bad in ('{pf}','{commonpf}','{pf32}','{pf64}','{commonappdata}','{commondesktop}','admin','privilegesrequiredoverridesallowed','alwaysrestart'):
        assert bad not in low,bad
    assert set(sec)=={'Setup','Languages','Tasks','Files','Icons','Run','UninstallDelete','Code'}  # no [Registry], no [Dirs]

def test_iss_files_come_from_the_stage_without_user_data():
    text,sec=iss()
    files=entries(sec['Files'])
    assert len(files)==1 and files[0]['Source']=='{#StageDir}\\*' and files[0]['DestDir']=='{app}'
    assert set(files[0]['Flags'].split())=={'ignoreversion','recursesubdirs','createallsubdirs'}
    ex=files[0]['Excludes'].split(',')
    for need in ('\\app\\data','\\app\\.env','\\results','\\.venv','\\.venv-ui','\\packaging','__pycache__','*.pyc'):assert need in ex,need
    for entry in entries(sec['UninstallDelete']):
        name=entry['Name'].lower()
        assert name.startswith('{app}\\') and not any(k in name for k in ('\\app\\data','.env','\\results')),name
        assert name.rstrip('\\')!='{app}'

def test_iss_icons_and_run_match_the_bat_files():
    text,sec=iss()
    icons=entries(sec['Icons']);main=[i for i in icons if i['Filename'].endswith('pythonw.exe')]
    assert len(main)==2 and all(i['Filename']=='{app}\\.venv-ui\\Scripts\\pythonw.exe' and i['Parameters']=='-I "{app}\\desktop\\app_main.py"'
        and i['WorkingDir']=='{app}' for i in main)
    assert main[1]['Tasks']=='desktopicon' and entries(sec['Tasks'])[0]['Flags']=='unchecked'
    run=entries(sec['Run'])
    # 01_INSTALL / 16_DESKTOP --prepare run from [Code] (exit codes checked); [Run] only offers the window
    assert len(run)==1 and run[0]['Filename']=='{app}\\.venv-ui\\Scripts\\pythonw.exe' and run[0]['Parameters']=='-I "{app}\\desktop\\app_main.py"'
    assert set(run[0]['Flags'].split())=={'postinstall','nowait','skipifsilent','unchecked'} and run[0]['Check']=='WindowReady'
    code='\n'.join(sec['Code'])
    assert "RunBat('01_INSTALL.bat', ''," in code and "RunBat('16_DESKTOP.bat', ' --prepare'," in code
    inst=code[code.index('procedure InstallEnvironments'):code.index('procedure CurStepChanged')]
    assert inst.index("RunBat('01_INSTALL.bat'")<inst.index("RunBat('16_DESKTOP.bat'")<inst.index('EnvReady := True;')
    assert inst.count('if Code <> 0 then')==2 and inst.count('Exit;')==2 and inst.count('BatFailedText(')==2
    assert 'if CurStep = ssPostInstall then\n    InstallEnvironments();' in code.replace('\r','')
    assert 'function WindowReady(): Boolean;' in code and 'Result := EnvReady;' in code
    assert '--prepare' in (ROOT/'16_DESKTOP.bat').read_text(encoding='utf-8')

def test_iss_code_refuses_a_running_bot_and_cleans_old_modules():
    text,sec=iss();code='\n'.join(sec['Code'])
    for fn in ('function InitializeSetup(): Boolean;','function PrepareToInstall(var NeedsRestart: Boolean): String;','function InitializeUninstall(): Boolean;'):
        assert fn in code,fn
    for need in ("'\\app\\data\\clean_0762_launcher.lock'","'\\app\\data\\desktop\\engine_host.lock'","'\\app\\data\\desktop\\engine_state.json'",
        "fmOpenRead or fmShareExclusive","'PREFLIGHT'","'STARTING'","'RUNNING'","'STOP_REQUESTED'","ProcessAlive(JsonNumber(Text, 'pid'))",
        "DeletePyFiles(Dir + '\\app', Dir + '\\app\\data');","DeletePyFiles(Dir + '\\desktop', '');","FILE_ATTRIBUTE_REPARSE_POINT",
        "'{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'","'Software\\Python\\PythonCore\\3.14'"):
        assert need in code,need
    # PrepareToInstall checks the bot before it deletes anything
    prep=code[code.index('function PrepareToInstall'):code.index('function InitializeUninstall')]
    assert prep.index('BotRunning(Dir)')<prep.index('DeletePyFiles')
    # the engine host and launcher lock names are the ones the code uses
    engine=(ROOT/'app'/'desktop_engine.py').read_text(encoding='utf-8');launcher=(ROOT/'app'/'launcher.py').read_text(encoding='utf-8')
    assert "LOCK_NAME='engine_host.lock'" in engine and "STATE_NAME='engine_state.json'" in engine and "'_launcher.lock'" in launcher
    assert "PHASES=('PREFLIGHT','STARTING','RUNNING','STOP_REQUESTED','STOPPED','FAILED')" in engine and 'ACTIVE_PHASES=PHASES[:4]' in engine

def test_iss_code_is_balanced():
    text,sec=iss()
    code=re.sub(r"'(?:[^']|'')*'","''",'\n'.join(sec['Code']))  # strings out
    code=re.sub(r'\{[^}]*\}','',code)                             # { comments } out
    words=re.findall(r'\b(begin|end|try|case|repeat|until)\b',code,re.I)
    opened=sum(w.lower() in ('begin','try','case') for w in words);closed=sum(w.lower()=='end' for w in words)
    assert opened==closed and sum(w.lower()=='repeat' for w in words)==sum(w.lower()=='until' for w in words)
    assert code.count('(')==code.count(')')
    for m in re.finditer(r'\{#(\w+)\}',text):assert '#define '+m.group(1) in text,m.group(1)


# ---- review fixes: AppMutex (MINOR 8a), foreign folder (M4), non-interactive 01_INSTALL (MINOR 8b) ----
def test_iss_app_mutex_is_the_window_mutex():
    text,sec=iss()
    setup=dict(l.split('=',1) for l in sec['Setup'])
    constants=(ROOT/'desktop'/'constants.py').read_text(encoding='utf-8')
    m=re.search(r"^APP_MUTEX='([^']+)'",constants,re.M)
    assert m and setup['AppMutex']==m.group(1)=='PumpfunPaperDesktopApp'
    assert 'app_mutex_name=C.APP_MUTEX' in (ROOT/'desktop'/'single_instance.py').read_text(encoding='utf-8')

def own_install_patterns(code):
    body=code[code.index('function OwnInstall'):code.index('function ForeignDir')]
    body=body[:body.index('\nend;')+5]
    return body,re.findall(r"Pos\('((?:[^']|'')*)', Text\) > 0",body)

FOREIGN="'Папка уже содержит другие файлы. Выберите пустую папку или прежнюю папку установки.'"

def test_iss_own_install_recognises_only_this_product():
    text,sec=iss();code='\n'.join(sec['Code'])
    body,pats=own_install_patterns(code)
    assert pats==['"version": "v0.7.','"build_revision": "','"files": {','"app/release_guard.py": "','"app/bootstrap.py": "']
    assert "if not FileExists(AppDir + '\\app\\release_guard.py') then Exit;" in body
    assert "LoadStringFromFile(AppDir + '\\BUILD_MANIFEST.json', Raw)" in body and 'Result := False;' in body
    assert body.count(') and')==len(pats)-1 and ' or ' not in body  # every pattern is required
    def own(raw):return all(p in raw for p in pats)
    real=(ROOT/'BUILD_MANIFEST.json').read_text(encoding='utf-8')
    assert own(real) and own(real.replace('\n','\r\n')) and own(um.render(um.load(ROOT)).decode('utf-8'))
    for name in ('app/release_guard.py','app/bootstrap.py'):assert name in json.loads(real)['files']
    assert json.loads(real)['version'].startswith('v0.7.')
    # other products / damaged manifests are not ours
    assert not own(real.replace('"version": "v0.7.','"version": "v1.0.'))
    assert not own(real.replace('"app/release_guard.py": "','"app/other.py": "'))
    assert not own(json.dumps(dict(version='v0.7.1',build_revision='x',files={'app/release_guard.py':'x'})))  # compact JSON
    assert not own('{"name": "some-other-app", "files": ["app/release_guard.py"]}')

def test_iss_deletes_old_modules_only_in_an_own_install_and_refuses_a_foreign_folder():
    text,sec=iss();code='\n'.join(sec['Code'])
    assert 'FOREIGN_TEXT = '+FOREIGN+';' in code
    foreign=code[code.index('function ForeignDir'):code.index('procedure DeletePyFiles')]
    for need in ("DirExists(AppDir + '\\app')","DirExists(AppDir + '\\desktop')",'and not OwnInstall(AppDir)'):assert need in foreign,need
    prep=code[code.index('function PrepareToInstall'):code.index('function RunBat')]
    # foreign folder -> abort before anything else; then the running-bot check; deletion only inside OwnInstall
    order=[prep.index(s) for s in ('if ForeignDir(Dir) then','Result := FOREIGN_TEXT;','BotRunning(Dir)','if OwnInstall(Dir) then')]
    assert order==sorted(order)
    own=prep[prep.index('if OwnInstall(Dir) then'):]
    order=[own.index(s) for s in ('begin',"DeletePyFiles(Dir + '\\app', Dir + '\\app\\data');","DeletePyFiles(Dir + '\\desktop', '');",'end;')]
    assert order==sorted(order)
    assert code.count('DeletePyFiles(Dir')==2 and code.count('DeletePyFiles(')==4  # two calls + recursion + declaration
    # directory page: the same refusal, so another folder can be chosen
    nb=code[code.index('function NextButtonClick'):code.index('function PrepareToInstall')]
    assert 'CurPageID = wpSelectDir' in nb and 'ForeignDir(RemoveBackslashUnlessRoot(WizardDirValue()))' in nb
    assert 'SuppressibleMsgBox(FOREIGN_TEXT, mbError, MB_OK, IDOK);' in nb and 'Result := False;' in nb

RUNBAT_ARGS="'/d /c \"\"' + ExpandConstant('{app}') + '\\' + Bat + '\"' + Args + ' <nul\"'"

def test_iss_runs_the_install_bats_with_stdin_from_nul():
    text,sec=iss();code='\n'.join(sec['Code'])
    rb=code[code.index('function RunBat'):code.index('function BatFailedText')]
    assert RUNBAT_ARGS in rb and "Exec(ExpandConstant('{cmd}'), " in rb
    assert 'SW_SHOWNORMAL, ewWaitUntilTerminated, Code)' in rb and 'Code := -1;' in rb  # console stays visible
    assert 'runhidden' not in text.lower()
    # only a final "pause" waits for input in both bats and in what they run (venv, pip, bootstrap install)
    for bat in ('01_INSTALL.bat','16_DESKTOP.bat'):
        t=(ROOT/bat).read_text(encoding='utf-8').lower()
        assert t.count('pause')>=1 and re.findall(r'(?m)^\s*(pause|set /p|choice|timeout)\b',t)==['pause']*t.count('pause'),bat
    boot=(ROOT/'app'/'bootstrap.py').read_text(encoding='utf-8')
    assert boot.count('input(')==1 and boot.index("if command=='preserve-data':")<boot.index('input(')<boot.index("if command=='review-archive':")
    launcher=(ROOT/'app'/'launcher.py').read_text(encoding='utf-8')
    assert 'input(' not in launcher and 'getpass' not in launcher

def comspec():return os.environ.get('ComSpec','cmd.exe')

def runbat_line(app,bat,args):
    """The command line RunBat builds (RUNBAT_ARGS), with the installation folder expanded."""
    return '"'+comspec()+'" '+'/d /c ""'+app+'\\'+bat+'"'+args+' <nul"'

def run_open_stdin(line,cwd,timeout):
    """Run with a stdin pipe that stays open and is never written (an interactive console that nobody answers)."""
    proc=subprocess.Popen(line,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,cwd=cwd)
    try:
        code=proc.wait(timeout=timeout)
        return code,proc.stdout.read()
    finally:
        if proc.poll() is None:proc.kill();proc.wait(10)
        proc.stdin.close();proc.stdout.close()

@pytest.mark.skipif(os.name!='nt',reason='cmd.exe only')
def test_runbat_command_line_returns_through_pause_and_keeps_the_exit_code(tmp_path):
    app=tmp_path/'app dir';app.mkdir()
    (app/'x.bat').write_bytes(b'@echo off\r\necho [%~1]\r\necho Failed. Keep this window and copy the error text.\r\npause\r\nexit /b 7\r\n')
    code,out=run_open_stdin(runbat_line(str(app),'x.bat',' --prepare'),app,30)
    assert code==7 and b'[--prepare]' in out and b'Failed.' in out
    code,out=run_open_stdin(runbat_line(str(app),'x.bat',''),app,30)
    assert code==7 and b'[]' in out
    with pytest.raises(subprocess.TimeoutExpired):  # control: the same bat without <nul waits in pause
        run_open_stdin('"'+comspec()+'" /d /c ""'+str(app/'x.bat')+'""',app,3)

# ---- MINOR 11: CRLF .bat in the ZIP still passes release_guard (CRLF read as LF) ----
def test_portable_bat_files_are_crlf_and_release_guard_accepts_them(tmp_path):
    files={'01_INSTALL.bat':b'@echo off\ngoto x\n:x\nexit /b 0\n','16_DESKTOP.bat':b'@echo off\r\nrem mixed\nexit /b 0\r\n',
        '03_START.bat':b'@echo off\r\nexit /b 0\r\n','app/bootstrap.py':b'print(1)\n','desktop/__init__.py':b''}
    root=tree(tmp_path,files=files)
    path,top,names=bp.build(root,tmp_path/'out')
    m=um.load(root);bats=[n for n in m['files'] if n.endswith('.bat')]
    assert len(bats)==3
    with zipfile.ZipFile(path) as z:
        for n in bats:
            data=z.read(top+'/'+n)
            assert data.count(b'\n')==data.count(b'\r\n')>0 and b'\r\r' not in data,n  # CRLF throughout
            assert sha(data)!=m['files'][n] and sha(data.replace(b'\r\n',b'\n'))==m['files'][n],n
        assert z.read(top+'/app/bootstrap.py')==b'print(1)\n'
        z.extractall(tmp_path/'u')
    unpacked=tmp_path/'u'/top
    g=um.guard(unpacked)
    for n in bats:assert g.lf_digest(unpacked/n)==m['files'][n],n  # release_guard's own LF-normalised hash
    assert g.verify(unpacked)['version']==G.VERSION  # the shipped release check accepts the CRLF .bat files
    assert bp.verify_zip(path,top,tmp_path)['release_guard_verify']=='PASSED'
    # a .bat with a bare LF in the ZIP is refused by the build check
    bad=tmp_path/'bad.zip'
    with zipfile.ZipFile(path) as src,zipfile.ZipFile(bad,'w') as dst:
        for i in src.infolist():
            data=src.read(i)
            dst.writestr(i,data.replace(b'\r\n',b'\n',1) if i.filename.endswith('/03_START.bat') else data)
    with pytest.raises(RuntimeError,match='PORTABLE_ZIP_NOT_CRLF: 03_START.bat'):bp.verify_zip(bad,top,tmp_path)

@pytest.mark.skipif(os.name!='nt',reason='cmd.exe only')
def test_crlf_bat_from_the_zip_finds_its_labels(tmp_path):
    # a label far past the first 512 bytes, reached by goto: the case cmd.exe can get wrong with LF-only files
    body=b'@echo off\nrem '+b'x'*600+b'\ngoto target\necho WRONG\nexit /b 3\n:target\necho LABEL_OK\nexit /b 0\n'
    root=tree(tmp_path,files={'01_INSTALL.bat':body,'app/bootstrap.py':b'print(1)\n','desktop/__init__.py':b''})
    path,top,names=bp.build(root,tmp_path/'out')
    with zipfile.ZipFile(path) as z:z.extract(top+'/01_INSTALL.bat',tmp_path/'u')
    bat=tmp_path/'u'/top/'01_INSTALL.bat'
    assert b'\r\n:target\r\n' in bat.read_bytes()
    r=subprocess.run(['cmd','/d','/c',str(bat)],stdin=subprocess.DEVNULL,capture_output=True,timeout=30)
    assert r.returncode==0 and b'LABEL_OK' in r.stdout and b'WRONG' not in r.stdout

# ---- round 2: git working copy is foreign (MAJOR-2), orphaned bot processes (MINOR-1) ----
def section(code,start,end):return code[code.index(start):code.index(end)]

def own_install_model(d,pats):
    """GitCheckout / OwnInstall / ForeignDir of the .iss, in Python (the static asserts pin the Pascal text to it)."""
    git=(d/'.git').exists()
    m=d/'BUILD_MANIFEST.json'
    own=not git and (d/'app'/'release_guard.py').is_file() and m.is_file() and all(p in m.read_text(encoding='utf-8') for p in pats)
    foreign=git or (any((d/n).exists() for n in ('app','desktop')) and not own)
    return foreign,own

def test_iss_a_git_working_copy_is_foreign_and_never_loses_its_modules(tmp_path):
    text,sec=iss();code='\n'.join(sec['Code'])
    git=section(code,'function GitCheckout','function OwnInstall')
    assert "Result := DirExists(AppDir + '\\.git') or FileExists(AppDir + '\\.git');" in git  # worktree/submodule: .git is a file
    own,pats=own_install_patterns(code)
    order=[own.index(s) for s in ('Result := False;','if GitCheckout(AppDir) then Exit;',"if not FileExists(AppDir + '\\app\\release_guard.py') then Exit;",'LoadStringFromFile(')]
    assert order==sorted(order)  # .git -> False before any other test
    foreign=section(code,'function ForeignDir','procedure DeletePyFiles')
    assert "Result := GitCheckout(AppDir) or ((DirExists(AppDir + '\\app') or" in foreign and foreign.count('GitCheckout(')==1
    assert code.index('function GitCheckout')<code.index('function OwnInstall')<code.index('function ForeignDir')  # declared before use
    prep=section(code,'function PrepareToInstall','function RunBat')
    assert prep.index('if ForeignDir(Dir) then')<prep.index('if OwnInstall(Dir) then')<prep.index('DeletePyFiles(')
    # the case the round-2 critic found: a checkout has BUILD_MANIFEST.json v0.7 and app\release_guard.py
    inst=tmp_path/'inst';(inst/'app').mkdir(parents=True)
    shutil.copy(ROOT/'BUILD_MANIFEST.json',inst);shutil.copy(ROOT/'app'/'release_guard.py',inst/'app')
    assert own_install_model(inst,pats)==(False,True)  # an installation (no .git): updated in place
    (inst/'.git').mkdir()
    assert own_install_model(inst,pats)==(True,False)  # git clone
    (inst/'.git').rmdir();(inst/'.git').write_text('gitdir: C:/repo/.git/worktrees/x\n')
    assert own_install_model(inst,pats)==(True,False)  # git worktree
    bare=tmp_path/'bare';bare.mkdir();(bare/'.git').mkdir()
    assert own_install_model(bare,pats)==(True,False)  # .git alone is enough
    assert own_install_model(tmp_path/'empty',pats)==(False,False)
    if (ROOT/'.git').exists():assert own_install_model(ROOT,pats)==(True,False)  # this very checkout

RUN_FILES=['app\\data\\clean_0762_launcher.lock','app\\data\\clean_0762_monitor.lock','app\\data\\clean_0762.db','app\\data\\desktop\\engine_host.lock']

def test_iss_bot_running_sees_every_file_a_run_keeps_open():
    text,sec=iss();code='\n'.join(sec['Code'])
    body=section(code,'function BotRunning','function RunningText')
    held=re.findall(r"if FileHeld\(AppDir \+ '\\([^']+)'\) then\s+begin\s+Result := '([^']+)';\s+Exit;\s+end;",body)
    assert [a for a,b in held]==RUN_FILES and all(a==b for a,b in held)
    assert body.count('FileHeld(')==len(RUN_FILES) and body.rindex('FileHeld(')<body.index("'\\app\\data\\desktop\\engine_state.json'")
    # names and open modes as the Python code has them (app\data = folder of config.DB; default clean_0762.db)
    def src(name):return (ROOT/'app'/name).read_text(encoding='utf-8')
    assert 'DB=os.getenv("DATABASE_PATH","data/clean_0762.db")' in src('config.py') and 'DATABASE_PATH=data/clean_0762.db' in src('.env.example')
    launcher=src('launcher.py')
    assert "lock=Path(config.DB).resolve().with_name(Path(config.DB).stem+'_launcher.lock')\n    with monitor_service.singleton(lock):" in launcher
    monitor=src('monitor_service.py')
    assert "lock=base.with_name(base.stem+'_monitor.lock')" in monitor and "singleton(pths['lock'])" in monitor
    single=section(monitor,'def singleton(path):','def read_state')
    assert "f=path.open('a+b')" in single and 'msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)' in single and 'yield' in single
    database=src('database.py')
    assert 'c=sqlite3.connect(config.DB,' in database and '_LOCAL.conn=c' in database  # one open connection per process
    engine=src('desktop_engine.py')
    assert "LOCK_NAME='engine_host.lock'" in engine and "stream=open(Path(control or CONTROL_DIR)/LOCK_NAME,'a+b')" in engine
    # no other lock file is held for a whole run (rpc_control slot/publisher locks: only around one RPC call)
    for name in ('main.py','dashboard/server.py'):
        assert not re.search(r"\.lock'|msvcrt|fcntl",src(name)),name

@pytest.mark.skipif(os.name!='nt',reason='Win32 share modes')
def test_file_held_rule_sees_python_locks_and_sqlite(tmp_path):
    """FileHeld = TFileStream.Create(fmOpenRead or fmShareExclusive) = CreateFileW(GENERIC_READ, share 0)."""
    import ctypes,msvcrt,sqlite3
    from ctypes import wintypes
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.restype=wintypes.HANDLE
    k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    def held(path):
        h=k.CreateFileW(str(path),0x80000000,0,None,3,0x80,None)
        if h in (None,ctypes.c_void_p(-1).value):return ctypes.get_last_error()==32  # ERROR_SHARING_VIOLATION
        k.CloseHandle(h);return False
    lock=tmp_path/'clean_0762_monitor.lock'
    f=lock.open('a+b');f.seek(0)
    if f.read(1)==b'':f.write(b'0');f.flush()
    f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)  # monitor_service.singleton
    try:assert held(lock)
    finally:f.close()
    assert not held(lock)
    db=tmp_path/'clean_0762.db';c=sqlite3.connect(db);c.execute('CREATE TABLE t(x)')
    try:assert held(db)
    finally:c.close()
    assert not held(db)

# ---- round 2 BLOCKER guard: 04_CHECK's desktop tests in the unpacked ZIP (no .git) ----
PLAN=b"TECHNICAL_TEST_FILES=('tests/test_other.py','../desktop/tests/test_desktop_x.py','tests/test_desktop_engine_v1015.py','tests/test_desktop_release_v1015.py')\n"
LIKE_CHECK=(b"import os\nfrom pathlib import Path\nROOT=Path(__file__).resolve().parents[2]\n"
    b"def test_runs_like_04_check():\n"
    b"    assert Path.cwd()==ROOT/'app' and not (ROOT/'.git').exists()\n"
    b"    for name in ('PFDESK_BOT_PYTHON','PYTHONPATH','DATABASE_PATH','DASHBOARD_PORT','PYTEST_ADDOPTS'):assert name not in os.environ,name\n"
    b"    assert os.environ.get('PYTHONUTF8')=='1'\n")
NEEDS_GIT=b"from pathlib import Path\ndef test_needs_git():\n    assert (Path(__file__).resolve().parents[2]/'.git').exists()\n"

def plan_tree(tmp_path,desktop_test,plan=PLAN):
    return tree(tmp_path,files={'app/bootstrap.py':b'print(1)\n','desktop/__init__.py':b'','app/check_plan_v972.py':plan,
        'desktop/tests/test_desktop_x.py':desktop_test,'app/tests/test_other.py':b'def test_other():\n    assert False\n',
        'app/tests/test_desktop_engine_v1015.py':b'def test_engine():pass\n','app/tests/test_desktop_release_v1015.py':b'def test_release():pass\n'})

@pytest.fixture
def leaky_env(monkeypatch):
    """Variables a developer shell has and a user's 04_CHECK does not: the unpacked run must not see them."""
    for k,v in dict(PFDESK_BOT_PYTHON='x',PYTHONPATH='x',DATABASE_PATH='x',DASHBOARD_PORT='1',PYTEST_ADDOPTS='-p no:doctest').items():monkeypatch.setenv(k,v)

def test_unpacked_desktop_tests_run_like_04_check(tmp_path,capsys,leaky_env):
    root=plan_tree(tmp_path,LIKE_CHECK);out=tmp_path/'out';work=tmp_path/'w';work.mkdir()
    path,top,names=bp.build(root,out)
    r=bp.verify_zip(path,top,work,python=sys.executable)['desktop_tests_unpacked']
    assert r['status']=='PASSED' and r['files']==3 and r['passed']==3 and r['skipped']==0  # test_other.py is not a desktop test
    assert bp.main(['--root',str(root),'--out',str(out),'--work',str(work)])==0
    summary=json.loads(capsys.readouterr().out)
    assert summary['status']=='PASSED' and summary['desktop_tests_unpacked']['passed']==3
    assert not list(work.iterdir())  # unpack folder, basetemp and junit report removed

def test_unpacked_desktop_test_failure_fails_the_build(tmp_path,capsys):
    root=plan_tree(tmp_path,NEEDS_GIT);out=tmp_path/'out'
    path,top,names=bp.build(root,out)
    with pytest.raises(RuntimeError,match=r'PORTABLE_DESKTOP_TESTS_FAILED: exit 1: .*test_needs_git'):bp.verify_zip(path,top,tmp_path,python=sys.executable)
    assert bp.main(['--root',str(root),'--out',str(out),'--work',str(tmp_path)])==1
    assert json.loads(capsys.readouterr().out)['status']=='FAILED'
    root=plan_tree(tmp_path/'b',LIKE_CHECK,plan=PLAN.replace(b",'tests/test_desktop_release_v1015.py'",b''))
    path,top,names=bp.build(root,tmp_path/'o2')
    with pytest.raises(RuntimeError,match='PORTABLE_CHECK_PLAN_WITHOUT_DESKTOP_TESTS: tests/test_desktop_release_v1015.py'):
        bp.verify_zip(path,top,tmp_path,python=sys.executable)
    inside=tmp_path/'repo';(inside/'x'/'app').mkdir(parents=True);(inside/'.git').write_text('gitdir: y\n')
    with pytest.raises(RuntimeError,match='PORTABLE_TESTS_INSIDE_GIT'):bp.run_desktop_tests(inside/'x',sys.executable,tmp_path)

def test_real_zip_unpacked_without_git_passes_the_04_check_desktop_tests(tmp_path,leaky_env):
    """SLOW (about a minute): the real tree's ZIP, unpacked where there is no .git, runs the desktop tests of
    04_CHECK (check_plan_v972) with this interpreter (the bot venv) from its app/ folder. build_portable.py main
    does the same on every build."""
    path,top,names=bp.build(ROOT,tmp_path/'o')
    d=bp.verify_zip(path,top,tmp_path,python=sys.executable)['desktop_tests_unpacked']
    spec=importlib.util.spec_from_file_location('pf_real_check_plan',ROOT/'app'/'check_plan_v972.py')
    plan=importlib.util.module_from_spec(spec);spec.loader.exec_module(plan)
    expected=[p for p in plan.TECHNICAL_TEST_FILES if p.startswith('../desktop/tests/')]+list(bp.DESKTOP_APP_TESTS)
    assert len(expected)==9 and d['files']==len(expected) and d['status']=='PASSED'
    assert d['skipped_tests']==[] and d['passed']>200,d
