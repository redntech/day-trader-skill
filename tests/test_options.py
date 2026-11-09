import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

options = load("trader-budget", "options")
risk = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

SYM = "CRWD261120C00225000"
TODAY = "2026-11-09"
NOW = datetime.datetime(2026, 11, 9, 15, 30)      # 10:30 New York
CFG = {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000,
       "min_order_usd": 5, "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150,
       "max_trades_per_day": 3, "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
       "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55", "paper": True,
       "allow_options": True, "allow_margin": False, "allow_short": False, "blocked": []}


def sized(**kw):
    args = dict(symbol=SYM, bid=0.40, ask=0.42, open_interest=900, today=TODAY)
    args.update(kw)
    return options.size_option(CFG, **args)


class Sizing(unittest.TestCase):
    def test_contract_parsed(self):
        c = options.parse_contract(SYM)
        self.assertEqual((c["underlying"], c["right"], c["strike"]), ("CRWD", "call", 225.0))
        self.assertEqual(options.words(c), "CRWD Nov 20 2026 225 call")
        with self.assertRaises(options.OptionError):
            options.parse_contract("CRWD")

    def test_size_is_bound_by_the_whole_premium(self):
        r = sized()                       # 0.42 x 100 = 42 a contract, $50 max loss -> 1
        self.assertTrue(r["ok"])
        self.assertEqual(r["qty"], 1)
        self.assertEqual(r["max_loss"], 42.0)
        r = sized(bid=0.10, ask=0.11)     # 11 a contract -> 4
        self.assertEqual(r["qty"], 4)
        self.assertEqual(r["max_loss"], 44.0)

    def test_one_contract_over_the_loss_limit_is_a_refusal(self):
        r = sized(bid=2.40, ask=2.50)     # 250 a contract
        self.assertFalse(r["ok"])
        self.assertIn("one contract costs $250.00", r["reason"])

    def test_wide_spread_thin_interest_and_near_expiry_refused(self):
        self.assertIn("spread", sized(bid=0.20, ask=0.42)["reason"])
        self.assertIn("open interest", sized(open_interest=20)["reason"])
        self.assertIn("open interest", sized(open_interest=None)["reason"])
        self.assertIn("expires", sized(today="2026-11-20")["reason"])
        self.assertIn("overpay", sized(limit=0.50)["reason"])

    def test_options_off_by_default(self):
        r = options.size_option({"budget": 5000}, SYM, 0.4, 0.42, 900, today=TODAY)
        self.assertFalse(r["ok"])
        self.assertIn("options are off", r["reason"])

    def test_order_shape(self):
        o = options.order_from(sized())
        self.assertEqual((o["side"], o["qty"], o["type"], o["limit_price"], o["time_in_force"],
                          o["position_intent"]), ("buy", "1", "limit", "0.42", "day", "buy_to_open"))
        self.assertNotIn("order_class", o)
        self.assertEqual(o["plan"]["max_loss"], 42.0)

    def test_cli_writes_orders(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp, outp = os.path.join(d, "c.json"), os.path.join(d, "o.json")
            json.dump(CFG, open(cfgp, "w"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = options.main(["--symbol", SYM, "--bid", "0.40", "--ask", "0.42", "--open-interest", "900",
                                   "--config", cfgp, "--portfolio", os.path.join(d, "none.json"),
                                   "--orders", outp, "--now", "2026-11-09 10:15", "--write"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.load(open(outp))["orders"][0]["symbol"], SYM)


def order(**over):
    o = options.order_from(sized())
    o.update(over)
    return o


def check(o, cfg=None, state=None, portfolio=None):
    c = dict(CFG)
    c.update(cfg or {})
    return risk.check([o], c, portfolio=portfolio, state=state, today=TODAY, now=NOW)["results"][0]


class RiskGate(unittest.TestCase):
    def test_good_option_passes_with_premium_as_max_loss(self):
        r = check(order())
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["max_loss"], 42.0)
        self.assertEqual(r["value_usd"], 42.0)

    def test_blocked_when_options_off(self):
        r = check(order(), {"allow_options": False})
        self.assertEqual(r["verdict"], "BLOCK")

    def test_loss_over_the_limit_blocks(self):
        r = check(order(qty="2"))
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("loss:") for x in r["reasons"]))

    def test_market_order_bracket_and_gtc_blocked(self):
        for over in ({"type": "market", "limit_price": None}, {"time_in_force": "gtc"},
                     {"order_class": "bracket", "stop_loss_stop_price": "0.20", "take_profit_limit_price": "0.80"},
                     {"qty": "1.5"}):
            r = check(order(**over))
            self.assertEqual(r["verdict"], "BLOCK", over)

    def test_liquidity_and_expiry_blocked(self):
        o = order()
        o["plan"]["bid"] = 0.20
        self.assertTrue(any("spread" in x for x in check(o)["reasons"]))
        o = order()
        o["plan"]["open_interest"] = 5
        self.assertTrue(any("open interest" in x for x in check(o)["reasons"]))
        o = order(symbol="CRWD261109C00225000")
        self.assertTrue(any("expires" in x for x in check(o)["reasons"]))
        o = order()
        del o["plan"]["bid"]
        self.assertEqual(check(o)["verdict"], "BLOCK")

    def test_selling_to_open_blocked_selling_to_close_is_an_exit(self):
        r = check(order(side="sell"))
        self.assertEqual(r["verdict"], "BLOCK")
        pf = {"cash": 4958, "positions": [{"ticker": SYM, "shares": 1,
                                           "lots": [{"id": "a", "date": TODAY, "shares": 1, "cost": 42.0}]}]}
        o = {"symbol": SYM, "side": "sell", "qty": "1", "type": "limit", "limit_price": "0.60",
             "time_in_force": "day", "position_intent": "sell_to_close"}
        r = check(o, portfolio=pf)
        self.assertEqual(r["kind"], "exit")
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertIn("1 contract", r["amount"])

    def test_option_sell_needs_sell_to_close_even_when_held(self):
        # a stale portfolio.json must not turn a plain sell into a written option at Alpaca
        pf = {"cash": 4958, "positions": [{"ticker": SYM, "shares": 1,
                                           "lots": [{"id": "a", "date": TODAY, "shares": 1, "cost": 42.0}]}]}
        base = {"symbol": SYM, "side": "sell", "qty": "1", "type": "limit", "limit_price": "0.60",
                "time_in_force": "day"}
        for intent in (None, "sell_to_open"):
            o = dict(base, position_intent=intent) if intent else dict(base)
            r = check(o, portfolio=pf)
            self.assertEqual(r["verdict"], "BLOCK", intent)
            self.assertTrue(any("sell_to_close" in x for x in r["reasons"]), r["reasons"])

    def test_option_buy_to_close_blocked(self):
        r = check(order(position_intent="buy_to_close"))
        self.assertEqual(r["verdict"], "BLOCK")

    def test_counts_toward_the_daily_loss(self):
        state = {"orders": [{"status": "placed", "date": TODAY, "symbol": "AAA", "side": "buy",
                             "kind": "entry", "max_loss": 120.0}]}
        r = check(order(), state=state)
        self.assertTrue(any(x.startswith("daily loss:") for x in r["reasons"]))


class Ticket(unittest.TestCase):
    def test_ticket_words_tool_and_max_loss(self):
        o = order()
        text = ticket.plain(o)
        self.assertIn("1 contract of %s (CRWD Nov 20 2026 225 call)" % SYM, text)
        self.assertIn("max loss $42", text)
        self.assertEqual(ticket.tool_name(o), "place_option_order")
        self.assertEqual(ticket.tool_name({"symbol": "TSLA"}), "place_stock_order")
        ti = ticket.tool_input(o)
        self.assertEqual(ti["position_intent"], "buy_to_open")
        self.assertNotIn("why", ti)
        self.assertEqual(ticket.max_loss(o), (42.0, None))

    def test_fingerprints_match_between_risk_and_ticket(self):
        o = order()
        self.assertEqual(risk.fingerprint(o), ticket.fingerprint(o))

    def test_fills_use_the_100_multiplier(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pf.json")
            json.dump({"cash": 5000.0, "positions": []}, open(p, "w"))
            pf = ticket.apply_fill(p, CFG, SYM, "buy", 2, 0.42, TODAY)
            self.assertEqual(pf["cash"], 4916.0)
            pf = ticket.apply_fill(p, CFG, SYM, "sell", 2, 0.60, TODAY)
            self.assertEqual(pf["cash"], 5036.0)
            self.assertEqual(pf["positions"], [])

    def test_flatten_option_needs_a_limit_and_builds_sell_to_close(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            pf = os.path.join(d, "pf.json")
            json.dump({"cash": 1, "positions": [{"ticker": SYM, "shares": 3}]}, open(pf, "w"))
            err = io.StringIO()
            with redirect_stderr(err):
                rc = ticket.main(["flatten", "--symbol", SYM, "--orders", out, "--portfolio", pf,
                                  "--plan", os.path.join(d, "x.json"), "--config", os.path.join(d, "y.json")])
            self.assertEqual(rc, 1)
            self.assertIn("--limit", err.getvalue())
            with redirect_stdout(io.StringIO()):
                rc = ticket.main(["flatten", "--symbol", SYM, "--limit", "0.55", "--orders", out, "--portfolio", pf,
                                  "--plan", os.path.join(d, "x.json"), "--config", os.path.join(d, "y.json")])
            self.assertEqual(rc, 0)
            o = json.load(open(out))["orders"][0]
            self.assertEqual((o["side"], o["qty"], o["type"], o["limit_price"], o["position_intent"]),
                             ("sell", "3", "limit", "0.55", "sell_to_close"))


if __name__ == "__main__":
    unittest.main()
