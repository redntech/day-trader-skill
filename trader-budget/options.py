#!/usr/bin/env python3
"""options.py - size ONE long option (a call or a put you buy) to your max loss.

Part of trader-budget. It never talks to a broker and never places anything.
You give it a contract and its quote (from Alpaca's options-data tools), it
finds how many contracts fit, and with --write it proposes one order in the
shape of Alpaca's place_option_order. trader-risk checks it, trader-broker
asks for your yes.

An option has no stop. The most a bought option can lose is what you pay for
it, the premium: limit price x 100 x contracts. So the size is simple:

    contracts = floor(max_loss_per_trade_usd / (limit price x 100))

and it must also fit max_order_usd, max_position_pct and your cash after the
buffer. If even one contract costs more than your max loss, that is a plain
refusal, not a smaller order.

    python3 options.py --symbol AAPL261120C00250000 --bid 2.40 --ask 2.50 --open-interest 1800
    python3 options.py --symbol AAPL261120C00250000 --bid 2.40 --ask 2.50 --open-interest 1800 --write

--limit PRICE   buy limit (default: the ask, so it fills)
--underlying T  the stock this is on, for the note (default: from the symbol)
--why TEXT      one line on the setup, kept in orders.json (never sent)
--now           "2026-11-09 10:15" New York time, to pin the clock

Reads trader/config.json (and trader/portfolio.json for cash). Writes
trader/orders.json with --write. Education and tooling, not financial advice.
Options can lose 100% of what you pay.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys

OCC_PARTS = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DEFAULTS = {
    "budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000,
    "min_order_usd": 5, "max_loss_per_trade_usd": 50, "paper": True, "allow_options": False,
    "max_option_spread_pct": 10, "min_option_open_interest": 100, "min_option_dte_days": 1,
}


class OptionError(Exception):
    pass


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    return "${:,.2f}".format(x)


def parse_contract(symbol):
    """AAPL261120C00250000 -> dict. Raises OptionError for anything else."""
    sym = str(symbol or "").strip().upper().replace(" ", "")
    m = OCC_PARTS.match(sym)
    if not m:
        raise OptionError("%r is not an OCC option symbol (like AAPL261120C00250000: root, YYMMDD, C or P, "
                          "strike x 1000 in 8 digits)" % symbol)
    root, yy, mm, dd, cp, strike = m.groups()
    try:
        expiry = datetime.date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        raise OptionError("%s has an impossible expiry date" % sym)
    k = int(strike) / 1000.0
    return {"symbol": sym, "underlying": root, "expiry": expiry, "right": "call" if cp == "C" else "put",
            "strike": k}


def words(c):
    k = ("%d" % c["strike"]) if c["strike"] == int(c["strike"]) else ("%g" % c["strike"])
    return "%s %s %d %d %s %s" % (c["underlying"], MONTHS[c["expiry"].month - 1], c["expiry"].day,
                                  c["expiry"].year, k, c["right"])


def size_option(cfg, symbol, bid, ask, open_interest=None, limit=None, cash=None, today=None):
    """Returns a result dict. result["ok"] is False with result["reason"] when nothing may be bought."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    con = parse_contract(symbol)
    bid, ask = _f(bid), _f(ask)
    if not bid or not ask or bid <= 0 or ask < bid:
        raise OptionError("give a real quote: --bid and --ask, with ask at or above bid")
    limit = _f(limit) if limit is not None else ask
    if not limit or limit <= 0:
        raise OptionError("--limit must be a positive price")
    out = {"ok": False, "contract": con, "bid": bid, "ask": ask, "limit": round(limit, 2),
           "open_interest": _f(open_interest), "notes": []}
    if not c.get("allow_options"):
        out["reason"] = "options are off (\"allow_options\": false in trader/config.json)"
        return out
    today = today or datetime.date.today().isoformat()  # local date; --now pins it
    dte = (con["expiry"] - datetime.date.fromisoformat(today)).days
    out["dte"] = dte
    if dte < int(c.get("min_option_dte_days") or 0):
        out["reason"] = ("it expires %s, %d day(s) away; your minimum is %s (\"min_option_dte_days\")"
                         % (con["expiry"].isoformat(), dte, c["min_option_dte_days"]))
        return out
    mid = (bid + ask) / 2.0
    spread = 100.0 * (ask - bid) / mid
    out["spread_pct"] = round(spread, 1)
    max_sp = _f(c.get("max_option_spread_pct"))
    if max_sp and spread > max_sp + 1e-9:
        out["reason"] = "the spread is %.1f%% of the mid price, over your %s%% limit: too costly to trade" % (spread, max_sp)
        return out
    min_oi = _f(c.get("min_option_open_interest"))
    if min_oi and (out["open_interest"] is None or out["open_interest"] < min_oi):
        out["reason"] = ("open interest is %s, under your minimum %s (--open-interest is required)"
                         % (out["open_interest"] if out["open_interest"] is not None else "unknown", int(min_oi)))
        return out
    if limit > ask + 0.005:
        out["reason"] = "the limit %.2f is above the ask %.2f: you would overpay" % (limit, ask)
        return out
    per = round(limit * 100, 2)
    out["per_contract"] = per
    budget = _f(c.get("budget")) or 0.0
    cash = budget if cash is None else cash
    caps = {}
    cap = _f(c.get("max_loss_per_trade_usd"))
    if cap:
        caps["max loss (the whole premium)"] = (cap, int(cap // per))
    if _f(c.get("max_order_usd")):
        caps["max order"] = (c["max_order_usd"], int(c["max_order_usd"] // per))
    if budget and _f(c.get("max_position_pct")):
        lim = budget * c["max_position_pct"] / 100.0
        caps["position cap"] = (lim, int(lim // per))
    buffer_usd = budget * (_f(c.get("cash_buffer_pct")) or 0) / 100.0
    avail = max(cash - buffer_usd, 0.0)
    caps["cash after buffer"] = (avail, int(avail // per))
    out["limits"] = {k: {"dollars": round(v[0], 2), "contracts": v[1]} for k, v in caps.items()}
    binding = min(caps.items(), key=lambda kv: kv[1][1])
    n = binding[1][1]
    if n < 1:
        out["reason"] = ("one contract costs %s (%.2f x 100), more than your %s limit of %s. "
                         "A smaller order is not possible; pick a cheaper contract"
                         % (money(per), limit, binding[0], money(binding[1][0])))
        return out
    if per * n < (_f(c.get("min_order_usd")) or 0):
        out["reason"] = "the order is under your minimum order size"
        return out
    out.update({"ok": True, "qty": n, "binding": binding[0], "cost": round(per * n, 2),
                "max_loss": round(per * n, 2)})
    return out


def order_from(res, why=""):
    con = res["contract"]
    plan = {"kind": "option", "underlying": con["underlying"], "expiry": con["expiry"].isoformat(),
            "right": con["right"], "strike": con["strike"], "bid": res["bid"], "ask": res["ask"],
            "open_interest": res["open_interest"], "premium": res["limit"],
            "max_loss": res["max_loss"], "stop": None,
            "exit": "no broker stop: sell to close by flat_by, or earlier if the premium falls to your "
                    "exit price"}
    return {"symbol": con["symbol"], "side": "buy", "qty": str(res["qty"]), "type": "limit",
            "limit_price": "%.2f" % res["limit"], "time_in_force": "day", "position_intent": "buy_to_open",
            "why": why or ("long %s: %d contract%s, bound by %s (%d x %.2f x 100 = %s, all of it at risk)"
                           % (words(con), res["qty"], "" if res["qty"] == 1 else "s", res["binding"],
                              res["qty"], res["limit"], money(res["max_loss"]))),
            "plan": plan}


def report(res):
    con = res["contract"]
    lines = ["%s (%s)" % (con["symbol"], words(con)),
             "Quote: bid %.2f, ask %.2f%s, open interest %s, %s days to expiry."
             % (res["bid"], res["ask"], (", spread %.1f%%" % res["spread_pct"]) if "spread_pct" in res else "",
                int(res["open_interest"]) if res["open_interest"] is not None else "unknown", res.get("dte", "?"))]
    if not res["ok"]:
        lines.append("REFUSED: " + res["reason"] + ".")
        lines.append("Nothing was written.")
        return "\n".join(lines)
    lines.append("Limits (whole contracts; the smallest one wins):")
    for k, v in res["limits"].items():
        lines.append("  %-30s %s / %s = %d contract%s%s" % (k, money(v["dollars"]), money(res["limit"] * 100),
                                                         v["contracts"], "" if v["contracts"] == 1 else "s",
                                                         "  <- binds" if k == res["binding"] else ""))
    lines.append("Size: %d contract%s at limit %.2f = %s. Max loss: the whole %s, if it expires worthless "
                 "or you sell at zero." % (res["qty"], "" if res["qty"] == 1 else "s", res["limit"],
                                            money(res["cost"]), money(res["max_loss"])))
    lines.append("There is no stop at the broker for an option: you watch it and sell to close by flat_by.")
    lines.append("Education and tooling, not financial advice. Options can lose 100% of what you pay.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size one long option to your max loss (trader-budget). "
                                             "Never places orders.")
    ap.add_argument("--symbol", required=True, help="OCC symbol, like AAPL261120C00250000")
    ap.add_argument("--bid", required=True)
    ap.add_argument("--ask", required=True)
    ap.add_argument("--open-interest")
    ap.add_argument("--limit")
    ap.add_argument("--why", default="")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help="New York time, like \"2026-11-09 10:15\"")
    a = ap.parse_args(argv)
    try:
        cfg = {}
        if os.path.exists(a.config):
            with open(a.config, encoding="utf-8") as fh:
                cfg = json.load(fh)
        cash = None
        if os.path.exists(a.portfolio):
            with open(a.portfolio, encoding="utf-8") as fh:
                cash = _f(json.load(fh).get("cash"))
        today = a.now.split()[0] if a.now else None
        res = size_option(cfg, a.symbol, a.bid, a.ask, a.open_interest, a.limit, cash, today)
    except OptionError as e:
        print("options.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(report(res))
    if not res["ok"]:
        return 1
    if a.write:
        order = order_from(res, a.why)
        d = os.path.dirname(a.orders)
        if d:
            os.makedirs(d, exist_ok=True)
        doc = {"created_by": "trader-budget", "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "status": "proposed", "paper": bool(cfg.get("paper", True)),
               "note": "Proposals only. Nothing is placed until trader-risk passes each order and you say yes "
                       "to it in the chat.",
               "source": "trader-budget/options.py %s" % res["contract"]["symbol"], "orders": [order]}
        with open(a.orders + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(a.orders + ".tmp", a.orders)
        print("Wrote 1 proposed option order to %s. Next: trader-risk, then trader-broker." % a.orders)
    return 0


if __name__ == "__main__":
    sys.exit(main())
