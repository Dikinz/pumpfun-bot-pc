"""Windows checkout and BAT exit-code regressions (PROD-1, PROD-3)."""
import hashlib,json,os,shutil,subprocess,sys
from pathlib import Path
import pytest
import release_guard

ROOT=Path(__file__).resolve().parents[2]
CHECK_BATS=('06_AMM_CHECK.bat','09_RECOVERY_CHECK.bat','10_TEST_AMM_STOP.bat','11_PERF_CHECK.bat')


def release(root,files):
    for name,data in files.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    manifest=dict(version=release_guard.VERSION,files={n:hashlib.sha256(d).hexdigest() for n,d in files.items()})
    (root/'BUILD_MANIFEST.json').write_bytes(json.dumps(manifest).encode())


def crlf(data):return data.replace(b'\n',b'\r\n')


def test_win314_autocrlf_checkout_of_clean_release_is_accepted(tmp_path):
    # PROD-1: git core.autocrlf=true writes CRLF for every LF text file of the ZIP.
    lf={'01_INSTALL.bat':b'@echo off\nexit /b 0\n','app/bootstrap.py':b'print(1)\n',
        'README.txt':'Тест\n'.encode(),'app/data.json':b'{"a":\n1}\n'}
    release(tmp_path,lf)
    for name,data in lf.items():(tmp_path/name).write_bytes(crlf(data))
    assert release_guard.verify(tmp_path)


@pytest.mark.parametrize('change',[b'print(2)\r\n',b'print(1)\r\n\r\n',b'print(1)\r',b'print(1)\rx\n'])
def test_win314_crlf_tolerance_never_accepts_other_byte_changes(tmp_path,change):
    release(tmp_path,{'app/bootstrap.py':b'print(1)\n'})
    (tmp_path/'app/bootstrap.py').write_bytes(change)
    with pytest.raises(RuntimeError,match='FILE_CHANGED'):release_guard.verify(tmp_path)


def test_win314_binary_files_are_still_compared_raw(tmp_path):
    data=b'PK\x03\x04\n\x00binary\n'
    release(tmp_path,{'docs/input.zip':data})
    (tmp_path/'docs/input.zip').write_bytes(crlf(data))
    with pytest.raises(RuntimeError,match='FILE_CHANGED'):release_guard.verify(tmp_path)


def test_win314_lf_digest_streams_crlf_across_chunk_boundary(tmp_path):
    head=b'x'*((1<<20)-1) # CR is the last byte of the first 1 MiB chunk
    lf=head+b'\n'+b'y\n'*1000;p=tmp_path/'big.txt';p.write_bytes(crlf(lf))
    assert p.read_bytes()[(1<<20)-1:(1<<20)+1]==b'\r\n'
    assert release_guard.lf_digest(p)==hashlib.sha256(lf).hexdigest()
    p.write_bytes(head+b'\r')
    assert release_guard.lf_digest(p)==hashlib.sha256(head+b'\r').hexdigest()


def test_win314_check_bats_propagate_failure_and_require_venv():
    # PROD-3: 'if errorlevel 1 echo' followed by 'pause' returned exit code 0.
    for p in sorted(ROOT.glob('*.bat')):
        text=p.read_text(encoding='utf-8')
        if 'bootstrap.py' not in text:continue
        assert 'exit /b %CHECK_CODE%' in text or ('goto failed' in text and 'exit /b 1' in text),p.name
        if p.name!='01_INSTALL.bat':assert 'if not exist ".venv\\Scripts\\python.exe"' in text,p.name


@pytest.fixture(scope='module')
def stub_install(tmp_path_factory):
    if os.name!='nt':pytest.skip('cmd.exe BAT execution is Windows-only; static checks run everywhere')
    import venv
    root=tmp_path_factory.mktemp('bat check')
    venv.create(root/'.venv',with_pip=False,symlinks=False)
    (root/'app').mkdir()
    for name in CHECK_BATS:shutil.copy(ROOT/name,root/name)
    return root


def run_bat(root,name):
    return subprocess.run(['cmd','/d','/c',str(root/name)],stdin=subprocess.DEVNULL,capture_output=True,timeout=60,cwd=root)


@pytest.mark.parametrize('name',CHECK_BATS)
@pytest.mark.parametrize('code',[0,1,7])
def test_win314_check_bat_returns_bootstrap_exit_code(stub_install,name,code):
    (stub_install/'app/bootstrap.py').write_text('raise SystemExit(%d)\n'%code,encoding='utf-8')
    assert run_bat(stub_install,name).returncode==code


def test_win314_check_bat_without_venv_fails(stub_install,tmp_path):
    shutil.copy(ROOT/'09_RECOVERY_CHECK.bat',tmp_path/'09_RECOVERY_CHECK.bat')
    r=run_bat(tmp_path,'09_RECOVERY_CHECK.bat')
    assert r.returncode==1 and b'01_INSTALL' in r.stdout
