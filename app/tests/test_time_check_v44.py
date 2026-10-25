import asyncio,json,subprocess,zipfile
import pytest
import time_check as tc

def sample(o=10,u=25,status='MEASURED'):return dict(status=status,offset_ms=o,uncertainty_ms=u)

@pytest.mark.parametrize('samples,step,expected',[
 ([sample()]*3,0,'PASS'),([sample(25,25)]*3,0,'PASS'),
 ([sample(175)]*3,0,'OFFSET_DETECTED'),([sample(-175)]*3,0,'OFFSET_DETECTED'),
 ([sample(40)]*3,0,'UNCERTAIN'),([sample()]*2,0,'UNCERTAIN'),
 ([sample(),sample(),sample(100,90,'TOO_IMPRECISE')],0,'UNCERTAIN'),
 ([sample()]*3,21,'UNCERTAIN'),([sample(0,1),sample(20,1),sample(0,1)],0,'UNCERTAIN'),
 ([{'status':'UNAVAILABLE'}]*3,0,'UNAVAILABLE'),([{'status':'SERVER_REFUSAL'}],0,'UNAVAILABLE'),
 ([sample(float('nan'))]*3,0,'UNCERTAIN'),([sample(0,-1)]*3,0,'UNCERTAIN')])
def test_clock_classification_does_not_hide_uncertainty(samples,step,expected):
    assert tc.assess(samples,step)['status']==expected

@pytest.mark.parametrize('text,expected',[
 ('Last Successful Sync Time: 10/18/2026 9:30:01 PM','10/18/2026 9:30:01 PM'),
 ('Время последней успешной синхронизации: 18.10.2026 21:30:01','18.10.2026 21:30:01'),
 ('Час останньої успішної синхронізації: 18.10.2026 21:30:01','18.10.2026 21:30:01'),
 ('Unknown locale: 10/18/2026',None)])
def test_localized_last_sync_preserved_without_date_guessing(text,expected):assert tc.last_sync_text(text)==expected

def test_windows_readonly_commands_and_service_warning(monkeypatch):
    calls=[]
    def command(args,encoding=None):
        calls.append(args)
        if args[0]=='powershell.exe':return {'status':'OK','stdout':'{"status":"Stopped","start_type":"Manual"}','stderr':''}
        if args[-1]=='/source':return {'status':'COMMAND_FAILED','stdout':'Access denied','stderr':''}
        return {'status':'OK','stdout':'Last Successful Sync Time: unspecified','stderr':''}
    monkeypatch.setattr(tc,'command',command);r=tc.windows_info('nt')
    assert r['service']['status']=='Stopped' and r['status']=='PARTIAL_OR_UNAVAILABLE'
    assert r['last_successful_sync_text']=='unspecified' and r['source'] is None
    assert calls[1:]==[['w32tm.exe','/query','/status','/verbose'],['w32tm.exe','/query','/source']]
    assert all('/resync' not in x and '/config' not in x for x in calls)

def test_command_timeout_encoding_and_no_shell(monkeypatch):
    def fail(*args,**kw):
        assert kw['shell'] is False and kw['timeout']==5
        raise subprocess.TimeoutExpired(args[0],5)
    monkeypatch.setattr(subprocess,'run',fail)
    assert tc.command(['w32tm.exe','/query','/status'])['status']=='TIMEOUT'
    assert tc.decode_output('Время'.encode('cp866'),'cp866')=='Время'
    assert tc.decode_output('Время'.encode('utf-16'))=='Время'

def test_refusal_stops_more_probes(monkeypatch):
    calls=[]
    async def probe():calls.append(1);return {'status':'SERVER_REFUSAL','stop_requests':True}
    monkeypatch.setattr(tc.clock_lab,'probe',probe);monkeypatch.setattr(tc,'windows_info',lambda: {'status':'NOT_WINDOWS'})
    r=asyncio.run(tc.collect());assert len(calls)==1 and r['reference']['status']=='UNAVAILABLE'
    assert r['clock_modified'] is False and r['market_stream_checked'] is False and r['active_tolerance_ms']==0

def test_collect_three_probes_and_separate_windows_failure(monkeypatch):
    calls=[]
    async def probe():calls.append(1);return sample()
    async def no_sleep(*args):pass
    monkeypatch.setattr(tc.clock_lab,'probe',probe);monkeypatch.setattr(tc.asyncio,'sleep',no_sleep)
    monkeypatch.setattr(tc,'windows_info',lambda: {'status':'PARTIAL_OR_UNAVAILABLE','service':{'status':'UNKNOWN'}})
    r=asyncio.run(tc.collect());assert len(calls)==3 and r['reference']['status']=='PASS'
    assert 'не полностью' in tc.format_report(r)

def test_save_age_corruption_and_export(tmp_path,monkeypatch):
    monkeypatch.setattr(tc,'data_dir',lambda:tmp_path/'data')
    assert tc.latest()['state']=='NOT_CHECKED'
    r=dict(schema='device_time_check_v0744',id='test',created_ts=100,reference=tc.assess([sample()]*3),windows={'status':'NOT_WINDOWS'},samples=[],source='test',version='test',local_time='test')
    tc.save(r);assert tc.latest(101)['state']=='PASS' and not tc.latest(101)['stale']
    assert tc.latest(1001)['stale'] and tc.latest(99)['stale']
    tc.capture_start();out=tmp_path/'export';out.mkdir();tc.export(out)
    assert (out/'time_check_at_start.json').exists() and (out/'time_check.txt').exists()
    with zipfile.ZipFile(out/'time_check_history.zip') as z:assert z.namelist()==['test.json']
    (tc.data_dir()/'time_check.json').write_text('[]')
    assert tc.latest()['state']=='INVALID_REPORT'

def test_launcher_distinguishes_time_warning_from_failed_tests(monkeypatch):
    import launcher
    monkeypatch.setattr(launcher.sys,'argv',['launcher.py','check']);calls=[];codes=iter([0,2,0,0])
    def call(*args):calls.append(args);return next(codes)
    monkeypatch.setattr(launcher,'call',call)
    assert launcher.main()==2 and calls[:2]==[('recovery_selftest.py',),('time_check.py',)] and calls[-1][0]=='-m'
    codes=iter([0,0,0,1]);assert launcher.main()==1
    calls.clear();codes=iter([1]);assert launcher.main()==1 and calls==[('recovery_selftest.py',)]
