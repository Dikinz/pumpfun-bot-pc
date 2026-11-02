Устойчивость SQLite в длинных прогонах и стоп-флаги наблюдения: RELEASE_NOTES_v1014.md (ранее RELEASE_NOTES_v1013.md). Критерии дорожной карты сохранены.

v0.7.101.4 — background WAL checkpoints, journal retention, observation-only risk flags (02.11.2026)
Read RELEASE_NOTES_v101.md / README_RU.txt.
Fresh folder: install, settings, restore the complete stopped campaign if resuming, finish 04_CHECK.
14_RPC_STATUS performs exact audited recovery and one guarded read-only genesis RPC.
03_START: normal PAPER. 15_PAPER_FAULT_TEST: 7200 seconds, synthetic priority read denial at 3600 for 10 seconds, normal STOP at the end.
05_RESULTS after normal STOP. Existing open positions and capital remain open until a valid sale.
No live execution, new Windows acceptance, or actual 2-hour provider run is asserted by offline tests.
