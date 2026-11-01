Исправления по запуску 01.11.2026 и порядок теста: RELEASE_NOTES_v1012.md (ранее RELEASE_NOTES_v1011.md). Критерии дорожной карты сохранены.

v0.7.101.2 — RPC priority recovery + Windows run review fixes (01.11.2026)
Read RELEASE_NOTES_v101.md / README_RU.txt.
Fresh folder: install, settings, restore the complete stopped campaign if resuming, finish 04_CHECK.
14_RPC_STATUS performs exact audited recovery and one guarded read-only genesis RPC.
03_START: normal PAPER. 15_PAPER_FAULT_TEST: 7200 seconds, synthetic priority read denial at 3600 for 10 seconds, normal STOP at the end.
05_RESULTS after normal STOP. Existing open positions and capital remain open until a valid sale.
No live execution, new Windows acceptance, or actual 2-hour provider run is asserted by offline tests.
