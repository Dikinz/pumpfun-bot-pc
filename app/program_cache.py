"""Rooted loader-v3 bytecode cache; trading accounts are never cached.

A cache hit requires fresh confirmed Program+ProgramData metadata after the live
bank. Misses load finalized code; unsupported or changing data uses a full fresh
bank. SHA256 detects local corruption, not a compromised RPC/local administrator.
"""
import base64,hashlib,json,math,sqlite3,struct,threading,time
from pathlib import Path
from contextlib import closing
LOADER='BPFLoaderUpgradeab1e11111111111111111111111'
MAX_BYTES=16*1024*1024
CACHE=Path(__file__).parent/'data/program_cache_v57.db'
GENESIS_CACHE=Path(__file__).parent/'data/program_cache_genesis_v99.json'
_GENESIS_LOCK=threading.Lock()

class CacheError(RuntimeError):pass

def raw(a):
    try:
        if a['data'][1]!='base64':raise ValueError()
        return base64.b64decode(a['data'][0],validate=True)
    except Exception:raise CacheError('CACHE_ACCOUNT_ENCODING') from None

def header(a,payload=None):
    b=raw(a) if payload is None else payload
    if a.get('owner')!=LOADER or a.get('executable') or len(b)<45 or struct.unpack_from('<I',b)[0]!=3 or b[12] not in (0,1):raise CacheError('CACHE_PROGRAMDATA_LAYOUT')
    space=a.get('space')
    if type(space)!=int or not 49<=space<=MAX_BYTES:raise CacheError('CACHE_ACCOUNT_SPACE_UNSUPPORTED')
    return b[:45],struct.unpack_from('<Q',b,4)[0],space

def mapping(lab,accounts,programs):
    for pid,pd in programs.items():
        if lab.program_data_address(accounts.get(pid))!=pd:raise CacheError('CACHE_PROGRAM_POINTER_CHANGED')
        header(accounts.get(pd))

def fingerprint(a,payload=None):
    b,slot,space=header(a,payload)
    return hashlib.sha256(b+space.to_bytes(8,'little')).hexdigest()

class Store:
    def __init__(self,path=CACHE):self.path=Path(path);self.write_failed=False
    def connect(self,readonly=False):
        if readonly:
            c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2);c.row_factory=sqlite3.Row
            return c
        self.path.parent.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(self.path,timeout=.2);c.row_factory=sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL')
        c.execute('CREATE TABLE IF NOT EXISTS programs(namespace TEXT,pid TEXT,pd TEXT,root INTEGER,header_hash TEXT,sha TEXT,account TEXT,touched REAL,PRIMARY KEY(namespace,pid,pd))')
        return c
    def get(self,namespace,pid,pd):
        return self.get_many(namespace,{pid:pd})[pid]
    def get_many(self,namespace,programs):
        # One read-only connection for all code records. No DDL, journal-mode
        # transition, touching timestamps, or cache write lock on a warm hit.
        results={pid:None for pid in programs}
        try:
            with closing(self.connect(readonly=True)) as c:
                for pid,pd in programs.items():
                    r=c.execute('SELECT * FROM programs WHERE namespace=? AND pid=? AND pd=?',(namespace,pid,pd)).fetchone()
                    results[pid]=self.decode_record(r)
        except (sqlite3.Error,OSError,ValueError,TypeError,CacheError):return {pid:None for pid in programs}
        return results
    @staticmethod
    def decode_record(r):
        if not r or type(r['root'])!=int or r['root']<0:return None
        if len(r['account'])>MAX_BYTES*2:return None
        a=json.loads(r['account']);b=raw(a);_,slot,space=header(a,b)
        if len(b)!=space or b[45:49]!=b'\x7fELF' or hashlib.sha256(b).hexdigest()!=r['sha'] or fingerprint(a,b)!=r['header_hash'] or not slot<r['root']:return None
        return dict(account=a,root=r['root'],sha=r['sha'],header_hash=r['header_hash'],code_bytes=len(b)-45)
    def put(self,namespace,pid,pd,a,root):
        b=raw(a);_,slot,space=header(a,b)
        if type(root)!=int or len(b)!=space or b[45:49]!=b'\x7fELF' or not slot<root:raise CacheError('CACHE_CODE_NOT_ROOTED')
        digest=hashlib.sha256(b).hexdigest();fp=fingerprint(a,b)
        try:
            with closing(self.connect()) as c, c:
                c.execute('INSERT OR REPLACE INTO programs VALUES(?,?,?,?,?,?,?,?)',(namespace,pid,pd,root,fp,digest,json.dumps(a),time.time()))
                c.execute('DELETE FROM programs WHERE rowid IN (SELECT rowid FROM programs ORDER BY touched DESC LIMIT -1 OFFSET 16)')
        except (sqlite3.Error,OSError):self.write_failed=True # No writable cache is not a trading failure.
        return dict(account=a,root=root,sha=digest,header_hash=fp,code_bytes=len(b)-45)
    def evict(self,namespace,pid,pd):
        try:
            with closing(self.connect()) as c,c:c.execute('DELETE FROM programs WHERE namespace=? AND pid=? AND pd=?',(namespace,pid,pd))
        except (sqlite3.Error,OSError):pass

def _valid_genesis(value):
    from solders.hash import Hash
    try:Hash.from_string(value);return True
    except Exception:return False # solders.ParseHashError is not a ValueError.

def _genesis_key(url):return hashlib.sha256(url.encode()).hexdigest()

def _genesis_load(path):
    try:
        path=Path(path)
        with path.open('rb') as stream:raw=stream.read(32769)
        if len(raw)>32768:return {}
        data=json.loads(raw)
        return data if isinstance(data,dict) and len(data)<=8 else {}
    except (OSError,ValueError,TypeError,RecursionError):return {}

def _genesis_cached(path,url,ttl):
    from solders.hash import Hash
    row=_genesis_load(path).get(_genesis_key(url))
    try:
        stamp=row['ts'];value=row['value']
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=time.time()-stamp<=ttl:return None
        return value if _valid_genesis(value) else None
    except (TypeError,ValueError,KeyError,OverflowError):return None

def _genesis_store(path,url,value):
    # Non-authoritative, optional cache. Unique state_io temporary files and an
    # OS-held writer lock avoid the supplied patch's shared-PID temp-file race.
    from solders.hash import Hash
    import rpc_budget,state_io
    fd=None
    try:
        if not _valid_genesis(value):return False
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        with _GENESIS_LOCK:
            fd=rpc_budget.lock_slot(Path(str(path)+'.lock'))
            if fd is None:return False
            try:
                now=time.time();data={}
                for key,row in _genesis_load(path).items():
                    try:
                        if len(key)!=64 or any(x not in '0123456789abcdef' for x in key):continue
                        if type(row['ts']) not in (int,float) or not math.isfinite(row['ts']) or row['ts']>now:continue
                        if _valid_genesis(row['value']):data[key]=row
                    except (TypeError,ValueError,KeyError,OverflowError):continue
                data[_genesis_key(url)]=dict(value=value,ts=now)
                data=dict(sorted(data.items(),key=lambda pair:pair[1]['ts'],reverse=True)[:8])
                state_io.atomic_json(path,data)
                return True
            finally:rpc_budget.unlock_slot(fd)
    except (OSError,ValueError,TypeError,OverflowError):return False

def genesis(lab):
    import exit_quotes as q
    from solders.hash import Hash
    url=lab.config.QUOTE_RPC_URL;ttl=getattr(lab.config,'GENESIS_CACHE_TTL_S',0)
    enabled=type(ttl) in (int,float) and math.isfinite(ttl) and 0<ttl<=21600
    path=getattr(lab.config,'GENESIS_CACHE_PATH',GENESIS_CACHE)
    if enabled:
        cached=_genesis_cached(path,url,ttl)
        if cached:return cached
    d=q.request_json(url,'PROGRAM_CACHE_GENESIS',{'jsonrpc':'2.0','id':1,'method':'getGenesisHash','params':[]})
    try:
        value=d['result'];Hash.from_string(value)
    except Exception:raise CacheError('CACHE_GENESIS_UNVERIFIED') from None
    if enabled:_genesis_store(path,url,value)
    return value

def capture_bank(lab,addresses,programs,min_slot,store=None,chain=None):
    import rpc_timing as timing
    store=store or Store();stats=dict(schema='VERIFIED_PROGRAM_CACHE_V57',hits=0,downloads=0,bytecode_bytes_reused=0,mode='PREPARING',programs=[])
    all_addresses=list(dict.fromkeys(list(addresses)+list(programs.values())))
    fallback_slot=min_slot
    if timing.ACTIVE:timing.ACTIVE.data['program_cache']=stats
    def fallback(reason):
        stats['fallback_reason']=reason
        with timing.stage_scope('PROGRAM_CACHE_FULL_FALLBACK'):bank=lab.read_accounts(all_addresses,fallback_slot)
        # Fall back to the original complete-bank path, with all loader checks.
        for pd in programs.values():
            a=bank['accounts'].get(pd)
            if a is not None:a.setdefault('space',len(raw(a)))
        mapping(lab,bank['accounts'],programs)
        for pd in programs.values():
            a=bank['accounts'][pd];b=raw(a)
            if len(b)!=header(a)[2] or b[45:49]!=b'\x7fELF' or not header(a)[1]<bank['slot']:raise CacheError('CACHE_FALLBACK_INVALID_ELF')
        stats['mode']='FULL_FRESH'
        bank['program_cache']=stats
        if timing.ACTIVE:timing.ACTIVE.flush()
        return bank
    if not getattr(lab.config,'PROGRAM_CACHE_ENABLED',True):return fallback('DISABLED')
    with timing.stage_scope('PROGRAM_CACHE_GENESIS'):
        try:chain_id=chain if chain is not None else genesis(lab)
        except Exception:return fallback('GENESIS_UNAVAILABLE')
    namespace=hashlib.sha256((lab.config.QUOTE_RPC_URL+'\0'+chain_id).encode()).hexdigest()
    records=store.get_many(namespace,programs)
    missing={pid:pd for pid,pd in programs.items() if not records[pid]}
    if missing:
        fetch=list(missing)+list(missing.values())
        with timing.stage_scope('PROGRAM_CACHE_FINALIZED_DOWNLOAD'):
            try:
                rooted=lab.read_accounts(fetch,commitment='finalized')
                if type(rooted['slot'])!=int:raise CacheError('CACHE_ROOT_SLOT_INVALID')
                fallback_slot=max(fallback_slot,rooted['slot'])
                for pd in missing.values():
                    a=rooted['accounts'].get(pd)
                    if a is not None:a.setdefault('space',len(raw(a)))
                mapping(lab,rooted['accounts'],missing)
                for pid,pd in missing.items():
                    records[pid]=store.put(namespace,pid,pd,rooted['accounts'][pd],rooted['slot']);stats['downloads']+=1
            except CacheError:return fallback('UNSUPPORTED_FINALIZED_CODE')
    floor=max([min_slot]+[x['root'] for x in records.values()])
    with timing.stage_scope('FRESH_TRADING_BANK'):
        bank=lab.read_accounts(list(addresses),floor)
    # minContextSlot rejects older responses; all trading account bytes come
    # from this one live bank. Fresh metadata is a later version proof only.
    if type(bank['slot'])!=int or bank['slot']<floor:raise CacheError('CACHE_TRADING_BANK_STALE')
    fallback_slot=bank['slot']
    with timing.stage_scope('PROGRAM_CACHE_VERSION_CHECK'):
        metadata=lab.read_accounts(list(programs)+list(programs.values()),bank['slot'],data_slice=45)
    if type(metadata['slot'])!=int:raise CacheError('CACHE_METADATA_SLOT_INVALID')
    fallback_slot=max(fallback_slot,metadata['slot'])
    try:
        mapping(lab,metadata['accounts'],programs)
        for pid,pd in programs.items():
            rec=records[pid];remote=metadata['accounts'][pd]
            if len(raw(remote))!=45 or lab.program_data_address(bank['accounts'].get(pid))!=pd or fingerprint(remote)!=rec['header_hash']:raise CacheError('CACHE_VERSION_CHANGED')
            # Cached rooted code cannot be used for an earlier/fork bank.
            if not rec['root']<=bank['slot']<=metadata['slot']:raise CacheError('CACHE_SLOT_ORDER')
    except CacheError:
        for pid,pd in programs.items():store.evict(namespace,pid,pd)
        return fallback('VERSION_OR_METADATA_CHANGED')
    for pid,pd in programs.items():
        rec=records[pid];a=dict(metadata['accounts'][pd]);a['data']=rec['account']['data']
        bank['accounts'][pd]=a
        reused=pid not in missing;stats['hits']+=reused
        if reused:stats['bytecode_bytes_reused']+=rec['code_bytes']
        stats['programs'].append(dict(program=pid,programdata=pd,sha256=rec['sha'],finalized_code_bank=rec['root'],last_modified_slot=header(metadata['accounts'][pd])[1],cache_hit=reused))
    stats.update(mode='VERIFIED_CODE_WITH_FRESH_TRADING_BANK',trading_slot=bank['slot'],metadata_slot=metadata['slot'],cache_write_failed=store.write_failed)
    bank['program_cache']=stats
    if timing.ACTIVE:timing.ACTIVE.flush()
    return bank
