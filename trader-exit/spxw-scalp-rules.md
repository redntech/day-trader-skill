# SPXW / SPY 0DTE scalp rules, v3 (day-trader mode)

> **Backtest verdict: no edge. Do not run these rules.** Checked twice: 12 sessions, then 126
> sessions (2026-05-11 to 2026-11-06) with a clock-based, no-look-ahead backtest. At a 1% half-spread
> on SPY, v1 lost $1,385 over 809 trades and v3 lost $6,811 over 2,261. On SPXW at 3%: v1 -$4,526 (608 trades), v3 -$13,080 (1,665).
> Trading more often only adds costs. The one rule set that held up out of sample is slower:
> morning-only SPY momentum, at most 3 trades a day, 60-minute holds. See
> [spy-morning-momentum.md](spy-morning-momentum.md). The rules below are kept only as the spec the
> backtest checks.

Paper only. Buy to open calls or puts only. No margin, no shorting, no selling or writing options,
no spreads. The most you can lose on a trade is the premium you paid.

## Limits (trader/config.json)
- max_loss_per_trade_usd 200, max_daily_loss_usd 500, max_trades_per_day 30, max_orders_per_day 100.
- The daily loss counts realized losses on closed options (a winner counts 0) plus the full
  premium of any option still open (trader-risk/check.py, closed_option_losses).

## Entry
- Signal: the last completed 1-minute SPY bar breaks the prior 8 bars' high (call) or low (put)
  on at least 1.2x their average volume.
- Window 09:50 to 15:20 ET. 2-minute cooldown after a close. One open scalp at a time.
- Contract: today's SPXW (or SPY), strike within 40 SPX points (about 4 SPY dollars), spread
  at most 10% of mid, open interest at least 100. Limit = ask + 0.05, never above $200 total.
- Re-quote just before sending. Not filled in about 30 seconds: cancel and mark it failed.

## Exit (checked every tick)
1. Take-profit: rest a sell limit at entry x 1.20, rounded up to the tick (0.05 under $3, 0.10 above).
2. Break-even stop: once the bid has reached entry x 1.10, the stop moves up to entry x 1.00.
3. Initial stop: bid at or below entry x 0.70.
4. Trailing exit (replaces the resting take-profit): once the bid reaches entry x 1.20, cancel the
   resting sell and track the high-water bid. Sell to close at the bid when it falls the larger of
   12% or $0.10 below that high. The high is kept in trader/state/scalp.json as "hwm".
5. Time stop: 5 minutes after entry, or 15:30 ET, whichever comes first.
6. A sell that only closes a held option is never blocked by the daily limits.

## Known limits
- The loop polls once a minute at best, and these options can move $0.80 in that time, so stops
  and the trail can fill well past their trigger.
- Spreads of 5% or more on $1 to $2 options take a large share of a 20% target.
- SPY 0DTE options are usually tighter than far out-of-the-money SPXW. Test both.
