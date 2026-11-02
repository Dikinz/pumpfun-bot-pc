"""Window settings in %LOCALAPPDATA%\\PumpfunPaperDesktop\\<identity>\\settings.json (never app/data):
geometry, "tray notice shown", console mode, the token of the last acknowledged self-stopped run. Unknown keys and bad values fall back to defaults; a damaged
file never stops the window. Writes are atomic (temp file + os.replace)."""
import json,os,re,tempfile
from pathlib import Path
DEFAULTS=dict(geometry=None,tray_notice_shown=False,console_mode='hidden',self_stop_acked=None)
TOKEN=re.compile('[0-9a-f]{32}')  # desktop_engine.TOKEN_RE
CONSOLE_MODES=('hidden',)  # spec decision 7: the engine console is hidden only in the MVP
MIN_SIZE=(480,320);MAX_SIZE=(10000,10000);MAX_BYTES=64*1024

def _geometry(value):
    if not isinstance(value,dict):return None
    try:x,y,w,h=(value[k] for k in ('x','y','width','height'))
    except KeyError:return None
    if not all(type(v) is int for v in (x,y,w,h)):return None
    if not (-20000<=x<=20000 and -20000<=y<=20000):return None
    w=min(max(w,MIN_SIZE[0]),MAX_SIZE[0]);h=min(max(h,MIN_SIZE[1]),MAX_SIZE[1])
    return dict(x=x,y=y,width=w,height=h)

def clean(data):
    out=dict(DEFAULTS)
    if not isinstance(data,dict):return out
    out['geometry']=_geometry(data.get('geometry'))
    if type(data.get('tray_notice_shown')) is bool:out['tray_notice_shown']=data['tray_notice_shown']
    if data.get('console_mode') in CONSOLE_MODES:out['console_mode']=data['console_mode']
    token=data.get('self_stop_acked')
    if isinstance(token,str) and TOKEN.fullmatch(token):out['self_stop_acked']=token
    return out

class SettingsStore:
    def __init__(self,path):self.path=Path(path);self.data=dict(DEFAULTS)
    def load(self):
        try:
            if self.path.stat().st_size>MAX_BYTES:raise ValueError('too big')
            self.data=clean(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError,ValueError):self.data=dict(DEFAULTS)
        return self.data
    def get(self,key):return self.data.get(key,DEFAULTS.get(key))
    def update(self,**values):
        unknown=set(values)-set(DEFAULTS)
        if unknown:raise KeyError('DESKTOP_SETTING_UNKNOWN: '+sorted(unknown)[0])
        self.data=clean(dict(self.data,**values));return self.save()
    def save(self):
        """True when written; a failed write is reported, never raised (settings are a convenience)."""
        temp=None
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd,temp=tempfile.mkstemp(prefix='.settings.',suffix='.tmp',dir=self.path.parent)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(self.data,stream,ensure_ascii=False,indent=1)
            os.replace(temp,self.path);temp=None;return True
        except OSError:return False
        finally:
            if temp:
                try:os.unlink(temp)
                except OSError:pass
