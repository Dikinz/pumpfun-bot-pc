v0.7.103.0 — LP pool recorder for the virtual maker test (no trading): selects 12 Orca/Raydium CLMM/Meteora DLMM pools once, records their tape, raw pool snapshots, DLMM bin arrays and a feed-completeness audit for 14 days; AMM observer defaults to counts (05.11.2026): RELEASE_NOTES_v1030.md.
Previous v0.7.102.0 — AMM observer without trading: records pump.fun migrations to AMM pools, their trades for 30 minutes, pool reserves and Jupiter quotes in its own database; 05_RESULTS no longer fails on reports above 16 MB (05.11.2026): RELEASE_NOTES_v1020.md.
Previous v0.7.101.11 — fixes the v0.7.101.10 recovery check (dashboard overview payload over its size budget: a not-started monitor shipped its full file-path diagnostic); trading logic unchanged (05.11.2026): RELEASE_NOTES_v10111.md.
Previous v0.7.101.10 — a drained curve closes at the value a real position would get (instead of a zero write-off); stage 1 "AMM exit (or not needed)" may be proven by the PRIMARY monitor (05.11.2026): RELEASE_NOTES_v10110.md.
Previous v0.7.101.9 — stage 1 checkpoint "AMM exit (or not needed)", clean stop log (04.11.2026): RELEASE_NOTES_v1019.md.
Previous v0.7.101.8 — PRIMARY writes off a SELL on a drained curve by the ROUTE rule; buyers-at-entry check in observation mode (04.11.2026): RELEASE_NOTES_v1018.md.
Previous v0.7.101.7 — ROUTE writes off positions on drained curves (zero receipt) instead of holding both slots forever (03.11.2026): RELEASE_NOTES_v1017.md. Add the whole bot folder to antivirus exclusions.
Устойчивость SQLite в длинных прогонах и стоп-флаги наблюдения: RELEASE_NOTES_v1014.md (ранее RELEASE_NOTES_v1013.md). Критерии дорожной карты сохранены.

v0.7.101.5 — diagnostics written off the event loop, atomic branch start file, desktop app 16_DESKTOP (02.11.2026)
Read RELEASE_NOTES_v101.md / README_RU.txt.
Fresh folder: install, settings, restore the complete stopped campaign if resuming, finish 04_CHECK.
14_RPC_STATUS performs exact audited recovery and one guarded read-only genesis RPC.
03_START: normal PAPER. 15_PAPER_FAULT_TEST: 7200 seconds, synthetic priority read denial at 3600 for 10 seconds, normal STOP at the end.
05_RESULTS after normal STOP. Existing open positions and capital remain open until a valid sale.
No live execution, new Windows acceptance, or actual 2-hour provider run is asserted by offline tests.
