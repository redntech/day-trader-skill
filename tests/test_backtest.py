import unittest

from _load import load

bt = load("trader-exit/backtest", "backtest")


def bar(t, o, h, l, c, v=100):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def minute(m):
    """UTC bar time for 10:00 + m minutes New York (EST, UTC-5)."""
    return "2026-11-09T%02d:%02d:00Z" % (15 + (m // 60), m % 60)


class Signal(unittest.TestCase):
    P = dict(bt.VARIANTS["v2"])

    def flat(self, n=8):
        return [bar(minute(i), 100, 100.5, 99.5, 100, 100) for i in range(n)]

    def test_call_on_breakout_with_volume(self):
        spy = self.flat() + [bar(minute(8), 100, 101, 100, 100.8, 200)]
        self.assertEqual(bt.signal(spy, 8, self.P), "C")

    def test_no_signal_without_volume(self):
        spy = self.flat() + [bar(minute(8), 100, 101, 100, 100.8, 110)]
        self.assertIsNone(bt.signal(spy, 8, self.P))

    def test_put_on_breakdown(self):
        spy = self.flat() + [bar(minute(8), 100, 100, 99, 99.2, 200)]
        self.assertEqual(bt.signal(spy, 8, self.P), "P")


class Exits(unittest.TestCase):
    def setUp(self):
        self.spy = [bar(minute(i), 0, 0, 0, 0) for i in range(20)]

    def opts(self, closes, highs=None):
        highs = highs or closes
        return {"X": {"kind": "C", "strike": 7590.0,
                      "bars": {minute(i): bar(minute(i), c, h, c, c) for i, (c, h) in enumerate(zip(closes, highs))}}}

    def test_tick_up(self):
        self.assertEqual(bt.tick_up(2.275), 2.30)
        self.assertEqual(bt.tick_up(3.01), 3.10)
        self.assertEqual(bt.tick_up(1.50), 1.50)

    def test_take_profit_fills_at_limit(self):
        opts = self.opts([1.0, 1.1, 1.2], highs=[1.0, 1.1, 1.35])
        px, why, _ = bt.simulate(self.spy, opts, 0, "X", 1.0, bt.VARIANTS["v1"], 0.0)
        self.assertEqual((px, why), (1.30, "take-profit"))

    def test_stop_uses_minute_close_less_half_spread(self):
        opts = self.opts([1.0, 0.9, 0.6])
        px, why, _ = bt.simulate(self.spy, opts, 0, "X", 1.0, bt.VARIANTS["v1"], 0.02)
        self.assertEqual(why, "stop")
        self.assertAlmostEqual(px, 0.6 * 0.98)

    def test_break_even_after_ten_percent(self):
        opts = self.opts([1.0, 1.12, 0.99])
        px, why, _ = bt.simulate(self.spy, opts, 0, "X", 1.0, bt.VARIANTS["v3"], 0.0)
        self.assertEqual(why, "break-even")

    def test_time_stop(self):
        opts = self.opts([1.0] * 12)
        _, why, j = bt.simulate(self.spy, opts, 0, "X", 1.0, bt.VARIANTS["v3"], 0.0)
        self.assertEqual((why, j), ("time", 5))


class Realism(unittest.TestCase):
    def test_densify_fills_missing_minutes_with_zero_volume(self):
        spy = [bar("2026-11-09T14:30:00Z", 1, 1, 1, 1), bar("2026-11-09T14:33:00Z", 2, 2, 2, 2)]
        d = bt.densify(spy)
        self.assertEqual(len(d), 390)
        self.assertEqual([b["v"] for b in d[1:3]], [0, 0])
        self.assertEqual(d[2]["c"], 1)
        self.assertEqual(d[3]["c"], 2)

    def opt(self, times_prices):
        bars = {t: bar(t, p, p, p, p) for t, p in times_prices}
        return {"X": {"kind": "C", "strike": 600.0, "bars": bars, "times": sorted(bars)}}

    def test_pick_uses_signal_minute_and_fills_only_under_limit(self):
        # traded at the signal minute at 1.00 -> limit 1.05; next minute opens at 1.02 -> filled at the ask
        o = self.opt([(minute(0), 1.00), (minute(1), 1.02)])
        self.assertEqual(bt.pick_contract(o, "C", 600, 4, minute(0), minute(1), 0.0, 0.05), ("X", 1.02))
        # next minute jumps past the limit -> no fill
        o = self.opt([(minute(0), 1.00), (minute(1), 1.20)])
        self.assertIsNone(bt.pick_contract(o, "C", 600, 4, minute(0), minute(1), 0.0, 0.05))
        # no trade known by the signal minute -> not picked, even though it trades next minute
        o = self.opt([(minute(1), 1.00)])
        self.assertIsNone(bt.pick_contract(o, "C", 600, 4, minute(0), minute(1), 0.0, 0.05))

    def test_fade_flips_the_signal(self):
        spy = [bar(minute(i), 100, 100.5, 99.5, 100, 100) for i in range(8)] + [bar(minute(8), 100, 101, 100, 100.8, 200)]
        self.assertEqual(bt.signal(spy, 8, dict(bt.VARIANTS["v2"], fade=True)), "P")

    def test_opening_range_breakout_fires_once_on_the_cross(self):
        spy = [bar(minute(i), 100, 101, 99, 100) for i in range(15)]
        spy += [bar(minute(15), 100, 101.5, 100, 101.2), bar(minute(16), 101, 102, 101, 101.8)]
        p = dict(signal="orb", orb=15)
        self.assertEqual(bt.signal(spy, 15, p), "C")
        self.assertIsNone(bt.signal(spy, 16, p))


if __name__ == "__main__":
    unittest.main()
