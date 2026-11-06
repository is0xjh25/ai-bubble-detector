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


def _crashes(episodes: list[dict]) -> list[dict]:
    """Every fall of CRASH_PCT or worse, oldest first (the last may be ongoing)."""
    return [
        {"peak": _iso(ep["peak"]), "trough": _iso(ep["trough"]), "pct": round(ep["low"], 2),
         "recovered": _iso(ep["recovered"]) if ep["recovered"] is not None else None,
         "days_down": (ep["trough"] - ep["peak"]).days,
         "days_to_recover": (ep["recovered"] - ep["trough"]).days if ep["recovered"] is not None else None}
        for ep in episodes if ep["low"] <= CRASH_PCT + 1e-9  # 80/100 - 1 is -19.999...%, still a 20% fall
    ]


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
        "crashes": _crashes(episodes),
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
        items.append(("Lowest so far", f'<span class="down">{_pct(d["low_pct"])}</span> <span class="muted nw">({_day(d["low_date"])})</span>', "low"))
    facts = "".join(f'<div><dt>{label}</dt><dd data-k="{k}">{value}</dd></div>' for label, value, k in items)

    if not d:
        story = "No drop under way: the last close is a record."
    elif not d["past"]:
        story = f'The index has never been this far below its high since {s["history"]["from_year"]}.'
    else:
        p = d["past"]
        story = (f'Since {s["history"]["from_year"]}, <b>{p["count"]}</b> earlier drops got this deep. They hit bottom a '
                 f'median of <b>{_span(p["to_low_median_days"])}</b> after their high, and were back at the high a median '
                 f'of <b>{_span(p["recover_median_days"])}</b> after reaching {_pct(s["drop_pct"])} '
                 f'(fastest {_span(p["recover_min_days"])}, slowest {_span(p["recover_max_days"])}).')
    title = "This drop" if d else "Trend"
    return (f'<section class="card"><h2>{title}</h2><dl class="facts">{facts}</dl>'
            f'<p class="context" data-k="story">{story}</p></section>')


def _crash_table(s: dict) -> str:
    """Every fall of 20% or more, newest first, with the drop under way on top. Rows zoom the chart."""
    last = _days_since_epoch(s["last_date"])
    rows = []
    d = s["this_drop"]
    if d:
        note = f'This drop: {_pct(d["low_pct"])} so far, from the high on {_day(d["high_date"])}'
        rows.append(f'<tr class="now" data-a="{_days_since_epoch(d["high_date"])}" data-b="{last}" '
                    f'data-note="{html.escape(note)}" tabindex="0"><td>Now</td>'
                    f'<td class="num down">{_pct1(d["low_pct"])}</td><td class="num">{_span(d["days_since_high"])}</td>'
                    f'<td class="num muted">not yet</td></tr>')
    for c in reversed(s["crashes"]):
        if c["recovered"] is None:
            continue  # the drop under way, already shown as "Now"
        name = _crash_name(c)
        note = (f'{name}: {_pct(c["pct"])}, from the high on {_day(c["peak"])} to the low on {_day(c["trough"])}, '
                f'back at the high on {_day(c["recovered"])}')
        rows.append(f'<tr data-a="{_days_since_epoch(c["peak"])}" data-b="{_days_since_epoch(c["recovered"])}" '
                    f'data-note="{html.escape(note)}" tabindex="0"><td>{name}</td>'
                    f'<td class="num down">{_pct1(c["pct"])}</td>'
                    f'<td class="num">{_span(c["days_down"])}</td><td class="num">{_span(c["days_to_recover"])}</td></tr>')
    return (f'<section class="card"><h2>Past crashes</h2>'
            f'<p class="hint muted" style="margin:4px 0 10px">Every fall of {abs(CRASH_PCT):.0f}% or more since '
            f'{s["history"]["from_year"]}. Tap one to see it on the chart.</p>'
            f'<table id="crashes"><thead><tr><th>Crash</th><th class="num">Drop</th><th class="num">Fell for</th>'
            f'<th class="num">Recovered in</th></tr></thead><tbody>{"".join(rows)}</tbody></table></section>')


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

    h = s["history"]
    share = "under 1" if 0 < h["share_pct"] < 1 else f'{h["share_pct"]:.0f}'
    if h["at_high"]:
        context = f'A record close. Since {h["from_year"]}, <b>{share}%</b> of trading days have ended at a record.'
    else:
        context = (f'Since {h["from_year"]}, the index has been this far or further below its high on '
                   f'<b>{share}%</b> of trading days.')

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
    pill = "US market open" if s["market_open"] else "US market closed"
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
  <header class="top">
    <div><h1>{e(s["index"])}</h1><div class="kicker">Drop from all-time high</div></div>
    <span class="pill{' open' if s['market_open'] else ''}" id="mkt">{pill}</span>
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
    <p class="context" data-k="ctx">{context}</p>
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
    <div>Updated <time data-t="{s["updated_at"]}">{s["updated_at"]}</time><span id="next"></span></div>
    <div><button type="button" class="link" id="share">Share</button> · <a href="/data.json">data.json</a> · Source: Yahoo Finance ({e(s["ticker"])})</div>
    <div>Built by {e(AUTHOR)} · <a href="{e(REPO_URL)}">{e(REPO_URL.removeprefix("https://"))}</a></div>
    <p class="disclaimer">{e(DISCLAIMER)}</p>
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
.top { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-bottom:14px; }
h1 { font-size:17px; font-weight:650; margin:0; }
.kicker { color:var(--muted); font-size:13px; }
.pill { flex:none; padding:3px 10px; border-radius:99px; font-size:12px; font-weight:600; border:1px solid var(--line); color:var(--muted); white-space:nowrap; }
.pill.open { color:var(--up); border-color:var(--up); }
.pill.open::before { content:""; display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--up); margin-right:6px; vertical-align:1px; animation:pulse 2s infinite; }
@keyframes pulse { 50% { opacity:.3; } }
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
footer { color:var(--muted); font-size:13px; text-align:center; display:grid; gap:4px; }
.disclaimer { margin:10px auto 0; max-width:520px; font-size:12px; line-height:1.5; }
button.link { font:inherit; color:inherit; background:none; border:0; padding:0; text-decoration:underline; cursor:pointer; }
.today { font-size:15px; font-weight:650; font-variant-numeric:tabular-nums; white-space:nowrap; }
.today .arrow { display:inline-block; font-style:normal; font-size:12px; }
.today.down .arrow { animation:nudge-down .9s ease-in-out 3; }
.today.up .arrow { animation:nudge-up .9s ease-in-out 3; }
@keyframes nudge-down { 50% { transform:translateY(4px); } }
@keyframes nudge-up { 50% { transform:translateY(-4px); } }
.facts { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:14px 16px; margin:12px 0 0; }
.facts dt { font-size:13px; color:var(--muted); }
.facts dd { margin:2px 0 0; font-size:17px; font-weight:600; font-variant-numeric:tabular-nums; }
.facts dd .muted { font-size:13px; font-weight:400; }
#crashes td, #crashes th { white-space:nowrap; }
#crashes td { font-size:15px; }
@media (max-width:500px) { #crashes td { font-size:14px; } #crashes td.num { padding-left:8px; } }
.chart-head h2 { flex:1 0 auto; }
.chart .high { fill:none; stroke:var(--muted); stroke-width:1.2; stroke-dasharray:4 4; }
.chart .drawin { stroke-dasharray:1; stroke-dashoffset:1; animation:draw 1.1s ease-out forwards; }
.chart .fadein { animation:fade 1.1s ease-out both; }
@keyframes draw { to { stroke-dashoffset:0; } }
@keyframes fade { from { opacity:0; } }
.flash { animation:flash 2.4s ease-out; border-radius:6px; }
@keyframes flash { from { background:var(--flash); } to { background:transparent; } }
@media (prefers-reduced-motion: reduce) {
  .flash, .pill.open::before, .today .arrow, .chart .fadein { animation:none !important; }
  .chart .drawin { animation:none; stroke-dashoffset:0; }
  .mark { transition:none; }
  .about[open] p { animation:none; }
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
            + c.trough.slice(0, 4) + " −" + Math.round(-c.pct) + "%</text>");
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
  function clearPick() { all("tr.active").forEach(function (t) { t.classList.remove("active"); }); st.hi = null; st.note = null; st.zoom = false; }
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
  function pickCrash(tr) {
    if (tr.classList.contains("active")) { setRange(st.range); return; }
    clearPick(); tr.classList.add("active");
    var a = +tr.dataset.a, b = +tr.dataset.b, pad = Math.max(10, (b - a) * 0.12);
    st.zoom = true; st.hi = [a, b]; st.note = tr.dataset.note;
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
    if (D.market_open) { var c = Date.parse(D.market_closes_at) - now; mkt.textContent = c > 0 ? "Open · closes in " + dur(c) : "Closing…"; }
    else { var o = Date.parse(D.market_opens_at) - now; mkt.textContent = o > 0 ? "Closed · opens in " + dur(o) : "Opening…"; }
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
    if (navigator.clipboard) navigator.clipboard.writeText(text + " " + location.href).then(function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Share"; }, 2000); });
  }

  function init(prev) {
    D = JSON.parse($("data").textContent);
    all("time[data-t]").forEach(function (t) { t.textContent = when(t.dataset.t); });
    all("#ranges button").forEach(function (b) { b.addEventListener("click", function () { setRange(b.dataset.r); }); });
    all("#views button").forEach(function (b) { b.addEventListener("click", function () { setView(b.dataset.v); }); });
    var refs = D.changes.filter(function (c) { return c.ref_date; });
    all("#periods tr[data-from]").forEach(function (tr, i) { tr.dataset.label = refs[i].label; tr.dataset.pct = pct(refs[i].pct); bindRow(tr, pickPeriod); });
    all("#crashes tr[data-a]").forEach(function (tr) { bindRow(tr, pickCrash); });
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
  <header class="top"><div><h1>{html.escape(INDEX_NAME)}</h1><div class="kicker">Drop from all-time high</div></div></header>
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
