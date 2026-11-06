import tempfile
import unittest
from pathlib import Path

import pandas as pd

from datetime import datetime, timezone

from bubble_detector.web import build, market_is_open, next_update


class WebPageTest(unittest.TestCase):
    def test_drop_from_ath(self):
        idx = pd.bdate_range("2025-01-01", periods=300)
        closes = pd.Series([100.0] * 150 + [200.0] + [150.0] * 149, index=idx)
        with tempfile.TemporaryDirectory() as d:
            s = build(closes, Path(d))
            self.assertEqual(s["ath_close"], 200.0)
            self.assertEqual(s["drop_pct"], -25.0)
            self.assertIn("−25.00%", (Path(d) / "index.html").read_text())
            self.assertTrue((Path(d) / "data.json").exists())

    def test_market_hours(self):
        # 2026-11-05 is a Thursday; New York is UTC-5 in November.
        bar = pd.Timestamp("2026-11-05")
        utc = lambda h, m: datetime(2026, 11, 5, h, m, tzinfo=timezone.utc)
        self.assertFalse(market_is_open(bar, utc(14, 29)))   # 09:29 NY
        self.assertTrue(market_is_open(bar, utc(14, 30)))    # 09:30 NY
        self.assertFalse(market_is_open(bar, utc(21, 0)))    # 16:00 NY
        self.assertFalse(market_is_open(pd.Timestamp("2026-11-04"), utc(15, 0)))  # no bar today (holiday)

    def test_next_update(self):
        self.assertEqual(next_update(datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc)),
                         datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(next_update(datetime(2026, 11, 5, 10, 59, tzinfo=timezone.utc)),
                         datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
