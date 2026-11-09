# SPY 0DTE morning momentum (backtest candidate, not proven)

Paper only. Buy to open calls or puts only. No margin, no shorting, no selling or writing options,
no spreads. The most a trade can lose is the premium paid (about $170 for one contract here).

This is the one rule set that held up when tuned on older days and checked on newer ones. It is
still a backtest on 6 months of data with assumed spreads, picked from about 600 tries, so it may
be luck. Paper trade it for a few weeks and compare before trusting it.

## Rules (`backtest.py --root SPY --variants spy-am`)
- Signal: a completed 1-minute SPY bar closes above the high (call) or below the low (put) of the
  prior 15 minutes, on at least 1.2x their average volume.
- Entries 09:50 to 11:00 ET only. At most 3 trades a day. 5-minute cooldown after a close.
- Contract: today's SPY option, strike within $4 of SPY, spread at most 10% of mid, the priciest one
  whose ask fits $200. Limit = the ask (options.py refuses to pay above it). Not filled in a minute: cancel.
- Run it with trader-exit/scalp/tick.py, one tick a minute (trader-exit/scalp/loop.md).
- Exits, checked at every 1-minute close:
  1. Stop: bid at or below 70% of entry.
  2. Break-even: once the bid has reached entry x 1.10, exit if it falls back to entry.
  3. Trail: once the bid reaches entry x 1.20, exit when it falls the larger of 15% or $0.10 below its high.
  4. Time: 60 minutes after entry. Hard close 15:30.
- No fixed take-profit. The edge comes from a few big runners (best +$370), so do not cap winners.

## Evidence (126 sessions, 2026-05-11 to 2026-11-06, 1 contract, $0.03 fee per side)
| half-spread | train (84 days) | test (42 days, never tuned on) | every month |
|---|---|---|---|
| 0.5% | 156 trades, +$1,932, PF 1.68 | 78, +$1,202, PF 1.90 | all 7 positive |
| 1.0% | 157, +$1,601, PF 1.53 | 78, +$887, PF 1.60 | all 7 positive |
| 1.5% | 156, +$1,125, PF 1.35 | 78, +$827, PF 1.56 | 5 of 7 positive |

- About $20 a day on average, 61 of 122 days green, worst day -$167, max drawdown -$477 (1% spread).
- Fade control (same rules, opposite direction): -$2,865. The direction matters, which is the sign
  of a real effect, not noise.
- Neighbours hold up: lookback 10 or 20, time stop 30 or 90, stop 60%, and no break-even all stay
  positive. Wider spreads shrink it: break-even near 2% half-spread.
- Trading all day kills it: the same signal with no daily cap and entries to 15:00 loses $1,038.
  The afternoon trades lose. Quick scalps lose: v1 -$1,385, v3 -$6,811 on the same data.
- It does not work on SPXW (−$822 at 3%): SPXW contracts under $200 are far out of the money with wide spreads.

## Known weak spots
- The 11:00 cutoff was chosen after seeing all 126 days. The train-picked version (entries to 15:00)
  was also positive on the test days (+$1,382), but less so.
- Option bars have no real bid or ask. Spreads are assumed. Near-the-money SPY 0DTE is usually a
  penny or two wide, about 0.3% to 1%.
- Exits are checked once a minute at the close, like the loop. Real fills on a fast move can be worse.
- SPY volume is the IEX feed only, a small share of the market.
- Re-run before relying on it: `python3 trader-exit/backtest/fetch.py --days 130 --root SPY`, then
  `python3 trader-exit/backtest/sweep.py --root SPY` and `backtest.py --root SPY --variants spy-am`.
