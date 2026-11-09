#!/usr/bin/env python3
"""One tick of the SPY 0DTE morning-momentum loop (rules: trader-exit/spy-morning-momentum.md).

It never places, cancels or changes an order. It reads market data (the Alpaca data API, with the
paper keys from the Alpaca MCP entry in ~/.claude.json, never printed), decides ONE next action,
and prints it as JSON on the last line. Claude does the order steps with trader-budget,
trader-risk, trader-broker and the Alpaca MCP tools, then records what happened here.

    python3 trader-exit/scalp/tick.py plan                  # what to do now
    python3 trader-exit/scalp/tick.py record placed --side buy --symbol SPY261110C00600000 --qty 1 --limit 1.62 --order-id ID
    python3 trader-exit/scalp/tick.py record filled --side buy --price 1.62
    python3 trader-exit/scalp/tick.py record cancelled
    python3 trader-exit/scalp/tick.py status

Actions: hold, done (no more entries today), buy, sell, check-order (a pending order to look up),
cancel (a pending order that waited too long). The signal, entry and exit rules are the same
functions and numbers the backtest uses (backtest.py, variant spy-am), so the two cannot drift.
Limits come from trader/config.json each tick. Paper only. Buy to open, sell to close, nothing else.
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backtest"))
import backtest as bt  # noqa: E402

STATE = "trader/state/scalp.json"
CONFIG = "trader/config.json"
UNDERLYING = "SPY"
FILL_WAIT_S = 60          # a buy not filled in a minute is cancelled; a sell is re-priced
STALE_QUOTE_S = 120


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def et_date(now):
    return bt.datetime.datetime.combine(now.date(), now.time(), bt.UTC).astimezone(bt.ET).date()


def et_hm(now):
    return bt.et_minutes(iso(now))


def params(cfg):
    p = dict(bt.VARIANTS["spy-am"])
    p.update((cfg.get("scalp") or {}))
    return p


def read(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as fh:
        return json.load(fh)


def write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(path + ".tmp", path)


def fresh_state(today, old=None):
    s = {"strategy": "spy-am", "date": today, "trades_today": 0, "last_close": None, "position": None,
         "pending": None, "results": [], "last_tick": None}
    if old and old.get("position"):
        s["position"] = old["position"]       # never forget an open position across days
    return s


def load_state(path, today):
    s = read(path, None)
    if not s or s.get("strategy") != "spy-am" or s.get("date") != today:
        s = fresh_state(today, s if s and s.get("strategy") == "spy-am" else None)
    return s


# ---------------------------------------------------------------- the decision (pure, tested)

def completed_bars(raw, now):
    """Dense 1-minute SPY bars for today that have closed by `now`."""
    cut = iso(now.replace(second=0))
    return [b for b in bt.densify(raw) if b["t"] < cut]


def decide(cfg, state, raw_bars, now, chain_fn, quote_fn):
    """Returns one action dict. chain_fn(kind, lo, hi) -> [{symbol, strike, bid, ask, open_interest}];
    quote_fn(symbol) -> {bid, ask, t} or None. Mutates state (high-water mark, last tick)."""
    p = params(cfg)
    hm = et_hm(now)
    state["last_tick"] = iso(now)
    if not cfg.get("paper", True):
        return {"action": "hold", "why": "the loop runs on the paper account only (\"paper\": false)"}
    if not cfg.get("allow_options"):
        return {"action": "hold", "why": "options are off in trader/config.json"}

    pend = state.get("pending")
    if pend:
        age = (now - parse_iso(pend["placed_at"])).total_seconds()
        if age >= FILL_WAIT_S:
            return {"action": "cancel", "order_id": pend.get("order_id"), "side": pend["side"],
                    "why": "the %s order has waited %ds without a recorded fill" % (pend["side"], age)}
        return {"action": "check-order", "order_id": pend.get("order_id"), "side": pend["side"],
                "why": "a %s order is working (%ds old)" % (pend["side"], age)}

    pos = state.get("position")
    if pos:
        q = quote_fn(pos["symbol"])
        hard = hm + 1 >= bt.hm(bt.HARD_CLOSE) or (cfg.get("flat_by") and hm >= bt.hm(cfg["flat_by"]))
        if not q or not q.get("bid"):
            if hard:
                return {"action": "sell", "symbol": pos["symbol"], "qty": pos["qty"], "limit": None,
                        "why": "hard close and no quote: get_option_latest_quote, then sell at the bid"}
            return {"action": "hold", "why": "no quote for %s this tick" % pos["symbol"]}
        bid, entry = float(q["bid"]), float(pos["entry"])
        if q.get("t"):
            try:
                age = (now - parse_iso(q["t"][:19] + "Z")).total_seconds()
                if age > STALE_QUOTE_S and not hard:
                    return {"action": "hold", "why": "the %s quote is %ds old" % (pos["symbol"], age)}
            except ValueError:
                pass
        pos["hwm"] = max(float(pos.get("hwm") or 0), bid)
        hwm = pos["hwm"]
        held = (now - parse_iso(pos["opened_at"])).total_seconds() / 60.0
        why = None
        if hard:
            why = "hard close %s" % bt.HARD_CLOSE
        elif bid <= entry * p["stop"]:
            why = "stop: bid %.2f is at or under %.0f%% of %.2f" % (bid, p["stop"] * 100, entry)
        elif p.get("be") and hwm >= entry * p["be"] and bid <= entry:
            why = "break-even: it reached %.2f, back to %.2f" % (hwm, bid)
        elif p.get("trail"):
            arm, pct, floor = p["trail"]
            if hwm >= entry * arm and bid <= hwm - max(floor, pct * hwm):
                why = "trail: high %.2f, bid %.2f" % (hwm, bid)
        if not why and held >= p["time"]:
            why = "time: held %.0f minutes" % held
        if why:
            return {"action": "sell", "symbol": pos["symbol"], "qty": pos["qty"], "limit": round(bid, 2),
                    "why": why}
        return {"action": "hold", "why": "holding %s: entry %.2f, bid %.2f, high %.2f, %.0f min"
                % (pos["symbol"], entry, bid, hwm, held)}

    # flat: may we enter?
    if hm > bt.hm(p["last_entry"]):
        return {"action": "done", "why": "entries end at %s New York; nothing open" % p["last_entry"]}
    if hm < bt.hm(p.get("first_entry", bt.FIRST_ENTRY)):
        return {"action": "hold", "why": "entries start at %s" % p.get("first_entry", bt.FIRST_ENTRY)}
    if state.get("trades_today", 0) >= p.get("max_per_day", 99):
        return {"action": "done", "why": "%d trades today, the most this rule set takes" % state["trades_today"]}
    lost = -sum(min(0.0, r.get("pnl", 0)) for r in state.get("results", []))
    if cfg.get("max_daily_loss_usd") and lost >= float(cfg["max_daily_loss_usd"]):
        return {"action": "done", "why": "realized loss today %.2f reached max_daily_loss_usd" % lost}
    if state.get("last_close"):
        since = (now - parse_iso(state["last_close"])).total_seconds() / 60.0
        if since < p["cooldown"]:
            return {"action": "hold", "why": "cooldown: %.1f of %d minutes since the last close" % (since, p["cooldown"])}
    bars = completed_bars(raw_bars, now)
    if len(bars) <= p["lookback"]:
        return {"action": "hold", "why": "%d bars so far" % len(bars)}
    last_t = parse_iso(bars[-1]["t"])
    if (now.replace(second=0) - last_t).total_seconds() > 60:
        return {"action": "hold", "why": "the last SPY bar (%s) is not the minute that just closed" % bars[-1]["t"]}
    side = bt.signal(bars, len(bars) - 1, p)
    if not side:
        m = bt.et_minutes(bars[-1]["t"])
        return {"action": "hold", "why": "no signal on the %02d:%02d ET bar" % (m // 60, m % 60)}
    level = bars[-1]["c"]
    near = bt.ROOTS["SPY"]["near"]
    cap = min(float(cfg.get("max_loss_per_trade_usd") or bt.MAX_COST), bt.MAX_COST)
    max_sp = float(cfg.get("max_option_spread_pct") or 10)
    min_oi = float(cfg.get("min_option_open_interest") or 100)
    best = None
    for c in chain_fn(side, level - near, level + near) or []:
        bid, ask = c.get("bid"), c.get("ask")
        if not bid or not ask or ask < bid:
            continue
        if 100.0 * (ask - bid) / ((ask + bid) / 2.0) > max_sp or ask * 100 > cap:
            continue
        if (c.get("open_interest") or 0) < min_oi:
            continue
        if best is None or ask > best["ask"]:
            best = c
    if not best:
        return {"action": "hold", "why": "%s signal, but no %s contract fits $%.0f with a tight spread"
                % ("call" if side == "C" else "put", "call" if side == "C" else "put", cap)}
    return {"action": "buy", "symbol": best["symbol"], "qty": 1, "bid": best["bid"], "ask": best["ask"],
            "limit": best["ask"], "open_interest": best.get("open_interest"),
            "why": "%s: SPY %.2f closed %s the prior %d minutes on volume"
            % ("call" if side == "C" else "put", level, "above" if side == "C" else "below", p["lookback"])}


def record(state, what, now, side=None, symbol=None, qty=None, limit=None, order_id=None, price=None):
    """Update the loop state after an order step. Returns a one-line summary."""
    if what == "placed":
        state["pending"] = {"side": side, "symbol": symbol or (state.get("position") or {}).get("symbol"),
                            "qty": int(qty or (state.get("position") or {}).get("qty") or 1),
                            "limit": float(limit) if limit else None, "order_id": order_id, "placed_at": iso(now)}
        return "pending %s %s" % (side, state["pending"]["symbol"])
    if what == "cancelled":
        pend = state.get("pending")
        state["pending"] = None
        return "cancelled the pending %s" % (pend or {}).get("side")
    if what == "filled":
        pend = state.get("pending") or {}
        side = side or pend.get("side")
        price = float(price)
        if side == "buy":
            state["position"] = {"symbol": symbol or pend.get("symbol"), "qty": int(qty or pend.get("qty") or 1),
                                 "entry": price, "opened_at": iso(now), "hwm": price}
            state["trades_today"] = state.get("trades_today", 0) + 1
            state["pending"] = None
            return "opened %s at %.2f" % (state["position"]["symbol"], price)
        pos = state.get("position")
        if not pos:
            raise SystemExit("tick.py: no open position to close")
        pnl = round((price - float(pos["entry"])) * 100 * int(pos["qty"]), 2)
        state.setdefault("results", []).append({"symbol": pos["symbol"], "entry": pos["entry"], "exit": price,
                                                "pnl": pnl, "at": iso(now)})
        state["position"] = None
        state["pending"] = None
        state["last_close"] = iso(now)
        return "closed %s at %.2f, P/L %+.2f" % (pos["symbol"], price, pnl)
    raise SystemExit("tick.py: unknown record %r" % what)


# ---------------------------------------------------------------- market data (read only)

def live_bars(today):
    import fetch
    return fetch.spy_bars(today)


def live_chain(today):
    import fetch
    exp = today.isoformat()

    def chain(kind, lo, hi):
        typ = "call" if kind == "C" else "put"
        snaps = fetch.get("/v1beta1/options/snapshots/%s" % UNDERLYING,
                          {"feed": "indicative", "expiration_date": exp, "type": typ,
                           "strike_price_gte": "%.0f" % lo, "strike_price_lte": "%.0f" % hi, "limit": 100})
        oi = {}
        try:
            cons = fetch.get("/v2/options/contracts", {"underlying_symbols": UNDERLYING, "expiration_date": exp,
                                                       "type": typ, "strike_price_gte": "%.0f" % lo,
                                                       "strike_price_lte": "%.0f" % hi, "limit": 100},
                             base=fetch.PAPER)
            oi = {c["symbol"]: float(c.get("open_interest") or 0) for c in cons.get("option_contracts", [])}
        except RuntimeError:
            pass
        out = []
        for sym, sn in (snaps.get("snapshots") or {}).items():
            q = sn.get("latestQuote") or {}
            m = bt.OCC.match(sym)
            if not m:
                continue
            out.append({"symbol": sym, "strike": int(m.group(4)) / 1000.0, "bid": q.get("bp"), "ask": q.get("ap"),
                        "open_interest": oi.get(sym)})
        return out
    return chain


def live_quote(symbol):
    import fetch
    d = fetch.get("/v1beta1/options/quotes/latest", {"symbols": symbol, "feed": "indicative"})
    q = (d.get("quotes") or {}).get(symbol)
    return {"bid": q.get("bp"), "ask": q.get("ap"), "t": q.get("t")} if q else None


# ---------------------------------------------------------------- what Claude does next

NEXT = {
    "buy": [
        "python3 trader-budget/options.py --symbol {symbol} --bid {bid} --ask {ask} --limit {limit} "
        "--open-interest {open_interest} --why \"spy-am: {why}\" --write",
        "python3 trader-risk/check.py   (must PASS)",
        "python3 trader-broker/ticket.py approve --index 1 --standing   (or ask the user for a yes)",
        "place_option_order with the ticket JSON, then ticket.py result --index 1 --status placed --broker-id ID",
        "python3 trader-exit/scalp/tick.py record placed --side buy --symbol {symbol} --qty 1 --limit {limit} --order-id ID",
    ],
    "sell": [
        "python3 trader-broker/ticket.py flatten --symbol {symbol} --qty {qty} --limit {limit}",
        "python3 trader-risk/check.py   (a closing sell is never blocked by the daily limits)",
        "python3 trader-broker/ticket.py approve --index 1 --standing   (or ask the user for a yes)",
        "place_option_order with the ticket JSON, then ticket.py result --index 1 --status placed --broker-id ID",
        "python3 trader-exit/scalp/tick.py record placed --side sell --order-id ID",
    ],
    "check-order": [
        "get_order_by_id {order_id}",
        "filled: ticket.py fill --symbol SYM --side {side} --qty Q --price P, then tick.py record filled --side {side} --price P",
        "canceled or rejected: tick.py record cancelled. Still open: nothing; the next tick looks again",
    ],
    "cancel": [
        "cancel_order_by_id {order_id}, then get_order_by_id {order_id}",
        "filled before the cancel: ticket.py fill ..., then tick.py record filled --side {side} --price P",
        "canceled: tick.py record cancelled (a sell is re-priced on the next tick)",
    ],
    "done": ["nothing more today: delete the loop's cron job (CronDelete)"],
    "hold": [],
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="One tick of the SPY morning-momentum loop. Never places orders.")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--now", help="UTC time like 2026-11-10T15:02:05Z (default: the clock)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("plan")
    sub.add_parser("status")
    r = sub.add_parser("record")
    r.add_argument("what", choices=["placed", "filled", "cancelled"])
    r.add_argument("--side", choices=["buy", "sell"])
    r.add_argument("--symbol")
    r.add_argument("--qty")
    r.add_argument("--limit")
    r.add_argument("--order-id")
    r.add_argument("--price")
    a = ap.parse_args(argv)
    now = parse_iso(a.now) if a.now else utc_now()
    today = et_date(now)
    cfg = read(a.config, {})
    state = load_state(a.state, today.isoformat())
    if a.cmd == "status" or not a.cmd:
        print(json.dumps(state, indent=1))
        return 0
    if a.cmd == "record":
        if a.what == "filled" and not a.price:
            raise SystemExit("tick.py: record filled needs --price")
        print(record(state, a.what, now, a.side, a.symbol, a.qty, a.limit, a.order_id, a.price))
        write(a.state, state)
        return 0
    p = params(cfg)
    print("spy-am tick %s ET | entries %s-%s, max %s/day, stop %.0f%%, time %d min | config: $%s/trade, "
          "$%s/day, paper %s" % (bt.datetime.datetime.combine(now.date(), now.time(), bt.UTC).astimezone(bt.ET)
                                 .strftime("%H:%M:%S"), p.get("first_entry"), p["last_entry"], p.get("max_per_day"),
                                 p["stop"] * 100, p["time"], cfg.get("max_loss_per_trade_usd"),
                                 cfg.get("max_daily_loss_usd"), cfg.get("paper", True)))
    need_bars = not state.get("pending") and not state.get("position")
    try:
        raw = live_bars(today) if need_bars else []
        act = decide(cfg, state, raw, now, live_chain(today), live_quote)
    except RuntimeError as e:
        act = {"action": "hold", "why": "market data error: %s" % e}
    write(a.state, state)
    for step in NEXT.get(act["action"], []):
        print("  next: " + step.format(**dict({"symbol": "", "qty": "", "limit": "", "bid": "", "ask": "",
                                               "open_interest": "", "order_id": "", "side": ""}, **act)))
    print(json.dumps(act))
    return 0


if __name__ == "__main__":
    sys.exit(main())
