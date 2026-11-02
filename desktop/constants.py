"""Window-side constants. The engine contract comes only from app/desktop_engine.py (stdlib-only, no
side effects on import), loaded by file path so the window never needs app/ on sys.path for it."""
import importlib.util,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'

def _engine():
    module=sys.modules.get('desktop_engine')
    if module is not None and Path(module.__file__).resolve()==(APP/'desktop_engine.py').resolve():return module
    spec=importlib.util.spec_from_file_location('desktop_engine',APP/'desktop_engine.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    sys.modules.setdefault('desktop_engine',module)
    return module
engine=_engine()
# Contract (single source of truth: app/desktop_engine.py).
SCHEMA=engine.SCHEMA;STATE_NAME=engine.STATE_NAME;STOP_TEMPLATE=engine.STOP_TEMPLATE;PHASES=engine.PHASES
ACTIVE_PHASES=engine.ACTIVE_PHASES;TOKEN_RE=engine.TOKEN_RE;FIELDS=engine.FIELDS;LOG_TEMPLATE=engine.LOG_TEMPLATE
LOG_DIR_NAME=engine.LOG_DIR_NAME;CLEAR_ENV=engine.CLEAR_ENV;CLEAN_DB=engine.CLEAN_DB
EXIT_FAILED=engine.EXIT_FAILED;EXIT_USAGE=engine.EXIT_USAGE;EXIT_ALREADY_RUNNING=engine.EXIT_ALREADY_RUNNING

APP_ID='PumpfunPaperDesktop'
APP_USER_MODEL_ID='PumpfunPaper.Desktop'  # taskbar/notification identity (spec 2.5), set before any window
APP_MUTEX='PumpfunPaperDesktopApp'  # fixed name for the Inno Setup AppMutex, held while a window process runs
DEFAULT_PORT=8188
FORBIDDEN_PORTS=(49190,)  # PUMPAPI singleton: the window never touches it
HOST='127.0.0.1'
POLL_S=2.
HEALTH_TIMEOUT_S=.5
HEALTH_MISS_LIMIT=2  # EXTERNAL_SAME/ORPHAN_RUNNING -> STOPPED/ENGINE_LOST only after this many failed /health polls in a row
START_TIMEOUT_S=120.  # after this a start is shown as slow; it never fails on time while the host lives
STOP_SLOW_S=180.
LOG_KEEP=20
LOG_WARN_BYTES=1<<30
# Window states (spec 2.1). OWN_<phase> for every active engine phase. ORPHAN_RUNNING: the ENGINE_LOST conditions
# (active phase, host dead/unknown) while /health answers with this installation's identity and the dashboard process
# descends from the recorded host: dashboard/main.py/monitor outlived the host, whose launcher alone could stop them
# gracefully. Not a descendant (or unknown): EXTERNAL_SAME.
OWN='OWN_';ENGINE_LOST='ENGINE_LOST';ORPHAN_RUNNING='ORPHAN_RUNNING';EXTERNAL_SAME='EXTERNAL_SAME';EXTERNAL_FOREIGN='EXTERNAL_FOREIGN';STOPPED='STOPPED'
OWN_PREFLIGHT=OWN+'PREFLIGHT';OWN_STARTING=OWN+'STARTING';OWN_RUNNING=OWN+'RUNNING';OWN_STOP_REQUESTED=OWN+'STOP_REQUESTED'
STATES=tuple(OWN+p for p in ACTIVE_PHASES)+(ENGINE_LOST,ORPHAN_RUNNING,EXTERNAL_SAME,EXTERNAL_FOREIGN,STOPPED)
TERMINAL_STATES=(OWN_RUNNING,OWN_STOP_REQUESTED,EXTERNAL_SAME,ORPHAN_RUNNING)  # dashboard answers: 'Терминал' allowed
AUTO_TERMINAL_STATES=(OWN_RUNNING,OWN_STOP_REQUESTED,EXTERNAL_SAME)  # the window loads the dashboard by itself
# /health-derived changes that wait for HEALTH_MISS_LIMIT. EXTERNAL_SAME -> ENGINE_LOST: 03_START over a stale active
# engine_state.json (M1 round 2) falls back to ENGINE_LOST when its dashboard goes, never on one slow answer.
HEALTH_HELD={EXTERNAL_SAME:(STOPPED,ENGINE_LOST),ORPHAN_RUNNING:(ENGINE_LOST,)}
# Log-off/shutdown (spec 2.6, MINOR 4): the session is held while the own campaign starts, runs or stops; a stop
# file is written in the first three (the host honours it once its stop_condition runs).
SESSION_HOLD_STATES=(OWN_PREFLIGHT,OWN_STARTING,OWN_RUNNING,OWN_STOP_REQUESTED)
SESSION_STOP_STATES=(OWN_PREFLIGHT,OWN_STARTING,OWN_RUNNING)
# Actions (spec 2.3/2.4) and the js_api method names that map onto them.
ACTIONS=('start','stop','results','check','settings','preserve','open_results','open_logs','show_log','show_terminal')
BACKGROUND=('results','check')
JS_API=('get_status','start','stop','run_results','run_check','open_settings','open_preserve','open_results_folder','open_logs','show_terminal')
# Process creation (Windows constants; plain ints so this module imports anywhere).
CREATE_NEW_CONSOLE=0x10;CREATE_NEW_PROCESS_GROUP=0x200;CREATE_NO_WINDOW=0x08000000
DETACHED_FLAGS=CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP
ENGINE_CLEAR_ENV=CLEAR_ENV+('EXECUTION_MODE',)
# No PYTHON* additions (MINOR 1): -I implies -E for the host and bootstrap, but the launcher's children (main.py,
# dashboard, monitor) read the environment; they get exactly what 03_START.bat / 05_RESULTS.bat would give them.
CHECK_SET_ENV=(('PYTHONUTF8','1'),)  # 04_CHECK.bat sets PYTHONUTF8=1 itself; nothing else is added anywhere
CONSOLE_CLEAR_ENV=('PYTHONPATH','PYTHONHOME')  # what every .bat clears itself
# Short-path preflight (spike workaround 1): Windows refuses longer DLL/.pyd paths even with LongPathsEnabled.
DLL_PATH_MAX=259;PYD_PATH_MAX=247
WEBVIEW2_LOADER=('Lib','site-packages','webview','lib','runtimes','win-x64','native','WebView2Loader.dll')
SHUTDOWN_PRIORITY=0x3FF  # highest application level: the window hears WM_QUERYENDSESSION first
AUDIT_PREFIX='SQLITE SHUTDOWN AUDIT'
EVIDENCE_PREFIXES=('FULL EVIDENCE FOLDER:','PRESERVED EVIDENCE:')
# Texts (Russian UI; 04 texts are the exact 04_CHECK.bat lines).
TITLE='PumpFun PAPER {version} — {state}'
TRAY_TIP='PumpFun PAPER — {state}'
STATE_TEXT=dict(OWN_PREFLIGHT='проверка перед запуском',OWN_STARTING='запуск',OWN_RUNNING='работает',OWN_STOP_REQUESTED='останавливается',
    ENGINE_LOST='движок завершился без итога',ORPHAN_RUNNING='бот работает без управляющего процесса',
    EXTERNAL_SAME='работает (03_START)',EXTERNAL_FOREIGN='порт занят другой установкой',STOPPED='остановлен')
CHECK_TEXT={0:('PASSED','Technical tests and device time check PASSED.'),
    2:('TIME_WARNING','Technical tests passed, but time check needs attention.\nSee app\\data\\time_check.txt. Keep this report before the next test.\nNo clock correction was performed. Trading tolerance is unchanged.')}
CHECK_FAILED=('FAILED','Technical check failed. Keep this window and copy the error text.')
TEXT=dict(
    no_venv='Не найден .venv. Запустите 01_INSTALL.bat в этой папке.',
    lost='Движок завершился без итога: процессы бота закрыты без корректной остановки, скорее всего извне '
        '(антивирус, диспетчер задач, отключение питания). Проверьте журнал движка, журнал и карантин антивируса, '
        'затем выполните 05_RESULTS / 09_RECOVERY_CHECK.',
    lost_seen=' Последний признак работы бота: {when}.',
    lost_confirm='Сначала подтвердите предупреждение о движке, завершившемся без итога.',
    orphan='Управляющий процесс бота завершился, а процессы бота ещё работают. Запуск, 05, 04 и настройки недоступны, пока они не завершатся. '
        'Перезагрузите компьютер, затем запустите 09_RECOVERY_CHECK.',
    self_stopped='Бот остановился сам (код {code}). Откройте журнал.',
    external_same='Бот запущен через 03_START.bat: остановите его Ctrl+C в окне 03_START.',
    external_foreign='На порту {port} работает другая установка/версия.',
    running='Бот работает. Сначала остановите его.',
    not_running='Бот не запущен из этого окна.',
    busy='Уже выполняется действие: {action}.',
    starting='Идёт запуск бота.',
    stopping='Идёт остановка бота.',
    stop_confirm='Корректная остановка, как Ctrl+C в 03_START. Займёт обычно до 1–2 мин. Не выключайте ПК.',
    stop_slow='Остановка идёт дольше обычного. Процессы не завершаются принудительно; см. журнал.',
    stop_bad='Остановка завершилась с кодом {code}: см. журнал, выполните 05_RESULTS/09_RECOVERY_CHECK.',
    stop_ok='Бот остановлен корректно (код 0).',
    start_slow='Запуск идёт дольше обычного (проверки на большой базе).',
    start_exited='Хост движка завершился до запуска (код {code}).',
    already_running='Движок этой папки уже запущен (ALREADY_RUNNING).',
    quit_confirm='Окно закроется, PAPER-бот продолжит работать. Остановить его можно, снова открыв приложение.',
    tray_notice='Бот продолжает работать. Окно свёрнуто в трей.',
    shutdown_block='Идёт корректная остановка PAPER-бота',
    log_big='Журнал движка больше 1 ГБ: {name}.',
)
