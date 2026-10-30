"""Unit tests for the calculation layer. Run with:  python -m unittest -v
No network access: market data is synthetic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from bubble_detector.calc import (
    compute_action, compute_changes, compute_ladder, compute_recovery, compute_sma,
    compute_snapshot, history_columns,
)
from bubble_detector.data import MarketData
from bubble_detector.report import (append_history, previous_logged_row, prune_reports, read_history,
                                    render_console, render_html)


def series(values, start="2018-01-01") -> pd.Series:
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(np.asarray(values, dtype="float64"), index=idx)


def random_walk(n=2200, seed=3, drift=0.0004, vol=0.01, start=10_000.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    return series(start * np.exp(np.cumsum(rng.normal(drift, vol, n))))


def md(closes: pd.Series, etfs=None) -> MarketData:
    return MarketData("^IXIC", closes, etfs or {})


class LadderTests(unittest.TestCase):
    def test_levels_and_fired(self):
        rows = compute_ladder(last_close=7_400.0, ath_close=10_000.0)
        for row, expected in zip(rows, [7_500.0, 6_000.0, 4_500.0, 3_000.0]):
            self.assertAlmostEqual(row.level, expected, places=6)
        self.assertEqual([r.fired for r in rows], [True, False, False, False])
        self.assertAlmostEqual(rows[1].pct_to_fall, (6_000 / 7_400 - 1) * 100, places=2)
        self.assertAlmostEqual(rows[0].points_from_close, 100.0)

    def test_fires_exactly_at_level(self):
        rows = compute_ladder(last_close=7_500.0, ath_close=10_000.0)
        self.assertTrue(rows[0].fired)


class ChangeTests(unittest.TestCase):
    def test_reference_is_last_close_on_or_before(self):
        closes = series(np.arange(1, 601, dtype=float), start="2024-01-01")
        ath = (closes.idxmax().date(), float(closes.max()))
        by_label = {c.label: c for c in compute_changes(closes, ath)}
        last_ts = closes.index[-1]
        self.assertEqual(by_label["1 day"].ref_date, closes.index[-2].date())
        self.assertLessEqual(pd.Timestamp(by_label["1 week"].ref_date), last_ts - pd.Timedelta(days=7))
        self.assertEqual(by_label["YTD"].ref_date.year, last_ts.year - 1)
        self.assertEqual(by_label["From ATH"].pct, 0.0)
        self.assertAlmostEqual(by_label["1 day"].points, 1.0)

    def test_short_history_gives_none_not_crash(self):
        closes = series([100.0, 101.0, 102.0])
        ath = (closes.idxmax().date(), 102.0)
        by_label = {c.label: c for c in compute_changes(closes, ath)}
        self.assertIsNone(by_label["1 year"].ref_close)
        self.assertEqual(by_label["1 day"].pct, round((102 / 101 - 1) * 100, 2))


class SmaTests(unittest.TestCase):
    def test_not_enough_history(self):
        sma = compute_sma(series(np.ones(50)))
        self.assertIsNone(sma.value)
        self.assertIsNone(sma.above)

    def test_rising_and_above(self):
        closes = series(np.linspace(100, 200, 400))
        sma = compute_sma(closes)
        self.assertTrue(sma.above)
        self.assertEqual(sma.slope, "rising")
        self.assertGreater(sma.pct_vs_sma, 0)


class RecoveryTests(unittest.TestCase):
    def test_not_armed_in_bull_market(self):
        closes = series(np.linspace(100, 200, 400))
        ladder = compute_ladder(float(closes.iloc[-1]), float(closes.max()))
        rec = compute_recovery(compute_sma(closes), closes, ladder)
        self.assertFalse(rec.armed)
        self.assertFalse(rec.active)
        self.assertIn("not armed", rec.state)

    def test_armed_after_crash_then_active_on_recovery(self):
        up = np.linspace(100, 200, 300)
        crash = np.linspace(200, 130, 60)          # -35%: Buy 1 fires
        flat = np.full(150, 135.0)
        rally = np.linspace(135, 190, 120)         # back above a rising SMA, still below the ATH
        closes = series(np.concatenate([up, crash, flat, rally]))
        ladder = compute_ladder(float(closes.iloc[-1]), float(closes.max()))
        rec = compute_recovery(compute_sma(closes), closes, ladder)
        self.assertTrue(rec.armed)
        self.assertTrue(rec.active)

    def test_new_ath_disarms(self):
        closes = series(np.concatenate([np.linspace(100, 200, 300), np.linspace(200, 130, 60), np.linspace(130, 210, 300)]))
        ladder = compute_ladder(float(closes.iloc[-1]), float(closes.max()))
        rec = compute_recovery(compute_sma(closes), closes, ladder)
        self.assertFalse(rec.armed)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.base = random_walk()
        self.ath = float(self.base.max())

    def snap(self, tail, prev=None):
        c = self.base.copy()
        c.iloc[-len(tail):] = tail
        return compute_snapshot(md(c), "test", prev)

    def logged(self, **fired):
        """A previous logged row from the current ATH cycle."""
        row = {"date": "2020-01-01", "ath_date": self.base.idxmax().date().isoformat(), "buy5_active": False}
        row.update({f"buy{i}_fired": False for i in range(1, 5)})
        row.update(fired)
        return row

    def test_no_buy(self):
        a = self.snap([self.ath * 0.95, self.ath * 0.96]).action
        self.assertEqual(a.signal, "NONE")
        self.assertEqual(a.nearest_waiting, "Buy 1")

    def test_buy_when_crossing_today(self):
        a = self.snap([self.ath * 0.76, self.ath * 0.74]).action
        self.assertEqual(a.signal, "BUY")
        self.assertEqual(a.newly_fired, ["Buy 1"])

    def test_buy_when_crossed_on_an_unlogged_day(self):
        a = self.snap([self.ath * 0.74, self.ath * 0.73], self.logged()).action
        self.assertEqual(a.signal, "BUY")

    def test_no_refire_once_logged(self):
        a = self.snap([self.ath * 0.76, self.ath * 0.74], self.logged(buy1_fired=True)).action  # bounce and back
        self.assertEqual(a.signal, "NONE")
        self.assertEqual(a.already_fired, ["Buy 1"])

    def test_logged_state_from_an_older_cycle_is_ignored(self):
        """Buy 1 was logged as fired in a previous crash; a new ATH since then resets the ladder,
        so the first cross below the new Buy 1 level must be a BUY again."""
        stale = self.logged(buy1_fired=True)
        stale["ath_date"] = "2019-03-01"
        a = self.snap([self.ath * 0.76, self.ath * 0.74], stale).action
        self.assertEqual(a.signal, "BUY")

    def test_ath_today_is_fine(self):
        a = self.snap([self.ath * 1.01, self.ath * 1.05]).action
        self.assertEqual(a.signal, "NONE")


class LiveBarTests(unittest.TestCase):
    def test_todays_row_dropped_while_market_open(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from bubble_detector.data import drop_live_bar
        today = pd.Timestamp(datetime.now(ZoneInfo("America/New_York")).date())
        closes = pd.Series([1.0, 2.0, 3.0], index=pd.DatetimeIndex([today - pd.Timedelta(days=2), today - pd.Timedelta(days=1), today]))
        self.assertEqual(len(drop_live_bar(closes, close_time="23:59")), 2)   # market still open: drop today
        self.assertEqual(len(drop_live_bar(closes, close_time="00:00")), 3)   # market closed: keep today
        self.assertEqual(len(drop_live_bar(closes.iloc[:-1], close_time="23:59")), 2)  # no row for today: untouched


class HistoryTests(unittest.TestCase):
    def test_append_dedupes_and_keeps_column_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            snap = compute_snapshot(md(random_walk(), {"HNDQ.AX": series([10.0, 10.5], start="2026-01-01"),
                                                        "NDQ.AX": pd.Series(dtype="float64")}), "t")
            append_history(out, snap.history_row())
            hist = append_history(out, snap.history_row())
            self.assertEqual(len(hist), 1)
            self.assertEqual(list(hist.columns), history_columns())
            self.assertIsNone(hist.iloc[0]["ndq_close"] if pd.isna(hist.iloc[0]["ndq_close"]) else None)

    def test_old_rows_without_new_columns_still_load(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "history.csv").write_text("date,close,ath,drawdown_pct\n2026-01-02,100,120,-16.67\n")
            snap = compute_snapshot(md(random_walk()), "t")
            hist = append_history(out, snap.history_row())
            self.assertEqual(len(hist), 2)
            prev = previous_logged_row(read_history(out), snap.last_date)
            self.assertEqual(prev["date"], "2026-01-02")
            self.assertIsNone(prev.get("buy1_fired"))


class PruneTests(unittest.TestCase):
    def test_prunes_only_old_dated_reports(self):
        from datetime import date
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            # 2026-08-01 is exactly 90 days before 2026-10-30: kept. 2026-07-31 is 91 days: pruned.
            keep = ["report_2026-10-30.html", "report_2026-10-30.pdf", "report_2026-08-01.html",
                    "latest.html", "history.csv", "notes_2020-01-01.html"]
            old = ["report_2026-07-31.html", "report_2026-07-31.pdf", "report_2025-01-01.html"]
            for name in keep + old:
                (out / name).write_text("x")
            removed = prune_reports(out, keep_days=90, today=date(2026, 10, 30))
            self.assertEqual(sorted(p.name for p in removed), sorted(old))
            self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(keep))

    def test_zero_disables(self):
        from datetime import date
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "report_2000-01-01.html").write_text("x")
            self.assertEqual(prune_reports(Path(d), keep_days=0, today=date(2026, 10, 30)), [])
            self.assertTrue((Path(d) / "report_2000-01-01.html").exists())


class RenderTests(unittest.TestCase):
    def test_console_and_html_render(self):
        closes = random_walk()
        snap = compute_snapshot(md(closes), "t")
        text = render_console(snap)
        self.assertIn("Bubble Detector", text)
        self.assertIn("CRASH-BUY LADDER", text)
        html = render_html(snap, closes, pd.DataFrame())
        self.assertIn("<svg", html)
        self.assertNotIn("http://", html.split("<body")[1].split("<script")[0])  # self-contained body


if __name__ == "__main__":
    unittest.main()
