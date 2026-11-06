# Pre-registration: Veles-style DCA bots on a $300 account (06.11.2026)

Written before any DCA simulation was run. Price data (Binance USDT-M 5m klines and funding, 20 coins, 10.2021-10.2026)
was already downloaded for `research/futures_rules_20261105` and is reused unchanged (manifest there). The SHA-256
of this file is printed with every result.

## Question
The owner points to Veles (veles.finance) as proof that bot trading earns money: its showcase presets claim +50% to
+117% a year (long, 2-4x). Veles' own live statistics exclude liquidated and error-interrupted deals and funding,
and its backtests ignore wicks by default and do not flag liquidation (help.veles.finance, read 06.11.2026).

What would a $300 account actually have earned running Veles-style DCA presets, as published, on 20 large coins
across every calendar year 2022-2026, with liquidation, funding, exchange fees and the Veles fee counted?

## Bot model (`dca.py`, unit-tested)
- Veles "simple" grid: n orders including the first (market) order, evenly spaced over the "overlap" range,
  each next order's notional = previous x (1 + martingale). Total notional of all orders = bot deposit x leverage.
  Logarithmic distribution is not modeled (even spacing = log 1.0).
- Take profit: limit order at TP % from the average price, re-placed after each fill, filled at its own price.
- Stop loss, Veles "simple" mode: market order at (overlap + SL %) from the first order's price, active only after
  the whole grid is filled.
- Entry filters as published (AND), evaluated on the last completed bar of the filter timeframe; without filters a
  new deal starts at the next 5m open after the previous one closes.
- One bot per account, cross margin: the whole $300 account backs the position. Liquidation when account equity +
  unrealized P&L at the bar's adverse extreme <= 0.5% maintenance margin x notional; the account is then wiped
  ($0) and the bot stops. Spot variant: no leverage, no liquidation, no funding.
- Within a 5m bar, the take profit of the pre-bar average is checked before the bar's safety orders (conservative
  for the bot's win count, generous for its loss count is not assumed either way otherwise).
- Fees (Bybit, non-VIP): futures maker 0.02% for grid and TP limit orders, taker 0.055% + 0.03% slippage for market
  entries and stops; spot 0.10% maker and taker. Funding: actual Binance rates on the open notional.
- Veles fee: 20% of each winning deal's net profit, capped at $50 per calendar month.

## Variants (K = 5, fixed now; [U] = my fill-in where the source gives no number)
- **V1 "$1 bot" (ForkLog 2023):** long, 20x, deposit $30, 5 orders, overlap 15% [U], martingale 20% [U],
  TP 0.75%, no SL, entry when RSI(14) < 30 and CCI(20) < -100 on 15m bars [U timeframe and thresholds].
- **V2 "x7 grid" (Veles blog 30.05.2025):** long, 7x, deposit $30 [U], 5 orders, overlap 10% [U], martingale
  20% [U], entry when RSI(14, 30m) < 30 and CCI(20, 30m) < -100 and ADX(14, 15m) > 40, TP 0.5% [U: the source uses
  a CMO(5m) signal exit with min PnL 0.3%], SL 3% beyond the last order.
- **V3 "conservative, no SL" (vc.ru presets 2024):** long, 4x, deposit $100 (reserve 2x deposit), 4 orders,
  overlap 25%, martingale 5% [U], TP 1.5% [U within 1-2%], no SL, no filter.
- **V4 "grid with hard stop" (wiki SL example):** long, 5x [U], deposit $30 [U], 5 orders, overlap 15%,
  martingale 20% [U], TP 1% [U], SL 5% beyond the last order (20% from the open), no filter.
- **V5 spot:** long, spot, deposit $300 (whole account), 5 orders, overlap 15%, martingale 20%, TP 1%, no SL,
  no filter.

## Runs
- Universe: the same 20 coins as the futures test. Coins whose data ended (LUNA 05.2022, MATIC 09.2024) are run
  while data exists; an open position is closed at the last close.
- **Coin-years:** for every variant, coin and calendar year 2022, 2023, 2024, 2025 and 2026 (to 31.10): a fresh $300
  account from 1 January (or first data) to the year's end. Open positions are marked to market at the year's last
  close (closed with a market order and taker fee).
- **Full run:** for every variant and coin, one $300 account from 01.01.2022 to 31.10.2026 without topping up.
- Benchmarks: buy-and-hold of the same coin, spot, $300, same windows; and USDT (0%).

## Verdict rule (per variant)
A variant "earns robustly" only if ALL hold over its coin-years:
1. Median 1-year return after all fees > 0.
2. Mean 1-year return after all fees > 0.
3. Liquidated or >= 50% lost in at most 5% of coin-years.
4. Conditions 1-2 also hold on the 2025-2026 coin-years alone.
5. In the full 2022-2026 run, the median coin ends above $300.

## Also reported
- Share of coin-years in profit, best and worst coin-year, 1-year return distribution (10/25/50/75/90%).
- "Showcase effect": the best coin-year per variant versus the median, i.e. what a curated preset page would show.
- Deals, deal win rate, deals at full grid, stops, liquidations, funding and fees paid, months with the Veles fee
  cap hit, max drawdown of marked-to-market equity.
- Comparison with buy-and-hold of the same coin in the same windows.

## Known limitations (stated in advance)
- Parameters partly my fill-ins ([U]); logarithmic spacing, trailing TP, partial placement and pull-up not modeled.
- Binance prices stand in for Bybit; 5m bars, not ticks; conservative intrabar order.
- The analyst knows market history to mid-2026; the presets are taken from published sources, not tuned here.
