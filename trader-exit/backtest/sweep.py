#!/usr/bin/env python3
"""Search many scalp rule sets on the saved bars, honestly: tune on the older days (train), then
score only the top few on the newer days (test) that the search never saw.

  python3 trader-exit/backtest/sweep.py [--root SPY|SPXW] [--half-spread 0.01] [--train 0.67] [--top 10]

A rule set is worth a paper trial only if it is positive on the test days too, at a realistic
spread, and its fade control (same rules, direction flipped) is not also positive.
Education and tooling only.
"""
import argparse
import glob
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402


def grid():
    exits = [dict(tp=tp, stop=st, be=None, trail=None, time=tm)
             for tp, st, tm in itertools.product((1.2, 1.5, 2.0, None), (0.5, 0.7), (5, 15, 30, 60))]
    exits += [dict(tp=None, stop=st, be=1.1, trail=(1.2, 0.15, 0.10), time=tm)
              for st, tm in itertools.product((0.5, 0.7), (15, 30, 60))]
    sigs = [dict(signal="breakout", lookback=lb, vol=v, cooldown=5, first_entry="09:50", last_entry="15:00",
                 max_per_day=mpd)
            for lb, v, mpd in itertools.product((8, 15, 30), (1.2, 2.0), (3, 99))]
    sigs += [dict(signal="orb", orb=n, lookback=0, vol=0, cooldown=5, first_entry="09:%d" % (30 + n),
                  last_entry="12:00", max_per_day=mpd)
             for n, mpd in itertools.product((15, 30), (1, 2))]
    for sg, ex in itertools.product(sigs, exits):
        p = dict(sg, **ex)
        p["buffer"] = 0.05
        yield p


def name(p):
    sg = ("orb%d" % p["orb"]) if p["signal"] == "orb" else ("bo%d/%.1fx" % (p["lookback"], p["vol"]))
    ex = ("trail" if p["trail"] else "tp%s" % p["tp"]) + " st%s t%d" % (p["stop"], p["time"])
    return "%s max%d %s%s" % (sg, p["max_per_day"], ex, " FADE" if p.get("fade") else "")


def run(days, p, hs, root):
    trades = []
    for spy, opts in days:
        trades.extend(bt.run_day(spy, opts, p, hs, root))
    return trades


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="trader/backtest")
    ap.add_argument("--root", choices=sorted(bt.ROOTS), default="SPY")
    ap.add_argument("--half-spread", type=float, default=0.01)
    ap.add_argument("--train", type=float, default=0.67)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-trades", type=int, default=30)
    a = ap.parse_args(argv)
    days = []
    for ps in sorted(glob.glob(os.path.join(a.data, "spy_*.json"))):
        po = ps.replace("spy_", bt.ROOTS[a.root]["prefix"])
        if os.path.exists(po):
            days.append((os.path.basename(ps)[4:14], bt.load_day(ps, po)))
    if len(days) < 10:
        print("only %d sessions in %s: run fetch.py first" % (len(days), a.data))
        return 1
    cut = int(len(days) * a.train)
    train, test = [d for _, d in days[:cut]], [d for _, d in days[cut:]]
    print("%s: %d sessions; train %s..%s (%d), test %s..%s (%d); half-spread %.1f%%, fee $%.2f/side"
          % (a.root, len(days), days[0][0], days[cut - 1][0], cut, days[cut][0], days[-1][0],
             len(days) - cut, a.half_spread * 100, bt.FEE))
    scored = []
    for p in grid():
        tr = run(train, p, a.half_spread, a.root)
        if len(tr) < a.min_trades:
            continue
        scored.append((sum(t["pnl"] for t in tr), p, tr))
    scored.sort(key=lambda x: -x[0])
    pos = sum(1 for s in scored if s[0] > 0)
    print("searched %d rule sets with >= %d train trades; %d positive on train" % (len(scored), a.min_trades, pos))
    print("%-42s %18s %18s %18s" % ("top on train", "train n/total", "TEST n/total", "fade TEST"))
    for total, p, tr in scored[:a.top]:
        te = run(test, p, a.half_spread, a.root)
        fd = run(test, dict(p, fade=True), a.half_spread, a.root)
        print("%-42s %6d %+10.0f %6d %+10.0f %6d %+10.0f" % (
            name(p), len(tr), total, len(te), sum(t["pnl"] for t in te), len(fd), sum(t["pnl"] for t in fd)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
