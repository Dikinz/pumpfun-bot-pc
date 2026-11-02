"""02.11 run stopped by the stall guard: open() of diagnostic.zip took 17 s on the event loop."""
import asyncio,json,time,zipfile
import position_monitor as pm

ORIGINAL_ZIP=zipfile.ZipFile

class Done:
    pid=987654322
    returncode=0
    async def wait(self):return 0

class SlowZip(ORIGINAL_ZIP):
    def __init__(self,*a,**kw):
        time.sleep(1.0)
        super().__init__(*a,**kw)

async def spawn(args,out,trace):
    (out/'result.json').write_text(json.dumps({'status':'CHECK_PASSED'}),encoding='utf-8')
    return Done()

def test_slow_diagnostic_archive_does_not_block_the_event_loop(tmp_path,monkeypatch):
    monkeypatch.setattr(pm,'create_simulation_process',spawn)
    monkeypatch.setattr(pm.zipfile,'ZipFile',SlowZip)
    async def run():
        gaps=[];stop=asyncio.Event()
        async def tick():
            last=time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(.02);now=time.monotonic();gaps.append(now-last);last=now
        ticker=asyncio.create_task(tick())
        started=time.monotonic()
        try:r=await pm.launch_check({'id':'slow-zip'},tmp_path,timeout=5)
        finally:stop.set();await ticker
        return r,max(gaps),time.monotonic()-started
    r,gap,elapsed=asyncio.run(run())
    assert elapsed>=1.0 and gap<.5
    assert r['status']=='CHECK_PASSED' and r['child_returncode']==0
    assert json.loads((tmp_path/'diagnostic.json').read_text())['status']=='CHECK_PASSED'
    with ORIGINAL_ZIP(tmp_path/'diagnostic.zip') as z:assert 'diagnostic.json' in z.namelist()

def test_diagnostic_failure_in_the_worker_is_recorded_and_raised(tmp_path,monkeypatch):
    monkeypatch.setattr(pm,'create_simulation_process',spawn)
    class Broken(ORIGINAL_ZIP):
        def __init__(self,*a,**kw):raise OSError(13,'denied')
    monkeypatch.setattr(pm.zipfile,'ZipFile',Broken)
    try:
        asyncio.run(pm.launch_check({'id':'denied-zip'},tmp_path,timeout=5));raised=None
    except OSError as e:raised=e
    assert raised is not None and raised.errno==13
    t=json.loads((tmp_path/'child_lifecycle_v73.json').read_text())
    assert t['diagnostic_exception']['chain'][0]['errno']==13

def test_branch_ready_file_read_waits_for_a_complete_write(tmp_path):
    import threading,branch_transport as bt
    ready=tmp_path/'branch_ready_x.tmp';ready.write_text('',encoding='utf-8')
    def finish():time.sleep(.3);ready.write_text(json.dumps(dict(session='x',pid=1)),encoding='utf-8')
    threading.Thread(target=finish).start()
    assert bt.read_ready(ready,time.monotonic()+5)==dict(session='x',pid=1)
    bt.drop_ready(ready);assert not ready.exists()

def test_branch_ready_read_still_fails_after_the_start_deadline(tmp_path):
    import branch_transport as bt,pytest
    ready=tmp_path/'branch_ready_y.tmp';ready.write_text('',encoding='utf-8')
    with pytest.raises(ValueError):bt.read_ready(ready,time.monotonic()+.2)

def test_worker_publishes_the_ready_file_atomically():
    import inspect,paper_branch
    src=inspect.getsource(paper_branch._worker)
    assert "os.replace(part,ready)" in src and "Path(ready).write_text" not in src
