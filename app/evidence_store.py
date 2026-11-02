"""Evidence snapshots without embedded program binaries (standard library only).

Large account data (Solana ProgramData: whole executable programs) is stored once per content hash
in <evidence root>/blobs/<sha256>.zlib and the snapshot keeps {'blob_sha256': ...}; every sample
stays replayable through unpack().

v0.7.101.6: preentry/shadow archives carried ~17 MB of programs each (about 400 MB in 3 h).
On 02.11 the antivirus quarantined bot files and killed every bot process at once."""
import copy,hashlib,json,uuid,zlib
from pathlib import Path
LARGE=100000  # JSON length of an account's data above which it goes to a blob

def pack(bundle,root):
    bundle=copy.deepcopy(bundle);folder=Path(root)/'blobs';folder.mkdir(parents=True,exist_ok=True)
    for a in bundle['snapshot']['accounts'].values():
        if a and len(json.dumps(a.get('data',[])))>LARGE:
            raw=json.dumps(a['data']).encode();digest=hashlib.sha256(raw).hexdigest();path=folder/(digest+'.zlib')
            if not path.exists():
                tmp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp');tmp.write_bytes(zlib.compress(raw))
                try:tmp.replace(path)
                except PermissionError:
                    # A parallel child (or a scanner) holds the same blob: identical content, keep theirs.
                    if not path.exists():raise
                    tmp.unlink(missing_ok=True)
            a['data']={'blob_sha256':digest}
    return bundle

def unpack(bundle,root):
    bundle=copy.deepcopy(bundle)
    for a in bundle['snapshot']['accounts'].values():
        if a and isinstance(a.get('data'),dict):
            digest=a['data']['blob_sha256']
            if len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest):raise ValueError('INVALID_BLOB_ID')
            raw=zlib.decompress((Path(root)/'blobs'/(digest+'.zlib')).read_bytes())
            if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('BLOB_HASH_MISMATCH')
            a['data']=json.loads(raw)
    return bundle

def export_blobs(z,root):
    """Add every blob of one evidence root to an export archive (blobs/<sha256>.zlib)."""
    for f in sorted((Path(root)/'blobs').glob('*.zlib')):z.write(f,'blobs/'+f.name)
