import tempfile
import unittest
from pathlib import Path

import pandas as pd

from datetime import date, datetime, timezone

import json

from bubble_detector.web import (
    build, is_early_close, market_holidays, market_is_open, next_update, public_json, render_html, summarise, zone,
)


class WebPageTest(unittest.TestCase):
    def test_drop_from_ath(self):
        idx = pd.bdate_range("2025-01-01", periods=300)
        closes = pd.Series([100.0] * 150 + [200.0] + [150.0] * 149, index=idx)
        with tempfile.TemporaryDirectory() as d:
            s = build(closes, Path(d))
            self.assertEqual(s["ath_close"], 200.0)
            self.assertEqual(s["drop_pct"], -25.0)
            page = (Path(d) / "index.html").read_text()
            self.assertIn("−25.00%", page)
            self.assertIn("50.00 points below the all-time high", page)  # no sign: "below" already says it
            self.assertNotIn("-50.00 points", page)
            data = json.loads((Path(d) / "data.json").read_text())
            self.assertNotIn("series", data)  # chart arrays stay in the page only
            self.assertEqual(data["zone"], "Bear market")
            # 149 of 300 days were 25% or more below the high
            self.assertEqual(data["history"]["share_pct"], 49.7)
            self.assertEqual(data["crashes"], [{"peak": "2025-07-30", "trough": "2025-07-31", "pct": -25.0,
                                                "recovered": None, "days_down": 1, "days_to_recover": None,
                                                "from_record": True}])
            self.assertEqual(data["needs_pct"], 33.33)  # 150 -> 200
            self.assertEqual(data["this_drop"]["high_date"], "2025-07-30")
            self.assertIsNone(data["this_drop"]["past"])  # never been this deep before
            self.assertEqual(data["day_move"]["pct"], 0.0)
            self.assertIn("never been this far below", page)
            by = {c["label"]: c for c in data["changes"]}
            self.assertIsNone(by["1 day"]["deepest_pct"])
            self.assertEqual(by["1 year"]["deepest_pct"], -25.0)

    def test_page_embeds_data_safely(self):
        idx = pd.bdate_range("2025-01-01", periods=10)
        s = summarise(pd.Series(range(1, 11), index=idx, dtype=float))
        s["index"] = "</script><b>x"
        page = render_html(s)
        self.assertNotIn("</script><b>", page)
        self.assertIn("&lt;/script&gt;&lt;b&gt;x", page)
        self.assertIn('<\\/script><b>x', page)  # inside the JSON block
        self.assertIn("series", json.loads(page.split('id="data">')[1].split("</script>")[0]))

    def test_past_drops(self):
        # 100 -> 80 (-20%) -> back to 100 after 10 days -> 110 record -> 99 now (-10%)
        vals = [100.0, 90.0, 80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 104.0, 99.0]
        idx = pd.bdate_range("2025-01-06", periods=len(vals))
        s = summarise(pd.Series(vals, index=idx))
        past = s["this_drop"]["past"]
        self.assertEqual(past["count"], 1)
        self.assertEqual(past["to_low_median_days"], 2)     # Mon 6 -> Wed 8 Jan
        self.assertEqual(past["recover_median_days"], 7)    # first at -10% Tue 7 -> back at 100 Tue 14 Jan
        self.assertEqual(s["crashes"][0]["days_to_recover"], 6)  # low Wed 8 -> high Tue 14 Jan
        page = render_html(s)
        self.assertIn("<b>1</b> earlier drops got this deep", page)
        self.assertIn("Since 2025, the index has been this far or further below its high on", page)
        self.assertIn('class="crash now"', page)

    def test_crash_below_an_older_record(self):
        # record 100, crash to 20 (-80%), climb to 60, crash to 30 (-50% from 60, like 2007-09), then a new record
        vals = [100.0, 50.0, 20.0, 40.0, 60.0, 45.0, 30.0, 50.0, 70.0, 90.0, 110.0]
        idx = pd.bdate_range("2025-01-06", periods=len(vals))
        crashes = summarise(pd.Series(vals, index=idx))["crashes"]
        self.assertEqual([(c["pct"], c["from_record"]) for c in crashes], [(-80.0, True), (-50.0, False)])
        self.assertEqual(crashes[1]["recovered"], "2025-01-16")  # back above 60 at 70 (Thu 16 Jan)
        page = render_html(summarise(pd.Series(vals, index=idx)))
        self.assertIn("Measured from its own high", page)

    def test_span(self):
        from bubble_detector.web import _span
        self.assertEqual(_span(1), "1 day")
        self.assertEqual(_span(19), "19 days")
        self.assertEqual(_span(168), "6 months")
        self.assertEqual(_span(943), "2.6 years")
        self.assertEqual(_span(5498), "15 years")

    def test_app_routes(self):
        import io
        from api.index import handler

        def get(path):
            h = handler.__new__(handler)
            h.path, h.request_version, h.wfile = path, "HTTP/1.1", io.BytesIO()
            h._headers_buffer = []
            h.command = "GET"
            h.requestline = "GET " + path
            h.client_address = ("127.0.0.1", 0)
            h.log_request = lambda *a, **k: None
            h._handle(head=False)
            return h.wfile.getvalue()

        self.assertIn(b'"display": "standalone"', get("/api/index?format=manifest"))
        self.assertIn(b"caches.open", get("/api/index?format=sw"))
        self.assertIn(b"\x89PNG", get("/api/index?format=png&size=192"))
        self.assertIn(b"404", get("/api/index?format=png&size=99").split(b"\r\n")[0])

    def test_zone(self):
        self.assertEqual(zone(0.0), "At the high")
        self.assertEqual(zone(-4.99), "Near the high")
        self.assertEqual(zone(-5.0), "Pullback")
        self.assertEqual(zone(-10.0), "Correction")
        self.assertEqual(zone(-19.99), "Correction")
        self.assertEqual(zone(-20.0), "Bear market")

    def test_holidays_2026(self):
        expected = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
                    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)}
        self.assertEqual(set(market_holidays(2026)), expected)
        self.assertNotIn(date(2021, 12, 31), market_holidays(2021))  # Saturday New Year's Day is not made up
        self.assertIn(date(2027, 12, 24), market_holidays(2027))       # Christmas on Saturday -> Friday off

    def test_market_hours(self):
        # 2026-11-05 is a Thursday; New York is UTC-5 in November.
        bar = pd.Timestamp("2026-11-05")
        utc = lambda h, m: datetime(2026, 11, 5, h, m, tzinfo=timezone.utc)
        self.assertFalse(market_is_open(bar, utc(14, 29)))   # 09:29 NY
        self.assertTrue(market_is_open(bar, utc(14, 30)))    # 09:30 NY
        self.assertFalse(market_is_open(bar, utc(21, 0)))    # 16:00 NY
        self.assertFalse(market_is_open(pd.Timestamp("2026-11-04"), utc(15, 0)))  # no bar today (holiday)

    def test_at_ath_copy(self):
        idx = pd.bdate_range("2025-01-01", periods=10)
        page = render_html(summarise(pd.Series(range(1, 11), index=idx, dtype=float)))
        self.assertIn("At the all-time high of 10.00", page)
        self.assertIn('href="https://github.com/is0xjh25/bubble-detector"', page)
        self.assertIn("built by <b>is0xjh25</b>", page)
        self.assertIn("Not financial advice.", page)

    def test_early_close(self):
        self.assertTrue(is_early_close(date(2026, 11, 27)))   # day after Thanksgiving
        self.assertTrue(is_early_close(date(2026, 12, 24)))   # Christmas Eve, Thursday
        self.assertFalse(is_early_close(date(2026, 11, 26)))  # Thanksgiving itself (full holiday, no bar)
        self.assertFalse(is_early_close(date(2026, 11, 20)))
        bar = pd.Timestamp("2026-11-27")
        self.assertTrue(market_is_open(bar, datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc)))   # 12:59 NY
        self.assertFalse(market_is_open(bar, datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)))   # 13:00 NY

    def test_next_update(self):
        utc = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)
        self.assertEqual(next_update(utc(5, 10, 2)), utc(5, 14, 30))   # 05:02 NY -> the 09:30 open
        self.assertEqual(next_update(utc(5, 15, 2)), utc(5, 16, 0))    # in session -> next hour
        self.assertEqual(next_update(utc(5, 21, 30)), utc(5, 22, 0))   # 16:30 NY -> one more after the close
        self.assertEqual(next_update(utc(5, 22, 0)), utc(6, 14, 30))   # 17:00 NY -> Friday's open
        self.assertEqual(next_update(utc(6, 23, 0)), utc(9, 14, 30))   # Friday evening -> Monday's open
        self.assertEqual(next_update(utc(27, 18, 30)), utc(27, 19, 0))  # half day: 13:30 NY still updating
        self.assertEqual(next_update(utc(27, 19, 0)), utc(30, 14, 30))  # 14:00 NY -> Monday's open
        self.assertEqual(next_update(utc(25, 22, 0)), utc(27, 14, 30))  # skips Thanksgiving (Thu 26th)
        self.assertFalse(market_is_open(pd.Timestamp("2026-11-26"), utc(26, 15, 0)))  # holiday even with a bar


if __name__ == "__main__":
    unittest.main()
