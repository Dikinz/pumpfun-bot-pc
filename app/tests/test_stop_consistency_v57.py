import time
import diagnostics,runtime_status,system_health,config,main
from test_primary_integrity import fresh

def recorder_stop(code,reason):
    rec=system_health.Recorder(config.DB);rec.sample({},force=True);rec.stop(code,reason)

def meta(c,**values):
    for k,v in values.items():c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,str(v)))
def running(c):
    now=time.time();meta(c,runtime_started_ts_v41=now-100,runtime_updated_ts_v41=now-60,runtime_elapsed_s_v41=40,runtime_state_v41='RUNNING',heartbeat_ts=now-60,trade_stream_state='RECEIVING',stream_state='RUNNING')

def test_matching_user_stop_is_identical_in_standalone_report(fresh):
    running(fresh)
    assert 'heartbeat' in diagnostics.summary(fresh)['entry_status']
    recorder_stop(0,'USER_STOP')
    one=diagnostics.summary(fresh);exported=diagnostics.check(config.DB)
    assert one['runtime_state']==exported['runtime_state']=='STOPPED'
    assert one['stop_reason']==exported['stop_reason']=='USER_STOP'
    assert 'heartbeat' not in exported['entry_status'] and exported['frozen']

def test_terminal_runtime_with_stale_stream_is_stopped(fresh):
    running(fresh);meta(fresh,runtime_state_v41='STOPPED')
    d=diagnostics.summary(fresh)
    assert d['entry_blocks'][0]=='Бот остановлен' and 'heartbeat' not in d['entry_status']

def test_old_recorder_stop_cannot_hide_new_run(fresh):
    running(fresh);recorder_stop(0,'USER_STOP')
    meta(fresh,runtime_started_ts_v41=time.time()+1,runtime_updated_ts_v41=time.time()-60,trade_stream_state='STOPPED')
    d=diagnostics.summary(fresh)
    assert d['runtime_state']=='UNKNOWN' and 'heartbeat' in d['entry_status']

def test_failed_or_unconfirmed_stop_does_not_become_user_stop(fresh):
    running(fresh);recorder_stop(1,'COMPONENT_OR_STARTUP_FAILURE')
    assert 'heartbeat' in diagnostics.summary(fresh)['entry_status']
    meta(fresh,runtime_state_v41='FAILED')
    assert diagnostics.summary(fresh)['entry_blocks'][0]=='Бот завершён с ошибкой'
    assert diagnostics.summary(fresh)['stop_reason'] is None

def test_stop_preserves_position_slot_restrictions(fresh):
    assert main.buy('a',1)[0] and main.buy('b',1)[0]
    running(fresh);recorder_stop(0,'USER_STOP')
    snap=dict(runtime=runtime_status.summary(fresh),diagnostics=diagnostics.summary(fresh))
    assert system_health.apply_lifecycle(snap,config.DB)
    assert 'Заняты все места для позиций' in snap['diagnostics']['entry_blocks']
    assert fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==2

def test_foreign_recorder_cannot_hide_missing_heartbeat(fresh):
    running(fresh);recorder_stop(0,'USER_STOP')
    state=system_health.read_state(config.DB);state['database_identity']='foreign'
    system_health.write_state(config.DB,state)
    assert 'heartbeat' in diagnostics.summary(fresh)['entry_status']
