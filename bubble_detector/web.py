"""Static web page: how far the index is below its all-time high. No buy targets.

    python -m bubble_detector.web [--outdir site]

Writes <outdir>/index.html (self-contained) and <outdir>/data.json for a local preview.
On Vercel the same page is served on demand by api/index.py and cached until the next hourly update.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

import pandas as pd

from .calc import compute_changes
from .config import CHART_LOOKBACK_DAYS, EXCHANGE_TZ, INDEX_NAME, INDEX_TICKER, MARKET_CLOSE_TIME, TITLE

DRAWDOWN_PERIODS = [("1 month", 1), ("3 months", 3), ("1 year", 12)]
MARKET_OPEN_TIME = "09:30"
UPDATE_MINUTE = 0  # data is refreshed at this minute past each hour


def market_is_open(last_bar: pd.Timestamp, now: datetime | None = None) -> bool:
    """True while the exchange is in session and the data has a bar for today (holidays have none)."""
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(EXCHANGE_TZ))
    hm = now.strftime("%H:%M")
    return now.weekday() < 5 and MARKET_OPEN_TIME <= hm < MARKET_CLOSE_TIME and last_bar.date() == now.date()


def next_update(now: datetime) -> datetime:
    nxt = now.replace(minute=UPDATE_MINUTE, second=0, microsecond=0)
    return nxt if nxt > now else nxt + timedelta(hours=1)


def summarise(closes: pd.Series, now: datetime | None = None) -> dict:
    """Everything the page shows, as plain JSON-able values. The last value may be a live intraday price."""
    now = now or datetime.now(timezone.utc)
    live = market_is_open(closes.index[-1], now)
    ath_ts = closes.idxmax()
    ath_close = float(closes.max())
    last_ts = closes.index[-1]
    last_close = float(closes.iloc[-1])
    dd = (closes / closes.cummax() - 1) * 100

    chart = dd.loc[last_ts - pd.Timedelta(days=CHART_LOOKBACK_DAYS):]
    worst = {}
    for label, months in DRAWDOWN_PERIODS:
        win = dd.loc[last_ts - pd.DateOffset(months=months):]
        worst[label] = {"pct": round(float(win.min()), 2), "date": win.idxmin().date().isoformat()}

    changes = [
        {"label": c.label, "pct": c.pct, "points": None if c.points is None else round(c.points, 2),
         "ref_date": c.ref_date.isoformat() if c.ref_date else None}
        for c in compute_changes(closes, (ath_ts.date(), ath_close))
        if c.label != "From ATH"
    ]
    return {
        "index": INDEX_NAME,
        "ticker": INDEX_TICKER,
        "last_date": last_ts.date().isoformat(),
        "last_close": round(last_close, 2),
        "ath_date": ath_ts.date().isoformat(),
        "ath_close": round(ath_close, 2),
        "drop_pct": round((last_close / ath_close - 1) * 100, 2),
        "drop_points": round(last_close - ath_close, 2),
        "worst_drawdown": worst,
        "changes": changes,
        "chart": [[d.date().isoformat(), round(float(v), 2)] for d, v in chart.items()],
        "market_open": live,
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "next_update_at": next_update(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _svg_chart(points: list[list], w: int = 640, h: int = 220) -> str:
    """Underwater chart: % below the running all-time high, 0 at the top."""
    if len(points) < 2:
        return ""
    vals = [p[1] for p in points]
    lo = min(min(vals), -5.0)
    lo = -((-lo // 5) + 1) * 5  # round down to a multiple of 5
    pad_l, pad_b = 40, 22
    cw, ch = w - pad_l - 6, h - pad_b - 6

    def xy(i: int, v: float) -> tuple[float, float]:
        return pad_l + i / (len(vals) - 1) * cw, 6 + (v / lo) * ch

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, v) for i, v in enumerate(vals)))
    x0, y0 = xy(0, 0)
    x1, _ = xy(len(vals) - 1, 0)
    grid = []
    step = 5 if lo >= -30 else 10
    for g in range(0, int(lo) - 1, -step):
        _, gy = xy(0, g)
        grid.append(f'<line x1="{pad_l}" x2="{w - 6}" y1="{gy:.1f}" y2="{gy:.1f}" class="grid"/>'
                    f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" text-anchor="end">{g}%</text>')
    first, last = points[0][0], points[-1][0]
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Percent below all-time high, {first} to {last}">'
        + "".join(grid)
        + f'<polygon points="{x0:.1f},{y0:.1f} {line} {x1:.1f},{y0:.1f}" class="area"/>'
        + f'<polyline points="{line}" class="line"/>'
        + f'<text x="{pad_l}" y="{h - 4}">{first}</text>'
        + f'<text x="{w - 6}" y="{h - 4}" text-anchor="end">{last}</text>'
        + "</svg>"
    )


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:+.2f}%".replace("-", "−")


def _cls(v: float | None) -> str:
    return "" if v is None else ("down" if v < 0 else "up")


def render_html(s: dict) -> str:
    e = html.escape
    rows = "".join(
        f'<tr><td>{e(c["label"])}</td><td class="num {_cls(c["pct"])}">{_pct(c["pct"])}</td>'
        f'<td class="num muted">{c["ref_date"] or ""}</td></tr>'
        for c in s["changes"]
    )
    worst = "".join(
        f'<tr><td>{e(k)}</td><td class="num down">{_pct(v["pct"])}</td><td class="num muted">{v["date"]}</td></tr>'
        for k, v in s["worst_drawdown"].items()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(TITLE)}</title>
<meta name="description" content="How far the {e(s["index"])} is below its all-time high.">
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e3e3e0; --down:#c4372b; --up:#1f7a4d; --area:rgba(196,55,43,.15); }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#141416; --card:#1e1e21; --fg:#f2f2f2; --muted:#9a9aa0; --line:#33333a; --down:#ff6b5e; --up:#4cc38a; --area:rgba(255,107,94,.18); }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:720px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:15px; font-weight:600; color:var(--muted); margin:0 0 16px; letter-spacing:.02em; text-transform:uppercase; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px; margin-bottom:16px; }}
.big {{ font-size:clamp(44px,12vw,72px); font-weight:700; line-height:1; color:var(--down); font-variant-numeric:tabular-nums; }}
.big.zero {{ color:var(--up); }}
.sub {{ color:var(--muted); margin-top:8px; }}
h2 {{ font-size:14px; font-weight:600; margin:0 0 10px; }}
table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
td {{ padding:7px 0; border-top:1px solid var(--line); }}
tr:first-child td {{ border-top:0; }}
.num {{ text-align:right; }}
.muted {{ color:var(--muted); font-size:13px; }}
.down {{ color:var(--down); }} .up {{ color:var(--up); }}
svg {{ width:100%; height:auto; display:block; }}
svg text {{ fill:var(--muted); font-size:11px; }}
svg .grid {{ stroke:var(--line); stroke-width:1; }}
svg .area {{ fill:var(--area); }}
svg .line {{ fill:none; stroke:var(--down); stroke-width:1.5; }}
footer {{ color:var(--muted); font-size:12px; text-align:center; }}
.status {{ display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; justify-content:space-between; font-size:14px; }}
.status .muted {{ font-size:14px; }}
.pill {{ display:inline-block; padding:2px 10px; border-radius:99px; font-size:12px; font-weight:600; border:1px solid var(--line); }}
.pill.open {{ color:var(--up); border-color:var(--up); }}
button {{ font:inherit; font-size:14px; padding:6px 14px; border-radius:8px; border:1px solid var(--line); background:var(--bg); color:var(--fg); cursor:pointer; }}
</style>
</head>
<body>
<main>
  <h1>{e(s["index"])} · drop from all-time high</h1>
  <section class="card">
    <div class="big{' zero' if s['drop_pct'] >= 0 else ''}">{_pct(s["drop_pct"])}</div>
    <div class="sub">{s["drop_points"]:+,.2f} points below the all-time high of {s["ath_close"]:,.2f} ({s["ath_date"]})</div>
    <div class="sub">{"Live price" if s["market_open"] else "Last close"} {s["last_close"]:,.2f} on {s["last_date"]}</div>
  </section>
  <section class="card status">
    <div>
      <span class="pill{' open' if s['market_open'] else ''}">{"US market open" if s["market_open"] else "US market closed"}</span>
      <div class="muted" style="margin-top:6px">Updated <time datetime="{s["updated_at"]}">{s["updated_at"]}</time><br>
      Next update around <time datetime="{s["next_update_at"]}">{s["next_update_at"]}</time></div>
    </div>
    <button onclick="location.reload()">Refresh</button>
  </section>
  <section class="card">
    <h2>Below all-time high, last {CHART_LOOKBACK_DAYS} days</h2>
    {_svg_chart(s["chart"])}
  </section>
  <section class="card">
    <h2>Change over time</h2>
    <table>{rows}</table>
  </section>
  <section class="card">
    <h2>Deepest drop below the high</h2>
    <table>{worst}</table>
  </section>
  <footer>Source: Yahoo Finance ({e(s["ticker"])}), updated hourly · <a href="/data.json" style="color:inherit">data.json</a></footer>
</main>
<script>
// Show times in the viewer's own time zone; reload by itself once a newer version should be up.
document.querySelectorAll("time").forEach(function (t) {{
  t.textContent = new Date(t.dateTime).toLocaleString([], {{ weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }});
}});
var next = new Date("{s["next_update_at"]}").getTime() + 60 * 1000;
setInterval(function () {{ if (Date.now() > next) location.reload(); }}, 60 * 1000);
</script>
</body>
</html>
"""


def fetch_closes() -> pd.Series:
    """Full daily history, keeping today's live bar while the market is open."""
    from .data import _yf_history

    closes = _yf_history(INDEX_TICKER, period="max")
    if closes.empty:
        raise RuntimeError(f"no price history for {INDEX_TICKER}")
    return closes


def public_json(s: dict) -> str:
    return json.dumps({k: v for k, v in s.items() if k != "chart"}, indent=2)


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
