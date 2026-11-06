"""Web page: how far the index is below its all-time high, with history for context. No buy targets.

    python -m bubble_detector.web [--outdir site]

Writes <outdir>/index.html (self-contained) and <outdir>/data.json for a local preview.
On Vercel the same page is served on demand by api/index.py and cached until the next update.

The server works out every number; the browser only draws the chart, runs the countdowns and swaps in
a newer copy of the page when one is due. The page works without JavaScript except for the chart.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from urllib.parse import quote

from zoneinfo import ZoneInfo

import pandas as pd

from .calc import compute_changes, compute_sma
from .config import EXCHANGE_TZ, INDEX_NAME, INDEX_TICKER, MARKET_CLOSE_TIME, TITLE

MARKET_OPEN_TIME = "09:30"
EARLY_CLOSE_TIME = "13:00"
AFTER_CLOSE_UPDATES = timedelta(hours=1)  # keep updating for an hour after the close so the final close lands
WEB_FETCH_RETRIES = 2                     # on Vercel: stay well inside the function's 30s limit
WEB_FETCH_TIMEOUT_SECONDS = 10
DAILY_CHART_YEARS = 5                     # chart ranges up to 5Y use daily closes; Max uses the last close of each week
CRASH_PCT = -20.0                         # drops at least this deep are marked on the chart and the gauge
# (deepest drop, name), checked in order. Standard market terms describing the size of a drop, not signals.
ZONES = [(-20.0, "Bear market"), (-10.0, "Correction"), (-5.0, "Pullback"), (0.0, "Near the high")]
# Why each crash happened, keyed by the date of the high it fell from. Shown when a row in "Past crashes" is
# opened. The last entry describes the drop under way and is only shown while that drop lasts.
FRH = "https://www.federalreservehistory.org/essays/"
CRASH_NOTES = {
    "1973-01-11": ("Oil shock and stagflation",
                   "The post-war boom ended in rising inflation, the breakdown of the Bretton Woods currency system "
                   "and Watergate. The Arab oil embargo of October 1973 quadrupled oil prices and pushed the US into a "
                   "deep recession, and the high-flying “Nifty Fifty” growth stocks were cut down.",
                   [("Federal Reserve History", FRH + "oil-shock-of-1973-74")]),
    "1978-09-13": ("The October massacre",
                   "A sliding dollar, rising inflation and sharply higher interest rates set off a two-week rout in "
                   "late October 1978. It ended when Washington announced a rescue plan for the dollar on 1 November.",
                   [("Time, 1978", "https://content.time.com/time/subscriber/article/0,33009,965842,00.html")]),
    "1980-02-08": ("Inflation, credit controls and the silver crash",
                   "With inflation near 15%, interest rates headed toward 20% and in March 1980 the government imposed "
                   "credit controls. On 27 March, “Silver Thursday”, the Hunt brothers’ attempt to corner "
                   "silver collapsed and rattled brokers. Stocks recovered once the controls were lifted that summer.",
                   [("Federal Reserve History", FRH + "credit-control-of-1980"),
                    ("Silver Thursday", "https://en.wikipedia.org/wiki/Silver_Thursday")]),
    "1981-05-29": ("Volcker’s war on inflation",
                   "The Fed held interest rates near 20% to break inflation, tipping the economy into a deep recession "
                   "with unemployment near 11%. Stocks bottomed in August 1982 as the Fed began to ease, starting a long "
                   "bull market.",
                   [("Federal Reserve History", FRH + "recession-of-1981-82")]),
    "1983-06-24": ("The first tech bust",
                   "Personal-computer, video-game and biotech stocks soared in early 1983 on a wave of new listings, then "
                   "collapsed as competition and IBM’s price cuts crushed profits. Tech shares stayed in the "
                   "doldrums until 1985.",
                   [("Christian Science Monitor, 1986", "https://www.csmonitor.com/1986/0331/fwall31.html")]),
    "1987-08-27": ("Black Monday",
                   "After a big run-up, worries about rising interest rates, a weak dollar and the trade deficit turned "
                   "into panic. On 19 October 1987 US stocks had their worst day ever, made worse by automated "
                   "“portfolio insurance” selling. The Fed’s promise of liquidity stopped the slide.",
                   [("Federal Reserve History", FRH + "stock-market-crash-of-1987")]),
    "1989-10-09": ("Junk bonds, the savings-and-loan crisis and the Gulf War",
                   "A failed buyout of United Airlines set off a mini-crash on 13 October 1989 as the junk-bond market "
                   "unravelled, and hundreds of failing savings-and-loan banks squeezed credit. When Iraq invaded Kuwait "
                   "in August 1990 oil prices more than doubled and the US slipped into recession.",
                   [("Federal Reserve History", FRH + "recession-of-1990-91")]),
    "1998-07-20": ("Russia defaults, LTCM nearly fails",
                   "Russia’s debt default in August 1998 set off a global flight from risk and pushed the giant "
                   "hedge fund Long-Term Capital Management to the brink. The Fed organised a rescue and cut rates three "
                   "times, and stocks recovered within weeks.",
                   [("Federal Reserve History", FRH + "ltcm-near-failure")]),
    "2000-03-10": ("The dot-com bust",
                   "Internet companies with little or no profit had driven the Nasdaq up fivefold in five years. As rates "
                   "rose and the money ran out, the bubble burst; the 2001 recession, the 9/11 attacks and the Enron and "
                   "WorldCom scandals deepened the fall. The Nasdaq didn’t regain its 2000 high until 2015.",
                   [("Dot-com bubble", "https://en.wikipedia.org/wiki/Dot-com_bubble")]),
    "2007-10-31": ("The global financial crisis",
                   "Losses on US subprime mortgages spread through the banking system, and the collapse of Lehman "
                   "Brothers in September 2008 froze credit worldwide. The Great Recession followed; stocks bottomed in "
                   "March 2009 after huge government and Fed rescues.",
                   [("Federal Reserve History", FRH + "great-recession-of-200709")]),
    "2018-08-29": ("Rate hikes and the trade war",
                   "The Fed kept raising interest rates while the US–China trade war escalated and fears of a "
                   "slowdown grew. December 2018 was the worst December since 1931, bottoming on Christmas Eve, and the "
                   "Fed paused its hikes soon after.",
                   [("PBS NewsHour, 2018", "https://www.pbs.org/newshour/nation/u-s-stocks-plunge-on-christmas-eve")]),
    "2020-02-19": ("COVID-19",
                   "As the pandemic spread and economies locked down, stocks fell by about a third in a month, one of the "
                   "fastest crashes on record. Huge Fed and government support sparked an equally fast recovery, led by "
                   "tech.",
                   [("2020 stock market crash", "https://en.wikipedia.org/wiki/2020_stock_market_crash")]),
    "2021-11-19": ("Inflation and the fastest rate hikes in decades",
                   "Inflation hit a 40-year high and the Fed raised rates at the fastest pace since the 1980s. Higher "
                   "rates hit richly valued tech and growth stocks hardest, and the crypto crash added to the pain. The "
                   "AI boom that began with ChatGPT led the recovery.",
                   [("2022 stock market decline", "https://en.wikipedia.org/wiki/2022_stock_market_decline")]),
    "2024-12-16": ("DeepSeek and the tariff shock",
                   "China’s low-cost DeepSeek AI model shook AI chip stocks in January 2025. Then the sweeping "
                   "“Liberation Day” tariffs announced on 2 April 2025 set off a global sell-off; stocks "
                   "rebounded after most of the tariffs were paused on 9 April.",
                   [("2025 stock market crash", "https://en.wikipedia.org/wiki/2025_stock_market_crash")]),
    "2026-10-06": ("The AI trade cracks",
                   "After a record on 6 October, tech slid on worries about the cost and payoff of the AI build-out, "
                   "weak chip stocks and disappointing Big Tech earnings. On 5 November, after Democratic midterm gains "
                   "raised fears that new governments would slow data-center construction, the Nasdaq fell 5.5%, its "
                   "worst day since April 2025, and chip stocks dropped about 12%. (Written 6 November 2026.)",
                   [("CNN", "https://edition.cnn.com/2026/11/05/investing/tech-heavy-nasdaq-plunges"),
                    ("Charles Schwab", "https://www.schwab.com/learn/story/market-volatility")]),
}

def _icon(path: str, filled: bool = False) -> str:
    paint = 'fill="currentColor"' if filled else 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
    return f'<svg viewBox="0 0 24 24" aria-hidden="true"><path {paint} d="{path}"/></svg>'


ICON_SHARE = _icon("M12 3v12M7 8l5-5 5 5M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6")
ICON_DATA = _icon("M8 4H7a2 2 0 0 0-2 2v4l-2 2 2 2v4a2 2 0 0 0 2 2h1M16 4h1a2 2 0 0 1 2 2v4l2 2-2 2v4a2 2 0 0 1-2 2h-1")
ICON_GITHUB = _icon(
    "M12 .5a11.5 11.5 0 0 0-3.64 22.41c.58.1.79-.25.79-.56v-2c-3.2.7-3.88-1.37-3.88-1.37-.53-1.33-1.28-1.69-1.28-1.69"
    "-1.05-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.56-.29-5.25-1.28"
    "-5.25-5.69 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.47.11-3.06 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.77 0c2.2-1.49 3.17"
    "-1.18 3.17-1.18.63 1.59.23 2.77.11 3.06.74.81 1.19 1.84 1.19 3.1 0 4.42-2.7 5.39-5.27 5.68.41.36.78 1.06.78 2.14v3.17"
    "c0 .31.21.67.8.56A11.5 11.5 0 0 0 12 .5Z", filled=True)

AUTHOR = "is0xjh25"
REPO_URL = "https://github.com/is0xjh25/bubble-detector"
DISCLAIMER = ("Not financial advice. This page only shows how far the index is below its all-time high; "
              "it does not recommend buying, selling or holding anything.")

ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
    '<rect x="100" y="100" width="824" height="824" rx="185" fill="#0a0a0a"/>'
    '<circle cx="512" cy="512" r="290" fill="none" stroke="#fff" stroke-width="34"/>'
    '<path d="M330 420 A 220 220 0 0 1 430 320" fill="none" stroke="#fff" stroke-width="34" stroke-linecap="round"/>'
    "</svg>"
)  # same as assets/icons/icon.svg
ICON_HREF = "data:image/svg+xml," + quote(ICON_SVG)
LOGO_SVG = (
    '<svg class="logo" viewBox="0 0 1024 1024" aria-hidden="true">'
    '<rect width="1024" height="1024" rx="230" fill="#0a0a0a"/>'
    '<circle cx="512" cy="512" r="290" fill="none" stroke="#fff" stroke-width="44"/>'
    '<path d="M330 420 A 220 220 0 0 1 430 320" fill="none" stroke="#fff" stroke-width="44" stroke-linecap="round"/>'
    "</svg>"
)
STATIC_DIR = Path(__file__).parent / "static"  # full-bleed PNG app icons (icon-180/192/512.png)
APP_ICON_SIZES = (180, 192, 512)

# "Add to Home Screen": app name, icons, full-screen launch.
MANIFEST = json.dumps({
    "name": TITLE,
    "short_name": "Bubble",
    "description": f"How far the {INDEX_NAME} is below its all-time high.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#141416",
    "theme_color": "#141416",
    "icons": [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any maskable"}
              for n in APP_ICON_SIZES if n != 180],
}, indent=2)

# Network first, so visitors always get the newest copy; the last good copy is kept for when there's no signal.
SW_JS = r"""
const CACHE = "bubble-v1";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req, { ignoreSearch: true }).then((hit) =>
      hit || (req.mode === "navigate" ? caches.match("/") : Response.error())))
  );
});
"""


# --- US market calendar -------------------------------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th given weekday of a month (Mon=0); n=-1 is the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=None)
def market_holidays(year: int) -> frozenset[date]:
    """NYSE/Nasdaq full-day holidays. Unscheduled closures are caught by the 'no bar today' check instead."""
    days = {
        _nth_weekday(year, 1, 0, 3),           # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),           # Washington's Birthday
        _easter(year) - timedelta(days=2),     # Good Friday
        _nth_weekday(year, 5, 0, -1),          # Memorial Day
        _nth_weekday(year, 9, 0, 1),           # Labor Day
        _nth_weekday(year, 11, 3, 4),          # Thanksgiving
        _observed(date(year, 7, 4)),
        _observed(date(year, 12, 25)),
    }
    if date(year, 1, 1).weekday() != 5:        # a Saturday New Year's Day is not made up on the Friday
        days.add(_observed(date(year, 1, 1)))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    return frozenset(days)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in market_holidays(d.year)


def is_early_close(d: date) -> bool:
    """US half days (13:00 close): July 3, the day after Thanksgiving, Christmas Eve, when they are trading days."""
    if not is_trading_day(d):
        return False
    return (d.month, d.day) in ((7, 3), (12, 24)) or d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1)


def close_time(d: date) -> str:
    return EARLY_CLOSE_TIME if is_early_close(d) else MARKET_CLOSE_TIME


def _at(d: date, hm: str) -> datetime:
    h, m = (int(x) for x in hm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo(EXCHANGE_TZ))


def _next_open(after: date) -> datetime:
    d = after + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return _at(d, MARKET_OPEN_TIME)


def market_is_open(last_bar: pd.Timestamp, now: datetime | None = None) -> bool:
    """True while the exchange is in session and the data has a bar for today (holidays have none)."""
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(EXCHANGE_TZ))
    d = local.date()
    return (is_trading_day(d) and _at(d, MARKET_OPEN_TIME) <= local < _at(d, close_time(d))
            and last_bar.date() == d)


def next_open(now: datetime) -> datetime:
    """The next session start strictly after `now` (today's, if it has not started yet)."""
    local = now.astimezone(ZoneInfo(EXCHANGE_TZ))
    d = local.date()
    if is_trading_day(d) and local < _at(d, MARKET_OPEN_TIME):
        return _at(d, MARKET_OPEN_TIME).astimezone(timezone.utc)
    return _next_open(d).astimezone(timezone.utc)


def next_update(now: datetime) -> datetime:
    """Top of the next hour while the market is open (and for an hour after the close), else the next open."""
    local = now.astimezone(ZoneInfo(EXCHANGE_TZ))
    d = local.date()
    if is_trading_day(d) and _at(d, MARKET_OPEN_TIME) <= local < _at(d, close_time(d)) + AFTER_CLOSE_UPDATES:
        # New York is a whole number of hours from UTC, so the UTC top of the hour is also New York's.
        return now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_open(now)


# --- numbers --------------------------------------------------------------------------------------------

def zone(drop_pct: float) -> str:
    if drop_pct >= 0:
        return "At the high"
    return next(name for limit, name in ZONES if drop_pct <= limit)


def _iso(ts) -> str:
    return ts.date().isoformat()


def _episodes(dd: pd.Series) -> list[dict]:
    """Every stretch from one all-time high to the next, with its low. The last one may still be under way."""
    groups = [g for _, g in dd.groupby((dd >= 0).cumsum())]  # each group starts on an all-time-high day
    out = []
    for k, g in enumerate(groups):
        if len(g) < 2:
            continue  # a record straight after a record: no drop in between
        out.append({"dd": g, "peak": g.index[0], "trough": g.idxmin(), "low": float(g.min()),
                    "recovered": groups[k + 1].index[0] if k + 1 < len(groups) else None})
    return out


def _crashes(closes: pd.Series, from_record: bool = True) -> list[dict]:
    """Every fall of CRASH_PCT or worse from a high, oldest first (the last may be ongoing).

    The top level measures from all-time highs. The climb back after each crash is searched again with
    its own running high, which finds bear markets that started below an older record: 2007-09 began
    at a 2007 high while the index was still under its March 2000 peak.
    """
    dd = (closes / closes.cummax() - 1) * 100
    out = []
    for ep in _episodes(dd):
        if ep["low"] > CRASH_PCT + 1e-9:  # 80/100 - 1 is -19.999...%, still a 20% fall
            continue
        out.append({"peak": _iso(ep["peak"]), "trough": _iso(ep["trough"]), "pct": round(ep["low"], 2),
                    "recovered": _iso(ep["recovered"]) if ep["recovered"] is not None else None,
                    "days_down": (ep["trough"] - ep["peak"]).days,
                    "days_to_recover": (ep["recovered"] - ep["trough"]).days if ep["recovered"] is not None else None,
                    "from_record": from_record})
        climb = closes.loc[ep["trough"]:ep["recovered"]]
        if ep["recovered"] is not None:
            climb = climb.iloc[:-1]  # stop the day before the old high is regained
        if len(climb) > 2:
            out += _crashes(climb, from_record=False)
    return sorted(out, key=lambda c: c["peak"])


def _this_drop(episodes: list[dict], dd: pd.Series) -> dict | None:
    """The drop under way, and how past drops that got at least this deep played out. None at a record."""
    now = float(dd.iloc[-1])
    if now >= 0:
        return None
    cur = episodes[-1]
    out = {"high_date": _iso(cur["peak"]), "days_since_high": (dd.index[-1] - cur["peak"]).days,
           "low_pct": round(cur["low"], 2), "low_date": _iso(cur["trough"]), "past": None}
    past = [ep for ep in episodes[:-1] if ep["recovered"] is not None and ep["low"] <= now + 1e-9]
    if past:
        to_low = [(ep["trough"] - ep["peak"]).days for ep in past]
        # from the first day the past drop was this deep, until it was back at a record
        back = [(ep["recovered"] - ep["dd"][ep["dd"] <= now + 1e-9].index[0]).days for ep in past]
        out["past"] = {"count": len(past), "to_low_median_days": int(median(to_low)),
                       "recover_median_days": int(median(back)), "recover_min_days": min(back),
                       "recover_max_days": max(back)}
    return out


def _series(dd: pd.Series, closes: pd.Series) -> dict:
    """Chart data as compact arrays: d = days since 1970-01-01, p = % below the high, c = close."""
    def pack(idx: pd.DatetimeIndex) -> dict:
        return {"d": [int(x) for x in (idx - pd.Timestamp("1970-01-01")).days],
                "p": [round(float(v), 2) for v in dd.loc[idx]],
                "c": [round(float(v), 2) for v in closes.loc[idx]]}

    last = closes.index[-1]
    daily = closes.loc[last - pd.DateOffset(years=DAILY_CHART_YEARS):].index
    weekly = closes.groupby(closes.index.to_period("W")).tail(1).index  # real trading days, ends on the latest close
    return {"daily": pack(daily), "weekly": pack(weekly)}


def summarise(closes: pd.Series, now: datetime | None = None) -> dict:
    """Everything the page shows, as plain JSON-able values. The last value may be a live intraday price."""
    now = now or datetime.now(timezone.utc)
    live = market_is_open(closes.index[-1], now)
    dd = (closes / closes.cummax() - 1) * 100
    ath_ts, ath_close = closes.idxmax(), float(closes.max())
    last_ts, last_close = closes.index[-1], float(closes.iloc[-1])
    drop = round((last_close / ath_close - 1) * 100, 2)

    changes = []
    for c in compute_changes(closes, (ath_ts.date(), ath_close)):
        if c.label == "From ATH":
            continue
        row = {"label": c.label, "pct": c.pct, "points": None if c.points is None else round(c.points, 2),
               "ref_date": c.ref_date.isoformat() if c.ref_date else None, "deepest_pct": None, "deepest_date": None}
        if c.ref_date and c.label != "1 day":  # over a single session "deepest" is just today's figure
            win = dd.loc[pd.Timestamp(c.ref_date):]
            row["deepest_pct"], row["deepest_date"] = round(float(win.min()), 2), _iso(win.idxmin())
        changes.append(row)

    at_high = drop >= 0
    share = float((dd >= -1e-9).mean() if at_high else (dd <= dd.iloc[-1] + 1e-9).mean()) * 100
    today = now.astimezone(ZoneInfo(EXCHANGE_TZ)).date()
    closes_at = _at(today, close_time(today)).astimezone(timezone.utc) if live else None
    episodes = _episodes(dd)
    one_day = next((c for c in changes if c["label"] == "1 day"), None)
    sma = compute_sma(closes)
    return {
        "index": INDEX_NAME,
        "ticker": INDEX_TICKER,
        "last_date": _iso(last_ts),
        "last_close": round(last_close, 2),
        "ath_date": _iso(ath_ts),
        "ath_close": round(ath_close, 2),
        "drop_pct": drop,
        "drop_points": round(last_close - ath_close, 2),
        "zone": zone(drop),
        # at the high: share of days that were record closes; otherwise: share of days at least this far down
        "history": {"from_year": int(closes.index[0].year), "share_pct": round(share, 1), "at_high": at_high},
        "deepest_ever": {"pct": round(float(dd.min()), 2), "date": _iso(dd.idxmin())},
        "needs_pct": round((ath_close / last_close - 1) * 100, 2),  # gain needed to get back to the high
        "this_drop": _this_drop(episodes, dd),
        "crashes": _crashes(closes),
        "day_move": None if one_day is None or one_day["pct"] is None else {
            "pct": one_day["pct"],
            "label": "today" if live or last_ts.date() == today else f"on {last_ts.strftime('%a')}"},
        "sma": None if sma.value is None else {"window": sma.window, "pct": sma.pct_vs_sma, "slope": sma.slope},
        "changes": changes,
        "market_open": live,
        "market_opens_at": next_open(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_closes_at": closes_at.strftime("%Y-%m-%dT%H:%M:%SZ") if closes_at else None,
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "next_update_at": next_update(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "series": _series(dd, closes),
    }


# --- HTML -----------------------------------------------------------------------------------------------

def _pct(v: float | None) -> str:
    if v is None:
        return "–"
    if abs(v) < 0.005:
        return "0.00%"
    return f"{v:+.2f}%".replace("-", "−")


def _cls(v: float | None) -> str:
    return "" if v is None or abs(v) < 0.005 else ("down" if v < 0 else "up")


def _day(iso: str, year: bool = True) -> str:
    """'2026-11-05' -> '5 Nov 2026' (or '5 Nov')."""
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")


def _days_since_epoch(iso: str) -> int:
    return (date.fromisoformat(iso) - date(1970, 1, 1)).days


def _span(days: int) -> str:
    """19 -> '19 days', 168 -> '6 months', 5498 -> '15 years'."""
    if days < 45:
        return f"{days} day{'s' if days != 1 else ''}"
    months = round(days / 30.44)
    if months < 24:
        return f"{months} months"
    years = days / 365.25
    return f"{years:.0f} years" if years >= 10 else f"{years:.1f} years".replace(".0 ", " ")


def _pct1(v: float) -> str:
    return f"{v:+.1f}%".replace("-", "−")


def _crash_name(c: dict) -> str:
    """'2000–02', or '1987' when the high and the low fall in the same year."""
    a, b = c["peak"][:4], c["trough"][:4]
    return a if a == b else f"{a}–{b[2:]}"


def _facts(s: dict) -> str:
    """The 'This drop' card: duration, recovery maths, 200-day average and how past drops played out."""
    e, d = html.escape, s["this_drop"]
    items = []
    if d:
        items.append(("Since the high", f'{_span(d["days_since_high"])} <span class="muted nw">({_day(d["high_date"])})</span>', "days"))
        items.append(("Back to the high needs", f'<span class="up">+{s["needs_pct"]:.2f}%</span>', "needs"))
    if s["sma"]:
        m = s["sma"]
        side = "above" if m["pct"] >= 0 else "below"
        slope = f', {e(m["slope"])}' if m["slope"] else ""
        items.append((f'{m["window"]}-day average', f'{abs(m["pct"]):.1f}% {side}{slope}', "sma"))
    if d:
        if d["low_date"] == s["last_date"]:  # the headline already shows the figure
            same_day = s["day_move"] and s["day_move"]["label"] == "today"
            when = "Today" if same_day else f'{date.fromisoformat(s["last_date"]).strftime("%A")}’s close'
            low = f'<span class="down">{when}</span>'
        else:
            low = f'<span class="down">{_pct(d["low_pct"])}</span> <span class="muted nw">({_day(d["low_date"])})</span>'
        items.append(("Lowest so far", low, "low"))
    facts = "".join(f'<div><dt>{label}</dt><dd data-k="{k}">{value}</dd></div>' for label, value, k in items)

    h = s["history"]
    share = "under 1" if 0 < h["share_pct"] < 1 else f'{h["share_pct"]:.0f}'
    if not d:
        story = (f'No drop under way: the last close is a record. Since {h["from_year"]}, <b>{share}%</b> of '
                 f'trading days have ended at a record.')
    elif not d["past"]:
        story = f'The index has never been this far below its high since {h["from_year"]}.'
    else:
        p = d["past"]
        story = (f'Since {h["from_year"]}, the index has been this far or further below its high on <b>{share}%</b> '
                 f'of trading days, and <b>{p["count"]}</b> earlier drops got this deep. They hit bottom a median of '
                 f'<b>{_span(p["to_low_median_days"])}</b> after their high, and were back at the high a median of '
                 f'<b>{_span(p["recover_median_days"])}</b> after reaching {_pct(s["drop_pct"])} '
                 f'(fastest {_span(p["recover_min_days"])}, slowest {_span(p["recover_max_days"])}).')
    title = "This drop" if d else "Trend"
    return (f'<section class="card"><h2>{title}</h2><dl class="facts">{facts}</dl>'
            f'<p class="context" data-k="story">{story}</p></section>')


def _why(key: str, facts: str, a: int, b: int, note: str) -> str:
    """The opened part of a crash row: what happened, the dates, a chart button and the sources."""
    title, text, sources = CRASH_NOTES.get(key, ("", "", []))
    links = " · ".join(f'<a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(n)}</a>' for n, u in sources)
    return (f'<div class="why">' + (f'<h3>{html.escape(title)}</h3><p>{html.escape(text)}</p>' if title else "")
            + f'<p class="muted dates">{facts}</p><div class="why-foot">'
            f'<button type="button" class="chip" data-a="{a}" data-b="{b}" data-note="{html.escape(note)}">Show on chart</button>'
            + (f'<span class="muted">Source: {links}</span>' if links else "") + "</div></div>")


def _crash_row(cls: str, cells: list[str], why: str) -> str:
    spans = "".join(f'<span class="{c}">{v}</span>' for c, v in cells)
    return f'<details class="crash{cls}"><summary class="crow">{spans}</summary>{why}</details>'


def _crash_table(s: dict) -> str:
    """Every fall of 20% or more, newest first, with the drop under way on top. Each row opens to say why."""
    last = _days_since_epoch(s["last_date"])
    rows = []
    d = s["this_drop"]
    if d:
        a = _days_since_epoch(d["high_date"])
        facts = f'From the record on {_day(d["high_date"])}, {_pct(d["low_pct"])} at the lowest so far ({_day(d["low_date"])}).'
        note = f'This drop: {_pct(d["low_pct"])} so far, from the high on {_day(d["high_date"])}'
        rows.append(_crash_row(" now", [("", "Now"), ("num down", _pct1(d["low_pct"])), ("num", _span(d["days_since_high"])),
                                        ("num muted", "not yet")], _why(d["high_date"], facts, a, last, note)))
    for c in reversed(s["crashes"]):
        if c["recovered"] is None:
            continue  # the drop under way, already shown as "Now"
        name = _crash_name(c)
        start = "the record" if c["from_record"] else "a high"
        facts = (f'From {start} on {_day(c["peak"])} to the low on {_day(c["trough"])}; back above that high on '
                 f'{_day(c["recovered"])}.')
        if not c["from_record"]:
            facts += " Measured from its own high: the index was still below an older record."
        note = (f'{name}: {_pct(c["pct"])}, from the high on {_day(c["peak"])} to the low on {_day(c["trough"])}, '
                f'back at the high on {_day(c["recovered"])}')
        rows.append(_crash_row("", [("", name), ("num down", _pct1(c["pct"])), ("num", _span(c["days_down"])),
                                    ("num", _span(c["days_to_recover"]))],
                               _why(c["peak"], facts, _days_since_epoch(c["peak"]), _days_since_epoch(c["recovered"]), note)))
    head = ('<div class="crow head" aria-hidden="true"><span>Crash</span><span class="num">Drop</span>'
            '<span class="num">Fell for</span><span class="num">Recovered in</span></div>')
    return (f'<section class="card"><h2>Past crashes</h2>'
            f'<p class="hint muted" style="margin:4px 0 10px">Every fall of {abs(CRASH_PCT):.0f}% or more since '
            f'{s["history"]["from_year"]}. Tap one to see why it happened.</p>'
            f'<div class="crashes" id="crashes">{head}{"".join(rows)}</div></section>')


ZONE_CLASS = {"At the high": "z-high", "Near the high": "z-near", "Pullback": "z-pull",
              "Correction": "z-corr", "Bear market": "z-bear"}


def _gauge(s: dict) -> str:
    """Today's drop on a bar running from 0 to the deepest drop on record, with past crashes ticked."""
    deepest = s["deepest_ever"]
    floor = min(-30.0, math.floor(deepest["pct"] / 10) * 10)

    def pos(v: float) -> float:
        return max(0.0, min(100.0, v / floor * 100))

    bands = "".join(
        f'<i class="band b{i}" style="left:{pos(a):.2f}%;width:{pos(b) - pos(a):.2f}%"></i>'
        for i, (a, b) in enumerate([(0, -5), (-5, -10), (-10, -20), (-20, floor)])
    )
    ticks = "".join(
        f'<i class="tick" style="left:{pos(c["pct"]):.2f}%" title="{c["trough"][:4]}: {_pct(c["pct"])}"></i>'
        for c in s["crashes"] if c["recovered"]
    )
    year = deepest["date"][:4]
    labels = (f'<span style="left:0">0%</span><span class="mid" style="left:{pos(-10):.2f}%">−10%</span>'
              f'<span class="mid" style="left:{pos(-20):.2f}%">−20%</span>'
              f'<span style="right:0">−{abs(deepest["pct"]):.0f}% ({year})</span>')
    drop = min(s["drop_pct"], 0.0)
    return (
        f'<div class="gauge" role="img" aria-label="Today {abs(drop):.1f}% below the high, on a scale up to the '
        f'deepest drop since {s["history"]["from_year"]}: {abs(deepest["pct"]):.1f}% in {year}">'
        f'<div class="track">{bands}{ticks}<b class="mark" style="left:{pos(drop):.2f}%"></b></div>'
        f'<div class="scale">{labels}</div></div>'
    )


def render_html(s: dict) -> str:
    e = html.escape
    last_year = s["last_date"][:4]
    at_high = s["drop_pct"] >= 0
    drop_txt = _pct(s["drop_pct"])

    if at_high:
        vs_ath = f'At the all-time high of {s["ath_close"]:,.2f}'
    else:
        vs_ath = (f'{abs(s["drop_points"]):,.2f} points below the all-time high of {s["ath_close"]:,.2f} '
                  f'<span class="nw">({_day(s["ath_date"])})</span>')
    if s["market_open"]:
        price = f'Intraday {s["last_close"]:,.2f}'
    else:
        price = f'Last close {s["last_close"]:,.2f} <span class="nw">on {_day(s["last_date"])}</span>'


    rows = []
    for c in s["changes"]:
        ref = c["ref_date"]
        since = f'<div class="muted">since {_day(ref, ref[:4] != last_year)}</div>' if ref else ""
        if c["deepest_pct"] is None:
            deepest = '<td class="num muted">—</td>'
        else:
            deepest = (f'<td class="num"><span class="{_cls(c["deepest_pct"])}">{_pct(c["deepest_pct"])}</span>'
                       f'<div class="muted">{_day(c["deepest_date"], c["deepest_date"][:4] != last_year)}</div></td>')
        attrs = f' data-from="{_days_since_epoch(ref)}" tabindex="0"' if ref else ""
        rows.append(f'<tr{attrs}><td>{e(c["label"])}{since}</td>'
                    f'<td class="num {_cls(c["pct"])}" data-k="chg-{e(c["label"])}">{_pct(c["pct"])}</td>{deepest}</tr>')

    today = ""
    if s["day_move"]:
        m = s["day_move"]
        arrow = '<i class="arrow">▼</i> ' if m["pct"] < 0 else '<i class="arrow">▲</i> ' if m["pct"] > 0 else ""
        today = f'<span class="today {_cls(m["pct"])}" data-k="today">{arrow}{abs(m["pct"]):.2f}% {e(m["label"])}</span>'
    ny = ZoneInfo(EXCHANGE_TZ)
    if s["market_open"]:
        state = "Market open"
        when = "closes " + datetime.fromisoformat(s["market_closes_at"].replace("Z", "+00:00")).astimezone(ny).strftime("%H:%M") + " ET"
    else:
        state = "Market closed"
        when = "opens " + datetime.fromisoformat(s["market_opens_at"].replace("Z", "+00:00")).astimezone(ny).strftime("%a %H:%M") + " ET"
    description = (f'{s["index"]} is at an all-time high.' if at_high
                   else f'{s["index"]} is {abs(s["drop_pct"]):.2f}% below its all-time high ({s["zone"].lower()}).')
    data = json.dumps(s, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(TITLE)}</title>
<meta name="description" content="{e(description)}">
<meta property="og:title" content="{e(s["index"])}: {drop_txt} from its all-time high">
<meta property="og:description" content="{e(description)}">
<meta property="og:type" content="website">
<meta name="theme-color" content="#f7f7f5" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#141416" media="(prefers-color-scheme: dark)">
<link rel="icon" href="{ICON_HREF}">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Bubble">
<style>{PAGE_CSS}</style>
</head>
<body>
<main>
  <header class="card top{' at-high' if at_high else ''}">
    <div class="id">{LOGO_SVG}<div><h1>{e(s["index"])}</h1><div class="kicker">Drop from all-time high</div></div></div>
    <div class="status{' open' if s['market_open'] else ''}"><i class="dot"></i><b>{state}</b><span class="sep">·</span><span class="when" id="mkt">{when}</span></div>
  </header>
  <div class="banner" id="offline" hidden>You're offline, so this is the copy from <time data-t="{s["updated_at"]}">{s["updated_at"]}</time>.</div>
  <div class="banner" id="stale" hidden>Couldn't load newer data, so this is the copy from <time data-t="{s["updated_at"]}">{s["updated_at"]}</time>. Still retrying.</div>
  <details class="card about" id="about">
    <summary><span class="info" aria-hidden="true">i</span>What's a bubble?</summary>
    <p>When prices race far ahead of the businesses behind them, like the dot-com boom of the late 1990s.
    Bubbles are only obvious once they burst, so this page doesn't predict one. It measures the fallout:
    how far the {e(s["index"])} is below its peak, next to every past crash.</p>
    <p class="muted">The labels: <b>Pullback</b> 5–10% below the high, <b>Correction</b> 10–20%,
    <b>Bear market</b> 20% or more.</p>
  </details>
  <section class="card hero">
    <div class="headline">
      <div class="big{' zero' if at_high else ''}" data-k="drop">{drop_txt}</div>
      <span class="zone {ZONE_CLASS[s["zone"]]}" data-k="zone">{e(s["zone"])}</span>
      {today}
    </div>
    <div class="sub" data-k="vs">{vs_ath}</div>
    <div class="sub" data-k="price">{price}</div>
    {_gauge(s)}
  </section>
  {_facts(s)}
  <section class="card">
    <div class="chart-head">
      <h2 id="chart-title">Below all-time high</h2>
      <div class="ranges" id="views"><button type="button" data-v="drop" aria-pressed="true">Drop</button><button type="button" data-v="price">Price</button></div>
      <div class="ranges" id="ranges">
        <button type="button" data-r="1M">1M</button><button type="button" data-r="3M">3M</button><button type="button" data-r="1Y" aria-pressed="true">1Y</button><button type="button" data-r="5Y">5Y</button><button type="button" data-r="Max">Max</button>
      </div>
    </div>
    <div class="readout" id="readout">{_day(s["last_date"])} · {drop_txt} · {s["last_close"]:,.2f}</div>
    <div class="chart" id="chart"><noscript><p class="muted">The chart needs JavaScript.</p></noscript></div>
  </section>
  <section class="card">
    <table id="periods">
      <thead><tr><th>Period</th><th class="num">Change</th><th class="num">Deepest drop</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <p class="hint muted">Deepest drop is the furthest below the all-time high during the period. Tap a row to show it on the chart.</p>
  </section>
  {_crash_table(s)}
  <footer>
    <div class="actions">
      <button type="button" class="btn" id="share">{ICON_SHARE}<span class="lbl">Share</span></button>
      <a class="btn" href="/data.json">{ICON_DATA}Data</a>
      <a class="btn" href="{e(REPO_URL)}">{ICON_GITHUB}Source code</a>
    </div>
    <p class="muted">Updated <time data-t="{s["updated_at"]}">{s["updated_at"]}</time><span id="next"></span> · Data: Yahoo Finance ({e(s["ticker"])})</p>
    <p class="by">{LOGO_SVG}<span>{e(TITLE)} · built by <b>{e(AUTHOR)}</b></span></p>
    <p class="disclaimer"><b>{e(DISCLAIMER.split(". ", 1)[0])}.</b> {e(DISCLAIMER.split(". ", 1)[1])}</p>
  </footer>
  <script type="application/json" id="data">{data}</script>
</main>
<script>{PAGE_JS}</script>
</body>
</html>
"""


PAGE_CSS = """
:root { --bg:#f7f7f5; --card:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e3e3e0; --down:#c4372b; --up:#1f7a4d;
  --warn:#a55f00; --area:rgba(196,55,43,.13); --hl:rgba(29,29,31,.06); --flash:rgba(255,196,0,.35); }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141416; --card:#1e1e21; --fg:#f2f2f2; --muted:#9a9aa0; --line:#33333a; --down:#ff6b5e; --up:#4cc38a;
    --warn:#f0b44c; --area:rgba(255,107,94,.16); --hl:rgba(255,255,255,.07); --flash:rgba(255,196,0,.25); }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
main { max-width:720px; margin:0 auto; padding:20px 16px 40px; }
a { color:inherit; }
.card.top { --tint:var(--down); display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:14px 16px;
  background:linear-gradient(135deg, color-mix(in srgb, var(--tint) 11%, var(--card)), var(--card) 70%); }
.top.at-high { --tint:var(--up); }
.logo rect { stroke:var(--line); stroke-width:28; }  /* keeps the black tile visible on a dark card */
.top .id { display:flex; align-items:center; gap:14px; min-width:0; }
.logo { display:block; flex:none; width:48px; height:48px; filter:drop-shadow(0 2px 6px rgba(0,0,0,.18)); }
h1 { font-size:clamp(24px, 6.4vw, 30px); font-weight:750; letter-spacing:-.02em; line-height:1.1; margin:0; }
.kicker { color:var(--muted); font-size:15px; margin-top:3px; }
.status { display:inline-flex; align-items:center; gap:7px; padding:7px 14px; border-radius:99px; font-size:14px; white-space:nowrap;
  background:color-mix(in srgb, var(--card) 70%, transparent); border:1px solid var(--line); box-shadow:0 1px 2px rgba(0,0,0,.05); }
.status .dot { width:8px; height:8px; border-radius:50%; background:var(--muted); opacity:.7; }
.status.open .dot { background:var(--up); opacity:1; animation:pulse 2s infinite; }
.status.open b { color:var(--up); }
.status b { font-weight:650; }
.status .sep, .status .when { color:var(--muted); }
@keyframes pulse { 0%, 100% { box-shadow:0 0 0 0 color-mix(in srgb, var(--up) 55%, transparent); } 70% { box-shadow:0 0 0 7px transparent; } }
.banner { background:var(--card); border:1px solid var(--down); color:var(--down); border-radius:10px; padding:8px 12px; font-size:14px; margin-bottom:14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px; margin-bottom:14px; }
.card.about { padding:0; }
.about summary { display:flex; align-items:center; gap:10px; padding:12px 20px; cursor:pointer; list-style:none; font-size:15px; font-weight:600; }
.about summary::-webkit-details-marker { display:none; }
.about summary::after { content:""; margin-left:auto; width:8px; height:8px; border:solid var(--muted); border-width:0 2px 2px 0; transform:rotate(45deg) translateY(-2px); transition:transform .25s; }
.about[open] summary::after { transform:rotate(-135deg) translateY(-2px); }
.about .info { display:inline-grid; place-items:center; width:20px; height:20px; border-radius:50%; border:1.5px solid var(--muted); color:var(--muted); font:italic 700 12px Georgia,serif; }
.about p { margin:0 20px 14px; font-size:15px; }
.about[open] p { animation:open .3s ease-out; }
@keyframes open { from { opacity:0; transform:translateY(-4px); } }
@media (max-width:500px) { .about summary { padding:12px 16px; } .about p { margin:0 16px 14px; } }
.headline { display:flex; flex-wrap:wrap; align-items:center; gap:6px 14px; }
.big { font-size:clamp(46px,13vw,72px); font-weight:700; line-height:1; color:var(--down); font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
.big.zero { color:var(--up); }
.zone { padding:4px 10px; border-radius:8px; font-size:14px; font-weight:650; }
.z-high { color:var(--up); background:color-mix(in srgb, var(--up) 14%, transparent); }
.z-near { color:var(--muted); background:var(--hl); }
.z-pull { color:var(--warn); background:color-mix(in srgb, var(--warn) 15%, transparent); }
.z-corr { color:var(--down); background:color-mix(in srgb, var(--down) 14%, transparent); }
.z-bear { color:#fff; background:var(--down); }
.sub { color:var(--muted); margin-top:8px; }
.context { margin:16px 0 0; font-size:15px; }
.nw { white-space:nowrap; }
.gauge { margin-top:14px; }
.track { position:relative; height:12px; border-radius:6px; overflow:visible; background:var(--hl); }
.band { position:absolute; top:0; bottom:0; }
.b0 { background:color-mix(in srgb, var(--down) 8%, transparent); border-radius:6px 0 0 6px; }
.b1 { background:color-mix(in srgb, var(--down) 18%, transparent); }
.b2 { background:color-mix(in srgb, var(--down) 32%, transparent); }
.b3 { background:color-mix(in srgb, var(--down) 50%, transparent); border-radius:0 6px 6px 0; }
.tick { position:absolute; top:2px; bottom:2px; width:2px; margin-left:-1px; background:var(--card); opacity:.8; }
.mark { transition:left 1.1s cubic-bezier(.2,.8,.2,1); position:absolute; top:-5px; width:4px; height:22px; margin-left:-2px; border-radius:2px; background:var(--fg); box-shadow:0 0 0 2px var(--card); }
.scale { position:relative; height:18px; margin-top:6px; font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
.scale span { position:absolute; white-space:nowrap; }
.scale .mid { transform:translateX(-50%); }
h2 { font-size:14px; font-weight:650; margin:0; }
.chart-head { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:8px; }
.ranges { display:flex; background:var(--hl); border-radius:9px; padding:2px; }
.ranges button { font:inherit; font-size:13px; font-weight:600; border:0; background:none; color:var(--muted); padding:4px 10px; border-radius:7px; cursor:pointer; }
.ranges button[aria-pressed="true"] { background:var(--card); color:var(--fg); box-shadow:0 1px 2px rgba(0,0,0,.12); }
.readout { margin:12px 0 4px; font-size:14px; font-variant-numeric:tabular-nums; min-height:20px; }
.chart { height:240px; }
@media (max-width:500px) { .chart { height:200px; } .card { padding:16px; } }
.chart svg { display:block; width:100%; height:100%; touch-action:pan-y; cursor:crosshair; }
.chart text { fill:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
.chart .grid { stroke:var(--line); }
.chart .area { fill:var(--area); }
.chart .line { fill:none; stroke:var(--down); stroke-width:1.6; stroke-linejoin:round; }
.chart .hl { fill:var(--hl); }
.chart .cross { stroke:var(--muted); stroke-dasharray:3 3; }
.chart .dot { fill:var(--down); stroke:var(--card); stroke-width:2; }
.chart .crash { fill:var(--fg); }
.chart .crash-label { fill:var(--fg); font-weight:600; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th { text-align:left; font-size:13px; font-weight:600; color:var(--muted); padding:0 0 8px; }
td { padding:9px 0; border-top:1px solid var(--line); vertical-align:top; }
td .muted { font-size:12px; }
tbody tr[tabindex] { cursor:pointer; }
tbody tr[tabindex]:hover td, tbody tr.active td { background:var(--hl); }
tr.now td { font-weight:600; }
td:first-child, th:first-child { padding-left:6px; }
td:last-child, th:last-child { padding-right:6px; }
.num { text-align:right; }
.muted { color:var(--muted); }
.down { color:var(--down); } .up { color:var(--up); }
.hint { font-size:13px; margin:12px 0 0; }
footer { display:flex; flex-direction:column; align-items:center; text-align:center; gap:12px; padding:8px 0 4px; font-size:13px; }
footer p { margin:0; }
footer .actions { display:flex; flex-wrap:wrap; justify-content:center; gap:8px; }
.btn { display:inline-flex; align-items:center; gap:7px; font:inherit; font-size:14px; font-weight:600; color:var(--fg);
  background:var(--card); border:1px solid var(--line); border-radius:99px; padding:8px 14px; text-decoration:none; cursor:pointer;
  box-shadow:0 1px 2px rgba(0,0,0,.05); }
.btn:hover { background:var(--hl); }
.btn svg { width:16px; height:16px; flex:none; }
footer .by { display:flex; align-items:center; gap:8px; }
footer .by .logo { width:22px; height:22px; filter:none; }
.disclaimer { max-width:460px; color:var(--muted); font-size:12px; line-height:1.5; }
.disclaimer b { color:var(--fg); }
.today { font-size:15px; font-weight:650; font-variant-numeric:tabular-nums; white-space:nowrap; }
.today .arrow { display:inline-block; font-style:normal; font-size:12px; }
.today.down .arrow { animation:nudge-down .9s ease-in-out 3; }
.today.up .arrow { animation:nudge-up .9s ease-in-out 3; }
@keyframes nudge-down { 50% { transform:translateY(4px); } }
@keyframes nudge-up { 50% { transform:translateY(-4px); } }
.facts { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:14px 16px; margin:12px 0 0; }
.facts > div:only-child { grid-column:1 / -1; }
.facts dt { font-size:13px; color:var(--muted); }
.facts dd { margin:2px 0 0; font-size:17px; font-weight:600; font-variant-numeric:tabular-nums; }
.facts dd .muted { font-size:13px; font-weight:400; }
.crow { display:grid; grid-template-columns:minmax(0,1.1fr) repeat(3, minmax(0,1fr)) 14px; align-items:center; gap:8px;
  padding:10px 6px; font-size:15px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.crow.head { font-size:13px; font-weight:600; color:var(--muted); padding-top:0; }
.crow.head::after { content:""; }
.crash { border-top:1px solid var(--line); }
.crash summary { cursor:pointer; list-style:none; border-radius:8px; }
.crash summary::-webkit-details-marker { display:none; }
.crash summary:hover, .crash[open] summary { background:var(--hl); }
.crash summary::after { content:""; justify-self:end; width:7px; height:7px; border:solid var(--muted); border-width:0 2px 2px 0;
  transform:rotate(45deg) translateY(-2px); transition:transform .25s; }
.crash[open] summary::after { transform:rotate(-135deg) translateY(-2px); }
.crash.now summary { font-weight:600; }
.why { padding:10px 6px 16px; }
.why h3 { font-size:15px; margin:0 0 4px; }
.why p { margin:0 0 8px; font-size:15px; }
.why .dates { font-size:13px; }
.why-foot { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; font-size:13px; }
.chip { font:inherit; font-size:13px; font-weight:600; padding:5px 12px; border-radius:99px; border:1px solid var(--line);
  background:var(--card); color:var(--fg); cursor:pointer; }
.chip.active { background:var(--fg); color:var(--card); border-color:var(--fg); }
.crash[open] .why { animation:open .3s ease-out; }
@media (max-width:500px) { .crow { font-size:14px; gap:6px; padding:10px 2px; } }
.chart-head h2 { flex:1 0 auto; }
.chart .high { fill:none; stroke:var(--muted); stroke-width:1.2; stroke-dasharray:4 4; }
.chart .drawin { stroke-dasharray:1; stroke-dashoffset:1; animation:draw 1.1s ease-out forwards; }
.chart .fadein { animation:fade 1.1s ease-out both; }
@keyframes draw { to { stroke-dashoffset:0; } }
@keyframes fade { from { opacity:0; } }
.flash { animation:flash 2.4s ease-out; border-radius:6px; }
@keyframes flash { from { background:var(--flash); } to { background:transparent; } }
@media (prefers-reduced-motion: reduce) {
  .flash, .status .dot, .today .arrow, .chart .fadein { animation:none !important; }
  .chart .drawin { animation:none; stroke-dashoffset:0; }
  .mark { transition:none; }
  .about[open] p, .crash[open] .why { animation:none; }
  .about summary::after { transition:none; }
}
"""

PAGE_JS = r"""
(function () {
  "use strict";
  var MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  var RANGES = { "1M": 1 / 12, "3M": 0.25, "1Y": 1, "5Y": 5, "Max": null };
  var calm = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  var D, retryAt = 0, frames = {};
  // dom: the visible window in days since 1970; hi: a shaded stretch; note: readout text while something is picked
  var st = { range: "1Y", view: "drop", dom: null, hi: null, note: null, zoom: false };
  try {
    var savedRange = localStorage.getItem("range"), savedView = localStorage.getItem("view");
    if (savedRange && RANGES.hasOwnProperty(savedRange)) st.range = savedRange;
    if (savedView === "drop" || savedView === "price") st.view = savedView;
  } catch (e) {}

  function $(id) { return document.getElementById(id); }
  function all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function save(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function pct(v) { return Math.abs(v) < 0.005 ? "0.00%" : (v > 0 ? "+" : "−") + Math.abs(v).toFixed(2) + "%"; }
  function money(v) { return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function day(n) { return new Date(n * 864e5); }
  function dstr(n, year) { var t = day(n); return t.getUTCDate() + " " + MON[t.getUTCMonth()] + (year === false ? "" : " " + t.getUTCFullYear()); }
  function when(iso) { return new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }); }
  function dur(ms) {
    var s = Math.max(0, Math.round(ms / 1000)), m = Math.floor(s / 60), h = Math.floor(m / 60);
    if (h >= 48) return Math.round(h / 24) + " days";
    if (h >= 1) return h + "h " + (m % 60) + "m";
    if (m >= 5) return m + " min";
    return m + "m " + (s % 60) + "s";
  }
  function kfmt(v, step) {
    if (v < 1000) return String(Math.round(v));
    return (v / 1000).toFixed(step >= 1000 ? 0 : step >= 100 ? 1 : 2) + "k";
  }

  // Run step(e) with e easing from 0 to 1. One animation per key; a new one replaces the old.
  function tween(key, ms, step, done) {
    cancelAnimationFrame(frames[key]);
    if (calm) { step(1); if (done) done(); return; }
    var t0 = performance.now();
    step(0);
    function f() {
      // the frame timestamp can be a little before t0, so measure and clamp rather than trust it
      var k = Math.max(0, Math.min(1, (performance.now() - t0) / ms)), e = 1 - Math.pow(1 - k, 3);
      step(e);
      if (k < 1) frames[key] = requestAnimationFrame(f); else if (done) done();
    }
    frames[key] = requestAnimationFrame(f);
  }

  // --- chart data --------------------------------------------------------------------------------------
  function lastDay() { var d = D.series.daily.d; return d[d.length - 1]; }
  function rangeDom(r) { var y = RANGES[r]; return [y ? lastDay() - Math.round(y * 365.25) : D.series.weekly.d[0], lastDay()]; }
  function lb(arr, v) { var lo = 0, hi = arr.length; while (lo < hi) { var m = (lo + hi) >> 1; if (arr[m] < v) lo = m + 1; else hi = m; } return lo; }
  function slice(dom) {
    var s = dom[0] >= D.series.daily.d[0] ? D.series.daily : D.series.weekly;  // daily data covers the last 5 years
    var i = Math.min(lb(s.d, dom[0]), s.d.length - 2), j = Math.max(i + 1, Math.min(s.d.length - 1, lb(s.d, dom[1] + 1) - 1));
    return { d: s.d.slice(i, j + 1), p: s.p.slice(i, j + 1), c: s.c.slice(i, j + 1) };
  }
  function niceTicks(min, max, count) {
    var raw = (max - min) / count || 1, mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10)), step = 10 * mag;
    [1, 2, 2.5, 5, 10].some(function (m) { if (m * mag >= raw) { step = m * mag; return true; } return false; });
    var lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, ticks = [];
    for (var k = 0; lo + k * step <= hi + step / 2; k++) ticks.push(lo + k * step);
    return { lo: lo, hi: hi, step: step, ticks: ticks };
  }
  function xticks(s, W) {
    var out = [], n = s.d.length, span = s.d[n - 1] - s.d[0], prev = null;
    var unit = span <= 50 ? "d" : span <= 800 ? "m" : "y", max = Math.max(2, Math.floor(W / 80));
    if (unit === "d") { for (var i = 0; i < n; i += Math.ceil(n / max)) out.push([i, dstr(s.d[i], false)]); return out; }
    for (var j = 0; j < n; j++) {
      var t = day(s.d[j]), k = unit === "m" ? t.getUTCFullYear() * 12 + t.getUTCMonth() : t.getUTCFullYear();
      if (prev !== null && k !== prev) out.push([j, t]);
      prev = k;
    }
    var every = Math.ceil(out.length / max);
    if (unit === "y") {
      every = [1, 2, 5, 10, 20].filter(function (x) { return x >= every; })[0] || 20;
      return out.filter(function (o) { return o[1].getUTCFullYear() % every === 0; }).map(function (o) { return [o[0], String(o[1].getUTCFullYear())]; });
    }
    return out.filter(function (o, i) { return i % every === 0; }).map(function (o, i) {
      var m = o[1].getUTCMonth();
      return [o[0], MON[m] + (m === 0 || i === 0 ? " ’" + String(o[1].getUTCFullYear()).slice(2) : "")];
    });
  }

  // --- chart drawing -----------------------------------------------------------------------------------
  // t morphs between the two views: 0 = % below the high, 1 = price with the running high as a dashed line.
  function render(o) {
    o = o || {};
    var box = $("chart"); if (!box || !D) return;
    var W = box.clientWidth || 640, H = box.clientHeight || 240, dom = o.dom || st.dom;
    var t = o.t != null ? o.t : st.view === "price" ? 1 : 0;
    var s = slice(dom), n = s.d.length, L = 50, R = 12, T = 10, B = 24;
    var weekly = dom[0] < D.series.daily.d[0];
    var high = s.c.map(function (c, i) { return c / (1 + s.p[i] / 100); });
    var dt = niceTicks(Math.min(-2, Math.min.apply(null, s.p) * 1.08), 0, 4);
    var pmin = Math.min.apply(null, s.c), pmax = Math.max.apply(null, high), pad = (pmax - pmin) * 0.05 || pmax * 0.02;
    var pt = niceTicks(pmin - pad, pmax + pad, 4);
    function x(i) { return L + (n < 2 ? 0 : i / (n - 1)) * (W - L - R); }
    function yd(v) { return T + v / dt.lo * (H - T - B); }
    function yp(v) { return T + (pt.hi - v) / (pt.hi - pt.lo) * (H - T - B); }
    function yl(i) { var a = yd(s.p[i]); return a + (yp(s.c[i]) - a) * t; }
    function yu(i) { var a = yd(0); return a + (yp(high[i]) - a) * t; }
    function grid(y, label, op) {
      return '<g opacity="' + op.toFixed(2) + '"><line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/>'
        + '<text x="' + (L - 6) + '" y="' + (y + 4) + '" text-anchor="end">' + label + "</text></g>";
    }
    var g = [], fadeDrop = Math.max(0, 1 - 2 * t), fadePrice = Math.max(0, 2 * t - 1);
    if (fadeDrop) dt.ticks.forEach(function (v) { g.push(grid(yd(v), v ? "−" + +(-v).toFixed(1) + "%" : "0%", fadeDrop)); });
    if (fadePrice) pt.ticks.forEach(function (v) { g.push(grid(yp(v), kfmt(v, pt.step), fadePrice)); });
    xticks(s, W).forEach(function (k) { g.push('<text x="' + x(k[0]) + '" y="' + (H - 6) + '" text-anchor="middle">' + k[1] + "</text>"); });

    var hl = "";
    if (st.hi) {
      var ia = lb(s.d, st.hi[0]), ib = Math.min(n - 1, lb(s.d, st.hi[1] + 1) - 1);
      if (ia < n && ib >= ia) hl = '<rect class="hl" x="' + x(ia) + '" y="' + T + '" width="' + Math.max(2, x(ib) - x(ia)) + '" height="' + (H - T - B) + '"/>';
    }
    var up = [], lo = [];
    for (var i = 0; i < n; i++) { var xi = x(i).toFixed(1); up.push(xi + "," + yu(i).toFixed(1)); lo.push(xi + "," + yl(i).toFixed(1)); }

    var marks = [], span = s.d[n - 1] - s.d[0];
    if (span > 3 * 365 || st.zoom) {
      var placed = [];
      D.crashes.slice().sort(function (p, q) { return p.pct - q.pct; }).forEach(function (c) {
        var td = Math.round(Date.parse(c.trough) / 864e5);
        if (td < s.d[0] || td > s.d[n - 1]) return;
        var j = Math.min(n - 1, lb(s.d, td)), cx = x(j), cy = yl(j);
        marks.push('<circle class="crash" cx="' + cx + '" cy="' + cy + '" r="3"/>');
        if (span > 3 * 365 && placed.every(function (px) { return Math.abs(px - cx) > 64; })) {
          placed.push(cx);
          var anchor = cx < L + 30 ? "start" : cx > W - 40 ? "end" : "middle";
          marks.push('<text class="crash-label" x="' + cx + '" y="' + Math.min(cy + 16, H - B - 4) + '" text-anchor="' + anchor + '">'
            + c.trough.slice(0, 4) + (c.from_record ? " −" + Math.round(-c.pct) + "%" : "") + "</text>");
        }
      });
    }
    box.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + (t > 0.5 ? "Price and all-time high" : "Percent below the all-time high")
      + ", " + dstr(s.d[0]) + " to " + dstr(s.d[n - 1]) + '">' + g.join("") + hl
      + '<polygon class="area' + (o.drawIn ? " fadein" : "") + '" points="' + up.join(" ") + " " + lo.slice().reverse().join(" ") + '"/>'
      + (t > 0 ? '<polyline class="high" opacity="' + t.toFixed(2) + '" points="' + up.join(" ") + '"/>' : "")
      + '<polyline class="line' + (o.drawIn ? " drawin" : "") + '" pathLength="1" points="' + lo.join(" ") + '"/>' + marks.join("")
      + '<line class="cross" id="cross" y1="' + T + '" y2="' + (H - B) + '" visibility="hidden"/>'
      + '<circle class="dot" id="dot" r="4" cx="' + x(n - 1) + '" cy="' + yl(n - 1) + '"/></svg>';

    var svg = box.firstChild, cross = $("cross"), dot = $("dot");
    function show(k) {
      if (k === null) { cross.setAttribute("visibility", "hidden"); dot.setAttribute("cx", x(n - 1)); dot.setAttribute("cy", yl(n - 1)); readout(); return; }
      cross.setAttribute("x1", x(k)); cross.setAttribute("x2", x(k)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(k)); dot.setAttribute("cy", yl(k));
      $("readout").textContent = (weekly ? "Week of " : "") + dstr(s.d[k]) + " · " + pct(s.p[k]) + " · " + money(s.c[k]);
    }
    function at(e) { var r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * W / r.width; return Math.max(0, Math.min(n - 1, Math.round((px - L) / (W - L - R) * (n - 1)))); }
    svg.addEventListener("pointermove", function (e) { show(at(e)); });
    svg.addEventListener("pointerdown", function (e) { show(at(e)); });
    svg.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse") show(null); });
    readout();
  }

  function readout() {
    $("readout").textContent = st.note || dstr(lastDay()) + " · " + pct(D.drop_pct) + " · " + money(D.last_close);
  }

  // --- navigation ----------------------------------------------------------------------------------------
  function pressed() {
    all("#ranges button").forEach(function (b) { b.setAttribute("aria-pressed", String(!st.zoom && b.dataset.r === st.range)); });
    all("#views button").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.v === st.view)); });
    $("chart-title").textContent = st.view === "price" ? "Price and all-time high" : "Below all-time high";
  }
  function go(dom) {  // glide the visible window to dom
    var from = st.dom || dom;
    st.dom = dom;
    pressed();
    tween("chart", 600, function (e) { render({ dom: [from[0] + (dom[0] - from[0]) * e, from[1] + (dom[1] - from[1]) * e] }); }, function () { render(); });
  }
  function clearPick() {
    all("#periods tr.active").forEach(function (t) { t.classList.remove("active"); });
    all("#crashes .chip.active").forEach(function (b) { b.classList.remove("active"); b.textContent = "Show on chart"; });
    st.hi = null; st.note = null; st.zoom = false;
  }
  function setRange(r) { clearPick(); st.range = r; save("range", r); go(rangeDom(r)); }
  function setView(v) {
    if (v === st.view) return;
    var a = st.view === "price" ? 1 : 0, b = 1 - a;
    st.view = v; save("view", v); pressed();
    tween("chart", 500, function (e) { render({ t: a + (b - a) * e }); }, function () { render(); });
  }
  function toChart() {
    var c = $("chart");
    if (c.getBoundingClientRect().top < 0) c.scrollIntoView({ block: "center", behavior: calm ? "auto" : "smooth" });
  }
  function pickPeriod(tr) {
    if (tr.classList.contains("active")) { setRange(st.range); return; }
    clearPick(); tr.classList.add("active");
    var from = +tr.dataset.from, span = lastDay() - from;
    st.range = ["1M", "3M", "1Y", "5Y", "Max"].filter(function (r) { return !RANGES[r] || RANGES[r] * 365.25 >= span * 1.15; })[0];
    st.hi = [from, lastDay()];
    st.note = tr.dataset.label + ": " + tr.dataset.pct + " since " + dstr(from);
    go(rangeDom(st.range)); toChart();
  }
  function pickCrash(btn) {
    if (btn.classList.contains("active")) { setRange(st.range); return; }
    clearPick(); btn.classList.add("active"); btn.textContent = "Back to " + st.range;
    var a = +btn.dataset.a, b = +btn.dataset.b, pad = Math.max(10, (b - a) * 0.12);
    st.zoom = true; st.hi = [a, b]; st.note = btn.dataset.note;
    go([a - pad, Math.min(lastDay(), b + pad)]); toChart();
  }
  function bindRow(tr, fn) {
    tr.addEventListener("click", function () { fn(tr); });
    tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(tr); } });
  }

  // --- headline animation --------------------------------------------------------------------------------
  function animateHero(prev) {
    var big = document.querySelector(".big"), to = D.drop_pct, from = prev ? prev.drop : 0;
    if (big && from !== to) tween("big", 1000, function (e) { big.textContent = pct(from + (to - from) * e); });
    var mark = document.querySelector(".mark");
    if (mark && !calm) {
      var target = mark.style.left;
      mark.style.transition = "none";
      mark.style.left = prev ? prev.mark : "0%";
      void mark.offsetWidth;  // apply the start position before transitioning
      mark.style.transition = "";
      mark.style.left = target;
    }
  }

  // --- live bits -----------------------------------------------------------------------------------------
  function tick() {
    if (!D) return;
    var now = Date.now(), mkt = $("mkt"), next = Date.parse(D.next_update_at), online = navigator.onLine !== false;
    if (D.market_open) { var c = Date.parse(D.market_closes_at) - now; mkt.textContent = c > 0 ? "closes in " + dur(c) : "closing…"; }
    else { var o = Date.parse(D.market_opens_at) - now; mkt.textContent = o > 0 ? "opens in " + dur(o) : "opening…"; }
    var overdue = now > next + 10 * 60e3;
    $("offline").hidden = online;
    $("stale").hidden = !overdue || !online;
    var sameAsOpen = !D.market_open && D.next_update_at === D.market_opens_at;  // the header already counts down to it
    $("next").textContent = overdue ? "" : now >= next ? " · updating…" : sameAsOpen ? "" : " · next update in " + dur(next - now);
    if (online && now > Math.max(next + 30e3, retryAt) && !document.hidden && location.protocol !== "file:") refresh();
  }

  function refresh() {
    retryAt = Date.now() + 5 * 60e3;  // back off until a newer copy arrives
    fetch(location.pathname, { cache: "no-cache" }).then(function (r) { return r.ok ? r.text() : Promise.reject(); }).then(function (txt) {
      var doc = new DOMParser().parseFromString(txt, "text/html"), fresh = doc.querySelector("main"), data = doc.getElementById("data");
      if (!fresh || !data || JSON.parse(data.textContent).updated_at === D.updated_at) return;  // the CDN still has the old copy
      var before = {}, changed = [], mark = document.querySelector(".mark");
      all("[data-k]").forEach(function (el) { before[el.dataset.k] = el.textContent; });
      Array.prototype.forEach.call(fresh.querySelectorAll("[data-k]"), function (el) { if (before[el.dataset.k] !== el.textContent) changed.push(el.dataset.k); });
      var prev = { drop: D.drop_pct, mark: mark ? mark.style.left : "0%" }, about = $("about");
      if (about && about.open) { var fa = fresh.querySelector("#about"); if (fa) fa.open = true; }
      document.querySelector("main").replaceWith(fresh);
      document.title = doc.title;
      init(prev);
      changed.forEach(function (k) { var el = document.querySelector('[data-k="' + k + '"]'); if (el) el.classList.add("flash"); });
    }).catch(function () {});
  }

  function share() {
    var b = $("share"), text = D.drop_pct >= 0 ? D.index + " is at an all-time high." : D.index + " is " + Math.abs(D.drop_pct).toFixed(2) + "% below its all-time high (" + D.zone.toLowerCase() + ").";
    if (navigator.share) { navigator.share({ title: document.title, text: text, url: location.href }).catch(function () {}); return; }
    if (navigator.clipboard) navigator.clipboard.writeText(text + " " + location.href).then(function () { var l = b.querySelector(".lbl") || b; l.textContent = "Copied"; setTimeout(function () { l.textContent = "Share"; }, 2000); });
  }

  function init(prev) {
    D = JSON.parse($("data").textContent);
    all("time[data-t]").forEach(function (t) { t.textContent = when(t.dataset.t); });
    all("#ranges button").forEach(function (b) { b.addEventListener("click", function () { setRange(b.dataset.r); }); });
    all("#views button").forEach(function (b) { b.addEventListener("click", function () { setView(b.dataset.v); }); });
    var refs = D.changes.filter(function (c) { return c.ref_date; });
    all("#periods tr[data-from]").forEach(function (tr, i) { tr.dataset.label = refs[i].label; tr.dataset.pct = pct(refs[i].pct); bindRow(tr, pickPeriod); });
    all("#crashes .chip").forEach(function (b) { b.addEventListener("click", function () { pickCrash(b); }); });
    $("share").addEventListener("click", share);
    clearPick();
    st.dom = rangeDom(st.range);
    pressed();
    render({ drawIn: true });
    animateHero(prev);
    tick();
  }

  init(null);
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", tick);
  addEventListener("online", tick);
  addEventListener("offline", tick);
  var pending = 0;
  addEventListener("resize", function () { cancelAnimationFrame(pending); pending = requestAnimationFrame(function () { render(); }); });
  if ("serviceWorker" in navigator && location.protocol === "https:") navigator.serviceWorker.register("/sw.js").catch(function () {});
})();
"""


def fetch_closes(retries: int | None = None, timeout: float | None = None) -> pd.Series:
    """Full daily history, keeping today's live bar while the market is open.

    Defaults to the patient settings in config.py; api/index.py passes the WEB_FETCH_* limits instead.
    """
    from .config import FETCH_RETRIES, FETCH_TIMEOUT_SECONDS
    from .data import _yf_history

    closes = _yf_history(INDEX_TICKER, period="max", retries=retries or FETCH_RETRIES,
                         timeout=timeout or FETCH_TIMEOUT_SECONDS)
    if closes.empty:
        raise RuntimeError(f"no price history for {INDEX_TICKER}")
    return closes


def render_error() -> str:
    """Shown only when the data can't be fetched and the CDN has no earlier copy to fall back on."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(TITLE)}</title><link rel="icon" href="{ICON_HREF}">
<style>{PAGE_CSS}</style></head>
<body><main>
  <header class="card top"><div class="id">{LOGO_SVG}<div><h1>{html.escape(INDEX_NAME)}</h1><div class="kicker">Drop from all-time high</div></div></div></header>
  <section class="card"><p style="margin:0">Market data couldn't be loaded right now. This page will try again in a minute.</p></section>
</main>
<script>setTimeout(function () {{ location.reload(); }}, 60 * 1000);</script></body></html>
"""


def public_json(s: dict) -> str:
    return json.dumps({k: v for k, v in s.items() if k != "series"}, indent=2)


def build(closes: pd.Series, outdir: Path) -> dict:
    s = summarise(closes)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "index.html").write_text(render_html(s), encoding="utf-8")
    (outdir / "data.json").write_text(public_json(s), encoding="utf-8")
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--outdir", default="site")
    args = ap.parse_args()
    s = build(fetch_closes(), Path(args.outdir))
    print(f"{s['index']} {s['last_date']}: {s['drop_pct']:+.2f}% from ATH -> {args.outdir}/index.html")


if __name__ == "__main__":
    main()
