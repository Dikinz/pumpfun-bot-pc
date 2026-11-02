"""Recompute BUILD_MANIFEST.json and add explicitly named files (desktop spec 5). Not shipped, stdlib only.

  python -I packaging/desktop/update_manifest.py [--root ROOT] [--check] [--build-revision R] [PATH ...]

Every existing entry gets its current hash; each PATH (a file, or a folder taken recursively) is appended in
sorted order. Hashes follow release_guard: text suffixes (release_guard.TEXT_SUFFIXES) are hashed with CRLF read
as LF, other files raw. Paths of the excluded zones (packaging, .venv*, app/data, .env, results, dist, caches)
are refused. version and build_revision stay unchanged unless --build-revision is given (a release step).
--check writes nothing and exits 1 when the manifest is stale. After a write release_guard.verify(ROOT) runs;
then run .venv\\Scripts\\python.exe -I app\\bootstrap.py verify-release."""
import argparse,hashlib,importlib.util,json,os,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
MANIFEST='BUILD_MANIFEST.json'
EXCLUDED_TOP=('packaging','results','dist','.git','.github','.claude')
EXCLUDED_PARTS=('__pycache__','.pytest_cache')

def guard(root=ROOT):
    """app/release_guard.py of that tree, loaded by path (stdlib-only; sys.path is not touched)."""
    path=Path(root)/'app'/'release_guard.py'
    spec=importlib.util.spec_from_file_location('pf_release_guard_'+hashlib.sha256(str(path).encode()).hexdigest()[:8],path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def excluded(rel):
    """Reason when a manifest/ZIP path lies in a zone that is never shipped, else None."""
    parts=Path(rel).parts if not isinstance(rel,str) else tuple(p for p in rel.replace('\\','/').split('/') if p)
    if not parts or any(p in ('.','..') for p in parts) or ':' in parts[0]:return 'INVALID_PATH'
    if parts[0] in EXCLUDED_TOP or parts[0].lower().startswith('.venv'):return 'EXCLUDED_'+parts[0].upper().strip('.')
    if parts[:2]==('app','data'):return 'EXCLUDED_APP_DATA'
    if '.env' in parts:return 'EXCLUDED_ENV'
    if any(p in EXCLUDED_PARTS for p in parts) or parts[-1].endswith(('.pyc','.pyo')):return 'EXCLUDED_CACHE'
    if parts==(MANIFEST,):return 'EXCLUDED_MANIFEST'
    return None

def digest(path,g):
    """release_guard's own rule: LF-normalised (streamed) for text suffixes, raw bytes otherwise."""
    path=Path(path)
    if path.suffix.lower() in g.TEXT_SUFFIXES:return g.lf_digest(path)
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def expand(root,item):
    """Relative posix paths for one argument: a file, or every file under a folder (caches skipped)."""
    root=Path(root).resolve();path=Path(item)
    path=(path if path.is_absolute() else root/path).resolve()
    if not path.is_relative_to(root):raise ValueError('MANIFEST_PATH_OUTSIDE_ROOT: '+str(item))
    if path.is_dir():
        out=[]
        for p in sorted(path.rglob('*')):
            rel=p.relative_to(root).as_posix()
            if p.is_file() and excluded(rel)!='EXCLUDED_CACHE':out.append(rel)
        return out
    if not path.is_file():raise ValueError('MANIFEST_FILE_MISSING: '+str(item))
    return [path.relative_to(root).as_posix()]

def load(root=ROOT):
    return json.loads((Path(root)/MANIFEST).read_text(encoding='utf-8'))

def update(root=ROOT,add=(),build_revision=None):
    """(new manifest, changes). changes: dict(changed=[..], added=[..], revision=old->new or None)."""
    root=Path(root).resolve();g=guard(root);old=load(root)
    files=dict(old['files']);changes=dict(changed=[],added=[],revision=None)
    for name in files:
        reason=excluded(name)
        if reason:raise ValueError('MANIFEST_'+reason+': '+name)
        try:value=digest(root/name,g)
        except OSError:raise ValueError('MANIFEST_FILE_MISSING: '+name)
        if value!=files[name]:files[name]=value;changes['changed'].append(name)
    for item in add:
        for name in expand(root,item):
            reason=excluded(name)
            if reason:raise ValueError('MANIFEST_'+reason+': '+name)
            if name in files:continue
            files[name]=digest(root/name,g);changes['added'].append(name)
    new=dict(old);new['files']=files
    if build_revision and build_revision!=old.get('build_revision'):
        changes['revision']=str(old.get('build_revision'))+' -> '+build_revision;new['build_revision']=build_revision
    return new,changes

def render(manifest):
    return (json.dumps(manifest,indent=2,ensure_ascii=False)+'\n').encode('utf-8')

def write(root,manifest):
    """Atomic LF write of BUILD_MANIFEST.json."""
    target=Path(root)/MANIFEST
    fd,name=tempfile.mkstemp(prefix='.'+MANIFEST+'.',suffix='.tmp',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(render(manifest))
        os.replace(name,target)
    except BaseException:
        try:os.unlink(name)
        except OSError:pass
        raise
    return target

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('paths',nargs='*');ap.add_argument('--root',default=str(ROOT))
    ap.add_argument('--check',action='store_true');ap.add_argument('--build-revision')
    a=ap.parse_args(argv);root=Path(a.root).resolve()
    try:new,changes=update(root,a.paths,a.build_revision)
    except ValueError as exc:print('FAILED:',exc);return 1
    stale=bool(changes['changed'] or changes['added'] or changes['revision'])
    print(json.dumps(dict(root=str(root),version=new.get('version'),build_revision=new.get('build_revision'),
        entries=len(new['files']),**changes),ensure_ascii=False,indent=1))
    if a.check:
        print('MANIFEST STALE' if stale else 'MANIFEST CURRENT');return 1 if stale else 0
    if stale:write(root,new)
    try:guard(root).verify(root)
    except RuntimeError as exc:print('VERIFY FAILED:',exc);return 1
    print('MANIFEST '+('UPDATED' if stale else 'UNCHANGED')+'; release_guard.verify PASSED. Next: .venv\\Scripts\\python.exe -I app\\bootstrap.py verify-release')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
