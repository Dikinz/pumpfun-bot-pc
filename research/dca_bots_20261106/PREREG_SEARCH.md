# Pre-registration: DCA parameter search on 2025, check on 2026 (06.11.2026)

Written before any run of this search. Same data and simulator as `PREREG.md` in this folder (`dca.py`, 14 unit tests;
`start_dir` added for per-deal direction). The earlier Veles replica results for 2025-2026 are known to the analyst
(all five presets lost in the median); none of their parameter sets is reused as a starting point here.

## Question
The owner wants a product built on DCA bots and asks to analyse only 2025-2026 (no bull market). Can a DCA
configuration chosen on 2025 alone keep earning on 01.2026-10.2026, which the selection never sees, on a $300
account per coin, after fees, funding and liquidation?

## Universe and accounts
- The 19 coins of the earlier tests that have data in 2025-2026 (LUNA and MATIC excluded: no data).
- One bot per coin, fresh $300 account per coin and period, cross margin (the whole account backs the position).
- Bybit futures fees (maker 0.02% grid/TP, taker 0.055% + 0.03% slippage market entries/stops), actual funding.
  No platform fee (own product).

## Search grid (576 configurations, fixed now)
- Direction mode: **L** always long; **S** always short; **R** regime switch: each new deal is long if the last
  completed daily close is above its 50-day SMA, otherwise short.
- Effective leverage k = total notional of all grid orders / account: 0.5, 1, 2, 3.
- 5 orders (first at market), even spacing over the overlap: 10%, 25%.
- Martingale (each next order x (1+m)): 0%, 25%.
- Take profit from the average price: 0.5%, 1%, 2%.
- Stop loss (Veles simple: beyond the last order, after the full grid): none, 5%.
- Entry filter: none; or RSI(14) on 1h bars: long deals start only when RSI < 30, short deals only when RSI > 70.

## Selection (2025-01-01 to 2025-12-31 only)
- Eligible: no liquidation on any coin and no coin below -50%.
- Score: median coin return; ties broken by mean coin return.
- The top 3 eligible configurations go to the check. If fewer than 3 are eligible, only those go.

## Check (2026-01-01 to 2026-10-31, K = 3), a configuration passes only if ALL hold
1. Median coin return > 0.
2. Mean coin return > 0.
3. No liquidation and no coin below -50%.
4. Equal-weight portfolio of the 19 accounts, daily marked-to-market returns: mean > 0 with one-sided p < 0.0167
   (= 0.05 / 3), stationary block bootstrap over days (mean block 10 days, 5000 resamples, percentile method).
5. Portfolio return annualized > 5% (cash-like hurdle).

If none passes, no DCA configuration from this search is a basis for a product.

## Reported for information
- In-sample (2025) versus out-of-sample (2026) results of the selected configurations: the selection shrinkage.
- The same configurations on 2022, 2023 and 2024 (fresh accounts per year), including liquidations: what the product
  would have done in other market regimes.
- Distribution of 2025 scores over all 576 configurations; $ per month on $300.

## Known limitations
- 576 configurations searched on one year: the 2025 winner is expected to be partly luck; that is what the 2026
  check is for. 2026 is only ten months and one market regime.
- The analyst knows that 2025-2026 was a falling market; the S and R modes are included because of the owner's
  framing, which is itself knowledge of the period.
- 5m bars, Binance prices, conservative intrabar order, as in `PREREG.md`.
