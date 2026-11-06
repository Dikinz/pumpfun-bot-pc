# Pre-registration: CEX daily systematic test (05.11.2026)

Written before any price data was downloaded or looked at. The SHA-256 of this file is printed at the top of every
result. Changing anything below after data is seen means a new hypothesis and a new holdout, not a re-run.

## Question
Can a simple, published-style systematic rule on Binance spot, daily horizon, beat simply holding the same coins
after realistic costs, convincingly enough to justify porting the bot core to a CEX?

## Data
- Binance spot daily klines (UTC days), pairs vs USDT, from the public market-data endpoint
  `https://data-api.binance.vision/api/v3/klines` (no key). Open, high, low, close, volume per day.
- Period: 2020-01-01 to 2026-10-31.
- Universe (fixed now, chosen to limit survivorship bias): the 15 largest non-stablecoin coins by market cap at
  01.01.2020 that had a USDT pair on Binance then, from memory of that time:
  BTC, ETH, XRP, BCH, LTC, EOS, BNB, XTZ, XLM, ADA, TRX, LINK, ETC, NEO, ATOM.
  Coins listed later (e.g. SOL) are deliberately excluded. If a pair's data ends (delisting), the position is
  closed at its last close with costs and the coin leaves the universe.

## Execution model (both rules)
- Signals are computed on the close of Sunday (UTC). Trades happen at the open of the next day (Monday). No
  look-ahead: only data up to and including the signal day is used.
- Long only, spot, no leverage, no shorts. Uninvested capital earns 0 (USDT, no yield).
- Cost per side on traded notional: 0.10% fee + 0.05% slippage = 0.15%. Stress case: 0.30% per side.
- Daily portfolio returns from close-to-close prices, weights drift between rebalances.

## Rules (K = 2, no parameters will be tuned)
- **R1, time-series momentum.** Each Sunday, for every coin with at least 28 days of history: target weight
  1/15 if its 28-day return (close today / close 28 days ago - 1) > 0, else 0. The remainder stays in cash.
- **R2, cross-sectional momentum with a market filter.** Each Sunday: if BTC close > its 100-day simple moving
  average, hold the 5 coins with the highest 28-day return, equal weight 1/5 each; otherwise hold 100% cash.

## Benchmarks
- EW: equal-weight buy-and-hold of the universe, rebalanced weekly with the same costs (main benchmark).
- BTC buy-and-hold (information only).
- Carry hurdle: 5% per year (delta-neutral staking carry, from the specialists' review).

## Periods
- Discovery (consistency check only, no tuning): 2020-01-01 to 2023-12-31.
- Holdout (decision): 2024-01-01 to 2026-10-31.

## Pass bar (a rule passes only if ALL hold)
On the holdout:
1. Net CAGR at base cost > 5% (carry hurdle), and net CAGR at stress cost > 0.
2. Sharpe(rule) - Sharpe(EW), daily returns annualized with sqrt(365): one-sided p < 0.006 for the
   difference > 0, by stationary block bootstrap (mean block length 20 days, 5000 resamples).
   0.006 = 0.05 / 8 directions under review (multiple-testing correction for the whole project).
3. Max drawdown of the rule <= max drawdown of EW.
On discovery:
4. Net CAGR at base cost > 5% and Sharpe(rule) > Sharpe(EW) (point estimates).

If neither rule passes, the CEX direction (F) and the large-cap taker direction on Solana DEXes (A) are closed:
a rule that cannot pay on clean, cheap CEX data will not pay at 3-8x the cost on a DEX.

## Known limitations (stated in advance)
- The universe is from memory of early-2020 market caps, not a verified snapshot.
- The rules are textbook choices, but the analyst knows crypto history up to mid-2026, so "no look at data"
  cannot fully exclude knowledge leakage. The strict p-value bar is the counterweight.
- Funding, taxes, withdrawal costs, exchange/account risk and the owner's jurisdiction are not modeled.
