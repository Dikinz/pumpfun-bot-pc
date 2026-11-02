"""Tray icon (spec 2.5): pystray, 4 state icons from desktop/assets, tooltip 'PumpFun PAPER — <state>', menu
Показать окно (default) / Состояние: … (disabled) / Остановить бота… / Выйти из окна, notifications.
pystray and Pillow are imported lazily (fake_gui puts fakes into sys.modules). The icon is started with
run_detached() before webview.start() (spike workaround 4); menu callbacks run in their own thread so a
dialog never blocks the tray message loop.
Icons: python desktop/tray.py --write-icons regenerates the four .ico files with Pillow."""
import threading,time
from pathlib import Path
ASSETS=Path(__file__).resolve().parent/'assets'
ICONS=dict(grey='app_idle.ico',green='app_running.ico',yellow='app_busy.ico',red='app_error.ico')
COLORS=dict(grey=(138,147,160),green=(46,189,133),yellow=(240,185,11),red=(246,70,93))  # terminal.html --mut/--up/--warn/--dn
SIZES=(16,20,24,32,40,48,64,128,256)
NAME='PumpfunPaperDesktop'
TITLE='PumpFun PAPER'
TEXT=dict(show='Показать окно',state='Состояние: {text}',stop='Остановить бота…',quit='Выйти из окна')

def draw(state,size=256):
    """Dark rounded square, state-coloured disc, white 'P' (PAPER). Pure Pillow drawing, no fonts."""
    from PIL import Image,ImageDraw
    s=size/256;img=Image.new('RGBA',(size,size),(0,0,0,0));d=ImageDraw.Draw(img)
    d.rounded_rectangle((4*s,4*s,252*s,252*s),radius=48*s,fill=(18,22,28,255),outline=(44,53,66,255),width=max(1,round(6*s)))
    d.ellipse((34*s,34*s,222*s,222*s),fill=COLORS[state]+(255,))
    w=(255,255,255,255);d.rectangle((92*s,70*s,120*s,186*s),fill=w)
    d.rounded_rectangle((92*s,70*s,170*s,136*s),radius=33*s,fill=w)
    d.rounded_rectangle((120*s,94*s,146*s,112*s),radius=9*s,fill=COLORS[state]+(255,))
    return img

def write_icons(folder=ASSETS):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);written=[]
    for state,name in ICONS.items():
        draw(state).save(folder/name,format='ICO',sizes=[(n,n) for n in SIZES]);written.append(folder/name)
    return written

def load_image(state,folder=ASSETS):
    """PIL image of the state icon (.ico from assets; drawn when the file is missing or damaged)."""
    from PIL import Image
    try:
        img=Image.open(Path(folder)/ICONS[state]);img.load();return img
    except (OSError,ValueError):return draw(state,64)

def thread(fn):threading.Thread(target=fn,name='desktop-tray-action',daemon=True).start()
def _later(fn,spawn):
    def run(icon,item):spawn(fn)
    return run

class Tray:
    """on_show/on_stop/on_quit are shell callbacks; update() is called on every poll."""
    def __init__(self,on_show,on_stop,on_quit,pystray=None,loader=load_image,spawn=thread):
        if pystray is None:import pystray
        self.pystray=pystray;self.loader=loader;self.state=None;self.tip=TITLE;self.text='…';self.stop_enabled=False
        self.started=False;self.notices=[];self.pending=[];self._images={};self._spawn=spawn
        P=pystray
        menu=P.Menu(P.MenuItem(TEXT['show'],_later(on_show,spawn),default=True),
            P.MenuItem(lambda item:TEXT['state'].format(text=self.text),None,enabled=False),
            P.Menu.SEPARATOR,
            P.MenuItem(TEXT['stop'],_later(on_stop,spawn),enabled=lambda item:self.stop_enabled),
            P.Menu.SEPARATOR,
            P.MenuItem(TEXT['quit'],_later(on_quit,spawn)))
        self.icon=P.Icon(NAME,self.image('grey'),TITLE,menu)
    def image(self,state):
        if state not in self._images:self._images[state]=self.loader(state)
        return self._images[state]
    NOTICE_DELAY=1.5  # a balloon right after run_detached() is lost while the icon is not yet shown
    def start(self):
        self.icon.run_detached();self.started=True
        if self.pending:
            title,message=self.pending[-1];self.pending=[]
            def later():
                time.sleep(self.NOTICE_DELAY);self.notify(message,title)
            self._spawn(later)
        return self
    def update(self,state,tip,text,stop_enabled):
        changed=False
        if state!=self.state:self.icon.icon=self.image(state);self.state=state;changed=True
        if tip!=self.tip:self.icon.title=tip[:127];self.tip=tip;changed=True
        if text!=self.text or bool(stop_enabled)!=self.stop_enabled:self.text=text;self.stop_enabled=bool(stop_enabled);changed=True
        if changed and self.started:
            try:self.icon.update_menu()
            except Exception:pass
        return changed
    def notify(self,message,title=TITLE):
        self.notices.append((title,message))
        if not self.started:self.pending.append((title,message));return False
        try:self.icon.notify(message,title);return True
        except Exception:return False
    def stop(self):
        if not self.started:return
        self.started=False
        try:self.icon.visible=False
        except Exception:pass
        try:self.icon.stop()
        except Exception:pass

if __name__=='__main__':
    import sys
    if sys.argv[1:]==['--write-icons']:
        for path in write_icons():print(path)
    else:raise SystemExit('usage: python desktop/tray.py --write-icons')
