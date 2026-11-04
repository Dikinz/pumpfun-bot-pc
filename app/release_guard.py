"""Standard-library-only validation before importing any bot module."""
import hashlib,json,socket
from pathlib import Path
VERSION='v0.7.101.9'
TEXT_SUFFIXES=frozenset({'.py','.bat','.json','.md','.txt','.xml','.html','.example'})

def lf_digest(path):
    # Streamed SHA-256 of the file with CRLF pairs read as LF; bounded memory.
    h=hashlib.sha256();carry=b''
    with path.open('rb') as source:
        while chunk:=source.read(1<<20):
            data=carry+chunk;carry=b''
            if data.endswith(b'\r'):data,carry=data[:-1],b'\r'
            h.update(data.replace(b'\r\n',b'\n'))
    h.update(carry)
    return h.hexdigest()

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
        try:
            # Validate every byte on every invocation, using a bounded buffer.
            # No timestamp/size cache: changed files must still block startup.
            with path.open('rb') as source:actual=hashlib.file_digest(source,'sha256').hexdigest()
        except OSError:raise RuntimeError('CLEAN_RELEASE_FILE_MISSING: '+name)
        if actual!=digest and path.suffix.lower() in TEXT_SUFFIXES:
            # A Windows git checkout (core.autocrlf) stores the same text with CRLF.
            # Only CRLF->LF differs; any other byte change still blocks startup.
            try:actual=lf_digest(path)
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
