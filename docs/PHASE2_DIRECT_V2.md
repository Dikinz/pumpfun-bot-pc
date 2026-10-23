# Phase 2: direct Pump v2 lab

Модуль direct_pump_v2.py создаёт VersionedTransaction для buy_v2/sell_v2 напрямую, без Jupiter swap route. Существующий v3 lab сохранён. Официальные v2 имеют 27/26 аккаунтов; v3 — 17. Оба интерфейса поддерживаются протоколом. Эта реализация принимает только обычные SOL-пары, которые проходят текущий строгий decoder; mayhem/cashback/другие специальные кривые, не-SOL и завершённые кривые отклоняются. Это не отправка на PumpSwap AMM.

Аккаунты берутся из pinned IDL research/pump_idl_v26.json. Проверяются owner/discriminator глобальной конфигурации, состояние кривой и mint. Выводятся PDA/ATA для global, base_mint, WSOL quote_mint, bonding_curve, associated_base/quote_bonding_curve, user, associated_base/quote_user, fee_recipient, buyback_fee_recipient и их quote ATA, creator_vault и его quote ATA, sharing_config, global_volume_accumulator (BUY), user_volume_accumulator и его quote ATA, fee_config, fee_program, system_program, event_authority и program. Token-2022 и SPL Token не смешиваются.

user_volume_accumulator обязателен. Для отсутствующего/недофинансированного аккаунта prepare вызывает getMinimumBalanceForRentExemption(137); значение не зашито как 0.00184 SOL. В документации текущий ориентир 0.0018444 SOL для 137 байт. Аккаунт создаёт инструкция Pump init_if_needed. Бюджет должен покрывать также ATA/creator/pool topups и комиссию; подписанный preflight проверяет общий дебет. Поле volume_topup — только часть бюджета, не полная цена сделки.

## Использование

В app скопируйте research/phase2_plan.example.json во внешний файл вне папки результатов. Заполните PUBLIC wallet/mint, точный ожидаемый genesis hash, собственный HTTPS RPC, amount/limit в целых raw единицах, fee_budget, дневные лимиты и пути к зашифрованному vault и signer policy. Шаблон не является рекомендованным размером сделки. Ключи подписи не вводятся в .env. Политика signer должна явно разрешать mint, максимальную стоимость buy / количество sell и комиссию. Не увеличивайте лимиты ради обхода отказа.

Из app, используя .venv\Scripts\python.exe из папки бота:

    python direct_pump_v2.py prepare --plan PATH_TO_EXTERNAL_PLAN

Только чтение RPC, без подписи и отправки. amount для buy_v2 — base tokens, limit — max quote cost; для sell_v2 amount — продаваемые raw tokens, limit — min quote output. partial_fill выключен.

До первой намеренной отправки и отдельно каждый UTC-день оператор задаёт daily_start_equity_lamports и выполняет:

    python direct_pump_v2.py begin-day --plan PATH_TO_EXTERNAL_PLAN

Повтор команды не сбрасывает существующий дневной риск. Это действие локального журнала, не перевод средств. Успешные покупки пополняют сверенный token inventory. Продажа внешних, неучтённых токенов блокируется до отдельной сверки; автоматического импорта чужих остатков нет.

Явная отправка (не выполнялась при сборке):

    python direct_pump_v2.py send --plan PATH_TO_EXTERNAL_PLAN --broadcast

Пароль запрашивается интерактивно, подпись выполняется изолированным signer subprocess. Перед отправкой сверяются SOL/token balances, лимиты и signature-valid preflight. sendTransaction использует maxRetries=0. skip_preflight берётся из плана (по умолчанию false, можно явно true); собственная симуляция с sigVerify=true сохраняется. Jito-адаптер прежней версии сохранён; новый CLI использует прямой RPC. После отправки выполняется ограниченное ожидание FINALIZED и сверка signature/slot/SOL/token deltas. Полученная signature не считается исполненной покупкой/продажей. Неизвестный результат/истёкший blockhash остаётся неразрешённым, без автоматического повторного перевода.

    python direct_pump_v2.py track --plan PATH_TO_EXTERNAL_PLAN --intent INTENT_ID

track продолжает проверку исходной signature без новой отправки. CLI send не пересобирает неоднозначную транзакцию; прежний Engine.execute_with_rebuild допускает ограниченную пересборку только однозначно отклонённых preflight ошибок. Порог slippage не расширяется автоматически. Таймаут ожидания не доказывает неисполнение.

## Источники, проверены 2026-10-22

https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/BUY.md
https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/SELL.md
https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/TRADE_V3.md
https://solana.com/uk/docs/rpc/http/sendtransaction

Тесты не подтверждают landing/стоимость/скорость на mainnet. Основная стратегия остаётся PAPER; отдельная ветка также PAPER.
