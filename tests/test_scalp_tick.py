import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load

tick = load("trader-exit/scalp", "tick")
ticket = load("trader-broker", "ticket")
risk = load("trader-risk", "check")

CFG = {"paper": True, "allow_options": True, "max_loss_per_trade_usd": 200, "max_daily_loss_usd": 500,
       "max_option_spread_pct": 10, "min_option_open_interest": 100, "flat_by": "15:55"}
DAY = "2026-11-10"          # a Tuesday, EST (UTC-5)
SYM = "SPY261110C00600000"


def utc(hh, mm, ss=0):
    """New York wall time on DAY -> naive UTC."""
    return datetime.datetime(2026, 11, 10, hh + 5, mm, ss)


def bar(dt, o, h, l, c, v):
    return {"t": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": o, "h": h, "l": l, "c": c, "v": v}


def bars_until(hh, mm, breakout=None):
    """Flat SPY bars from 09:30 to (hh:mm - 1); optionally the last one breaks out on volume."""
    out, t = [], utc(9, 30)
    end = utc(hh, mm)
    while t < end:
        out.append(bar(t, 600, 600.2, 599.8, 600, 1000))
        t += datetime.timedelta(minutes=1)
    if breakout:
        b = out[-1]
        if breakout == "C":
            b.update(h=601, c=600.9, v=3000)
        else:
            b.update(l=599, c=599.1, v=3000)
    return out


def chain(contracts):
    return lambda kind, lo, hi: [c for c in contracts if c["symbol"][9] == kind]


def no_quote(sym):
    return None


GOOD = {"symbol": SYM, "strike": 600.0, "bid": 1.58, "ask": 1.62, "open_interest": 5000}
PRICEY = {"symbol": "SPY261110C00598000", "strike": 598.0, "bid": 2.50, "ask": 2.55, "open_interest": 5000}
WIDE = {"symbol": "SPY261110C00601000", "strike": 601.0, "bid": 0.80, "ask": 1.20, "open_interest": 5000}


def flat_state():
    return tick.fresh_state(DAY)


class Entry(unittest.TestCase):
    def test_buys_the_priciest_tight_contract_under_the_cap_on_a_breakout(self):
        act = tick.decide(CFG, flat_state(), bars_until(10, 5, "C"), utc(10, 5, 5),
                          chain([GOOD, PRICEY, WIDE]), no_quote)
        self.assertEqual(act["action"], "buy")
        self.assertEqual((act["symbol"], act["limit"]), (SYM, 1.62))

    def test_no_signal_holds(self):
        act = tick.decide(CFG, flat_state(), bars_until(10, 5), utc(10, 5, 5), chain([GOOD]), no_quote)
        self.assertEqual(act["action"], "hold")

    def test_window_cap_and_cooldown(self):
        self.assertEqual(tick.decide(CFG, flat_state(), bars_until(9, 45, "C"), utc(9, 45, 5),
                                     chain([GOOD]), no_quote)["action"], "hold")
        self.assertEqual(tick.decide(CFG, flat_state(), bars_until(11, 2, "C"), utc(11, 2, 5),
                                     chain([GOOD]), no_quote)["action"], "done")
        s = flat_state()
        s["trades_today"] = 3
        self.assertEqual(tick.decide(CFG, s, bars_until(10, 5, "C"), utc(10, 5, 5), chain([GOOD]),
                                     no_quote)["action"], "done")
        s = flat_state()
        s["last_close"] = tick.iso(utc(10, 2))
        self.assertIn("cooldown", tick.decide(CFG, s, bars_until(10, 5, "C"), utc(10, 5, 5), chain([GOOD]),
                                              no_quote)["why"])

    def test_stale_signal_bar_is_ignored(self):
        # the breakout bar closed at 10:04 but it is now 10:07: the loop missed it
        act = tick.decide(CFG, flat_state(), bars_until(10, 5, "C"), utc(10, 7, 5), chain([GOOD]), no_quote)
        self.assertEqual(act["action"], "hold")

    def test_live_or_options_off_never_trades(self):
        for cfg in (dict(CFG, paper=False), dict(CFG, allow_options=False)):
            act = tick.decide(cfg, flat_state(), bars_until(10, 5, "C"), utc(10, 5, 5), chain([GOOD]), no_quote)
            self.assertEqual(act["action"], "hold")


class Exits(unittest.TestCase):
    def state(self, entry=1.60, opened=(10, 5), hwm=None):
        s = flat_state()
        s["position"] = {"symbol": SYM, "qty": 1, "entry": entry, "opened_at": tick.iso(utc(*opened)),
                         "hwm": hwm or entry}
        s["trades_today"] = 1
        return s

    def at(self, s, bid, hh=10, mm=10):
        return tick.decide(CFG, s, [], utc(hh, mm), chain([]), lambda sym: {"bid": bid, "ask": bid + 0.02})

    def test_stop_break_even_trail_time_and_hold(self):
        self.assertIn("stop", self.at(self.state(), 1.10)["why"])
        self.assertIn("break-even", self.at(self.state(hwm=1.80), 1.60)["why"])
        s = self.state(hwm=2.40)
        self.assertIn("trail", self.at(s, 2.00)["why"])
        self.assertEqual(self.at(self.state(), 1.65, 11, 6)["why"][:4], "time")
        act = self.at(self.state(), 1.70)
        self.assertEqual(act["action"], "hold")

    def test_sell_uses_the_bid_and_hwm_is_kept(self):
        s = self.state()
        self.at(s, 1.90)
        self.assertEqual(s["position"]["hwm"], 1.90)
        act = self.at(s, 1.10)
        self.assertEqual((act["action"], act["limit"]), ("sell", 1.10))

    def test_pending_order_is_checked_then_cancelled(self):
        s = flat_state()
        tick.record(s, "placed", utc(10, 5), side="buy", symbol=SYM, qty=1, limit=1.62, order_id="abc")
        self.assertEqual(tick.decide(CFG, s, [], utc(10, 5, 30), chain([]), no_quote)["action"], "check-order")
        self.assertEqual(tick.decide(CFG, s, [], utc(10, 6, 5), chain([]), no_quote)["action"], "cancel")

    def test_record_round_trip(self):
        s = flat_state()
        tick.record(s, "placed", utc(10, 5), side="buy", symbol=SYM, qty=1, limit=1.62, order_id="a")
        tick.record(s, "filled", utc(10, 5, 20), side="buy", price=1.62)
        self.assertEqual((s["position"]["entry"], s["trades_today"], s["pending"]), (1.62, 1, None))
        tick.record(s, "placed", utc(10, 30), side="sell", order_id="b")
        tick.record(s, "filled", utc(10, 30, 10), side="sell", price=2.10)
        self.assertIsNone(s["position"])
        self.assertEqual(s["results"][0]["pnl"], 48.0)
        self.assertEqual(s["last_close"], tick.iso(utc(10, 30, 10)))

    def test_new_day_keeps_an_open_position(self):
        old = flat_state()
        old["position"] = {"symbol": SYM, "qty": 1, "entry": 1.0, "opened_at": "x", "hwm": 1.0}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            tick.write(p, old)
            s = tick.load_state(p, "2026-11-11")
        self.assertEqual(s["position"]["symbol"], SYM)
        self.assertEqual(s["trades_today"], 0)


class Standing(unittest.TestCase):
    """ticket.py authorize / approve --standing / revoke."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "config.json")
        with open(self.cfg, "w") as fh:
            json.dump(dict(CFG, max_orders_per_day=100), fh)
        self.std = os.path.join(self.d, "standing.json")

    def run_t(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = ticket.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def authorize(self, reply="yes", until="11:30", n="4", usd="400", now="2026-11-10 09:40"):
        return self.run_t(["authorize", "--reply", reply, "--underlying", "SPY", "--until", until,
                           "--max-orders", n, "--max-loss-usd", usd, "--config", self.cfg,
                           "--standing", self.std, "--now", now])

    def order(self, **over):
        o = {"symbol": SYM, "side": "buy", "qty": "1", "type": "limit", "limit_price": "1.62",
             "time_in_force": "day", "position_intent": "buy_to_open",
             "plan": {"kind": "option", "premium": 1.62, "max_loss": 162.0}}
        o.update(over)
        return o

    def ok(self, order, state=None, now=utc(10, 5)):
        st = json.load(open(self.std)) if os.path.exists(self.std) else None
        return ticket.standing_ok(order, {"paper": True}, st, state or {"orders": []}, DAY, now)

    def test_needs_a_real_yes_and_sane_bounds(self):
        self.assertEqual(self.authorize(reply="sure")[0], 1)
        self.assertEqual(self.authorize(reply="yes but only calls")[0], 1)
        self.assertFalse(os.path.exists(self.std))
        self.assertNotEqual(self.authorize(usd="900")[0], 0)        # over max_daily_loss_usd
        self.assertNotEqual(self.authorize(until="16:30")[0], 0)    # after flat_by
        self.assertEqual(self.authorize()[0], 0)

    def test_covers_only_matching_option_orders(self):
        self.authorize()
        self.assertTrue(self.ok(self.order())[0])
        self.assertTrue(self.ok(self.order(side="sell", position_intent="sell_to_close"))[0])
        for bad in (self.order(symbol="TSLA"), self.order(symbol="QQQ261110C00500000"),
                    self.order(side="sell", position_intent="sell_to_open"), self.order(type="market"),
                    self.order(position_intent="")):
            self.assertFalse(self.ok(bad)[0], bad)
        self.assertFalse(self.ok(self.order(), now=utc(11, 31))[0])           # past --until

    def test_order_count_and_premium_caps(self):
        self.authorize(n="2", usd="300")
        used = {"orders": [{"standing": True, "status": "approved", "date": DAY, "side": "buy", "max_loss": 162.0}]}
        ok, why = self.ok(self.order(), used)
        self.assertFalse(ok)
        self.assertIn("premium", why)
        used["orders"].append({"standing": True, "status": "approved", "date": DAY, "side": "sell"})
        self.assertIn("orders today", self.ok(self.order(side="sell", position_intent="sell_to_close"), used)[1])

    def test_revoke_and_other_day(self):
        self.authorize()
        st = json.load(open(self.std))
        self.assertFalse(ticket.standing_ok(self.order(), {"paper": True}, st, {"orders": []}, "2026-11-11",
                                            utc(10, 5))[0])
        self.run_t(["revoke", "--standing", self.std, "--now", "2026-11-10 10:00"])
        self.assertIn("revoked", self.ok(self.order())[1])

    def test_live_account_refused(self):
        with open(self.cfg, "w") as fh:
            json.dump(dict(CFG, paper=False), fh)
        self.assertNotEqual(self.authorize()[0], 0)


class RebuyAfterClose(unittest.TestCase):
    def test_second_buy_of_a_closed_option_is_not_a_duplicate(self):
        today = "2026-11-10"
        base = [{"status": "placed", "date": today, "symbol": SYM, "side": "buy", "kind": "entry", "max_loss": 162.0},
                {"status": "filled", "date": today, "symbol": SYM, "side": "buy", "qty": "1", "price": 1.62}]
        o = {"symbol": SYM, "side": "buy", "qty": "1", "type": "limit", "limit_price": "1.62",
             "time_in_force": "day", "position_intent": "buy_to_open",
             "plan": {"kind": "option", "bid": 1.58, "ask": 1.62, "open_interest": 5000}}
        cfg = dict(CFG, max_trades_per_day=30, max_orders_per_day=100, budget=5000, min_reward_risk=0,
                   require_stop=False, no_entries_before="09:30", no_entries_after="15:30", min_option_dte_days=0)
        now = utc(10, 40)
        dup = lambda st: [x for x in risk.check([o], cfg, state=st, today=today, now=now)["results"][0]["reasons"]
                          if x.startswith("duplicate")]
        self.assertTrue(dup({"orders": list(base)}))           # still open: a real duplicate
        closed = base + [{"status": "filled", "date": today, "symbol": SYM, "side": "sell", "qty": "1", "price": 1.20}]
        self.assertFalse(dup({"orders": closed}))


if __name__ == "__main__":
    unittest.main()
