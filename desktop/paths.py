"""Installation layout seen by the window, the database identity and the short-path preflight."""
import hashlib,json,os,sys
from dataclasses import dataclass,field
from pathlib import Path
from . import constants as C

def identity(db):
    """Same value as app/system_health.identity: sha256(normcase(resolve(db)))[:20]."""
    return hashlib.sha256(os.path.normcase(str(Path(db).resolve())).encode()).hexdigest()[:20]

def manifest_version(root):
    try:value=json.loads((Path(root)/'BUILD_MANIFEST.json').read_text(encoding='utf-8')).get('version')
    except (OSError,ValueError,AttributeError):return None
    return value if isinstance(value,str) else None

@dataclass
class Layout:
    """All paths of one installation. Tests build it on a temporary folder with their own port."""
    root:Path=C.ROOT
    port:int=C.DEFAULT_PORT
    version:str|None=None   # None -> BUILD_MANIFEST.json version
    db:Path|None=None       # None -> app/data/clean_0762.db (the only database a clean start accepts)
    control:Path|None=None  # None -> app/data/desktop
    profile_base:Path|None=None  # None -> %LOCALAPPDATA%\PumpfunPaperDesktop
    def __post_init__(self):
        self.root=Path(self.root).resolve();self.app=self.root/'app'
        if self.db is None:self.db=self.app/C.CLEAN_DB
        self.db=Path(self.db).resolve()
        self.control=Path(self.control or self.app/'data'/'desktop')
        if self.version is None:self.version=manifest_version(self.root)
        if self.port in C.FORBIDDEN_PORTS:raise ValueError('DESKTOP_PORT_FORBIDDEN')
    @property
    def logs(self):return self.control/C.LOG_DIR_NAME
    @property
    def results(self):return self.root/'results'
    @property
    def venv_python(self):return self.root/'.venv'/'Scripts'/'python.exe'
    @property
    def engine_script(self):return self.app/'desktop_engine.py'
    @property
    def bootstrap(self):return self.app/'bootstrap.py'
    @property
    def identity(self):return identity(self.db)
    @property
    def url(self):return 'http://'+C.HOST+':'+str(self.port)+'/'
    @property
    def profile(self):
        base=self.profile_base
        if base is None:base=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local')/C.APP_ID
        return Path(base)/self.identity
    @property
    def webview_storage(self):return self.profile/'WebView2'
    @property
    def settings_file(self):return self.profile/'settings.json'
    def bat(self,name):return self.root/name

def _links(path):
    """Ancestors of path that are symlinks or junctions (spike: .NET refuses DLLs behind a junction)."""
    found=[];p=Path(os.path.abspath(path))
    for item in (p,*p.parents):
        try:
            if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):found.append(str(item))
        except OSError:pass
    return found

def short_path_problem(prefix=None,scan=True):
    """Spike workaround 1: None when the GUI environment's native files load, else a clear Russian error.
    Checks the WebView2Loader.dll path that pywebview will load (installed or not yet) and, with scan,
    every .dll/.pyd under site-packages. Junctions/symlinks are refused, never used as a shortcut."""
    prefix=Path(prefix or sys.prefix)
    links=_links(prefix)
    if links:return 'DESKTOP_PATH_LINK: папка окружения окна идёт через ссылку/junction ('+links[0]+'). Разместите приложение в обычной папке с коротким путём.'
    real=Path(os.path.realpath(prefix))
    worst=(len(str(real.joinpath(*C.WEBVIEW2_LOADER))),str(real.joinpath(*C.WEBVIEW2_LOADER)),C.DLL_PATH_MAX)
    if scan:
        site=real/'Lib'/'site-packages'
        try:files=[p for p in site.rglob('*') if p.suffix.lower() in ('.dll','.pyd')]
        except OSError:files=[]
        for p in files:
            limit=C.PYD_PATH_MAX if p.suffix.lower()=='.pyd' else C.DLL_PATH_MAX
            if len(str(p))-limit>worst[0]-worst[2]:worst=(len(str(p)),str(p),limit)
    if worst[0]>worst[2]:
        return ('DESKTOP_PATH_TOO_LONG: путь к файлу окружения окна длиной '+str(worst[0])+' символов (допустимо '+str(worst[2])+'): '+worst[1]
            +'. Распакуйте приложение в папку с коротким путём, например %LOCALAPPDATA%\\PumpfunBot, и запустите 16_DESKTOP.bat заново.')
    return None
