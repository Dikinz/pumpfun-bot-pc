"""v0.7.101.6: evidence archives keep program binaries once in blobs/, never inside every evidence.zip.

On 02.11.2026 the antivirus quarantined bot files and killed every bot process at 23:59; each preentry/shadow
archive then carried ~17 MB of Solana programs (about 400 MB in 3 h)."""
import base64,inspect,io,json,os,zipfile
from pathlib import Path
import pytest
import evidence_store as es,position_monitor as pm,preentry,shadow_preflight,auto_simulation

def bundle():
    program=base64.b64encode(b'\x03\x00\x00\x00'+os.urandom(150000)).decode()
    return dict(snapshot=dict(slot=1,accounts={'Prog':dict(data=[program,'base64'],owner='BPF'),'Small':dict(data=['AAAA','base64']),'Gone':None}))

def test_large_accounts_move_to_one_blob_and_round_trip(tmp_path):
    b=bundle();packed=es.pack(b,tmp_path)
    blobs=list((tmp_path/'blobs').glob('*.zlib'))
    assert len(blobs)==1 and packed['snapshot']['accounts']['Prog']['data']=={'blob_sha256':blobs[0].stem}
    assert packed['snapshot']['accounts']['Small']==b['snapshot']['accounts']['Small'] and len(json.dumps(packed))<1000
    stamp=blobs[0].stat().st_mtime_ns;es.pack(b,tmp_path)
    assert blobs[0].stat().st_mtime_ns==stamp and es.unpack(packed,tmp_path)==b  # written once, replayable
    assert pm.unpack_bundle(pm.pack_bundle(b,tmp_path),tmp_path)==b
    blobs[0].write_bytes(__import__('zlib').compress(b'[]'))
    with pytest.raises(ValueError,match='BLOB_HASH_MISMATCH'):es.unpack(packed,tmp_path)

def test_a_blob_held_by_a_parallel_writer_is_kept(tmp_path,monkeypatch):
    b=bundle();original=Path.replace
    def busy(self,target):
        original(self,target)  # the other child finished first ...
        raise PermissionError(13,'held')  # ... and our replace hits its open handle
    monkeypatch.setattr(Path,'replace',busy)
    packed=es.pack(b,tmp_path);monkeypatch.undo()
    assert es.unpack(packed,tmp_path)==b and not list((tmp_path/'blobs').glob('*.tmp'))

def test_export_carries_the_blobs(tmp_path):
    es.pack(bundle(),tmp_path);buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:es.export_blobs(z,tmp_path)
    with zipfile.ZipFile(buffer) as z:assert [n.split('/')[0] for n in z.namelist()]==['blobs']

def test_every_evidence_writer_packs_snapshots():
    for module in (preentry,auto_simulation):
        source=inspect.getsource(module)
        assert "z.writestr('snapshot.json',json.dumps(bundle))" not in source,module.__name__
        assert "evidence_store').pack(bundle,out.parent)" in source and "evidence_store').export_blobs(z,ROOT)" in source,module.__name__
    assert "evidence_store').export_blobs(z,ROOT)" in inspect.getsource(shadow_preflight.export)
    # shadow_preflight re-zips the preentry child's evidence.zip, so its snapshot is the packed one
    assert 'preentry.child(data,out,replay)' in inspect.getsource(shadow_preflight)
