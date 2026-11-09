#!/usr/bin/env python3
"""Replay the SPXW scalp rules on the 1-minute bars fetch.py saved. Education and tooling only.

  python3 trader-exit/backtest/backtest.py [--root SPXW|SPY] [--data trader/backtest] [--half-spread 0.03]

What it models
  signal   last completed SPY 1-minute bar B breaks the high (call) or low (put) of the prior N bars
           on at least V x their average volume.
  clock    SPY bars are filled to one per minute (densify), so "N bars" and "N minutes" agree.
  entry    picked with only what is known at the signal minute: the priciest contract within 40 SPX
           points (4 SPY dollars) whose limit (last trade + half-spread + buffer) fits the per-trade cap.
           It fills at the next minute's ask (open + half-spread) only if that is at or under the limit;
           otherwise no trade. One contract, one trade at a time, FEE per contract per side.
  exits    a resting take-profit at the broker (fills if the minute's high reaches it); everything
           else is checked once per minute at the minute's close, like the loop does:
           stop, break-even stop, trailing exit, time stop, hard close at 15:30 ET.
What it cannot know
  Option bars exist only for minutes with a trade; the real bid and ask are not in them. The spread is
  an assumption (--half-spread, a share of the price on each side). A missing minute reuses the last
  close. A resting take-profit is assumed filled whenever the bar's high touches it, which flatters
  results when that high is one thin print. Results are only as good as those assumptions.
"""
import argparse
import bisect
import datetime
import glob
import json
import math
import os
import re
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
OCC = re.compile(r"^(SPXW|SPY)(\d{6})([CP])(\d{8})$")
# root: SPY-to-underlying ratio, strike window around the money, options file prefix
ROOTS = {"SPXW": dict(ratio=10.04, near=40, prefix="opt_"), "SPY": dict(ratio=1.0, near=4, prefix="optspy_")}

VARIANTS = {
    # name: parameters
    "v1": dict(lookback=15, vol=1.5, tp=1.30, stop=0.65, be=None, trail=None, time=10, cooldown=5,
               last_entry="15:15", buffer=0.0),
    "v2": dict(lookback=8, vol=1.2, tp=1.30, stop=0.65, be=None, trail=None, time=10, cooldown=2,
               last_entry="15:20", buffer=0.05),
    "v3": dict(lookback=8, vol=1.2, tp=1.20, stop=0.70, be=1.10, trail=None, time=5, cooldown=2,
               last_entry="15:20", buffer=0.05),
    "v3-trail": dict(lookback=8, vol=1.2, tp=None, stop=0.70, be=1.10, trail=(1.20, 0.12, 0.10), time=5,
                     cooldown=2, last_entry="15:20", buffer=0.05),
    # the one rule set that held up out of sample (see trader-exit/spy-morning-momentum.md)
    "spy-am": dict(signal="breakout", lookback=15, vol=1.2, tp=None, stop=0.70, be=1.10, trail=(1.20, 0.15, 0.10),
                   time=60, cooldown=5, first_entry="09:50", last_entry="11:00", max_per_day=3, buffer=0.05),
}
MAX_COST = 200.0          # per-trade cap, dollars
FIRST_ENTRY = "09:50"
HARD_CLOSE = "15:30"
FEE = 0.03                # regulatory fees per contract per side, dollars (Alpaca charges no commission)
RECENT = 3                # a contract's last trade must be this many minutes old or newer to be picked


def et_minutes(t):
    d = datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(ET)
    return d.hour * 60 + d.minute


def hm(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def utc_text(day, minutes):
    d = datetime.datetime.combine(day, datetime.time(minutes // 60, minutes % 60), ET).astimezone(UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def densify(spy):
    """One bar per clock minute 09:30-15:59 ET. IEX skips minutes with no trade; a skipped minute
    becomes a flat bar at the last close with volume 0, so 'N bars' always means N minutes."""
    if not spy:
        return spy
    first = datetime.datetime.strptime(spy[0]["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(ET)
    by = {b["t"]: b for b in spy}
    out, prev = [], None
    for m in range(9 * 60 + 30, 16 * 60):
        t = utc_text(first.date(), m)
        b = by.get(t)
        if b:
            prev = b["c"]
            out.append(b)
        elif prev is not None:
            out.append({"t": t, "o": prev, "h": prev, "l": prev, "c": prev, "v": 0})
    return out


def load_day(path_spy, path_opt):
    with open(path_spy) as fh:
        spy = densify(json.load(fh))
    with open(path_opt) as fh:
        raw = json.load(fh)
    opts = {}
    for sym, bars in raw.items():
        m = OCC.match(sym)
        if not m:
            continue
        opts[sym] = {"kind": m.group(3), "strike": int(m.group(4)) / 1000.0,
                     "bars": {b["t"]: b for b in bars}, "times": sorted(b["t"] for b in bars)}
    return spy, opts


def signal(spy, i, p):
    """'C', 'P' or None for the bar at index i. p['signal']: breakout (default) or orb;
    p['fade'] flips the direction (a control: if fading does as well, the signal means nothing)."""
    s = orb(spy, i, p) if p.get("signal") == "orb" else breakout(spy, i, p)
    if s and p.get("fade"):
        s = "P" if s == "C" else "C"
    return s


def orb(spy, i, p):
    """Opening-range breakout: the first close above the high (call) or below the low (put) of the
    first p['orb'] minutes."""
    n = p["orb"]
    if i < n:
        return None
    rng = spy[:n]
    hi, lo = max(x["h"] for x in rng), min(x["l"] for x in rng)
    c, pc = spy[i]["c"], spy[i - 1]["c"]
    if c > hi >= pc:
        return "C"
    if c < lo <= pc:
        return "P"
    return None


def breakout(spy, i, p):
    """The bar at index i closes beyond the high or low of the p['lookback'] bars before it, on volume."""
    n = p["lookback"]
    if i < n:
        return None
    b, prior = spy[i], spy[i - n:i]
    avg_v = sum(x["v"] for x in prior) / float(n)
    if avg_v <= 0 or b["v"] < p["vol"] * avg_v:
        return None
    if b["c"] > max(x["h"] for x in prior):
        return "C"
    if b["c"] < min(x["l"] for x in prior):
        return "P"
    return None


def last_trade(o, t, recent=RECENT):
    """The option's last bar at or before time t, if it is at most `recent` minutes old."""
    k = bisect.bisect_right(o["times"], t) - 1
    if k < 0:
        return None
    tt = o["times"][k]
    age = et_minutes(t) - et_minutes(tt)
    return o["bars"][tt] if 0 <= age <= recent else None


def pick_contract(opts, kind, level, near, t_sig, t_fill, hs, buffer):
    """Choose with what is known at the signal minute: the priciest contract of this kind within
    `near` of the underlying whose limit (last price + half-spread + buffer) fits MAX_COST.
    It fills at the next minute's ask (open + half-spread) only if a trade prints and that ask is at
    or under the limit; otherwise the order is cancelled, as the loop does. Returns (sym, entry) or None."""
    best = None
    for sym, o in opts.items():
        if o["kind"] != kind or abs(o["strike"] - level) > near:
            continue
        last = last_trade(o, t_sig)
        if not last:
            continue
        limit = round(last["c"] * (1 + hs) + buffer, 2)
        if limit <= 0 or limit * 100 > MAX_COST:
            continue
        if best is None or limit > best[1]:
            best = (sym, limit)
    if not best:
        return None
    bar = opts[best[0]]["bars"].get(t_fill)
    if not bar:
        return None
    ask = round(bar["o"] * (1 + hs), 2)
    return (best[0], ask) if ask <= best[1] + 1e-9 else None


def tick_up(x):
    step = 0.05 if x < 3 else 0.10
    return round(math.ceil(x / step - 1e-9) * step, 2)


def simulate(spy, opts, idx_entry, sym, entry, p, hs):
    """Walk minutes from the entry minute. Returns (exit_price, reason, exit_index)."""
    o = opts[sym]["bars"]
    tp = tick_up(entry * p["tp"]) if p["tp"] else None
    last_cl = entry / (1 + hs)
    hwm = entry * (1 - hs)
    for j in range(idx_entry, len(spy)):
        t = spy[j]["t"]
        bar = o.get(t)
        hi = bar["h"] if bar else last_cl
        cl = bar["c"] if bar else last_cl
        last_cl = cl
        if tp is not None and hi >= tp:
            return tp, "take-profit", j
        bid = cl * (1 - hs)
        hwm = max(hwm, bid)
        if et_minutes(t) + 1 >= hm(HARD_CLOSE):
            return bid, "hard close", j
        if bid <= entry * p["stop"]:
            return bid, "stop", j
        if p["be"] and hwm >= entry * p["be"] and bid <= entry:
            return bid, "break-even", j
        if p["trail"]:
            arm, pct, floor = p["trail"]
            if hwm >= entry * arm and bid <= hwm - max(floor, pct * hwm):
                return bid, "trail", j
        if j - idx_entry >= p["time"]:
            return bid, "time", j
    return last_cl * (1 - hs), "end of data", len(spy) - 1


def run_day(spy, opts, p, hs, root="SPXW"):
    trades = []
    i = p["lookback"]
    while i < len(spy) - 2:
        t_next = spy[i + 1]["t"]
        mins = et_minutes(t_next)
        if mins < hm(p.get("first_entry", FIRST_ENTRY)) or mins > hm(p["last_entry"]):
            i += 1
            continue
        side = signal(spy, i, p)
        if not side:
            i += 1
            continue
        if len(trades) >= p.get("max_per_day", 99):
            break
        r = ROOTS[root]
        pick = pick_contract(opts, side, spy[i]["c"] * r["ratio"], r["near"], spy[i]["t"], t_next, hs, p["buffer"])
        if not pick:
            i += 1
            continue
        sym, entry = pick
        exit_px, why, j = simulate(spy, opts, i + 1, sym, entry, p, hs)
        trades.append({"at": t_next, "sym": sym, "entry": entry, "exit": round(exit_px, 2), "why": why,
                       "pnl": round((exit_px - entry) * 100 - 2 * FEE, 2), "mins": j - i})
        i = j + max(1, p["cooldown"])
    return trades


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    pnl = [t["pnl"] for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "win%": round(100.0 * len(wins) / n), "total": round(sum(pnl), 2),
            "avg": round(sum(pnl) / n, 2), "best": max(pnl), "worst": min(pnl), "maxdd": round(dd, 2),
            "pf": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="trader/backtest")
    ap.add_argument("--half-spread", type=float, default=0.03)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--root", choices=sorted(ROOTS), default="SPXW")
    a = ap.parse_args(argv)
    days = []
    for ps in sorted(glob.glob(os.path.join(a.data, "spy_*.json"))):
        po = os.path.join(os.path.dirname(ps), os.path.basename(ps).replace("spy_", ROOTS[a.root]["prefix"]))
        if os.path.exists(po):
            days.append(load_day(ps, po))
    if not days:
        print("no data in %s: run fetch.py first" % a.data)
        return 1
    print("%s: %d sessions, half-spread %.1f%% each side" % (a.root, len(days), a.half_spread * 100))
    for name in a.variants.split(","):
        p = VARIANTS[name]
        trades = []
        for spy, opts in days:
            trades.extend(run_day(spy, opts, p, a.half_spread, a.root))
        s = stats(trades)
        why = {}
        for t in trades:
            why[t["why"]] = why.get(t["why"], 0) + 1
        print("%-9s %s  exits %s" % (name, s, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
