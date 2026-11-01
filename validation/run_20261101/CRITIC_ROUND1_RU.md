# Независимый критик, раунд 1 (critic_r1), 01.11.2026

Кандидат: рабочее дерево ветки `claude/testing-strategy-agents-116a19` (HEAD db37741 плюс незакоммиченные правки fix_r1; VERSION `v0.7.101.1`, build_revision `AGENT_REVIEW_V1011`). Данные пользователя только читались, сам каталог results не изменялся. Для проверки я сделал свою изолированную копию `scratchpad/runs/cr1` (git archive HEAD плюс 18 изменённых или новых файлов, без `app/data`, `__pycache__` и `validation`) и копию HEAD `scratchpad/runs/cr1h`. Копию пользовательской БД разворачивал в новый каталог `scratchpad/user_results/cr1_db`.

## Вердикт: CHANGES_REQUIRED (узкий)

Код исправлений корректен. Регрессии настоящие, пороги не изменены, тесты не ослаблены. Мешает выпуску одно: **сборку нельзя отличить от той, что пользователь уже проверил** (CR1-01, P2). У пользователя в `results/` лежат `acceptance_v90.json` и `recovery_check_v89.json` с `version=v0.7.101.1, passed=true` от прежнего кода. Подтверждение привязано только к строке VERSION, поэтому новая сборка с той же VERSION унаследует этот PASS:
- на дашборде («Нативные проверки Windows» на этапе 1);
- в `release_checks_v94.passed`.

Это произойдёт, если новая сборка распакована поверх старой с сохранением `results/` и 04_CHECK не пройден до конца, например снова прерван Ctrl+C, как в первой попытке пользователя. Маркер AN_REC_3 лишь предупреждает в `export_release_checks`, а `roadmap_progress.acceptance()` его не читает. Пользователь просил «подготовить новую сборку», поэтому новая метка сборки обязательна. Это уже второй раунд, где замечание CR1-03 из `validation/win314` остаётся без исправления.

Что нужно сделать перед коммитом и упаковкой:
1. Повысить VERSION (например, `v0.7.101.2`) в `config.py`, `release_guard.py`, `network_check_v88.py`, `BUILD_MANIFEST.json`, в заголовках HTML, тестах и документах, где версия закреплена. Сменить `build_revision` (в manifest и в `recovery_selftest.py`). Альтернатива — проверять в `roadmap_progress.acceptance()` и `export_release_checks` ещё и `build_revision`. Затем пересчитать manifest и повторить связанные тесты и recovery. Проверено, что VERSION не участвует в идентичности кампании: `clean_campaign` только записывает её, так что кампания и счёт 11/100 не сбросятся.
2. Не коммитить сырые данные. В дереве лежат неотслеживаемые `app/data/clean_0762.db` (745 472 Б) и `.sqlite_diagnostics.jsonl`, `app/__pycache__/`, `validation/LIVE_MAP/state.json`, а `.gitignore` в репозитории нет. Нужно добавлять файлы в индекс явным списком или завести `.gitignore` для `app/data/` и `__pycache__/`.

RUN-01 (P1, зависание COMMIT на 53 с) остаётся открытым по первопричине. Я принимаю это как цель следующего PAPER-теста, а не как блокер, и вот почему:
- без новых данных исправление было бы догадкой;
- остановка остаётся fail-closed, учёт не теряется (accepted=committed, dropped=0, integrity OK);
- сборка теперь собирает именно недостающие данные: стеки всех потоков, размеры БД/WAL/SHM, CPU и I/O по потокам;
- ветки при зависании основного процесса получают прямой STOP.

## Что проверено лично

### Diff и пороги
- Изменено 17 файлов, добавлено 2: `stall_evidence_v1012.py` и `test_win314_run20261101.py`. Файлы `config.py`, `preentry.py`, правила выхода, `rpc_budget` и свежести не тронуты. `SUITE_TIMEOUT_S=600` и порог heartbeat 15/16 с прежние. `BRANCH_ACK_WAIT_S=15` — это ожидание при остановке, на торговлю и RPC оно не влияет.
- `test_system_strategy_v51::test_bounded_journal_and_partial_line`: добавлена только фиксация `BACKUPS=2`, утверждение «ровно 3 файла» не изменилось. Это не ослабление.
- `quantile` используется только в диагностическом `cost_evidence_results.json` (grep), в гейты и PNL не попадает.
- `campaign_view`: правило NO_HEARTBEAT (>=16 с) повторяет `monitor_service.service_status`. Других потребителей `live_v87.monitor_status`, кроме отображения, нет.
- RUN-02 проверен по коду реального воркера, а не только по тесту с имитацией. Ветка запускается через `paper_branch.run`, её `runtime_control.Probe(control=(target,session))` каждые 0,5 с опрашивает `stop_requested(target,session)`. Здесь `target=engine.seed(primary)=engine.path(primary)`, и это тот же путь, что `request_branch_stops` → `request_stop(engine.path(primary),session)`. Значит, прямой STOP-файл действительно доходит до ROUTE, когда основной процесс завис. В данных пользователя STOP-файл ROUTE существовал, но был записан только в ветке таймаута, примерно за 0,4 с до kill.

### Тесты (Windows 10, Python 3.14, своя копия)
| Прогон | Результат |
|---|---|
| `test_win314_run20261101.py` на копии HEAD | 21 из 21 FAILED: дефекты воспроизводятся |
| 18 файлов: регрессии и связанные (recovery_evidence, system_strategy, sqlite_preserve, terminal, check_plan, review_v100, runtime_delivery, coordinated_stop, branch_shutdown, shutdown_audit, io_dashboard, overview, win314_release, clean_release, stop_consistency, watchdog_lifecycle, smoke) | **222 passed**, 35,6 с |
| Выборка из 18 файлов по путям лаунчера, нативного spawn и recovery (late_spawn_ownership_v85, cleanup_owner_v93, spawn_boundaries_v82 и т. д.), короткий basetemp | 155 passed, 1 failed: `test_win314_tools::test_win314_empty_installation_dashboard_has_no_epoch_age_or_monitor_alarm` |
| Тот же падающий тест отдельно | passed. В той же последовательности файлов на **неизменённом HEAD** падает так же (`STATE_READ_ERROR` вместо `NOT_STARTED`). Это существующая зависимость от порядка тестов, не регрессия (CR1-04) |
| `release_guard.verify` на копии | OK (v0.7.101.1, AGENT_REVIEW_V1011) |

Полный набор и controlled recovery я не повторял: verify_r1 уже выполнил их, получив 1379 passed с 3 тестами окружения при длинном basetemp и recovery 605 из 605 за 215 с, артефакты лежат в `verify_r1_*`. Классификацию трёх нативных тестов как окружения я подтвердил косвенно: в моей выборке `late_spawn_ownership_v85`, `cleanup_owner_v93` и `spawn_boundaries_v82` при коротком basetemp прошли.

### Дашборд на копии пользовательской БД (порт 8197, сервер остановлен)
- `/`: баннер «Отчёт заморожен: данные на момент 16:04:41 (последний heartbeat)…»; монитор ROUTE показан как «нет heartbeat (последний статус RUNNING)», PRIMARY — «остановлен»; CPU / RAM `1.8% / 265.4 MB`. Нет undefined, NaN или `[object`, нет ошибок консоли.
- `/terminal`: заголовок «данные на 16:04:41 (заморожено) · снимок …»; смещение `7.8 ± 27.5 мс`; капитал PRIMARY 24.1336. Ошибок нет.
- `/details`: «Все процессы: RAM 265.4 МБ · CPU 1.8% · Задержка цикла 4.6 мс»; возраст рынка подписан «на момент последнего heartbeat». Но **украинский текст остался**: «Частина часу позицій…», «Немає свіжого heartbeat…», «Мала вибірка: 11 закритих угод». Он приходит из Python (`strategy_diagnostics.py`, `system_health.py`, `time_check.py`, `campaign_view.py` и др.), а тест TERM-5 проверяет только `details.html` (CR1-03).

### Выборочная сверка выводов аналитиков с данными пользователя (`python -I`, только чтение)
| Утверждение | Факт в данных |
|---|---|
| Первая попытка recovery: 934 события, +35,97 с, нет FAILED, только progress.jsonl и MANIFEST, error_type=null | 934, 35,968 с, failed=0, `['progress.jsonl','MANIFEST.json']`, error_type/error=None, last phase — setup `test_transport99_snapshot_slot_validation_is_unchanged` |
| Вторая попытка: created_ts 14:26:14, 605 тестов, 300,59 с, rc 0 | 14:26:14.628, 605, 300.59, 0, `acceptance.native_windows_verified=true` |
| COMMIT завис на 53,18 с, стек hot_trade_tick | `sql_v91.operation=COMMIT, busy_timeout_ms=250, age_s=53.18`; кадры logged→commit→atomic→setmany→hot_trade_tick→process_trade; started 16:04:41.6, тревога 16:04:59.49 |
| PRIMARY net −5,8664, cash 24,1336, расходы 1,0274 = 0,7645+0,1529+0,11 | совпадает до 4 знаков |
| WAL ≈1054 кадра | (4 342 512−32)/4120 = 1054,0 |
| В preservation нет `.jsonl.2`, а в 05_RESULTS он есть | preservation: `.jsonl` и `.jsonl.1`; `system_flight_recorder.zip`: `.jsonl.2` = 2 099 358 Б |
| Остановка: bot и dashboard forced, `writer_acks_complete=false` | совпадает |
| p90 занижен (ACC-1) | BUY 250 мс, n=4: mean 2,93, p90 −0,85; SELL 1000 мс, n=3: mean 6,24, p90 4,74 |
| Прогресс дорожной карты 21,5 %, этап 2 — 6 % | совпадает |
| ACC-3: `min_tokens_out` без учёта комиссии | `preentry.py:107`: `budget/1e9/signal_price*10^dec*buy_min_fraction`, комиссия не вычитается. Порог трогать нельзя, решение остаётся за владельцем стратегии, это правильно |

Расхождений с выводами аналитиков не нашёл.

### Классификация неисправленного
- RUN-05 (нет AMM-выходов), ACC-2, ACC-4…ACC-7, RUN-06, RUN-07, RUN-09, TERM-7 — стратегия или окружение. Это обосновано данными.
- RUN-01, часть 2 (`wal_autocheckpoint`), отклонена обоснованно: размер WAL согласуется с checkpoint внутри COMMIT. Однако PASSIVE-автоcheckpoint не ждёт читателей, а при 0 % CPU 53 с указывают на блокировку I/O на уровне ОС, фильтр-драйвера или антивируса. Без данных менять поведение записи для нескольких писателей нельзя.
- **TERM-6 выпал из журнала исправлений**: его нет ни среди исправленных, ни среди отклонённых. Я разобрал его: статус `REQUEST_OR_LAB_ERROR_Deferred` (124 упоминания в summary.json) означает исключение `rpc_budget.Deferred(TimeoutError)`, то есть локальную отсрочку RPC-регулятором. Это не ошибка лаборатории. `auto_simulation.py:106` записывает его под общей меткой ошибки. Получается, что лаборатория сравнения выходов по сигналу в этом прогоне не имела покрытия (11 FIRST и 11 REPEAT отложены), и это по правилам бюджета RPC (как ACC-6). Дефект диагностики P3: нужна метка `RPC_DEFERRED`, а не «ошибка» (CR1-05).

## Замечания критика

| ID | Серьёзность | Вид | Суть | Требуется |
|---|---|---|---|---|
| CR1-01 | P2 | product_defect (релиз) | VERSION и build_revision не изменились. Подтверждение привязано только к VERSION, поэтому прежний PASS v0.7.101.1, который уже лежит у пользователя, засчитается новой сборке в дорожной карте и `release_checks_v94`, если 04_CHECK прерван или пропущен | **да**: повысить VERSION и build_revision или привязать подтверждение к build_revision |
| CR1-02 | P2 | strategy_observation (гигиена коммита) | Неотслеживаемые `app/data/clean_0762.db`, `__pycache__`, `LIVE_MAP/state.json`; `.gitignore` нет | **да**: добавлять в индекс явным списком или завести `.gitignore` |
| CR1-03 | P3 | doc | TERM-5 закрыт частично: украинский текст из Python-диагностики виден на /details | нет, можно в следующем раунде |
| CR1-04 | P3 | test_defect (существующий) | `test_win314_tools::test_win314_empty_installation…` зависит от порядка (STATE_READ_ERROR), так же падает на HEAD | нет |
| CR1-05 | P3 | product_defect (диагностика) | TERM-6: `rpc_budget.Deferred` помечается как `REQUEST_OR_LAB_ERROR_Deferred`; лаборатория выходов по сигналу без покрытия | нет; классифицировать и сменить метку |
| CR1-06 | P3 | product_defect (не подтверждено) | `write_last_attempt('INTERRUPTED')` и печать причины выполняются после выхода из `TemporaryDirectory`. Если очистка на Windows упадёт на занятом файле, маркер и строка `Reason:` не появятся. ZIP при этом уже сохранён. В сквозной проверке fix_r1 маркер записался | нет; лучше записывать маркер внутри `with` |
| CR1-07 | P3 | strategy_observation | RUN-01 открыт по первопричине. Сборка собирает данные, следующий PAPER-тест должен их сохранить (05_RESULTS и 08_PRESERVE_DATA) | нет, это цель теста |

## Границы проверки
- Нативные ситуации Windows (реальное зависание диска, антивирус, настоящий STOP ветки при зависшем основном процессе) локально не воспроизводились. RUN-02 проверен тестами с имитацией воркера и чтением кода реального пути опроса STOP-файла.
- Полный набор и recovery в этом раунде не повторялись, использованы результаты verify_r1. Нет внешних RPC, кошелька, mainnet и WebSocket, порт 8188 не использовался.
- Сырые данные пользователя в репозиторий не добавлялись. В этом отчёте только агрегаты.
