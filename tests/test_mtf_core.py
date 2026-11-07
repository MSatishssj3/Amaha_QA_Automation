"""Tests for the mtf_core package.  Run:  python -m unittest discover tests"""

import json
import os
import re
import tempfile
import unittest

import mtf_core
from mtf_core import Stock, compare_platforms, interest_rate, max_position, plan

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHLIST = os.path.join(ROOT, "mtf_calculator_bot", "watchlist.csv")
HTML = os.path.join(ROOT, "mtf_calculator_bot", "mtf_calculator.html")

RELIANCE = Stock("RELIANCE", 1450, 25)


class MaxPositionTests(unittest.TestCase):
    def test_qty_fits_under_limit(self):
        p = max_position(RELIANCE, 1_00_000)
        self.assertEqual(p.qty, 91)                      # 91 * 1087.5 = 98,962.5
        self.assertAlmostEqual(p.borrowed, 98_962.5)
        self.assertAlmostEqual(p.trade_value, 1_31_950)
        self.assertAlmostEqual(p.own_margin, 32_987.5)
        self.assertLessEqual(p.borrowed, 1_00_000)
        self.assertGreater(p.borrowed + 1087.5, 1_00_000)  # one more share would not fit

    def test_exact_fit_is_allowed(self):
        p = max_position(Stock("X", 1000, 25), 75_000)   # 750 borrowed per share
        self.assertEqual(p.qty, 100)

    def test_capital_caps_quantity(self):
        p = max_position(RELIANCE, 1_00_000, capital=10_000)
        self.assertEqual(p.qty, 27)                      # 10,000 / 362.5

    def test_invalid_stocks_give_zero(self):
        for s in (Stock("A", 0, 25), Stock("B", 100, 100), Stock("C", 100, -5)):
            self.assertEqual(max_position(s, 1_00_000).qty, 0)

    def test_too_small_limit_gives_zero(self):
        self.assertEqual(max_position(Stock("ITC", 410, 25), 100).qty, 0)

    def test_rate_override(self):
        p = max_position(RELIANCE, 1_00_000, rate=10)
        self.assertEqual(p.rate, 10)
        self.assertAlmostEqual(p.interest_per_day, p.borrowed * 10 / 100 / 365)


class RateTests(unittest.TestCase):
    def test_pocketful_slabs(self):
        self.assertEqual(interest_rate(1_00_000, "Pocketful"), 5.99)
        self.assertEqual(interest_rate(1_00_001, "Pocketful"), 14.60)
        self.assertEqual(interest_rate(5_00_00_000, "Pocketful"), 16.00)

    def test_dhan_slabs(self):
        self.assertEqual(interest_rate(5_00_000, "Dhan"), 12.49)
        self.assertEqual(interest_rate(7_00_000, "Dhan"), 13.49)
        self.assertEqual(interest_rate(25_00_001, "Dhan"), 15.49)

    def test_next_slab(self):
        self.assertEqual(mtf_core.next_slab(50_000, "Pocketful").rate, 14.60)
        self.assertIsNone(mtf_core.next_slab(1_00_000, "Zerodha"))

    def test_unknown_platform(self):
        with self.assertRaises(KeyError):
            interest_rate(1000, "Nope")

    def test_register_platform(self):
        mtf_core.register_platform("TestBroker", [(50_000, 1.0), (None, 2.0)])
        try:
            self.assertEqual(interest_rate(60_000, "TestBroker"), 2.0)
            self.assertIn("TestBroker", [c.platform for c in compare_platforms(60_000)])
        finally:
            del mtf_core.PLATFORMS["TestBroker"]

    def test_bad_slabs_rejected(self):
        with self.assertRaises(ValueError):
            mtf_core.register_platform("Bad", [(50_000, 1.0)])           # no open slab
        with self.assertRaises(ValueError):
            mtf_core.register_platform("Bad", [(9, 1.0), (5, 2.0), (None, 3.0)])  # not increasing


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.stocks = mtf_core.load_watchlist(WATCHLIST)

    def test_each_stock_on_its_own(self):
        r = plan(self.stocks, 1_00_000)
        self.assertIsNone(r.totals)
        self.assertEqual(len(r.positions), len(self.stocks))
        self.assertTrue(all(p.borrowed <= 1_00_000 for p in r.positions))
        self.assertEqual(r.borrowed, 1_00_000)

    def test_split_keeps_total_under_limit_and_uses_book_rate(self):
        r = plan(self.stocks, 25_00_000, platform="Dhan", split=True)
        self.assertLessEqual(r.totals.borrowed, 25_00_000)
        self.assertEqual(r.totals.qty, 3544)
        self.assertEqual(r.totals.rate, 14.49)
        self.assertTrue(all(p.rate == 14.49 for p in r.positions))

    def test_calculator_wrapper_and_custom_fetcher(self):
        calc = mtf_core.MTFCalculator(platform="Zerodha", max_borrow=5_00_000,
                                      price_fetcher=lambda syms: {"RELIANCE": 2900.0})
        r = calc.plan(self.stocks)
        rel = next(p for p in r.positions if p.symbol == "RELIANCE")
        self.assertEqual(rel.price, 2900.0)
        self.assertEqual(rel.rate, 14.60)
        self.assertEqual(calc.compare(r)[0].platform, "Dhan")   # cheapest at Rs 5 lakh

    def test_to_dict(self):
        d = plan(self.stocks[:1], 1_00_000).to_dict()
        self.assertEqual(d["positions"][0]["qty"], 91)
        self.assertIn("interest_30d", d["positions"][0])


class DataTests(unittest.TestCase):
    def test_watchlist_round_trip(self):
        stocks = [Stock("ABC", 12.5, 20), Stock("XYZ", 100, 33.3)]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "w.csv")
            mtf_core.save_watchlist(stocks, path)
            self.assertEqual(mtf_core.load_watchlist(path), stocks)

    def test_with_live_prices_keeps_unpriced(self):
        out = mtf_core.with_live_prices([RELIANCE, Stock("TCS", 3100, 25)],
                                        fetcher=lambda s: {"TCS": 3200.0})
        self.assertEqual([s.price for s in out], [1450, 3200.0])


class FormattingTests(unittest.TestCase):
    def test_inr(self):
        self.assertEqual(mtf_core.inr(1234567.8), "12,34,567.80")
        self.assertEqual(mtf_core.inr(999), "999.00")
        self.assertEqual(mtf_core.inr(100000, 0), "1,00,000")

    def test_words(self):
        self.assertEqual(mtf_core.amount_in_words(150000), "1.5 lakh")
        self.assertEqual(mtf_core.amount_in_words(2_00_00_000), "2 crore")
        self.assertEqual(mtf_core.short_amount(4000), "4 K")


class HtmlInSyncTests(unittest.TestCase):
    """The browser page has its own copy of the rates; it must match rates.json."""

    def test_html_rates_match_package(self):
        with open(HTML, encoding="utf-8") as fh:
            src = fh.read()
        block = re.search(r"const PLATFORMS = \{(.*?)\n  \};", src, re.S).group(1)
        html = {}
        for name, slabs in re.findall(r"(\w+): \[(.*?)\],\n", block + "\n"):
            html[name] = [(None if u == "INF" else float(u), float(r))
                          for u, r in re.findall(r"\[(\w+), ([\d.]+)\]", slabs)]
        pkg = {n: [(s.upto, s.rate) for s in slabs] for n, slabs in mtf_core.PLATFORMS.items()}
        self.assertEqual(html, pkg)


if __name__ == "__main__":
    unittest.main()
