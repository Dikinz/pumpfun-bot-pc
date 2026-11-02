"""Window logs in app/data/desktop/logs: names, rotation (only own name patterns), tails and markers."""
import re,time
from pathlib import Path
from . import constants as C
FAMILIES=dict(engine=re.compile(r'engine_\d{8}_\d{6}_[0-9a-f]{8}\.log'),results=re.compile(r'results_\d{8}_\d{6}\.log'),check=re.compile(r'check_\d{8}_\d{6}\.log'))

def stamp(ts=None):return time.strftime('%Y%m%d_%H%M%S',time.localtime(ts))
def engine_name(token,ts=None):return C.engine.log_name(token,ts)
def action_name(kind,ts=None):
    if kind not in ('results','check'):raise ValueError('DESKTOP_LOG_KIND')
    return kind+'_'+stamp(ts)+'.log'

def new_log(folder,name):
    """Create the log file exclusively. Never overwrite: on a same-second clash wait for the next second
    (a numbered twin would not match the rotation patterns)."""
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    for _ in range(3):
        path=folder/name
        try:return path.open('xb')
        except FileExistsError:
            time.sleep(1.05);name=re.sub(r'\d{8}_\d{6}',stamp(),name,count=1)
    raise FileExistsError(str(folder/name))

def rotate(folder,keep=C.LOG_KEEP,protect=()):
    """Keep the newest `keep` files of each family; delete only names matching the own patterns.
    Names sort by their timestamp. Protected names (the running engine's log) are never deleted."""
    folder=Path(folder);removed=[];protect=set(protect)
    try:names=[p.name for p in folder.iterdir() if p.is_file()]
    except OSError:return removed
    for pattern in FAMILIES.values():
        own=sorted(n for n in names if pattern.fullmatch(n))
        for name in own[:max(0,len(own)-keep)]:
            if name in protect:continue
            try:(folder/name).unlink();removed.append(name)
            except OSError:pass
    return removed

def ansi_encoding():
    """Windows ANSI code page (cp1251 on Russian Windows): what Python without UTF-8 mode writes into a redirected
    stdout. locale.getencoding() ignores UTF-8 mode; 'mbcs' is the fallback on Windows."""
    import codecs,locale,os
    try:enc=locale.getencoding()
    except AttributeError:enc=None
    try:
        if enc and codecs.lookup(enc).name!='utf-8':return enc
    except LookupError:pass
    return 'mbcs' if os.name=='nt' else 'latin-1'
ANSI=ansi_encoding()

def decode_line(raw,ansi=None):
    """One log line: strict UTF-8, else the ANSI code page, else UTF-8 with replacement characters. The host's
    children write ANSI (no PYTHONUTF8, like 03_START), the window and 04 write UTF-8: one log can hold both."""
    try:return raw.decode('utf-8')
    except UnicodeDecodeError:pass
    try:return raw.decode(ansi or ANSI)
    except (UnicodeDecodeError,LookupError):return raw.decode('utf-8',errors='replace')

def decode(data,ansi=None):return '\n'.join(decode_line(line,ansi) for line in data.split(b'\n'))

def read_tail(path,max_bytes=64*1024):
    """Last bytes of a log as text (decoded per line, see decode_line); '' when absent."""
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-max_bytes));data=stream.read()
    except OSError:return ''
    if size>max_bytes and b'\n' in data:data=data.split(b'\n',1)[1]  # the first line may start mid-character
    return decode(data)

def tail_lines(path,lines=40):return '\n'.join(read_tail(path).splitlines()[-lines:])

def last_line(text,prefix):
    found=None
    for line in text.splitlines():
        line=line.strip()
        if line.startswith(prefix):found=line
    return found

def audit_line(path):
    """Last SQLITE SHUTDOWN AUDIT line of the engine log (from the marker on, whatever precedes it)."""
    found=None
    for line in read_tail(path,256*1024).splitlines():
        if C.AUDIT_PREFIX in line:found=line[line.index(C.AUDIT_PREFIX):].strip()
    return found

def evidence(text):
    """Folder to open after 05: FULL EVIDENCE FOLDER wins, else the folder of PRESERVED EVIDENCE."""
    full=last_line(text,C.EVIDENCE_PREFIXES[0]);kept=last_line(text,C.EVIDENCE_PREFIXES[1])
    if full:return Path(full[len(C.EVIDENCE_PREFIXES[0]):].strip())
    if kept:
        target=Path(kept[len(C.EVIDENCE_PREFIXES[1]):].strip())
        return target.parent if target.suffix.lower()=='.zip' else target
    return None

def too_big(path,limit=C.LOG_WARN_BYTES):
    try:return Path(path).stat().st_size>limit
    except OSError:return False
