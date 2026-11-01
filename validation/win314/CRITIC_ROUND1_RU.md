# Независимый критик, раунд 1 — v0.7.101.1, Windows 10 + Python 3.14.7

Дата: 2026-11-01. Окружение: Windows 10 Pro 19045, Python 3.14.7 (venv из scratchpad), кодовая страница cp1251, `core.autocrlf=true`.
Кандидат: рабочее дерево worktree `testing-strategy-agents-116a19` (изменения **не закоммичены**, ZIP не пересобран).
Изолированная копия критика: `scratchpad/runs/critic_r1`. Это 460 файлов рабочего дерева в виде git-checkout с CRLF, без `validation/LIVE_MAP` и `.claude`. Сеть, ключи, кошелёк и пользовательские БД не использовались. NTP-проверка из 04_CHECK намеренно не запускалась.

## Вердикт: PASS_FOR_WINDOWS_PAPER_TEST (с ограничениями ниже)

Подтверждённых блокирующих дефектов продукта (P0/P1, а также P2, обесценивающих PAPER-тест) не осталось. Все исправления раунда я проверил сам, повторив тесты и воспроизведения. Торговые пороги, размер позиции, лимиты RPC и правила свежести не менялись. Тесты не пропускаются и не ослаблены.
Ниже перечислены оставшиеся замечания уровня P2/P3. Это ненадёжность отдельных тестов под нагрузкой и вопросы окружения. Ни одно из них не является ошибкой торговой или учётной логики.

## 1. Что изменено (git diff, проверено построчно)

| Область | Изменение | Оценка критика |
|---|---|---|
| `app/release_guard.py` | Если сырой хеш текстового файла не совпал, файл хешируется повторно с заменой CRLF на LF | Корректно. Собственная атакующая проверка (`critic_r1_guard_probe.py`) в отдельной копии: CRLF-checkout и LF принимаются; смесь CRLF/LF тоже принимается (смысл файла тот же). **Блокируются:** лишний CR (`\r\r\n`), одиночный CR вместо CRLF, хвостовой CR, добавленная строка, смена порта 8188→8189 при сохранённых CRLF, CRLF-искажённый `.zip` и лишний модуль `app/extra_mod.py`. Двоичные файлы (.zip, .zlib) сравниваются только по сырым байтам. Перенос CR через границу блока в 1 МиБ обработан верно. |
| `BUILD_MANIFEST.json` | Пересчитаны 13 хешей, добавлены 3 новых тестовых файла | Независимый аудит: 378 файлов; 32 совпадают по сырым байтам, 346 после LF-нормализации, несовпадений 0. `bootstrap verify-release` на CRLF-копии даёт exit 0. |
| `app/rpc_budget.py` (RPC-W1) | Незаписанный маркер отказа хранится в памяти; `blocked()` повторяет запись под RLock и снимает флаг только после fsync | Корректно, fail-closed. `recover()` отказывает, пока маркер не записан. |
| `app/rpc_budget.py` (RPC-W2) | Из `_recover_tmp` убран порог возраста `.tmp` в 10 с (`STALE_TMP_SECONDS`) | Проверил по коду. Издатель держит слот `publisher_v97_N.lock` с момента до `open('x')` и до конца `os.replace`. Если .tmp записан частично, разбор JSON падает и возвращается MetadataBusy (повтор). Если слот занят, тоже MetadataBusy. Если `.tmp` исчез, используется `.json` или квитанция. Неизвестный или чужой `.tmp` не удаляется (`test_transient97_unknown_aged_tmp_never_discarded` проходит). Константа нигде больше не используется. Это не лимит RPC и не правило свежести рыночных данных. |
| `app/perf_check_v87.py` | `read_text(encoding='utf-8')` | Проверено: `bootstrap perf-check` на cp1251 **без** PYTHONUTF8 даёт `OFFLINE PERFORMANCE: PASS`, exit 0. |
| 06/09/10/11 `.bat` | Добавлены проверка `.venv` и `exit /b %CHECK_CODE%` | Проверено: `cmd /c 06_AMM_CHECK.bat < nul` возвращает exit=1 со статусом INPUT_UNAVAILABLE, без traceback и без RPC. |
| `app/target_amm_check.py` | Обработка EOFError/ValueError, отчёт с passed=false | Проверено (`bootstrap amm-check < /dev/null` возвращает rc=1). |
| `app/launcher.py` | Фиксированные подсказки RESULTS_GUIDANCE попадают в вывод и REPORT_ERROR.json; HTML пишется с `newline=''` | Корректно. Три строки в RESULTS_GUIDANCE совпадают с `raise` на строках 74, 78 и 82. Произвольный текст исключений не экспортируется (покрыто тестом). |
| `app/campaign_view.py` | При `trade_last_market_ts='0'` возраст рыночных данных = None; до первого старта MONITOR_STATE_MISSING показывается как NOT_STARTED | Корректно. После старта отсутствие файла по-прежнему STATE_READ_ERROR; сырая диагностика сохраняется. |
| `app/check_plan_v972.py` | 3 файла `test_win314_*` добавлены в технический набор 04_CHECK | Корректно |
| `tests/test_rpc_outcomes_v892.py` | Мгновенная проверка `not _pending()` заменена: каждый оставшийся файл должен иметь квитанцию, плюс ограниченное ожидание удаления (10 с) | Не ослабление. Проверка строже по смыслу и отражает асинхронную очистку. |
| `tests/test_priority_v101.py` | Тест переведён на управляемые часы RateClock | Не ослабление: утверждения те же (slot/rate/429 bypass), окно 1/6 с не может «истечь» из-за fsync. |
| `README.md` | Переписан под v0.7.101.1 | Порт 8188 совпадает с config.PORT. Утверждения о границах корректны. |

`app/config.py`: изменений нет (diff пустой). Значения rate=6, slots=4, wait=0.75, RECOVER_AFTER_SECONDS=2, CLEANUP_LIMIT и RECORD_WAIT_SECONDS не менялись.

## 2. Собственные прогоны критика (JUnit/JSON и коды возврата)

| Проверка | Результат | Артефакт (`validation/win314/`) |
|---|---|---|
| Полный pytest `app/`, PYTHONUTF8=1, короткий `--basetemp` | exit 1: 1354 теста, 1348 passed, 5 failed, 1 skip, 372.8 с | `critic_r1_full_utf8.xml`, `critic_r1_full_utf8_stdout.log` |
| Разбор 5 падений: 4 теста кошелька/подписи (`cryptography _cffi_backend`: «имя файла слишком длинное») | Окружение (MAX_PATH): `.venv` подключён junction-ом внутри глубокого scratchpad-пути. Тем же интерпретатором через junction падает и простой `import cryptography...aead`; прямым путём venv импорт работает | `critic_r1_rerun_junction.xml` (4 failed) |
| Повтор этих файлов прямым путём venv (test_execution_safety_v52, test_wallet_rpc_phase1, test_late_spawn_ownership_v85) | exit 0: 41 passed, 1 skip | `critic_r1_rerun_direct.xml` |
| 5-е падение: `test_late_spawn_ownership_v85::test_native_late_owner_survives_stop_and_reaps_tree` (`DELAYED_CLEANUP_CONFIRMED` вместо `LATE_PROCESS_CLEANED`) | Отдельно 12 прогонов файла: 96/96 PASS. Падает только внутри полного набора, см. CR1-01 | `critic_r1_full_utf8.xml` |
| Controlled recovery (эквивалент 09_RECOVERY_CHECK: `python -I app/bootstrap.py recovery-check`, **без** PYTHONUTF8) | exit 0, CONTROLLED PAPER RECOVERY: PASS, 605/605 PASSED, 225/600 с; `recovery_check_v89.passed=true`, child_returncode=0; `acceptance_v90.passed=true`, native_windows_verified=true | `critic_r1_recovery_check_v89.json`, `critic_r1_acceptance_v90.json`, `critic_r1_recovery_stdout.log` |
| `bootstrap perf-check` (cp1251, без PYTHONUTF8) | exit 0, PASS | `critic_r1_perf_stdout.log` |
| `bootstrap verify-release` на CRLF-копии | exit 0 | — |
| RPC-W2 с реальным Win32-хендлом (CreateFileW, share R\|W, 0.5 с) на `.tmp` | `recovered_after_s=2.26` (до исправления 10.26) | скрипт `scratchpad/critic_scripts/winhandle.py` |
| RPC-W1 с реальным хендлом на `control_failed.json` | После освобождения хендла другой процесс получает `RPC_CONTROL_BLOCKED` (до исправления допускал HTTP). Пока хендл удерживается, другой процесс **допускает** HTTP (остаточное окно, CR1-04) | `scratchpad/critic_scripts/winhandle_c2_after.py` |

## 3. Независимый разбор XML верификатора (verify_r1_a2)

Я разобрал все XML в `validation/win314` своим парсером:
- `verify_r1_a2_full.xml`: 1354 теста, 7 failed (6 из-за кодировки в тестах, 1 из-за symlink pytest-current), 1 skip.
- `verify_r1_a2_full_utf8.xml`: 1 failed (symlink).
- `verify_r1_a2_full_utf8_bt.xml`: 1354 теста, **0 failed**, 1 skip.
- `verify_r1_a2_related_utf8.xml`: 335/335.
- Recovery: 605/605.

Заявления верификатора подтверждаются. Во всех прогонах есть только один skip: `test_loose_permissions_and_symlink_rejected` (POSIX-права). Это ожидаемо на Windows. В новых тестах единственный `pytest.skip` стоит в `test_win314_release.py`: это BAT-проверка через cmd.exe на не-Windows. На Windows этот тест выполняется.

## 4. Замечания критика

| ID | Важность | Суть | Подтверждено |
|---|---|---|---|
| CR1-01 | P2 (надёжность гейта, не логика) | Нативный тест late-spawn из recovery-набора 04_CHECK нестабилен в длинных прогонах Windows: исход `DELAYED_CLEANUP_CONFIRMED` (источник `PROCESS_HANDLE_CLEANUP_PENDING_V93`) вместо `LATE_PROCESS_CLEANED`. Предшествующие утверждения безопасности проходят: state=CONFIRMED, original_result_ineligible=true. Наблюдался дважды: в моём полном прогоне при малой нагрузке и в 09-прогоне fix_r1. Отдельно 96/96 PASS. Пользовательский 04_CHECK может иногда упасть и потребовать повтора. Ослаблять тест нельзя. Нужен разбор, почему под нагрузкой путь идёт через отложенную очистку хендла, и решение о точном допустимом исходе. | да |
| CR1-02 | P3 | Из прошлых отчётов: тайминговые тесты гейта `test_rpc94_keeps_six_per_second_spacing` и `test_repeated_cold_start_coordination_four_processes` (RPC-W3, джиттер старта HTTP до ~12 мс после штампа) могут ронять 04_CHECK под нагрузкой. В моих двух recovery-прогонах они прошли. Шаг штампов, то есть средний лимит 6/с, соблюдается. | нет (у меня не воспроизведено) |
| CR1-03 | P3 | `BUILD_MANIFEST.json`: `build_revision` остался `AGENT_REVIEW_V1011`, VERSION тоже прежний. Содержимое кандидата отличается от исходного ZIP v0.7.101.1, но метки совпадают, и отчёты и дашборд их не различают. Рекомендация: новая метка сборки (например WIN314_R1) при пересборке ZIP. | да |
| CR1-04 | P3 | Остаток RPC-W1. Пока внешний хендл удерживает `control_failed.json`, другие процессы продолжают допуск. Если упавший процесс завершится, не вызвав больше `blocked()`, маркер в памяти теряется: это видно по коду, `close()` его не дописывает. До исправления было хуже, отказ остаётся fail-closed локально. | частично (окно удержания воспроизведено) |
| CR1-05 | P3 | 6 тестов (`test_smoke` ×5 и `test_monitor_v30`) читают UTF-8 HTML через `read_text()` без encoding и падают при cp1251 без PYTHONUTF8. Код продукта не затронут. 04_CHECK.bat задаёт PYTHONUTF8 для дочернего pytest. Исправление на стороне тестов (`encoding='utf-8'`) не ослабляет проверку. | да (по XML) |
| CR1-06 | P3, окружение | Если venv лежит по пути длиннее MAX_PATH, на Windows не загружаются DLL (`cryptography._cffi_backend`, ранее `psutil`). Путь пользователя короткий. На стороне продукта можно добавить предупреждение о длине пути в 01_INSTALL. | да |
| CR1-07 | P3, окружение | Устаревший symlink `%TEMP%\pytest-of-OMG\pytest-current` (09:13) без доступа ломает pytest с basetemp по умолчанию. Его нужно удалить вручную с правами владельца. | да |

## 5. Что НЕ доказано (границы этого PASS)

- Реальный провайдер RPC/WebSocket, 14_RPC_STATUS с одним mainnet RPC, 12_NETWORK_CHECK: не запускались, сеть запрещена.
- Полный 04_CHECK.bat целиком у пользователя: не запускался его NTP-шаг (time.cloudflare.com). Recovery, technical и perf проверены по частям.
- Двухчасовой физический PAPER (15_PAPER_FAULT_TEST) и обычный PAPER через 03_START на рыночных данных.
- Текущий AMM-выход (0 AMM-выходов в прошлой кампании), закрытие остатков ROUTE.
- Стратегия и прибыльность: этап 2 дорожной карты, минимум 100 новых закрытий. Реальное исполнение, подпись и live.
- Первопричины зависания SQLite, остановки CreateProcessW на 27.8 с (WIN-QA-02) и Windows-отказа удаления не установлены. Флаги `windows_kernel_createprocess_hang_fixed_v85`, `windows_cleanup_root_cause_fixed_v84`, `windows_suspend_resume_proven` и `live_rpc_proven` остаются false.
- Проверен кандидат в виде рабочего дерева с CRLF. Итоговый ZIP после новой распаковки (шаг 7 TESTING_WORKFLOW) не собран и не проверен. Изменения не закоммичены.

## 6. Рекомендации перед пользовательским запуском

1. Пересобрать ZIP с новой меткой сборки (CR1-03) и проверить его после новой распаковки.
2. У пользователя: удалить устаревший `pytest-current` (CR1-07), запустить 04_CHECK.bat на ненагруженной машине. Если recovery упадёт на CR1-01 или CR1-02, повторить и сохранить `results/recovery_failure_v89_*.zip`.
3. Не обязательно для PAPER: исправить encoding в 6 тестах (CR1-05) и разобрать исход late-spawn (CR1-01) без ослабления утверждений.
