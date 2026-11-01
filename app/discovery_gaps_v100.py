"""Boundaries of observed discovery outages; missed events remain unknown."""
import time
SCHEMA='''CREATE TABLE IF NOT EXISTS discovery_gaps_v100(
 id INTEGER PRIMARY KEY,started_ts REAL,last_error_ts REAL,reconnected_ts REAL,
 duration_s REAL,error_count INTEGER,error_type TEXT,clock_basis TEXT);'''

def failed(c,error_type,now=None):
    now=time.time() if now is None else now
    row=c.execute('SELECT id FROM discovery_gaps_v100 WHERE reconnected_ts IS NULL ORDER BY id DESC LIMIT 1').fetchone()
    if row:c.execute('UPDATE discovery_gaps_v100 SET last_error_ts=?,error_count=error_count+1,error_type=? WHERE id=?',(now,str(error_type)[:80],row[0]))
    else:c.execute('INSERT INTO discovery_gaps_v100(started_ts,last_error_ts,error_count,error_type,clock_basis) VALUES(?,?,1,?,?)',(now,now,str(error_type)[:80],'WALL_CLOCK_OBSERVED_BOUNDARIES_NOT_PROVIDER_LOSS'))

def recovered(c,now=None):
    now=time.time() if now is None else now
    c.execute('UPDATE discovery_gaps_v100 SET reconnected_ts=?,duration_s=CASE WHEN ?>=started_ts THEN ?-started_ts ELSE NULL END WHERE reconnected_ts IS NULL',(now,now,now))
