"""Outputs: console text, history CSV, self-contained HTML (inline SVG charts), PDF."""

from __future__ import annotations

import html
import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from .calc import Snapshot, history_columns
from .config import CHART_LOOKBACK_DAYS, HISTORY_FILENAME, LADDER, SMA_WINDOW, TAGLINE, TITLE

# ------------------------------------------------------------------ formatting

def fnum(x: float | None, dp: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:,.{dp}f}"


def fsigned(x: float | None, dp: int = 2, suffix: str = "") -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:+,.{dp}f}{suffix}"


def fdate(d: date | None) -> str:
    return "n/a" if d is None else d.isoformat()


# ------------------------------------------------------------------ console

def _text_table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> str:
    right = right if right is not None else set(range(1, len(headers)))
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        return "  ".join(
            (str(c).rjust(widths[i]) if i in right else str(c).ljust(widths[i])) for i, c in enumerate(cells)
        )

    sep = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(headers), sep, *(fmt(r) for r in rows)])


def render_console(s: Snapshot) -> str:
    bar = "=" * 72
    out = [bar, f"  {TITLE}  -  {s.index_name} ({s.index_ticker})", bar,
           f"Last close : {fnum(s.last_close)}  on {fdate(s.last_date)}",
           f"ATH close  : {fnum(s.ath_close)}  on {fdate(s.ath_date)}   drawdown {fsigned(s.drawdown_pct, 2, '%')}",
           "",
           f">>> {s.action.headline.upper() if s.action.signal != 'NONE' else s.action.headline} <<<",
           f"    {s.action.detail}",
           ""]

    out.append("CHANGES")
    out.append(_text_table(
        ["Period", "Ref date", "Ref close", "Points", "Change %"],
        [[c.label, fdate(c.ref_date), fnum(c.ref_close), fsigned(c.points), fsigned(c.pct, 2, "%")] for c in s.changes],
    ))
    out.append("")

    out.append(f"{s.sma.window}-DAY SMA")
    if s.sma.value is None:
        out.append(f"  not enough history ({s.sma.window} closes needed)")
    else:
        side = "ABOVE" if s.sma.above else "BELOW"
        out.append(f"  SMA {fnum(s.sma.value)}   index is {side} by {fsigned(s.sma.pct_vs_sma, 2, '%')}   "
                   f"slope {s.sma.slope or 'n/a'} (vs {s.sma.slope_lookback} trading days ago: {fnum(s.sma.value_lookback)})")
    out.append("")

    out.append("CRASH-BUY LADDER (from ATH)")
    rows = [[f"{l.name} ({l.pct_from_ath:+.0f}%)", fnum(l.level), fsigned(l.points_from_close),
             fsigned(l.pct_to_fall, 2, "%"), l.status] for l in s.ladder]
    rows.append([f"{s.recovery.name} (recovery)", "-", "-", "-", s.recovery.state])
    out.append(_text_table(["Rung", "Trigger level", "Points from close", "% still to fall", "Status"], rows,
                           right={1, 2, 3}))
    out.append("")

    if s.etfs:
        out.append("ETF REFERENCE  (reference only, triggers are on the index)")
        out.append(_text_table(
            ["Ticker", "Date", "Last close", "Day %"],
            [[e.ticker, fdate(e.close_date), fnum(e.close), fsigned(e.day_change_pct, 2, "%")] if e.available
             else [e.ticker, "n/a", "unavailable", "n/a"] for e in s.etfs],
        ))
        out.append("")
    out.append(f"Source: {s.source}   generated {s.generated_at}")
    return "\n".join(out)


# ------------------------------------------------------------------ history csv

def read_history(outdir: Path) -> pd.DataFrame:
    path = outdir / HISTORY_FILENAME
    if not path.exists():
        return pd.DataFrame(columns=history_columns())
    try:
        return pd.read_csv(path, dtype={"date": str, "ath_date": str})
    except Exception:
        return pd.DataFrame(columns=history_columns())


def previous_logged_row(history: pd.DataFrame, last_date: date) -> dict | None:
    """Most recent logged row strictly before `last_date` (so a same-day re-run compares with yesterday's run)."""
    if history is None or history.empty or "date" not in history.columns:
        return None
    earlier = history[history["date"].astype(str) < last_date.isoformat()]
    if earlier.empty:
        return None
    row = earlier.sort_values("date").iloc[-1].to_dict()
    return {k: (None if (isinstance(v, float) and math.isnan(v)) else v) for k, v in row.items()}


def append_history(outdir: Path, row: dict) -> pd.DataFrame:
    """Append one row to history.csv, deduping on `date` (latest run wins). Returns the full log."""
    path = outdir / HISTORY_FILENAME
    cols = history_columns()
    new = pd.DataFrame([row])
    if path.exists():
        try:
            old = pd.read_csv(path, dtype={"date": str, "ath_date": str})
        except Exception as exc:  # corrupt file: keep going, start fresh but keep a backup
            print(f"warning: could not read {path} ({exc}); backing it up and starting a new log", file=sys.stderr)
            path.rename(path.with_suffix(".csv.bak"))
            old = pd.DataFrame(columns=cols)
        combined = pd.concat([old, new], ignore_index=True)
    else:
        combined = new
    for c in cols:
        if c not in combined.columns:
            combined[c] = None
    combined = combined.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    combined = combined[cols + [c for c in combined.columns if c not in cols]]
    combined.to_csv(path, index=False)
    return combined


# ------------------------------------------------------------------ retention

_DATED_REPORT = re.compile(r"^report_(\d{4}-\d{2}-\d{2})\.(html|pdf)$")


def prune_reports(outdir: Path, keep_days: int, today: date) -> list[Path]:
    """Delete report_YYYY-MM-DD.* files dated more than `keep_days` before `today`.
    Only files matching that exact pattern are considered; 0 disables pruning."""
    if keep_days <= 0:
        return []
    removed: list[Path] = []
    for path in outdir.iterdir():
        m = _DATED_REPORT.match(path.name)
        if not m:
            continue
        try:
            file_date = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if (today - file_date).days > keep_days:
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                print(f"warning: could not delete {path.name} ({exc})", file=sys.stderr)
    return removed


# ------------------------------------------------------------------ svg charts

PALETTE = {  # validated default palette from the dataviz reference (light / dark)
    "close": ("#2a78d6", "#3987e5"),
    "sma": ("#eb6834", "#d95926"),
    "fired": ("#e34948", "#e66767"),
}


def _nice_ticks(lo: float, hi: float, n: int = 6) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=raw)
    start = math.ceil(lo / step) * step
    ticks = []
    v = start
    while v <= hi + 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _month_ticks(dates: list[pd.Timestamp]) -> list[tuple[pd.Timestamp, str]]:
    firsts: list[pd.Timestamp] = []
    seen = set()
    for d in dates:
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            firsts.append(d)
    # drop a partial first month whose tick would collide with the next one
    if len(firsts) > 1 and (firsts[1] - firsts[0]).days < 15:
        firsts = firsts[1:]
    ticks = [(d, d.strftime("%b %y") if d.month == 1 or i == 0 else d.strftime("%b")) for i, d in enumerate(firsts)]
    if len(ticks) > 14:  # thin out for long ranges
        ticks = ticks[:: math.ceil(len(ticks) / 12)]
    return ticks


# Presentation attributes mirroring the dark-theme CSS. Browsers let the stylesheet override these;
# PDF renderers that ignore page CSS inside inline SVG fall back to them.
_SVG_ATTRS: dict[str, dict[str, str]] = {
    "grid": {"stroke": "#2e2e2b", "stroke-width": "1"},
    "axis": {"stroke": "#8a8984", "stroke-width": "1"},
    "tick": {"fill": "#c3c2b7", "font-size": "12", "font-family": "Helvetica, Arial, sans-serif"},
    "hlabel": {"fill": "#c3c2b7", "font-size": "12", "font-family": "Helvetica, Arial, sans-serif"},
    "fired-label": {"fill": "#e66767", "font-weight": "600"},
    "close-label": {"fill": "#3987e5", "font-weight": "600"},
    "close": {"fill": "none", "stroke": "#3987e5", "stroke-width": "2", "stroke-linejoin": "round"},
    "close-dot": {"fill": "#3987e5", "stroke": "#1a1a19", "stroke-width": "2"},
    "sma": {"fill": "none", "stroke": "#d95926", "stroke-width": "2"},
    "ath": {"stroke": "#c3c2b7", "stroke-width": "1.5", "stroke-dasharray": "6 4"},
    "fired": {"stroke": "#e66767", "stroke-width": "2"},
    "waiting": {"stroke": "#6f6e69", "stroke-width": "1.5", "stroke-dasharray": "3 4"},
    "cross": {"stroke": "#8a8984", "stroke-width": "1", "stroke-dasharray": "2 3"},
    "hover-dot": {"fill": "#3987e5", "stroke": "#1a1a19", "stroke-width": "2"},
}
_CLASS_RE = re.compile(r'class="([^"]+)"')


def _inline_presentation(svg: str) -> str:
    def repl(m: re.Match) -> str:
        attrs: dict[str, str] = {}
        for token in m.group(1).split():
            attrs.update(_SVG_ATTRS.get(token, {}))
        extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
        return m.group(0) + extra
    return _CLASS_RE.sub(repl, svg)


def _path(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))


def svg_price_chart(closes: pd.Series, sma: pd.Series, s: Snapshot, chart_id: str = "price") -> str:
    """Last-year closes with ATH, SMA and the four trigger levels as horizontal lines."""
    W, H = 960, 440
    ml, mr, mt, mb = 72, 150, 24, 44
    pw, ph = W - ml - mr, H - mt - mb

    dates = list(closes.index)
    if len(dates) < 2:
        return "<p>Not enough data to chart.</p>"
    x0, x1 = dates[0].value, dates[-1].value
    levels = [l.level for l in s.ladder]
    sma_vals = sma.dropna()
    y_lo = min([closes.min(), *levels] + ([sma_vals.min()] if not sma_vals.empty else []))
    y_hi = max([closes.max(), s.ath_close] + ([sma_vals.max()] if not sma_vals.empty else []))
    pad = (y_hi - y_lo) * 0.04
    y_lo, y_hi = y_lo - pad, y_hi + pad

    def X(ts: pd.Timestamp) -> float:
        return ml + (ts.value - x0) / (x1 - x0) * pw

    def Y(v: float) -> float:
        return mt + (1 - (v - y_lo) / (y_hi - y_lo)) * ph

    g = [f'<svg class="chart" id="{chart_id}" viewBox="0 0 {W} {H}" width="100%" role="img" '
         f'aria-label="{s.index_name} closes, last year, with ATH, {SMA_WINDOW}-day SMA and ladder levels">']
    # grid + y axis
    for t in _nice_ticks(y_lo, y_hi):
        y = Y(t)
        g.append(f'<line class="grid" x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        g.append(f'<text class="tick" x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end">{t:,.0f}</text>')
    for ts, label in _month_ticks(dates):
        x = X(ts)
        g.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{mt + ph}" y2="{mt + ph + 5}"/>')
        g.append(f'<text class="tick" x="{x:.1f}" y="{mt + ph + 20}" text-anchor="middle">{label}</text>')
    g.append(f'<line class="axis" x1="{ml}" x2="{ml + pw}" y1="{mt + ph}" y2="{mt + ph}"/>')

    # ATH + ladder levels
    def hline(v: float, cls: str, label: str) -> None:
        y = Y(v)
        g.append(f'<line class="{cls}" x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        g.append(f'<text class="hlabel {cls}-label" x="{ml + pw + 8}" y="{y + 4:.1f}">{html.escape(label)}</text>')

    hline(s.ath_close, "ath", f"ATH {s.ath_close:,.0f}")
    for l in s.ladder:
        hline(l.level, "fired" if l.fired else "waiting", f"{l.name} {l.pct_from_ath:+.0f}% · {l.level:,.0f}")

    # series
    if not sma_vals.empty:
        g.append(f'<path class="sma" d="{_path([(X(d), Y(v)) for d, v in sma_vals.items()])}"/>')
    g.append(f'<path class="close" d="{_path([(X(d), Y(v)) for d, v in closes.items()])}"/>')
    lx, ly = X(dates[-1]), Y(float(closes.iloc[-1]))
    g.append(f'<circle class="close-dot" cx="{lx:.1f}" cy="{ly:.1f}" r="4"/>')
    g.append(f'<text class="hlabel close-label" x="{lx - 6:.1f}" y="{ly - 10:.1f}" text-anchor="end">'
             f'{float(closes.iloc[-1]):,.0f}</text>')

    # hover layer (JS fills these in); harmless in PDF
    g.append(f'<line class="cross" id="{chart_id}-cross" x1="0" x2="0" y1="{mt}" y2="{mt + ph}" visibility="hidden"/>')
    g.append(f'<circle class="hover-dot" id="{chart_id}-dot" r="5" visibility="hidden"/>')
    g.append(f'<rect class="hit" x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="transparent"/>')
    g.append("</svg>")

    data = {
        "x": [X(d) for d in dates],
        "labels": [d.strftime("%Y-%m-%d") for d in dates],
        "close": [round(float(v), 2) for v in closes],
        "closeY": [Y(float(v)) for v in closes],
        "sma": [None if pd.isna(sma.get(d)) else round(float(sma.get(d)), 2) for d in dates],
        "unit": "",
    }
    return _inline_presentation("".join(g)) + f'<script type="application/json" id="{chart_id}-data">{json.dumps(data)}</script>'


def svg_drawdown_chart(history: pd.DataFrame, chart_id: str = "dd") -> str:
    """Drawdown % per logged run, from history.csv, with ladder levels as reference lines."""
    df = history.dropna(subset=["drawdown_pct"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    if len(df) < 2:
        return ""
    W, H = 960, 260
    ml, mr, mt, mb = 72, 150, 20, 40
    pw, ph = W - ml - mr, H - mt - mb
    dates = list(df["date"])
    vals = [float(v) for v in df["drawdown_pct"]]
    x0, x1 = dates[0].value, dates[-1].value
    y_hi = max(0.0, max(vals)) + 1
    y_lo = min(min(vals) * 1.15, -30.0)
    visible_levels = [(n, p) for n, p in LADDER if p >= y_lo]

    def X(ts: pd.Timestamp) -> float:
        return ml + (ts.value - x0) / (x1 - x0) * pw

    def Y(v: float) -> float:
        return mt + (1 - (v - y_lo) / (y_hi - y_lo)) * ph

    g = [f'<svg class="chart" id="{chart_id}" viewBox="0 0 {W} {H}" width="100%" role="img" '
         f'aria-label="Drawdown from ATH over logged runs">']
    for t in _nice_ticks(y_lo, y_hi, 5):
        y = Y(t)
        g.append(f'<line class="grid" x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        g.append(f'<text class="tick" x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end">{t:+.0f}%</text>')
    ticks = _month_ticks(dates) if len(dates) > 20 else [(d, d.strftime("%d %b")) for d in dates]
    if len(ticks) > 12:
        ticks = ticks[:: math.ceil(len(ticks) / 12)]
    for ts, label in ticks:
        g.append(f'<text class="tick" x="{X(ts):.1f}" y="{mt + ph + 18}" text-anchor="middle">{label}</text>')
    g.append(f'<line class="axis" x1="{ml}" x2="{ml + pw}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
    for name, pct in visible_levels:
        y = Y(pct)
        g.append(f'<line class="waiting" x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        g.append(f'<text class="hlabel waiting-label" x="{ml + pw + 8}" y="{y + 4:.1f}">{name} {pct:+.0f}%</text>')
    pts = [(X(d), Y(v)) for d, v in zip(dates, vals)]
    g.append(f'<path class="close" d="{_path(pts)}"/>')
    for x, y in pts:
        g.append(f'<circle class="close-dot" cx="{x:.1f}" cy="{y:.1f}" r="3"/>')
    g.append(f'<line class="cross" id="{chart_id}-cross" x1="0" x2="0" y1="{mt}" y2="{mt + ph}" visibility="hidden"/>')
    g.append(f'<circle class="hover-dot" id="{chart_id}-dot" r="5" visibility="hidden"/>')
    g.append(f'<rect class="hit" x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="transparent"/>')
    g.append("</svg>")
    data = {"x": [p[0] for p in pts], "labels": [d.strftime("%Y-%m-%d") for d in dates],
            "close": vals, "closeY": [p[1] for p in pts], "sma": [None] * len(pts), "unit": "%"}
    return _inline_presentation("".join(g)) + f'<script type="application/json" id="{chart_id}-data">{json.dumps(data)}</script>'


# ------------------------------------------------------------------ html

CSS = """
:root { color-scheme: light dark;
  --bg:#f6f6f4; --surface:#fcfcfb; --text:#0b0b0b; --text-2:#52514e; --muted:#8a8984; --line:#e2e1dc;
  --close:#2a78d6; --sma:#eb6834; --fired:#e34948; --fired-bg:#fdeceb; --wait:#9a9994; --ath:#52514e; --ok:#008300; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#111110; --surface:#1a1a19; --text:#f0efec; --text-2:#c3c2b7; --muted:#8a8984; --line:#2e2e2b;
  --close:#3987e5; --sma:#d95926; --fired:#e66767; --fired-bg:#3a1f1f; --wait:#6f6e69; --ath:#c3c2b7; --ok:#3fb950; } }
:root[data-theme="dark"] {
  --bg:#111110; --surface:#1a1a19; --text:#f0efec; --text-2:#c3c2b7; --muted:#8a8984; --line:#2e2e2b;
  --close:#3987e5; --sma:#d95926; --fired:#e66767; --fired-bg:#3a1f1f; --wait:#6f6e69; --ath:#c3c2b7; --ok:#3fb950; }
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
  font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width:1040px; margin:0 auto; }
h1 { font-size:28px; margin:0 0 4px; }
h2 { font-size:17px; margin:28px 0 10px; color:var(--text-2); text-transform:uppercase; letter-spacing:.04em; }
.sub { color:var(--text-2); margin:0 0 18px; }
.kpis { display:flex; flex-wrap:wrap; gap:12px; }
.kpi { flex:1 1 200px; background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.kpi .l { color:var(--text-2); font-size:13px; }
.kpi .v { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; }
.kpi .d { color:var(--muted); font-size:13px; }
table { width:100%; border-collapse:collapse; background:var(--surface); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
th, td { padding:8px 12px; text-align:right; border-top:1px solid var(--line); font-variant-numeric:tabular-nums; }
th { color:var(--text-2); font-weight:600; font-size:13px; border-top:none; }
th:first-child, td:first-child { text-align:left; }
tr.fired td { background:var(--fired-bg); font-weight:600; }
.pos { color:var(--ok); } .neg { color:var(--fired); }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:600; border:1px solid var(--line); color:var(--text-2); }
.badge.fired { background:var(--fired); color:#fff; border-color:var(--fired); }
.badge.active { background:var(--ok); color:#fff; border-color:var(--ok); }
.note { color:var(--muted); font-size:13px; }
.action { display:flex; gap:14px; align-items:center; padding:14px 18px; border-radius:10px; margin:0 0 18px;
  background:var(--surface); border:1px solid var(--line); }
.action .tag { flex:none; font-size:12px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; padding:4px 10px; border-radius:999px; border:1px solid var(--line); color:var(--text-2); }
.action .h { font-size:18px; font-weight:600; }
.action .d { color:var(--text-2); font-size:14px; margin-top:2px; }
.action.buy { background:var(--fired); border-color:var(--fired); color:#fff; }
.action.buy .tag { background:#fff; color:var(--fired); border-color:#fff; }
.action.buy .d { color:#fff; opacity:.9; }
.action.recovery { background:var(--ok); border-color:var(--ok); color:#fff; }
.action.recovery .tag { background:#fff; color:var(--ok); border-color:#fff; }
.action.recovery .d { color:#fff; opacity:.9; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px; position:relative; }
.legend { display:flex; gap:18px; flex-wrap:wrap; font-size:13px; color:var(--text-2); margin:0 0 8px 4px; }
.legend span::before { content:""; display:inline-block; width:18px; height:0; border-top:2px solid var(--c); margin-right:6px; vertical-align:middle; }
.legend .dash::before { border-top-style:dashed; }
svg.chart { display:block; overflow:visible; }
.chart .grid { stroke:var(--line); stroke-width:1; }
.chart .axis { stroke:var(--muted); stroke-width:1; }
.chart .tick { fill:var(--text-2); font-size:12px; }
.chart .hlabel { font-size:12px; fill:var(--text-2); }
.chart .close { fill:none; stroke:var(--close); stroke-width:2; stroke-linejoin:round; }
.chart .close-dot { fill:var(--close); stroke:var(--surface); stroke-width:2; }
.chart .sma { fill:none; stroke:var(--sma); stroke-width:2; }
.chart .ath { stroke:var(--ath); stroke-width:1.5; stroke-dasharray:6 4; }
.chart .fired { stroke:var(--fired); stroke-width:2; }
.chart .fired-label { fill:var(--fired); font-weight:600; }
.chart .waiting { stroke:var(--wait); stroke-width:1.5; stroke-dasharray:3 4; }
.chart .cross { stroke:var(--muted); stroke-width:1; stroke-dasharray:2 3; pointer-events:none; }
.chart .hover-dot { fill:var(--close); stroke:var(--surface); stroke-width:2; pointer-events:none; }
.chart .hit { cursor:crosshair; }
.tip { position:absolute; pointer-events:none; background:var(--text); color:var(--bg); font-size:12px; padding:6px 9px;
  border-radius:6px; white-space:nowrap; display:none; font-variant-numeric:tabular-nums; }
footer { margin-top:28px; color:var(--muted); font-size:12px; }
html { background:var(--bg); }
@media print { html, body { background:var(--bg); color:var(--text); -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  .card, table, .kpi { break-inside:avoid; } .tip { display:none !important; } }
@page { size: A4; margin: 14mm; background: #111110; }  /* page box incl. margins; theme is pinned to dark */
"""

JS = """
document.querySelectorAll('svg.chart').forEach(function(svg){
  var dataEl = document.getElementById(svg.id + '-data'); if (!dataEl) return;
  var d = JSON.parse(dataEl.textContent), card = svg.parentElement;
  var tip = document.createElement('div'); tip.className='tip'; card.appendChild(tip);
  var cross = document.getElementById(svg.id+'-cross'), dot = document.getElementById(svg.id+'-dot');
  var hit = svg.querySelector('.hit');
  function fmt(v, unit){ return unit==='%' ? (v>=0?'+':'')+v.toFixed(2)+'%' : v.toLocaleString(undefined,{maximumFractionDigits:2}); }
  hit.addEventListener('mousemove', function(ev){
    var pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
    var p = pt.matrixTransform(svg.getScreenCTM().inverse());
    var lo=0, hi=d.x.length-1; while(hi-lo>1){ var m=(lo+hi)>>1; if(d.x[m]<p.x) lo=m; else hi=m; }
    var i = (p.x - d.x[lo] < d.x[hi] - p.x) ? lo : hi;
    cross.setAttribute('x1', d.x[i]); cross.setAttribute('x2', d.x[i]); cross.setAttribute('visibility','visible');
    dot.setAttribute('cx', d.x[i]); dot.setAttribute('cy', d.closeY[i]); dot.setAttribute('visibility','visible');
    var txt = d.labels[i] + '  ' + fmt(d.close[i], d.unit); if (d.sma[i] != null) txt += '  ·  SMA ' + fmt(d.sma[i], '');
    tip.textContent = txt; tip.style.display='block';
    var r = card.getBoundingClientRect(); var left = ev.clientX - r.left + 12, top = ev.clientY - r.top - 34;
    if (left + tip.offsetWidth > r.width - 8) left = ev.clientX - r.left - tip.offsetWidth - 12;
    tip.style.left = left + 'px'; tip.style.top = top + 'px';
  });
  hit.addEventListener('mouseleave', function(){ tip.style.display='none'; cross.setAttribute('visibility','hidden'); dot.setAttribute('visibility','hidden'); });
});
"""


def _cls(x: float | None) -> str:
    if x is None:
        return ""
    return "pos" if x > 0 else "neg" if x < 0 else ""


def render_html(s: Snapshot, closes: pd.Series, history: pd.DataFrame) -> str:
    e = html.escape
    sma_full = closes.rolling(SMA_WINDOW).mean()
    start = closes.index[-1] - pd.Timedelta(days=CHART_LOOKBACK_DAYS)
    closes_1y = closes.loc[start:]
    sma_1y = sma_full.loc[start:]

    change_rows = "".join(
        f"<tr><td>{e(c.label)}</td><td>{fdate(c.ref_date)}</td><td>{fnum(c.ref_close)}</td>"
        f"<td class='{_cls(c.points)}'>{fsigned(c.points)}</td><td class='{_cls(c.pct)}'>{fsigned(c.pct, 2, '%')}</td></tr>"
        for c in s.changes)

    ladder_rows = "".join(
        f"<tr class='{'fired' if l.fired else ''}'><td>{e(l.name)} <span class='note'>({l.pct_from_ath:+.0f}% from ATH)</span></td>"
        f"<td>{fnum(l.level)}</td><td>{fsigned(l.points_from_close)}</td><td>{fsigned(l.pct_to_fall, 2, '%')}</td>"
        f"<td><span class='badge {'fired' if l.fired else ''}'>{'FIRED' if l.fired else 'waiting'}</span></td></tr>"
        for l in s.ladder)
    rec = s.recovery
    ladder_rows += (f"<tr><td>{e(rec.name)} <span class='note'>(recovery: after {e(rec.armed_by)} fires, above {SMA_WINDOW}d SMA and SMA rising)</span></td>"
                    f"<td colspan='3' style='text-align:left'>{e(rec.state)}</td>"
                    f"<td><span class='badge {'active' if rec.active else ''}'>"
                    f"{'ACTIVE' if rec.active else 'armed' if rec.armed else 'not armed'}</span></td></tr>")

    etf_rows = "".join(
        (f"<tr><td>{e(x.ticker)}</td><td>{fdate(x.close_date)}</td><td>{fnum(x.close)}</td>"
         f"<td class='{_cls(x.day_change_pct)}'>{fsigned(x.day_change_pct, 2, '%')}</td></tr>") if x.available
        else f"<tr><td>{e(x.ticker)}</td><td colspan='3' style='text-align:left' class='note'>data unavailable</td></tr>"
        for x in s.etfs)

    etf_section = (
        "<h2>ETF reference</h2>"
        "<table><thead><tr><th>Ticker</th><th>Date</th><th>Last close</th><th>Day change</th></tr></thead>"
        f"<tbody>{etf_rows}</tbody></table>"
        "<p class='note'>Reference only, triggers are on the index.</p>"
    ) if s.etfs else ""

    if s.sma.value is None:
        sma_html = f"<p>Not enough history for a {s.sma.window}-day SMA.</p>"
    else:
        side = "above" if s.sma.above else "below"
        sma_html = (
            f"<div class='kpis'>"
            f"<div class='kpi'><div class='l'>{s.sma.window}-day SMA</div><div class='v'>{fnum(s.sma.value)}</div></div>"
            f"<div class='kpi'><div class='l'>Index vs SMA</div><div class='v {_cls(s.sma.pct_vs_sma)}'>{fsigned(s.sma.pct_vs_sma, 2, '%')}</div>"
            f"<div class='d'>index is {side} the SMA</div></div>"
            f"<div class='kpi'><div class='l'>SMA slope</div><div class='v'>{e(s.sma.slope or 'n/a')}</div>"
            f"<div class='d'>vs {s.sma.slope_lookback} trading days ago: {fnum(s.sma.value_lookback)}</div></div>"
            f"</div>")

    dd_svg = svg_drawdown_chart(history) if history is not None and len(history) >= 2 else ""
    dd_section = (
        f"<h2>Drawdown history</h2><div class='card'>"
        f"<div class='legend'><span style='--c:var(--close)'>Drawdown from ATH at each logged run</span>"
        f"<span class='dash' style='--c:var(--wait)'>Ladder levels</span></div>{dd_svg}</div>"
    ) if dd_svg else ""

    fired_count = sum(l.fired for l in s.ladder)
    headline = f"{fired_count} of {len(s.ladder)} ladder rungs fired" if fired_count else "No ladder rungs fired"

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(TITLE)}</title><style>{CSS}</style></head>
<body><main>
<h1>{e(TITLE)}</h1>
<p class="sub">{e(s.index_name)} ({e(s.index_ticker)}) · data through {fdate(s.last_date)} · {e(headline)}</p>

<div class="action {s.action.signal.lower()}">
  <span class="tag">{'Buy' if s.action.signal == 'BUY' else 'Recovery' if s.action.signal == 'RECOVERY' else 'No action'}</span>
  <div><div class="h">{e(s.action.headline)}</div><div class="d">{e(s.action.detail)}</div></div>
</div>

<div class="kpis">
  <div class="kpi"><div class="l">Last close</div><div class="v">{fnum(s.last_close)}</div><div class="d">{fdate(s.last_date)}</div></div>
  <div class="kpi"><div class="l">All-time-high close</div><div class="v">{fnum(s.ath_close)}</div><div class="d">{fdate(s.ath_date)}</div></div>
  <div class="kpi"><div class="l">Drawdown from ATH</div><div class="v {_cls(s.drawdown_pct)}">{fsigned(s.drawdown_pct, 2, '%')}</div><div class="d">{fsigned(s.last_close - s.ath_close)} points</div></div>
</div>

<h2>Changes</h2>
<table><thead><tr><th>Period</th><th>Reference date</th><th>Reference close</th><th>Points</th><th>Change</th></tr></thead>
<tbody>{change_rows}</tbody></table>
<p class="note">Reference = last close on or before the lookback date.</p>

<h2>{SMA_WINDOW}-day status</h2>
{sma_html}

<h2>Crash-buy ladder</h2>
<table><thead><tr><th>Rung</th><th>Trigger level</th><th>Points from last close</th><th>% still to fall</th><th>Status</th></tr></thead>
<tbody>{ladder_rows}</tbody></table>
<p class="note">Levels are % below the all-time-high close. A rung fires when the last close is at or below its level.</p>

{etf_section}

<h2>Last year</h2>
<div class="card">
  <div class="legend"><span style="--c:var(--close)">{e(s.index_name)} close</span><span style="--c:var(--sma)">{SMA_WINDOW}-day SMA</span>
  <span class="dash" style="--c:var(--ath)">ATH</span><span class="dash" style="--c:var(--wait)">Ladder level (waiting)</span><span style="--c:var(--fired)">Ladder level (fired)</span></div>
  {svg_price_chart(closes_1y, sma_1y, s)}
</div>
{dd_section}

<footer>Source: {e(s.source)} · generated {e(s.generated_at)} · {e(s.note)}<br>{e(TITLE)}: {e(TAGLINE)}</footer>
</main>
<script>{JS}</script>
</body></html>
"""


# ------------------------------------------------------------------ pdf

def write_pdf(html_text: str, pdf_path: Path) -> str | None:
    """Render HTML to PDF. Returns the engine used, or None if skipped (with a message printed)."""
    if sys.platform == "darwin":
        # Let WeasyPrint's ctypes loader find Homebrew's pango/gobject without the user exporting anything.
        for prefix in ("/opt/homebrew/lib", "/usr/local/lib"):
            if Path(prefix).is_dir() and prefix not in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", ""):
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(
                    filter(None, [os.environ.get("DYLD_FALLBACK_LIBRARY_PATH"), prefix]))
    try:
        import weasyprint  # type: ignore
    except ImportError:
        weasyprint = None
    except OSError as exc:  # package present, system libraries (pango, gobject, cairo) missing
        weasyprint = None
        print("warning: weasyprint is installed but its system libraries are missing "
              "(macOS: brew install pango; Debian/Ubuntu: apt install libpango-1.0-0 libpangoft2-1.0-0).",
              file=sys.stderr)
    if weasyprint is not None:
        try:
            weasyprint.HTML(string=html_text, base_url=str(pdf_path.parent)).write_pdf(str(pdf_path))
            return "weasyprint"
        except Exception as exc:
            print(f"warning: weasyprint failed ({exc}); trying pdfkit", file=sys.stderr)
    try:
        import pdfkit  # type: ignore
        pdfkit.from_string(html_text, str(pdf_path), options={"quiet": "", "enable-local-file-access": ""})
        return "pdfkit"
    except ImportError:
        pass
    except Exception as exc:  # pdfkit present but wkhtmltopdf missing/broken
        print(f"warning: pdfkit failed ({exc})", file=sys.stderr)
    print("PDF skipped: install weasyprint (pip install weasyprint) or pdfkit + wkhtmltopdf to enable it.",
          file=sys.stderr)
    return None
