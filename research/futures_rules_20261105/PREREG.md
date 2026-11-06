# Pre-registration: USDT-M perpetual futures rules test (05.11.2026)

Written before any data for this test was downloaded or looked at. The SHA-256 of this file is printed at the top of
every result. Changing anything below after data is seen means a new hypothesis and a new holdout, not a re-run.

## Question
Owner's answers of 05.11.2026: goal is income, capital about $200-500, futures only, leverage 10-15x allowed,
short timeframes run automatically, daily and longer timeframes need the owner's confirmation; project stop:
4 weeks and $100 without a passed pre-registered test.

Can a simple, textbook rule on USDT-margined perpetual futures, long and short, without any LLM, show a positive
expectancy per trade after realistic Bybit costs and funding, convincingly enough to justify building the Bybit bot?

## Data
- Binance USDT-M futures 5-minute klines from the public archive
  `https://data.binance.vision/data/futures/um/monthly/klines/<SYMBOL>/5m/` and monthly funding rates from
  `.../futures/um/monthly/fundingRate/<SYMBOL>/` (no key). Binance is used for its complete public history,
  including delisted contracts. Major-coin perp prices on Binance and Bybit differ by basis points.
- Period loaded: 2021-10-01 to 2026-10-31 (three months of warm-up).
- 1h, 4h, 1D (UTC days) and 1W (Monday-start) bars are built from the 5m bars. Stops are resolved on 5m bars.
- Universe (fixed now, from memory of market caps at 01.01.2022, to limit survivorship bias): the 20 largest
  non-stablecoin coins that had a Binance USDT-M perpetual then:
  BTC, ETH, BNB, SOL, ADA, XRP, LUNA, DOT, AVAX, DOGE, 1000SHIB, MATIC, LTC, LINK, UNI, ALGO, BCH, TRX, ATOM, NEAR.
  CRO is excluded (no Binance perp). When a contract's data ends (LUNA in 05.2022, MATIC in 2024), an open position
  is closed at the last 5m close with costs and the coin leaves the universe. Successor contracts (LUNA2, POL) are
  not spliced in.

## Trade model (all variants)
- One position per symbol per variant at a time. Signals use only completed bars of the signal timeframe.
- Entry: market order at the open of the first 5m bar at or after the signal bar's close.
- Initial stop distance d is fixed at entry from ATR(14) of the signal timeframe (Wilder); the stop never moves.
  Trades with d < 0.10% of the entry price are skipped.
- Stop exit: the first 5m bar whose low (long) / high (short) reaches the stop. Fill at the stop price, or at the
  5m open if that open is already beyond the stop (gap).
- Other exits are decided on signal-bar closes and filled at the next 5m open.
- R = direction x (exit - entry) / d. All results are in R per trade, net of costs.
- Costs per side, as a fraction of price: base = 0.055% Bybit taker fee + 0.03% slippage (stop exits:
  + 0.05% extra slippage). Stress = all of that x2. Converted to R by dividing by d / entry.
- Funding: at every Binance funding timestamp while the position is open, a long pays and a short receives
  rate x entry notional (negative rates reverse it). Converted to R the same way. Missing funding data counts as 0
  and is reported.

## Rules (K = 6 decision variants, no parameters will be tuned)
Timeframes: 1h and 4h run without confirmation, 1D with confirmation. Each family below on each of the three.
- **T, trend breakout (Donchian).** Long when the close is above the highest high of the previous 20 bars; short
  when below the lowest low of the previous 20 bars. d = 2 x ATR(14). Exit when the close crosses the opposite
  10-bar channel (long: close < lowest low of previous 10 bars; short: close > highest high of previous 10),
  or after a maximum hold (1h: 72 bars, 4h: 60 bars, 1D: 60 bars).
- **M, mean reversion (Bollinger fade).** Long when the close is below SMA(20) - 2 x SD(20); short when above
  SMA(20) + 2 x SD(20). d = 1.5 x ATR(14). Exit when the close crosses SMA(20), or after 20 bars.

Descriptive only (outside K, cannot pass or fail): T and M on 1W bars (max hold 26 weeks). A 1M (monthly) timeframe
is not tested: 22 holdout bars per coin cannot give a usable sample.

## Periods
- Discovery (consistency check only, no tuning): entries 2022-01-01 to 2024-12-31.
- Holdout (decision): entries 2025-01-01 to 2026-10-31; positions open at the end are closed at the last close.

## Controls
- **Random entry:** for each variant, 200 replications. Per symbol, the same number of entries as the rule made in
  the holdout, at uniformly random signal-bar closes of that symbol's holdout, direction long with the rule's long
  share; same stop distance formula and same exit rules. p_random = (1 + #replications with mean R >= rule mean R)
  / 201.
- **Delayed entry:** the rule's holdout signals entered later: +15 min for 1h/4h (bot or exchange latency) and
  +4 h for 1D (owner's confirmation delay). d recomputed at the delayed entry from the same ATR value.

## Pass bar (a variant passes only if ALL hold, on the holdout unless stated)
1. At least 150 trades.
2. Mean net R at base cost > 0, one-sided p < 0.0083 (= 0.05 / 6) by stationary block bootstrap over calendar
   weeks of entry (mean block 4 weeks, 5000 resamples, statistic = total R / total trades).
3. Mean net R at stress cost > 0.
4. p_random < 0.05.
5. Mean net R of the delayed entry at base cost > 0.
6. Discovery mean net R at base cost > 0 (point estimate).

If no variant passes, the Bybit spec is not built as a trading system: no rules core with a measured edge exists for
the LLM to filter, and the project stops by the owner's rule.

## Reported for information
- Per variant: trades, win rate, mean R (base, stress, without funding), median hold, long/short split,
  per-year mean R, share of funding in costs.
- Leverage check: share of trades whose stop distance exceeds the isolated-margin liquidation distance at 10x
  (about 9.5%) and 15x (about 6.2%), i.e. where 10-15x would be liquidated before the stop.
- Portfolio illustration: $300 start, 0.5% of equity risked per trade, at most 3 open positions per variant
  (later signals skipped), Bybit minimum lots ignored; monthly P&L in dollars.

## Known limitations (stated in advance)
- The universe is from memory, not a verified 01.01.2022 snapshot.
- The rules are textbook choices, but the analyst knows crypto history up to mid-2026, so "no look at data" cannot
  fully exclude knowledge leakage. The strict p-value bar is the counterweight.
- The holdout (2025-2026) overlaps the holdout of the earlier daily spot test (2024-2026); this family
  (futures, long/short, intraday stops) was not tested there.
- Binance prices and funding stand in for Bybit. Order-book depth, outages, exchange and account risk are not
  modeled.
