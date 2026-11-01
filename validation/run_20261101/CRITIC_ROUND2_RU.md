# Независимый критик, раунд 2 (critic_r2), 01.11.2026

Кандидат: рабочее дерево ветки `claude/testing-strategy-agents-116a19`. Это HEAD db37741 плюс незакоммиченные правки fix_r1 и fix_r2, 35 файлов. Сборка `v0.7.101.2`, build_revision `RUN_REVIEW_V1012`.

Данные пользователя (`Desktop/.../results`) я только читал, через `python -I` и скрипты из `scratchpad/scripts_cr2`. В каталоге results ничего не изменено.

Для проверки сделаны две изолированные копии:
- `scratchpad/runs/cr2`: `git archive HEAD` плюс 35 изменённых или новых файлов. Без `app/data`, `__pycache__` и `validation`.
- `scratchpad/runs/cr2h`: чистый HEAD, для контроля регрессий.

Ключи, кошелёк, mainnet/провайдерский RPC, рыночный WebSocket и порт 8188 не использовались.

## Вердикт: PASS_FOR_WINDOWS_PAPER_TEST

Подтверждённых P0, P1 или P2, которые обесценили бы PAPER-тест, больше нет. Оба замечания раунда 1, которые мешали выпуску, закрыты:
- CR1-01: сборка получила новую метку. Старый PASS пользователя для v0.7.101.1 больше не засчитывается.
- CR1-03: украинский текст на /details убран, и в HTML, и в Python-диагностике.

Корректно закрыты также CR1-04 (утечка lru_cache между тестами) и CR1-05 (метка `RPC_DEFERRED` и покрытие лаборатории сигналов выхода).

RUN-01 (P1, зависание основного цикла ~53 с в SQLite COMMIT) по первопричине остаётся открытым. Классификация верная: это цель следующего PAPER-теста, а не скрытый дефект. В сборку добавлены сбор доказательств при зависании и прямая остановка веток. Учёт при такой остановке не теряется: accepted=committed, dropped=0, целостность OK.

### Обязательные условия коммита и упаковки (не код)
1. **Добавлять файлы в индекс только явным списком** (CR2-01). В дереве нет `.gitignore`. Неотслеживаемые `app/data/clean_0762.db` (сырая БД, ~733 КБ) и `app/data/*.sqlite_diagnostics.jsonl`, `app/__pycache__/`, `validation/LIVE_MAP/state.json` в коммит попасть не должны. `git add -A` использовать нельзя.
2. После установки новой сборки пользователь должен заново пройти 04_CHECK. Старые `acceptance_v90.json` и `recovery_check_v89.json` относятся к v0.7.101.1 и намеренно не засчитываются. После PAPER нужно сохранить 05_RESULTS и 08_PRESERVE_DATA: в них появится `*_stall_evidence_v1012.json`.

## Что я проверил сам

| Проверка | Результат |
|---|---|
| Пороги, размер позиции, лимиты RPC, свежесть | Не изменены. Дифф `config.py` меняет только `VERSION`. `rpc_budget.py` получил только функции меток `lab_status()` и `deferral()`, admission и лимиты не тронуты. `preentry.py` и `auto_simulation.py` меняют только строку статуса при исключении. В `shadow_preflight.complete` `RPC_DEFERRED` попадает в ту же техническую ветку, что прежний `REQUEST_OR_LAB_ERROR_Deferred`, поэтому backoff и streak работают как раньше. Других потребителей этих меток в app нет (grep). `cost_evidence.quantile` используется только в отчёте `cost_evidence`. `campaign_view.monitor_status` используется только в отображении. Правки `page.html`, `terminal.html` и `details.html` касаются только отображения |
| Ослабление тестов | Не найдено. Изменённые версионные тесты меняют только строку версии. В `test_system_strategy_v51` закреплено `BACKUPS=2`: это тест механизма ротации с исходной границей, сам механизм проверяется так же. Фикстура `conftest.native_reader_bindings_isolation` только очищает `lru_cache` до и после теста, ассерты не тронуты |
| Регрессии на чистом HEAD (`cr2h` + `test_win314_run20261101.py`) | **30 failed, 1 passed.** Тесты ловят исходные дефекты. Проходит только `..._empty_install_monitor_not_started_after_fake_last_error_test`, ему нужна фикстура conftest |
| Новые и связанные тесты на кандидате (18 файлов: win314_run20261101, recovery_evidence_v861, sqlite_preserve_v66, system_strategy_v51, shutdown_audit_v68, coordinated_stop_v72, branch_shutdown_v541, shadow_preflight_v43, preentry, smoke, clean_release_v62, terminal_v1011, win314_tools, win314_release, runtime_delivery_v60, check_plan_v972, local_simulation, exit_comparison) | **238 passed** за 38 с |
| Последовательность CR1-04 из 11 файлов | **92 passed** (в раунде 1 падал `STATE_READ_ERROR`) |
| Полный pytest из `app/`, короткий basetemp `bt/k` | **1392 passed, 1 skipped, exit 0**, 429 с (`critic_r2_full_junit.xml`, `critic_r2_full_summary.txt`) |
| `recovery_selftest.py` целиком | **CONTROLLED PAPER RECOVERY: PASS, 605/605, 211,4 с из 600.** `acceptance_v90.json` = {v0.7.101.2, passed=true, native_windows_verified=true}. В `recovery_check_v89.json` build_revision `RUN_REVIEW_V1012`, child_returncode 0. Маркер `recovery_last_attempt_v89.json` = PASSED. Heartbeat печатался каждые 20 с (`critic_r2_recovery_stdout.log`) |
| `release_guard.verify` по копии | OK: v0.7.101.2, 382 файла, в том числе `app/stall_evidence_v1012.py` и новый тест |
| Сирот-процессов после прогонов | Нет (`tasklist`) |

## Выборочная сверка выводов аналитиков с данными пользователя

Все проверенные числа совпали.

- Первая попытка recovery: MANIFEST `complete=false`, `error_type=null`, в архиве только `progress.jsonl` (934 события). SESSION_START в 1793535919,38, последнее событие (setup `test_transport99_snapshot_slot_validation_is_unchanged`) в 1793535955,35, то есть через +35,97 с. Вывод о Ctrl+C, а не о зависании подтверждается: 600-секундный таймаут на 36-й секунде сработать не мог.
- Вторая попытка: `recovery_check_v89.json` v0.7.101.1, AGENT_REVIEW_V1011, 605 PASSED, 300,59 с, child_returncode 0. `acceptance_v90.json`: created_ts 1793535974,63, passed=true.
- Запуск:
  - `system_state` FAILED, elapsed 5464,94 с (≈91 мин, а не 2 ч);
  - `runtime_v60`: COMMIT clean_0762.db, busy_timeout 250 мс, age 53,18 с, started_ts 1793541881,5957;
  - стек: `hot_trade_tick → setmany → atomic → commit → logged`;
  - события: ALERT_OPEN PRIMARY_HEARTBEAT_INVALID (899,49), затем STALL_CONTROLLED_STOP, UNHANDLED_ERROR в launcher и LAUNCHER_STOP COMPONENT_OR_STARTUP_FAILURE;
  - shutdown: bot и dashboard forced, exit 1; monitor exit 0; writer_acks_complete=false.
- Капитал PRIMARY: cash 24,1336, closed_net −5,8664, издержки 1,0274 = 0,7645 (пул) + 0,1529 (маршрут) + 0,11 (фикс.). ROUTE: cash 20,7295, то есть −9,2705. Закрыто 11 и 11, побед 3 и 2.
- AMM: local_exits 0, tick_fallback 11. Дорожная карта: 21,5%, этап 1 = 80% (не пройден только пункт AMM-выхода), этап 2 = 6%.
- В `summary.json` 124 вхождения `REQUEST_OR_LAB_ERROR_Deferred` (основа CR1-05). В `spawn_diagnostics_v89.json` 5 медленных spawn (≥1000 мс).
- В preservation есть только `system_events.jsonl` и `.jsonl.1`. Это подтверждает RUN-03 и RUN-04 (ротация, потерянное начало журнала).
- Дополнительно, ни в одном анализе этого нет: за 36 с до сигнала о зависании было ALERT_OPEN/CLEARED `BOT_SAMPLE_UNAVAILABLE` (863,7–868,8). Это ошибка psutil при обходе дерева бота, типично для короткоживущего дочернего процесса. Как причину зависания не трактую, но в следующем прогоне стоит сопоставить её с `stall_evidence`.

## Правильность классификации неисправленного
- **RUN-01 (P1)**: первопричина открыта. Менять checkpoint/WAL без данных не стали, и это обоснованно. Добавлена диагностика:
  - `latest_stall_v1012` со стеками всех потоков и размерами DB/WAL/SHM;
  - файл `*_stall_evidence_v1012.json`, который launcher пишет до остановки: CPU и I/O процессов и потоков, файлы, runtime-проба;
  - экспорт этого файла в 05_RESULTS и 08_PRESERVE.

  Я сверил по коду, что прямой STOP веткам работает: `paper_branch` (общий движок и для ROUTE) опрашивает тот же stop-файл через `Probe(control=(target,session))`. Финализация ветки пишет только в собственную БД. Ожидание ack ограничено 15 с и относится только к остановке. В данных пользователя stop-файл ROUTE появился лишь около 1793541934,6, за ~1,5 с до kill. Теперь он пишется до CTRL_BREAK.
- **RUN-05 и ACC-7** (нет AMM-выходов, выборка 11/100): это результат стратегии и рынка. Классифицировано верно.
- **ACC-3** (`min_tokens_out` без учёта комиссии, эффективный допуск ~3,8–4%): это фактически порог, менять его может только владелец стратегии. Оставлено без изменений, и это правильно.
- **ACC-2, ACC-4, ACC-5, ACC-6, RUN-06, RUN-07, RUN-09**: среда, протокол или поведение по задумке. Классификация подтверждается данными.
- **TERM-7** (дорожная карта зависит от `results/acceptance_v90.json` рядом с кодом): особенность среды проверки.
- **VR2-ENV-01** (3 нативных теста падают при длинном basetemp): это окружение. С коротким basetemp у меня проходит весь набор, 1392 теста, exit 0.

## Замечания раунда 2

| ID | Серьёзность | Вид | Суть | Блокирует? |
|---|---|---|---|---|
| CR2-01 | P2 | процесс (гигиена коммита) | Перенос CR1-02. `.gitignore` нет. Неотслеживаемая сырая БД `app/data/clean_0762.db` с diagnostics.jsonl, `__pycache__`, `LIVE_MAP/state.json` | Коммит — да: добавлять явным списком (исходники, тесты, BUILD_MANIFEST, README*, ROADMAP, `validation/run_20261101/*`, LIVE_MAP/index.html и stages). Сборку — нет |
| CR2-02 | P3 | product_defect (не подтверждено) | CR1-06 не исправлен и не указан как отклонённый в fix_r2. Маркер INTERRUPTED и строка `Reason:` пишутся после выхода из `TemporaryDirectory`. Ошибка очистки на Windows может подменить KeyboardInterrupt. ZIP при этом сохраняется | Нет. Рекомендация: `TemporaryDirectory(ignore_cleanup_errors=True)` или запись маркера внутри `with` |
| CR2-03 | P3 | doc | Нет `RELEASE_NOTES` для v0.7.101.2. В README_RU изменена одна строка. Пользователь не узнает из сборки, что нужен новый 04_CHECK, что консоль recovery теперь печатает прогресс и что после PAPER нужно сохранить 05_RESULTS и 08_PRESERVE_DATA со stall_evidence | Нет. Рекомендуется короткая русская заметка (файл в корне, manifest пересчитывать не нужно) |
| CR2-04 | P3 | strategy_observation | RUN-01 (P1) открыт по первопричине. Принят как цель следующего PAPER-теста | Нет |
| CR2-05 | P3 | strategy_observation | В логах `validation/run_20261101/*.xml/.log` встречаются временные пути с именем пользователя Windows. Мин, кошельков и сырых данных нет (проверено grep) | Нет |

## Итог для пользователя
Сборку v0.7.101.2 можно коммитить с явным списком файлов и передавать на Windows PAPER-тест. Порядок:
1. Новая папка.
2. 01_INSTALL, затем 02_SETTINGS.
3. **04_CHECK до конца, без Ctrl+C.** Консоль печатает прогресс каждые 20 с.
4. 03_START.
5. После остановки 05_RESULTS и 08_PRESERVE_DATA.

При повторном зависании SQLite сборка остановится управляемо, сначала остановит ветки и сохранит доказательства для поиска первопричины.
