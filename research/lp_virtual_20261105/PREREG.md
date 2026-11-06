# Pre-registration: virtual concentrated-liquidity maker test (direction D), 05.11.2026

Written before the recorder exists and before any pool tape is collected. The SHA-256 of this file is printed
with every result. Changes after data collection starts mean a new hypothesis and new days.

## Question
Do passive concentrated-liquidity positions on Solana (Orca Whirlpool, Raydium CLMM, Meteora DLMM), held one day
at a time without intraday rebalancing, earn fees that clearly exceed impermanent loss and costs at a $300 size?
This is the only reviewed direction where the bot would receive the pool fee instead of paying it.

## Data (collected by the bot's LP recorder, build v0.7.103.0, PAPER, no capital)
- Pool selection, frozen automatically after the first 2 hours of the first run and never changed:
  for each venue label `orca-whirlpool`, `raydium-clmm`, `meteora-dlmm`, the 4 pools with the most swap messages
  in those 2 hours, among pools whose quote mint is SOL or USDC, whose pair is not two stablecoins, and whose rate
  is at most 6000 swaps per hour (recording feasibility). 12 pools in total.
- For the selected pools, every PumpApi message (swaps, add/remove liquidity) with block, timestamp, amounts,
  price and the per-swap `poolFeeRate`.
- Raw pool accounts every 30 s; for DLMM pools also the bin arrays around the active bin every 5 minutes.
- On-chain signatures of each pool every 2 minutes, checked against the tape one cycle later, with one missing
  transaction per cycle fetched to classify it (completeness audit).

## Data-quality gates (a pool failing any gate is excluded before the result is computed)
1. Feed completeness: estimated share of state-changing transactions missing from the tape <= 2%. Estimate =
   (successful on-chain signatures of the pool missing from the tape, in non-truncated audit cycles) / (checked)
   x (share of sampled missing transactions that changed the pool's token balances, by getTransaction).
   Amended 05.11.2026 before any data was collected: the first version counted transactions that only read the
   pool as missing; the original hash was 5f8835de9479e1aab9ea2af3f4dce6c5c85176903695e7e387648b928fe94663.
2. Decoding: the price decoded from the pool snapshot is within 0.5% of the feed price at the nearest swap in at
   least 95% of snapshots.
3. At least 10 complete UTC days of tape for the pool.

## Virtual position (K = 2 widths, nothing else is tuned)
- Each complete UTC day, at 00:00 UTC, a position worth $300 is opened at the then-current pool price, as liquidity
  spread uniformly across the price range of +/-2% (W2) or +/-10% (W10), and closed at 24:00 UTC. No rebalancing.
- Fee income per swap inside the range: amount_in x `poolFeeRate` x 0.80 (LP share of fees assumed; sensitivity at
  1.00) x our share. Our share is L_ours / (L_active + L_ours), with L_active from the latest pool snapshot (CLMM,
  Whirlpool) or the bin reserves from the latest bin-array snapshot (DLMM).
- JIT haircut: swaps in a block that also contains an add or remove on the same pool earn nothing for us.
- Impermanent loss: position value at close minus the value of simply holding the opening token amounts.
- Costs per day: opening and closing transactions (2 x (5000 lamports + p75 priority fee seen on that pool that day)),
  plus a swap of half the notional at the pool fee at the open and at the close to balance the tokens.
  Position rent is treated as refundable and excluded (stated limitation).

## Pass bar (per width; a width passes only if ALL hold)
1. At least 10 pools pass the data gates and at least 150 pool-days are evaluated.
2. Aggregate (fee income) / (impermanent loss + costs) >= 1.5.
3. Mean net result per pool-day versus holding > 0 with one-sided p < 0.003 (0.006 for the project, split over
   K = 2 widths) by block bootstrap that resamples whole days and is clustered by pool (5000 resamples).
4. Net result > 0 in both halves of the collection period (first and second half of complete days).
5. Net result > 0 with the 80% LP fee share.

## Stopping rules
- Collection lasts 14 complete days, then stops.
- Futility check after 7 complete days: if for both widths the aggregate fees / (IL + costs) < 0.5, the test stops
  and direction D is closed.

## Known limitations (stated in advance)
- PAPER cannot show queue priority, real JIT competition or our own effect on prices.
- The LP fee share and the decoding of the three programs' accounts are verified against the feed, not against
  the programs' source.
- Position rent and the transactions of a real rebalancing bot are not modeled.
