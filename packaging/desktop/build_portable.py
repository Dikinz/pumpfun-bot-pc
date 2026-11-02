"""Portable desktop ZIP (desktop spec 8). Not shipped, stdlib only.

  python -I packaging/desktop/build_portable.py [--root ROOT] [--out DIR] [--top NAME]

Same rules as the release ZIP: exactly the BUILD_MANIFEST.json files (release_guard.verify first, plus the
desktop EXTRA_MODULE rule) in manifest order, then BUILD_MANIFEST.json, README.md and requirements.txt, all
under one top folder (default pumpfun_bot_<version>_desktop). Text suffixes (release_guard.TEXT_SUFFIXES) are
written with LF, except .bat files, which get CRLF (cmd.exe can miss labels/goto targets in LF-only batch files;
release_guard hashes text with CRLF read as LF, so the manifest hash is the same); binaries raw; fixed timestamp
and mode, so the same tree gives the same bytes. Nothing from .venv*, app/data, .env, results, packaging or
caches. The result is unpacked into a fresh temporary folder, every file is compared with its manifest hash
(byte-exactly; a .bat must be CRLF throughout and is compared with CRLF read as LF) and the unpacked tree's own
app/release_guard.verify runs there. Then (round-2 BLOCKER guard: 04_CHECK failed in a release folder, which has
no .git) the desktop tests 04_CHECK runs are run in that unpacked tree exactly as launcher.call does it:
<python> -m pytest -q <files> from its app/ folder, with the 04_CHECK environment (bootstrap.main clears
PYTHONPATH/PYTHONHOME/DATABASE_PATH/DASHBOARD_PORT; no PFDESK_* or PYTEST_* variables). <python> is --python,
default this interpreter: run the build with the bot venv python (pytest, psutil, dotenv; no pywebview), as .venv
is. Files: the ../desktop/tests entries of the unpacked app/check_plan_v972.TECHNICAL_TEST_FILES plus
DESKTOP_APP_TESTS. Any failure or error fails the build (about a minute). Prints one JSON summary; exit 0 only
when the ZIP was built and verified."""
import argparse,hashlib,importlib.util,json,os,shutil,subprocess,sys,tempfile,time,zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
EXTRAS=('BUILD_MANIFEST.json','README.md','requirements.txt')
ZIP_DATE=(2026,11,1,0,0,0)
MODE=0o644<<16
CRLF_SUFFIXES=frozenset({'.bat'})  # written with CRLF; must also be a release_guard text suffix (hashed CRLF-as-LF)
DESKTOP_APP_TESTS=('tests/test_desktop_engine_v1015.py','tests/test_desktop_release_v1015.py')
CHECK_CLEAR_ENV=('PYTHONPATH','PYTHONHOME','DATABASE_PATH','DASHBOARD_PORT')  # bootstrap.main before `check`
CHECK_ENV_PREFIXES=('PFDESK_','PYTEST_')  # a user's 04_CHECK has none of them
DESKTOP_TESTS_TIMEOUT_S=900

def _load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
um=_load('pf_update_manifest',HERE/'update_manifest.py')

def desktop_extra(root,files):
    """desktop/**/*.py outside __pycache__ that the manifest does not list (desktop/release_check rule)."""
    root=Path(root);out=[]
    for p in sorted((root/'desktop').rglob('*.py')) if (root/'desktop').is_dir() else ():
        rel=p.relative_to(root)
        if '__pycache__' not in rel.parts and rel.as_posix() not in files:out.append(rel.as_posix())
    return out

def check_source(root):
    """(manifest, release_guard). Refuses a tree that a clean start would refuse, and excluded zones."""
    root=Path(root).resolve();g=um.guard(root)
    if not CRLF_SUFFIXES<=g.TEXT_SUFFIXES:raise RuntimeError('PORTABLE_CRLF_SUFFIX_NOT_TEXT')
    manifest=g.verify(root)
    for name in manifest['files']:
        reason=um.excluded(name)
        if reason:raise RuntimeError('PORTABLE_'+reason+': '+name)
    extra=desktop_extra(root,manifest['files'])
    if extra:raise RuntimeError('DESKTOP_EXTRA_MODULE: '+extra[0])
    for name in EXTRAS:
        if not (root/name).is_file():raise RuntimeError('PORTABLE_FILE_MISSING: '+name)
    return manifest,g

def payload(root,name,g):
    data=(Path(root)/name).read_bytes();suffix=Path(name).suffix.lower()
    if suffix not in g.TEXT_SUFFIXES:return data
    data=data.replace(b'\r\n',b'\n')
    return data.replace(b'\n',b'\r\n') if suffix in CRLF_SUFFIXES else data

def zip_digest(name,data):
    """Manifest hash of one unpacked file: raw bytes, or for a CRLF suffix (no bare LF allowed) CRLF read as LF."""
    if Path(name).suffix.lower() in CRLF_SUFFIXES:
        if data.count(b'\n')!=data.count(b'\r\n'):raise RuntimeError('PORTABLE_ZIP_NOT_CRLF: '+name)
        data=data.replace(b'\r\n',b'\n')
    return hashlib.sha256(data).hexdigest()

def entry(top,name):
    info=zipfile.ZipInfo(top+'/'+name,ZIP_DATE)
    info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=0;info.external_attr=MODE
    return info

def build(root=ROOT,out=None,top=None):
    """Write the ZIP atomically; returns (path, top, names in archive order)."""
    root=Path(root).resolve();manifest,g=check_source(root)
    top=top or 'pumpfun_bot_'+manifest['version']+'_desktop'
    if '/' in top or '\\' in top or top in ('','.','..'):raise RuntimeError('PORTABLE_BAD_TOP')
    out=Path(out or root/'dist').resolve();out.mkdir(parents=True,exist_ok=True)
    target=out/(top+'.zip');names=list(manifest['files'])+[n for n in EXTRAS if n not in manifest['files']]
    fd,tmp=tempfile.mkstemp(prefix='.'+top+'.',suffix='.zip.tmp',dir=out);os.close(fd)
    try:
        with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
            for name in names:z.writestr(entry(top,name),payload(root,name,g))
        os.replace(tmp,target)
    except BaseException:
        try:os.unlink(tmp)
        except OSError:pass
        raise
    return target,top,names

def desktop_test_files(tree):
    """04_CHECK's desktop tests, taken from the unpacked tree's own check plan."""
    plan=_load('pf_unpacked_check_plan',Path(tree)/'app'/'check_plan_v972.py').TECHNICAL_TEST_FILES
    files=[p for p in plan if p.startswith('../desktop/tests/')]
    missing=[p for p in DESKTOP_APP_TESTS if p not in plan]
    if missing or not files:raise RuntimeError('PORTABLE_CHECK_PLAN_WITHOUT_DESKTOP_TESTS: '+(missing[0] if missing else '../desktop/tests'))
    return files+list(DESKTOP_APP_TESTS)

def check_env(base=None):
    env={k:v for k,v in (os.environ if base is None else base).items()
        if k.upper() not in CHECK_CLEAR_ENV and not k.upper().startswith(CHECK_ENV_PREFIXES)}
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')  # 04_CHECK.bat: chcp 65001, PYTHONUTF8=1
    return env

def run_desktop_tests(tree,python,work,timeout=DESKTOP_TESTS_TIMEOUT_S):
    """The desktop part of 04_CHECK in the unpacked tree (never inside a git working copy): dict, or RuntimeError."""
    tree=Path(tree).resolve();app=tree/'app'
    git=[str(p) for p in (tree,*tree.parents) if (p/'.git').exists()]
    if git:raise RuntimeError('PORTABLE_TESTS_INSIDE_GIT: '+git[0])
    files=desktop_test_files(tree);xml=Path(work)/'desktop_tests.xml'
    cmd=[str(python),'-m','pytest','-q','-p','no:cacheprovider','-rs','--basetemp='+str(Path(work)/'t'),'--junitxml='+str(xml),*files]
    started=time.monotonic()
    try:r=subprocess.run(cmd,cwd=app,env=check_env(),stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired:raise RuntimeError('PORTABLE_DESKTOP_TESTS_TIMEOUT: '+str(timeout)+' s')
    out=(r.stdout+r.stderr).decode('utf-8','replace')
    try:
        root=ET.parse(xml).getroot();suites=[root] if root.tag=='testsuite' else list(root.iter('testsuite'))
        count={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
        skipped=[c.get('classname','')+'::'+c.get('name','') for c in root.iter('testcase') if c.find('skipped') is not None]
    except (OSError,ET.ParseError,ValueError):count=None;skipped=[]
    if r.returncode!=0 or not count or count['failures'] or count['errors'] or count['tests']<=count['skipped']:
        tail=' | '.join(l for l in out.strip().splitlines()[-12:])
        raise RuntimeError('PORTABLE_DESKTOP_TESTS_FAILED: exit '+str(r.returncode)+': '+tail)
    return dict(status='PASSED',python=str(python),files=len(files),passed=count['tests']-count['skipped'],
        skipped=count['skipped'],skipped_tests=skipped,seconds=round(time.monotonic()-started,1))

def verify_zip(path,top,work=None,python=None):
    """Unpack into a fresh folder, compare bytes with the manifest, run the unpacked release_guard.verify; with
    `python`, also the 04_CHECK desktop tests in the unpacked tree (run_desktop_tests)."""
    work=Path(tempfile.mkdtemp(prefix='pfzip_',dir=work))
    try:
        with zipfile.ZipFile(path) as z:
            names=z.namelist()
            bad=[n for n in names if not n.startswith(top+'/') or um.excluded(n[len(top)+1:]) not in (None,'EXCLUDED_MANIFEST')]
            if bad:raise RuntimeError('PORTABLE_ZIP_BAD_ENTRY: '+bad[0])
            z.extractall(work)
        tree=work/top
        manifest=json.loads((tree/'BUILD_MANIFEST.json').read_text(encoding='utf-8'))
        expected={top+'/'+n for n in list(manifest['files'])+list(EXTRAS)}
        if set(names)!=expected or len(names)!=len(expected):raise RuntimeError('PORTABLE_ZIP_ENTRIES_MISMATCH')
        for name,value in manifest['files'].items():
            if zip_digest(name,(tree/name).read_bytes())!=value:raise RuntimeError('PORTABLE_ZIP_HASH_MISMATCH: '+name)
        for name in EXTRAS:
            if b'\r\n' in (tree/name).read_bytes():raise RuntimeError('PORTABLE_ZIP_CRLF: '+name)
        checked=_load('pf_unpacked_release_guard',tree/'app'/'release_guard.py').verify(tree)
        extra=desktop_extra(tree,checked['files'])
        if extra:raise RuntimeError('DESKTOP_EXTRA_MODULE: '+extra[0])
        result=dict(unpacked_files=len(names),release_guard_verify='PASSED',version=checked['version'],
            build_revision=checked.get('build_revision'))
        if python:result['desktop_tests_unpacked']=run_desktop_tests(tree,python,work)
        return result
    finally:shutil.rmtree(work,ignore_errors=True)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root',default=str(ROOT));ap.add_argument('--out');ap.add_argument('--top')
    ap.add_argument('--work',help='parent folder of the temporary unpack folder (default: system temp; keep it short)')
    ap.add_argument('--python',default=sys.executable,help='bot venv interpreter for the unpacked desktop tests (default: this one)')
    a=ap.parse_args(argv)
    try:
        path,top,names=build(a.root,a.out,a.top);result=verify_zip(path,top,a.work,a.python)
    except (RuntimeError,OSError,ValueError,zipfile.BadZipFile) as exc:
        print(json.dumps(dict(status='FAILED',error=str(exc)),ensure_ascii=False));return 1
    with open(path,'rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
    print(json.dumps(dict(status='PASSED',zip=str(path),top=top,entries=len(names),bytes=path.stat().st_size,sha256=sha,**result),ensure_ascii=False,indent=1))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
