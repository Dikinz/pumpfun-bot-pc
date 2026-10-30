import sqlite3,copy
from pathlib import Path
import pytest
import program_cache as cache
from test_program_cache_v57 import RPC

def test_prep95_warm_cache_is_one_readonly_connection_under_real_write_lock(tmp_path,monkeypatch):
 rpc=RPC();store=cache.Store(tmp_path/'programs.db');rpc.capture(store)
 sql=[];calls=[];real=cache.sqlite3.connect
 def connect(*a,**kw):
  c=real(*a,**kw);c.set_trace_callback(sql.append);calls.append(a[0]);return c
 blocker=real(store.path,isolation_level=None);blocker.execute('BEGIN IMMEDIATE')
 try:
  monkeypatch.setattr(cache.sqlite3,'connect',connect);rpc.calls=[];rpc.market=b'fresh-second-bank'
  result=rpc.capture(store)
  assert result['program_cache']['hits']==1 and result['accounts']['market']
  assert len(calls)==1 and 'mode=ro' in calls[0]
  assert all(s.startswith('SELECT') for s in sql),sql
  assert len(rpc.calls)==2 and rpc.calls[0][2] is None and rpc.calls[1][2]==45
 finally:blocker.rollback();blocker.close()

def test_prep95_missing_read_cache_creates_no_database_or_directory(tmp_path):
 p=tmp_path/'absent/programs.db';store=cache.Store(p)
 assert store.get_many('namespace',{'p1':'pd1','p2':'pd2'})=={'p1':None,'p2':None}
 assert not p.parent.exists()

def test_prep95_each_warm_capture_still_fetches_live_bank_and_later_version(tmp_path):
 rpc=RPC();store=cache.Store(tmp_path/'cache.db');rpc.capture(store)
 for i in range(3):
  rpc.calls=[];rpc.slot+=1;rpc.market=bytes([i]);result=rpc.capture(store)
  assert cache.raw(result['accounts']['market'])==bytes([i])
  assert len(rpc.calls)==2 and rpc.calls[1][1]>=result['slot']
  assert result['program_cache']['metadata_slot']>=result['program_cache']['trading_slot']
  assert result['received_ts']==2

def test_prep95_cache_upgrade_keeps_fresh_fallback_and_no_stale_code(tmp_path):
 rpc=RPC();store=cache.Store(tmp_path/'cache.db');old=rpc.capture(store);rpc.change=True
 fresh=rpc.capture(store)
 assert fresh['program_cache']['mode']=='FULL_FRESH'
 assert cache.raw(old['accounts'][rpc.pd])!=cache.raw(fresh['accounts'][rpc.pd])
 assert cache.raw(fresh['accounts'][rpc.pd])==rpc.code
