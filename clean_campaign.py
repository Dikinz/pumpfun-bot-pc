"""Separate PAPER campaign. Never deletes, resets or imports trading databases."""
import json,sqlite3,time,uuid,re,os
from pathlib import Path
from contextlib import closing
KEY='clean_campaign_v58'

class CampaignError(RuntimeError):pass

def marker(c):
    try:
        row=c.execute('SELECT v FROM meta WHERE k=?',(KEY,)).fetchone()
        if not row:return None
        d=json.loads(row[0])
        if d.get('schema')!='CLEAN_PAPER_V58' or not re.fullmatch('[a-f0-9]{32}',d.get('id','')):raise ValueError()
        return d
    except (ValueError,TypeError,AttributeError):raise CampaignError('CAMPAIGN_METADATA_INVALID') from None
    except sqlite3.OperationalError:return None

def populated(c):
    for table in ('trades','positions','paper_orders_v0720','runs_v45','preentry_v0729','signal_observations_v0722','capital_ledger_v0721','branch_info'):
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() and c.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():return True
    return False

def preflight(primary):
    p=Path(primary).resolve()
    if p.exists():
        with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as c:
            if marker(c):return
            if populated(c):raise CampaignError('LEGACY_TEST_DATA_BLOCKED: use the new separate folder; do not copy databases')
    import paper_branch
    if paper_branch.path(p).exists():raise CampaignError('UNBOUND_BRANCH_BLOCKED: preserve old files and use a new folder')

def ensure(c,primary):
    existing=marker(c)
    if existing:return existing
    if populated(c):raise CampaignError('LEGACY_TEST_DATA_BLOCKED')
    import config
    d=dict(schema='CLEAN_PAPER_V58',id=uuid.uuid4().hex,created_ts=time.time(),start_balance=config.START_BALANCE,version=config.VERSION,
        note='Independent PAPER scenarios, same initial cash. Restart resumes this campaign; no automatic resets.')
    c.execute('INSERT INTO meta(k,v) VALUES(?,?)',(KEY,json.dumps(d)))
    return d

def import_settings(source,target):
    """Local credentials/parameters only. Never reads/copies a DB or signing key."""
    from dotenv import dotenv_values,set_key
    source=Path(source).expanduser().resolve();target=Path(target).resolve()
    if source==target or not source.is_file():raise CampaignError('SETTINGS_SOURCE_INVALID')
    app=Path(__file__).parent
    allowed=set(re.findall(r'os\.getenv\([\"\x27]([A-Z0-9_]+)',(app/'config.py').read_text()))
    allowed.update(dotenv_values(app/'.env.example',interpolate=False))
    forbidden={'DATABASE_PATH','START_BALANCE','EXECUTION_MODE'}
    copied=0
    for key,value in dotenv_values(source,interpolate=False).items():
        if key not in allowed or key in forbidden or value is None or any(x in key for x in ('PRIVATE','SECRET','KEYPAIR','SEED')):continue
        set_key(target,key,value,quote_mode='always');copied+=1
    set_key(target,'DATABASE_PATH','data/clean_0762.db',quote_mode='always')
    set_key(target,'START_BALANCE','30',quote_mode='always');set_key(target,'EXECUTION_MODE','paper',quote_mode='always')
    if os.name!='nt':os.chmod(target,0o600)
    return copied

def offer_import():
    print('Optional: path to the PREVIOUS app/.env to retain data API keys and strategy settings. Enter skips.')
    source=input('Previous .env path: ').strip().strip('"')
    if source:
        n=import_settings(source,Path(__file__).parent/'.env')
        print('Imported known settings locally:',n,'. Database path and PAPER balance are new. No values printed.')
        from dotenv import dotenv_values
        return bool(dotenv_values(Path(__file__).parent/'.env').get('JUPITER_API_KEY'))
    return False
