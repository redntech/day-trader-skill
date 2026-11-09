#!/usr/bin/env python3
"""Download 1-minute SPY bars and same-day SPXW option bars from the Alpaca data API (paper keys,
market data only, no orders). The keys are read from the Alpaca MCP entry in ~/.claude.json and are
never printed or written anywhere.

  python3 trader-exit/backtest/fetch.py --days 10 [--root SPXW|SPY] [--out trader/backtest]

Writes <out>/spy_YYYY-MM-DD.json and <out>/opt_YYYY-MM-DD.json (symbol -> list of bars).
Option bars exist only for minutes with a trade, so quiet strikes have gaps. That is a real limit
of this data, and backtest.py treats a missing minute as "no fill possible".
"""
import argparse
import datetime
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
DATA = "https://data.alpaca.markets"


def load_keys():
    with open(os.path.expanduser("~/.claude.json")) as fh:
        env = json.load(fh)["mcpServers"]["alpaca"]["env"]
    return env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"]


_KEYS = []
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem") if os.path.exists("/etc/ssl/cert.pem") else None
PAPER = "https://paper-api.alpaca.markets"   # read-only calls only (option contracts); never orders


def get(path, params, base=DATA):
    if not _KEYS:
        _KEYS.extend(load_keys())         # read on first use, kept in memory only
    url = base + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": _KEYS[0], "APCA-API-SECRET-KEY": _KEYS[1],
                                               "accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code == 429 and attempt < 2:
                time.sleep(2 + attempt * 3)
                continue
            raise RuntimeError("HTTP %d for %s: %s" % (e.code, path, body))
        except (urllib.error.URLError, OSError) as e:
            if attempt < 2:
                time.sleep(2)
                continue
            raise RuntimeError("network error for %s: %s" % (path, getattr(e, "reason", type(e).__name__)))


def session_window(day):
    start = datetime.datetime.combine(day, datetime.time(9, 30), ET).astimezone(UTC)
    end = datetime.datetime.combine(day, datetime.time(16, 0), ET).astimezone(UTC)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def paged(path, params, key):
    out = {}
    token = None
    while True:
        p = dict(params)
        if token:
            p["page_token"] = token
        d = get(path, p)
        for sym, bars in (d.get(key) or {}).items():
            out.setdefault(sym, []).extend(bars)
        token = d.get("next_page_token")
        if not token:
            return out


def spy_bars(day):
    s, e = session_window(day)
    return paged("/v2/stocks/bars", {"symbols": "SPY", "timeframe": "1Min", "start": s, "end": e,
                                      "feed": "iex", "limit": 10000, "sort": "asc"}, "bars").get("SPY", [])


ROOTS = {"SPXW": dict(ratio=10.04, width=100, step=5, prefix="opt_"),
         "SPY": dict(ratio=1.0, width=6, step=1, prefix="optspy_")}


def occ(root, day, kind, strike):
    return "%s%s%s%08d" % (root, day.strftime("%y%m%d"), kind, int(round(strike * 1000)))


def option_bars(day, center, root="SPXW"):
    r = ROOTS[root]
    width, step = r["width"], r["step"]
    s, e = session_window(day)
    lo = int(round((center - width) / step) * step)
    hi = int(round((center + width) / step) * step)
    syms = [occ(root, day, k, x) for x in range(lo, hi + step, step) for k in ("C", "P")]
    out = {}
    for i in range(0, len(syms), 25):
        batch = syms[i:i + 25]
        got = paged("/v1beta1/options/bars", {"symbols": ",".join(batch), "timeframe": "1Min", "start": s,
                                              "end": e, "limit": 10000, "sort": "asc"}, "bars")
        out.update(got)
    return out


def weekdays_back(n, skip_today=True):
    d = datetime.datetime.now(ET).date()
    if skip_today:
        d -= datetime.timedelta(days=1)
    days = []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    return sorted(days)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="weekdays to try, counted back from yesterday")
    ap.add_argument("--out", default="trader/backtest")
    ap.add_argument("--no-options", action="store_true")
    ap.add_argument("--root", choices=sorted(ROOTS), default="SPXW", help="which 0DTE options to fetch")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    got = 0
    for day in weekdays_back(a.days):
        tag = day.isoformat()
        p_spy = os.path.join(a.out, "spy_%s.json" % tag)
        p_opt = os.path.join(a.out, "%s%s.json" % (ROOTS[a.root]["prefix"], tag))
        if os.path.exists(p_spy):
            with open(p_spy) as fh:
                spy = json.load(fh)
        else:
            try:
                spy = spy_bars(day)
            except RuntimeError as e:
                print("%s SPY: %s" % (tag, e))
                continue
        if len(spy) < 100:
            print("%s: %d SPY bars, skipped (holiday or no data)" % (tag, len(spy)))
            continue
        with open(p_spy, "w") as fh:
            json.dump(spy, fh)
        n_opt = 0
        if not a.no_options:
            center = spy[0]["o"] * ROOTS[a.root]["ratio"]
            try:
                opts = option_bars(day, center, a.root)
            except RuntimeError as e:
                print("%s options: %s" % (tag, e))
                opts = {}
            n_opt = sum(len(v) for v in opts.values())
            with open(p_opt, "w") as fh:
                json.dump(opts, fh)
        print("%s: %d SPY bars, %d option bars" % (tag, len(spy), n_opt))
        got += 1
    print("done: %d days saved in %s" % (got, a.out))
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main())
