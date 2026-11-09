# SPY morning loop: one tick

The cron prompt for this loop is only: "Run one tick of the SPY morning loop: follow
trader-exit/scalp/loop.md." Every number lives in trader/config.json and in the `spy-am` variant of
trader-exit/backtest/backtest.py, so the prompt cannot drift from them. Rules and evidence:
trader-exit/spy-morning-momentum.md.

Paper only. Buy to open calls or puts, sell to close them. Never margin, shorting, writing options,
spreads or multi-leg orders. Never hold past 15:30 ET. Run scripts from the repo root with
`SSL_CERT_FILE=/etc/ssl/cert.pem`.

## Before the first tick (once a day, with the user)
1. Show the user the bounds and ask for a standing yes, for example: "Paper loop, SPY options only,
   until 11:30 ET, at most 12 orders, at most $400 of premium at risk. Reply yes to start."
2. Only on their own reply that starts with "yes":
   `python3 trader-broker/ticket.py authorize --reply "<their words>" --underlying SPY --until 11:30 --max-orders 12 --max-loss-usd 400`
3. Start the loop: CronCreate every minute, 9:49 to 11:59 ET, with the prompt above.
   Without a standing yes, each order goes to the user for its own yes instead.

## Each tick
1. `python3 trader-exit/scalp/tick.py plan`. The last line is one JSON action, and the
   `next:` lines above it list the exact commands for it.
2. Do only that action:
   - **hold**: nothing. Say one line at most.
   - **buy** / **sell**: run the `next:` steps in order. If trader-risk BLOCKs, or
     `approve --standing` refuses, stop there and tell the user. Never work around either one.
   - **check-order** / **cancel**: look the order up (or cancel it), then record the result in
     both ticket.py (`fill`) and tick.py (`record filled` or `record cancelled`).
   - **done**: delete this cron job and give the user the day's results (`tick.py status`).
3. If a tool call fails, or the state looks wrong, do not guess. Tell the user and stop the loop.

## Ending early
- The user says stop: `python3 trader-broker/ticket.py revoke`, then delete the cron job. If a
  position is open, ask before selling it.
- An open position at 15:30 ET is always sold. tick.py returns **sell** at the hard close.
