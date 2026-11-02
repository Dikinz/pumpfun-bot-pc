"""Recording fakes of pywebview, pystray and Pillow for the GUI shell tests (spec 6.2). install(monkeypatch)
puts them into sys.modules ('webview', 'webview.menu', 'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw'),
so the shell runs in the bot's .venv without any GUI library and without a real window. The fake webview also
carries PFDESK_CONFIRM, the seam window.Shell uses instead of the native MessageBoxW: the answer comes from the
main window's `answers` list and is recorded as a ('confirm', title, text) call; nothing is shown."""
import sys,threading,types

class Event:
    """pywebview Event semantics: handlers with no parameters; set() is True when any handler returned False
    (for 'closing' that cancels the close)."""
    def __init__(self):self.items=[];self.flag=threading.Event()
    def __iadd__(self,fn):self.items.append(fn);return self
    def __isub__(self,fn):self.items.remove(fn);return self
    def set(self):
        values=[fn() for fn in list(self.items)];self.flag.set()
        return any(v is False for v in values)
    def wait(self,timeout=None):return self.flag.wait(timeout)
    def is_set(self):return self.flag.is_set()

class Events:
    def __init__(self):
        for name in ('closing','closed','shown','loaded','minimized','restored'):setattr(self,name,Event())

class Window:
    def __init__(self,webview,title,kw):
        self.webview=webview;self.title=title;self.kw=kw;self.url=kw.get('url');self.html=kw.get('html');self.js_api=kw.get('js_api')
        self.menu=kw.get('menu');self.events=Events();self.calls=[];self.visible=True;self.destroyed=False;self.answers=[]
        self.x=kw.get('x',100);self.y=kw.get('y',80);self.width=kw.get('width',800);self.height=kw.get('height',600)
    def _call(self,name,*args):self.calls.append((name,)+args)
    def load_url(self,url):self._call('load_url',url);self.url=url;self.html=None
    def load_html(self,html):self._call('load_html',len(html));self.html=html;self.url=None
    def set_title(self,title):self._call('set_title',title);self.title=title
    def hide(self):self._call('hide');self.visible=False
    def show(self):self._call('show');self.visible=True
    def restore(self):self._call('restore')
    def evaluate_js(self,script):self._call('evaluate_js',script)
    def create_confirmation_dialog(self,title,message):
        self._call('confirm',title,message)
        return self.answers.pop(0) if self.answers else False
    def destroy(self):
        """Like pywebview: destroy() fires 'closing' too; the close happens only when not cancelled."""
        self._call('destroy')
        if self.events.closing.set():return
        self.destroyed=True;self.visible=False;self.events.closed.set()
    def user_close(self):
        """The X button: 'closing' may cancel it."""
        if self.events.closing.set():return False
        self.destroyed=True;self.visible=False;self.events.closed.set();return True
    def names(self):return [c[0] for c in self.calls]

def make_webview():
    wv=types.ModuleType('webview');wv.windows=[];wv.started=[];wv.FAKE=True
    def create_window(title,**kw):
        w=Window(wv,title,kw);wv.windows.append(w);return w
    def start(func=None,**kw):
        wv.started.append(kw)
        for w in wv.windows:w.events.shown.set()
        if func:func()
    def confirm(window,title,text):return window.create_confirmation_dialog(title,text)
    wv.create_window=create_window;wv.start=start;wv.PFDESK_CONFIRM=confirm
    menu=types.ModuleType('webview.menu')
    class Menu:
        def __init__(self,title,items=()):self.title=title;self.items=list(items)
    class MenuAction:
        def __init__(self,title,function):self.title=title;self.function=function
    class MenuSeparator:pass
    menu.Menu=Menu;menu.MenuAction=MenuAction;menu.MenuSeparator=MenuSeparator;wv.menu=menu
    return wv,menu

def make_pystray():
    ps=types.ModuleType('pystray');ps.icons=[]
    class MenuItem:
        def __init__(self,text,action,checked=None,radio=False,default=False,visible=True,enabled=True):
            self._text=text;self.action=action;self.default=default;self._enabled=enabled
        @property
        def text(self):return self._text(self) if callable(self._text) else self._text
        @property
        def enabled(self):return self._enabled(self) if callable(self._enabled) else self._enabled
        def __call__(self,icon):
            if self.action is not None:self.action(icon,self)
    class Menu:
        SEPARATOR=MenuItem('- - - -',None)
        def __init__(self,*items):self.items=items
    class Icon:
        def __init__(self,name,icon=None,title=None,menu=None):
            self.name=name;self._icon=icon;self.title=title;self.menu=menu;self.icons=[icon];self.visible=False
            self.running=False;self.notes=[];self.menu_updates=0;self.detached=0;self.stopped=0;ps.icons.append(self)
        @property
        def icon(self):return self._icon
        @icon.setter
        def icon(self,value):self._icon=value;self.icons.append(value)
        def run_detached(self,setup=None):self.detached+=1;self.running=True;self.visible=True
        def run(self,setup=None):raise AssertionError('the shell must use run_detached (spike workaround 4)')
        def stop(self):self.stopped+=1;self.running=False
        def notify(self,message,title=None):self.notes.append((title,message))
        def update_menu(self):self.menu_updates+=1
        def item(self,text):
            for it in self.menu.items:
                if it is not Menu.SEPARATOR and it.text.startswith(text):return it
            raise KeyError(text)
    ps.MenuItem=MenuItem;ps.Menu=Menu;ps.Icon=Icon
    return ps

def make_pil():
    pil=types.ModuleType('PIL');image=types.ModuleType('PIL.Image');draw=types.ModuleType('PIL.ImageDraw');image.opened=[]
    class FakeImage:
        def __init__(self,path=None,size=None):self.path=path;self.size=size
        def load(self):return None
        def __repr__(self):return 'FakeImage('+str(getattr(self.path,'name',self.path))+')'
    def open_(path):
        from pathlib import Path
        if not Path(path).is_file():raise OSError('missing '+str(path))
        img=FakeImage(path);image.opened.append(img);return img
    def new(mode,size,color=None):return FakeImage(None,size)
    image.open=open_;image.new=new;image.Image=FakeImage
    class Draw:
        def __init__(self,img):pass
        def __getattr__(self,name):return lambda *a,**k:None
    draw.Draw=Draw;pil.Image=image;pil.ImageDraw=draw
    return pil,image,draw

def install(monkeypatch):
    """Fresh fakes for one test; returns SimpleNamespace(webview, menu, pystray, Image)."""
    wv,menu=make_webview();ps=make_pystray();pil,image,draw=make_pil()
    for name,module in (('webview',wv),('webview.menu',menu),('pystray',ps),('PIL',pil),('PIL.Image',image),('PIL.ImageDraw',draw)):
        monkeypatch.setitem(sys.modules,name,module)
    return types.SimpleNamespace(webview=wv,menu=menu,pystray=ps,Image=image)
