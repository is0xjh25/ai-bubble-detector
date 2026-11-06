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
from urllib.parse import quote

from zoneinfo import ZoneInfo

import pandas as pd

from .calc import compute_changes
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

ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
    '<rect x="100" y="100" width="824" height="824" rx="185" fill="#0a0a0a"/>'
    '<circle cx="512" cy="512" r="290" fill="none" stroke="#fff" stroke-width="34"/>'
    '<path d="M330 420 A 220 220 0 0 1 430 320" fill="none" stroke="#fff" stroke-width="34" stroke-linecap="round"/>'
    "</svg>"
)  # same as assets/icons/icon.svg
ICON_HREF = "data:image/svg+xml," + quote(ICON_SVG)


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


def _crashes(dd: pd.Series) -> list[dict]:
    """Every fall of CRASH_PCT or worse between one all-time high and the next (the last may be ongoing)."""
    out = []
    for _, g in dd.groupby((dd >= 0).cumsum()):  # each group starts on an all-time-high day
        low = float(g.min())
        if low > CRASH_PCT:
            continue
        after = dd.loc[g.index[-1]:].iloc[1:]
        out.append({"peak": _iso(g.index[0]), "trough": _iso(g.idxmin()), "pct": round(low, 2),
                    "recovered": _iso(after.index[0]) if len(after) else None})
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
        "crashes": _crashes(dd),
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

    pill = "US market open" if s["market_open"] else "US market closed"
    description = (f'{s["index"]} is at an all-time high.' if at_high
                   else f'{s["index"]} is {abs(s["drop_pct"]):.2f}% below its all-time high ({s["zone"].lower()}).')
    data = json.dumps(s, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(TITLE)} · {e(s["index"])} {drop_txt}</title>
<meta name="description" content="{e(description)}">
<meta property="og:title" content="{e(s["index"])}: {drop_txt} from its all-time high">
<meta property="og:description" content="{e(description)}">
<meta property="og:type" content="website">
<meta name="theme-color" content="#f7f7f5" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#141416" media="(prefers-color-scheme: dark)">
<link rel="icon" href="{ICON_HREF}">
<style>{PAGE_CSS}</style>
</head>
<body>
<main>
  <header class="top">
    <div><h1>{e(s["index"])}</h1><div class="kicker">Drop from all-time high</div></div>
    <span class="pill{' open' if s['market_open'] else ''}" id="mkt">{pill}</span>
  </header>
  <div class="banner" id="stale" hidden>Couldn't load newer data, so this is the copy from <time data-t="{s["updated_at"]}">{s["updated_at"]}</time>. Still retrying.</div>
  <section class="card hero">
    <div class="headline">
      <div class="big{' zero' if at_high else ''}" data-k="drop">{drop_txt}</div>
      <span class="zone {ZONE_CLASS[s["zone"]]}" data-k="zone">{e(s["zone"])}</span>
    </div>
    <div class="sub" data-k="vs">{vs_ath}</div>
    <div class="sub" data-k="price">{price}</div>
    <p class="context" data-k="ctx">{context}</p>
    {_gauge(s)}
  </section>
  <section class="card">
    <div class="chart-head">
      <h2>Below all-time high</h2>
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
  <footer>
    <div>Updated <time data-t="{s["updated_at"]}">{s["updated_at"]}</time><span id="next"></span></div>
    <div><button type="button" class="link" id="share">Share</button> · <a href="/data.json">data.json</a> · Source: Yahoo Finance ({e(s["ticker"])})</div>
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
.mark { position:absolute; top:-5px; width:4px; height:22px; margin-left:-2px; border-radius:2px; background:var(--fg); box-shadow:0 0 0 2px var(--card); }
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
tbody tr[data-from] { cursor:pointer; }
tbody tr[data-from]:hover td, tbody tr.active td { background:var(--hl); }
td:first-child, th:first-child { padding-left:6px; }
td:last-child, th:last-child { padding-right:6px; }
.num { text-align:right; }
.muted { color:var(--muted); }
.down { color:var(--down); } .up { color:var(--up); }
.hint { font-size:13px; margin:12px 0 0; }
footer { color:var(--muted); font-size:13px; text-align:center; display:grid; gap:4px; }
button.link { font:inherit; color:inherit; background:none; border:0; padding:0; text-decoration:underline; cursor:pointer; }
.flash { animation:flash 2.4s ease-out; border-radius:6px; }
@keyframes flash { from { background:var(--flash); } to { background:transparent; } }
@media (prefers-reduced-motion: reduce) { .flash, .pill.open::before { animation:none; } }
"""

PAGE_JS = r"""
(function () {
  "use strict";
  var MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  var RANGES = { "1M": 1 / 12, "3M": 0.25, "1Y": 1, "5Y": 5, "Max": null };
  var D, state = { range: "1Y", from: null }, retryAt = 0;
  try { var saved = localStorage.getItem("range"); if (saved && saved in RANGES) state.range = saved; } catch (e) {}

  function $(id) { return document.getElementById(id); }
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

  // --- chart ---------------------------------------------------------------------------------------
  function series() {
    var src = state.range === "Max" ? D.series.weekly : D.series.daily, years = RANGES[state.range], i = 0;
    if (years) { var start = src.d[src.d.length - 1] - Math.round(years * 365.25); while (src.d[i] < start) i++; }
    return { d: src.d.slice(i), p: src.p.slice(i), c: src.c.slice(i) };
  }
  function niceStep(span) { var c = [1, 2, 5, 10, 20, 25]; for (var k = 0; k < c.length; k++) if (span / c[k] <= 5) return c[k]; return 25; }
  function index(arr, v) { var i = 0; while (i < arr.length - 1 && arr[i] < v) i++; return i; }

  function xticks(s, W) {
    var out = [], n = s.d.length, prev = null, unit = state.range === "1M" ? "d" : (state.range === "3M" || state.range === "1Y") ? "m" : "y";
    if (unit === "d") { for (var i = 0; i < n; i += Math.ceil(n / 4)) out.push([i, dstr(s.d[i], false)]); return out; }
    for (var j = 0; j < n; j++) {
      var t = day(s.d[j]), k = unit === "m" ? t.getUTCFullYear() * 12 + t.getUTCMonth() : t.getUTCFullYear();
      if (prev !== null && k !== prev) out.push([j, unit === "m" ? MON[t.getUTCMonth()] + (t.getUTCMonth() === 0 ? " ’" + String(t.getUTCFullYear()).slice(2) : "") : String(t.getUTCFullYear()), t]);
      prev = k;
    }
    var max = Math.max(2, Math.floor(W / 80)), every = Math.ceil(out.length / max);
    if (unit === "y") { every = [1, 2, 5, 10, 20].filter(function (x) { return x >= every; })[0] || 20; return out.filter(function (o) { return o[2].getUTCFullYear() % every === 0; }); }
    return out.filter(function (o, i) { return i % every === 0; });
  }

  function draw() {
    var box = $("chart"); if (!box) return;
    var s = series(), n = s.d.length, W = box.clientWidth || 640, H = box.clientHeight || 240;
    var L = 44, R = 10, T = 10, B = 24, lo = Math.min(-2, Math.min.apply(null, s.p) * 1.08);
    var step = niceStep(-lo), bottom = -Math.ceil(-lo / step) * step;
    function x(i) { return L + (n < 2 ? 0 : i / (n - 1)) * (W - L - R); }
    function y(v) { return T + v / bottom * (H - T - B); }
    var g = [], line = [];
    for (var v = 0; v >= bottom; v -= step)
      g.push('<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '"/><text x="' + (L - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + (v ? "−" + (-v) : "0") + '%</text>');
    xticks(s, W).forEach(function (t) { g.push('<text x="' + x(t[0]) + '" y="' + (H - 6) + '" text-anchor="middle">' + t[1] + '</text>'); });
    for (var i = 0; i < n; i++) line.push(x(i).toFixed(1) + "," + y(s.p[i]).toFixed(1));
    var hl = "";
    if (state.from !== null) { var a = x(index(s.d, state.from)); hl = '<rect class="hl" x="' + a + '" y="' + T + '" width="' + (W - R - a) + '" height="' + (H - T - B) + '"/>'; }
    var marks = [];
    if (state.range === "5Y" || state.range === "Max") {
      var placed = [];
      D.crashes.slice().sort(function (p, q) { return p.pct - q.pct; }).forEach(function (c) {
        var td = Math.round(Date.parse(c.trough) / 864e5); if (td < s.d[0]) return;
        var j = index(s.d, td), cx = x(j), cy = y(s.p[j]);
        marks.push('<circle class="crash" cx="' + cx + '" cy="' + cy + '" r="3"/>');
        if (placed.every(function (px) { return Math.abs(px - cx) > 64; })) {
          placed.push(cx);
          var anchor = cx < L + 30 ? "start" : cx > W - 40 ? "end" : "middle";
          marks.push('<text class="crash-label" x="' + cx + '" y="' + Math.min(cy + 16, H - B - 4) + '" text-anchor="' + anchor + '">' + c.trough.slice(0, 4) + " −" + Math.round(-c.pct) + "%" + '</text>');
        }
      });
    }
    box.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Percent below the all-time high, ' + dstr(s.d[0]) + ' to ' + dstr(s.d[n - 1]) + '">'
      + g.join("") + hl
      + '<polygon class="area" points="' + x(0) + ',' + y(0) + ' ' + line.join(" ") + ' ' + x(n - 1) + ',' + y(0) + '"/>'
      + '<polyline class="line" points="' + line.join(" ") + '"/>' + marks.join("")
      + '<line class="cross" id="cross" y1="' + T + '" y2="' + (H - B) + '" visibility="hidden"/>'
      + '<circle class="dot" id="dot" r="4" cx="' + x(n - 1) + '" cy="' + y(s.p[n - 1]) + '"/></svg>';

    var svg = box.firstChild, cross = $("cross"), dot = $("dot");
    function show(i) {
      if (i === null) { cross.setAttribute("visibility", "hidden"); dot.setAttribute("cx", x(n - 1)); dot.setAttribute("cy", y(s.p[n - 1])); readout(); return; }
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(s.p[i]));
      $("readout").textContent = (state.range === "Max" ? "Week of " : "") + dstr(s.d[i]) + " · " + pct(s.p[i]) + " · " + money(s.c[i]);
    }
    function at(e) { var r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * W / r.width; return Math.max(0, Math.min(n - 1, Math.round((px - L) / (W - L - R) * (n - 1)))); }
    svg.addEventListener("pointermove", function (e) { show(at(e)); });
    svg.addEventListener("pointerdown", function (e) { show(at(e)); });
    svg.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse") show(null); });
    readout();
  }

  function readout() {
    var el = $("readout"), row = document.querySelector("#periods tr.active");
    if (row) { el.textContent = row.dataset.label + ": " + row.dataset.pct + " since " + dstr(+row.dataset.from); return; }
    el.textContent = dstr(Math.round(Date.parse(D.last_date) / 864e5)) + " · " + pct(D.drop_pct) + " · " + money(D.last_close);
  }

  function setRange(r) {
    state.range = r;
    try { localStorage.setItem("range", r); } catch (e) {}
    document.querySelectorAll("#ranges button").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.r === r)); });
    draw();
  }

  function pickRow(tr) {
    var on = !tr.classList.contains("active");
    document.querySelectorAll("#periods tr.active").forEach(function (t) { t.classList.remove("active"); });
    state.from = null;
    if (on) {
      tr.classList.add("active");
      state.from = +tr.dataset.from;
      var last = D.series.daily.d[D.series.daily.d.length - 1], span = last - state.from;
      var fit = ["1M", "3M", "1Y", "5Y", "Max"].filter(function (r) { return !RANGES[r] || RANGES[r] * 365.25 >= span * 1.15; })[0];
      setRange(fit);
      var c = $("chart"); if (c.getBoundingClientRect().top < 0) c.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    } else draw();
  }

  // --- live bits -----------------------------------------------------------------------------------
  function tick() {
    if (!D) return;
    var now = Date.now(), mkt = $("mkt"), next = Date.parse(D.next_update_at);
    if (D.market_open) { var c = Date.parse(D.market_closes_at) - now; mkt.textContent = c > 0 ? "Open · closes in " + dur(c) : "Closing…"; }
    else { var o = Date.parse(D.market_opens_at) - now; mkt.textContent = o > 0 ? "Closed · opens in " + dur(o) : "Opening…"; }
    var overdue = now > next + 10 * 60e3;
    $("stale").hidden = !overdue;
    var sameAsOpen = !D.market_open && D.next_update_at === D.market_opens_at;  // the header already counts down to it
    $("next").textContent = overdue ? "" : now >= next ? " · updating…" : sameAsOpen ? "" : " · next update in " + dur(next - now);
    if (now > Math.max(next + 30e3, retryAt) && !document.hidden && location.protocol !== "file:") refresh();
  }

  function refresh() {
    retryAt = Date.now() + 5 * 60e3;  // back off until a newer copy arrives
    fetch(location.pathname, { cache: "no-cache" }).then(function (r) { return r.ok ? r.text() : Promise.reject(); }).then(function (txt) {
      var doc = new DOMParser().parseFromString(txt, "text/html"), fresh = doc.querySelector("main"), data = doc.getElementById("data");
      if (!fresh || !data || JSON.parse(data.textContent).updated_at === D.updated_at) return;  // the CDN still has the old copy
      var before = {};
      document.querySelectorAll("[data-k]").forEach(function (el) { before[el.dataset.k] = el.textContent; });
      document.querySelector("main").replaceWith(fresh);
      document.title = doc.title;
      init();
      document.querySelectorAll("[data-k]").forEach(function (el) { if (before[el.dataset.k] !== el.textContent) el.classList.add("flash"); });
    }).catch(function () {});
  }

  function share() {
    var b = $("share"), text = D.drop_pct >= 0 ? D.index + " is at an all-time high." : D.index + " is " + Math.abs(D.drop_pct).toFixed(2) + "% below its all-time high (" + D.zone.toLowerCase() + ").";
    if (navigator.share) { navigator.share({ title: document.title, text: text, url: location.href }).catch(function () {}); return; }
    if (navigator.clipboard) navigator.clipboard.writeText(text + " " + location.href).then(function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Share"; }, 2000); });
  }

  function init() {
    D = JSON.parse($("data").textContent);
    document.querySelectorAll("time[data-t]").forEach(function (t) { t.textContent = when(t.dataset.t); });
    document.querySelectorAll("#ranges button").forEach(function (b) { b.addEventListener("click", function () { state.from = null; document.querySelectorAll("#periods tr.active").forEach(function (t) { t.classList.remove("active"); }); setRange(b.dataset.r); }); });
    document.querySelectorAll("#periods tr[data-from]").forEach(function (tr, i) {
      var c = D.changes.filter(function (x) { return x.ref_date; })[i];
      tr.dataset.label = c.label; tr.dataset.pct = pct(c.pct);
      tr.addEventListener("click", function () { pickRow(tr); });
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickRow(tr); } });
    });
    $("share").addEventListener("click", share);
    state.from = null;
    setRange(state.range);
    tick();
  }

  init();
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", tick);
  var pending = 0;
  addEventListener("resize", function () { cancelAnimationFrame(pending); pending = requestAnimationFrame(draw); });
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
