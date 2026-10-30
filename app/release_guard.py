"""Standard-library-only validation before importing any bot module."""
import hashlib,json,socket
from pathlib import Path
VERSION='v0.7.95'

def verify(root):
    root=Path(root).resolve()
    try:manifest=json.loads((root/'BUILD_MANIFEST.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):raise RuntimeError('CLEAN_RELEASE_MANIFEST_MISSING: extract the complete ZIP into a new folder')
    if manifest.get('version')!=VERSION:raise RuntimeError('CLEAN_RELEASE_VERSION_MISMATCH')
    files=manifest.get('files')
    if not isinstance(files,dict) or not files:raise RuntimeError('CLEAN_RELEASE_EMPTY_MANIFEST')
    for name,digest in files.items():
        path=(root/name).resolve()
        if not path.is_relative_to(root):raise RuntimeError('CLEAN_RELEASE_INVALID_PATH')
        try:actual=hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:raise RuntimeError('CLEAN_RELEASE_FILE_MISSING: '+name)
        if actual!=digest:raise RuntimeError('CLEAN_RELEASE_FILE_CHANGED: '+name+'; extract a fresh full ZIP')
    for path in (root/'app').rglob('*.py'):
        rel=path.relative_to(root)
        if '__pycache__' in rel.parts or rel.parts[:2]==('app','data'):continue
        if rel.as_posix() not in files:raise RuntimeError('CLEAN_RELEASE_EXTRA_MODULE: '+rel.as_posix())
    return manifest

def local_database(app,db):
    target=(Path(app)/db).resolve()
    if not target.is_relative_to((Path(app)/'data').resolve()):
        raise RuntimeError('CLEAN_DATABASE_OUTSIDE_FOLDER: use app/data in this installation')
    return target

def available(port):
    with socket.socket() as sock:
        try:sock.bind(('127.0.0.1',port))
        except OSError:raise RuntimeError('PORT_IN_USE: '+str(port)+'; stop the previous bot in its own window, then start this folder')
