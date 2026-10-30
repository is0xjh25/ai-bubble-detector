"""Core calculations: changes, 200-day SMA, crash-buy ladder, ETF reference."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import re

import pandas as pd

from .config import (
    CHANGE_PERIODS,
    ETF_TICKERS,
    INDEX_NAME,
    INDEX_TICKER,
    LADDER,
    RECOVERY_ARM_RUNG,
    RECOVERY_NAME,
    SMA_SLOPE_LOOKBACK,
    SMA_WINDOW,
    TITLE,
)
from .data import MarketData


@dataclass
class Change:
    label: str
    ref_date: date | None
    ref_close: float | None
    points: float | None
    pct: float | None


@dataclass
class SmaStatus:
    window: int
    value: float | None
    above: bool | None
    pct_vs_sma: float | None          # (close / sma - 1) * 100
    slope: str | None                 # "rising" / "falling" / "flat"
    slope_lookback: int = SMA_SLOPE_LOOKBACK
    value_lookback: float | None = None


@dataclass
class LadderRow:
    name: str
    pct_from_ath: float
    level: float
    points_from_close: float          # level - close (negative: still has to fall)
    pct_to_fall: float                # (level / close - 1) * 100 (negative: still has to fall)
    fired: bool

    @property
    def status(self) -> str:
        return "FIRED" if self.fired else "waiting"


@dataclass
class Recovery:
    name: str
    above_sma: bool | None
    sma_rising: bool | None
    armed: bool                       # a crash happened: the arming rung fired since the current ATH
    armed_by: str                     # which rung arms it
    trough_pct_since_ath: float | None
    active: bool                      # armed AND above SMA AND SMA rising
    state: str


@dataclass
class Action:
    """The one-line answer to 'do I buy today?'."""
    signal: str                       # "BUY" (a rung fired at this close), "RECOVERY" (the recovery rung turned on today), "NONE"
    headline: str
    detail: str
    newly_fired: list[str]            # rungs that fired at this close but not at the previous one
    already_fired: list[str]          # rungs fired at both closes
    nearest_waiting: str | None       # next rung still to come
    recovery_newly_active: bool


@dataclass
class EtfRef:
    ticker: str
    close_date: date | None
    close: float | None
    day_change_pct: float | None
    available: bool


@dataclass
class Snapshot:
    title: str
    index_ticker: str
    index_name: str
    last_date: date
    last_close: float
    ath_date: date
    ath_close: float
    drawdown_pct: float
    changes: list[Change]
    sma: SmaStatus
    ladder: list[LadderRow]
    recovery: Recovery
    etfs: list[EtfRef]
    action: Action
    source: str
    generated_at: str
    note: str = "ETF prices are reference only; triggers are evaluated on the index."

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for row, lad in zip(d["ladder"], self.ladder):
            row["status"] = lad.status
        return _jsonable(d)

    def history_row(self) -> dict[str, Any]:
        """One flat row for history.csv."""
        by_label = {c.label: c for c in self.changes}
        row: dict[str, Any] = {
            "date": self.last_date.isoformat(),
            "close": round(self.last_close, 2),
            "ath": round(self.ath_close, 2),
            "ath_date": self.ath_date.isoformat(),
            "drawdown_pct": round(self.drawdown_pct, 2),
        }
        for label, kind, amount in CHANGE_PERIODS:
            c = by_label.get(label)
            row[change_column(kind, amount)] = None if c is None or c.pct is None else round(c.pct, 2)
        row[f"sma{self.sma.window}"] = None if self.sma.value is None else round(self.sma.value, 2)
        row["above_sma"] = self.sma.above
        row["sma_slope"] = self.sma.slope
        for i, lad in enumerate(self.ladder, start=1):
            row[f"buy{i}_fired"] = lad.fired
        row[f"{recovery_key()}_armed"] = self.recovery.armed
        row[f"{recovery_key()}_active"] = self.recovery.active
        for etf in self.etfs:
            row[etf_column(etf.ticker)] = None if etf.close is None else round(etf.close, 2)
        row["signal"] = self.action.signal
        return row


def etf_column(ticker: str) -> str:
    """'HNDQ.AX' -> 'hndq_close'."""
    return ticker.split(".")[0].lower() + "_close"


def change_column(kind: str, amount: int) -> str:
    """CSV column for a change period: ('days',7) -> 'chg_1w_pct', ('months',3) -> 'chg_3m_pct'."""
    if kind == "ytd":
        return "chg_ytd_pct"
    if kind == "days":
        tag = "1w" if amount == 7 else f"{amount}d"
    elif kind == "months":
        tag = f"{amount}m"
    else:
        tag = f"{amount}y"
    return f"chg_{tag}_pct"


def recovery_key() -> str:
    """'Buy 5' -> 'buy5' (prefix of the recovery columns in history.csv)."""
    return re.sub(r"[^a-z0-9]+", "", RECOVERY_NAME.lower()) or "recovery"


def history_columns(etf_tickers: list[str] | None = None) -> list[str]:
    etf_tickers = ETF_TICKERS if etf_tickers is None else etf_tickers
    cols = ["date", "close", "ath", "ath_date", "drawdown_pct"]
    cols += [change_column(kind, amount) for _, kind, amount in CHANGE_PERIODS]
    cols += [f"sma{SMA_WINDOW}", "above_sma", "sma_slope"]
    cols += [f"buy{i}_fired" for i in range(1, len(LADDER) + 1)]
    cols += [f"{recovery_key()}_armed", f"{recovery_key()}_active"]
    cols += [etf_column(t) for t in etf_tickers]
    cols.append("signal")
    return cols


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (date, pd.Timestamp)):
        return obj.isoformat()
    if isinstance(obj, float):
        return None if pd.isna(obj) else obj
    return obj


# ---------------------------------------------------------------- helpers

def close_on_or_before(closes: pd.Series, when: pd.Timestamp) -> tuple[date, float] | None:
    """Last close at or before `when`. None if no data that early."""
    sub = closes.loc[:when]
    if sub.empty:
        return None
    return sub.index[-1].date(), float(sub.iloc[-1])


def lookback_date(last: pd.Timestamp, kind: str, amount: int) -> pd.Timestamp:
    if kind == "days":
        return last - pd.Timedelta(days=amount)
    if kind == "months":
        return last - pd.DateOffset(months=amount)
    if kind == "years":
        return last - pd.DateOffset(years=amount)
    if kind == "ytd":
        return pd.Timestamp(year=last.year - 1, month=12, day=31)
    raise ValueError(kind)


def _change(label: str, ref: tuple[date, float] | None, last_close: float) -> Change:
    if ref is None or ref[1] == 0:
        return Change(label, None, None, None, None)
    ref_date, ref_close = ref
    pts = last_close - ref_close
    return Change(label, ref_date, ref_close, pts, round(pts / ref_close * 100, 2))


def compute_changes(closes: pd.Series, ath: tuple[date, float]) -> list[Change]:
    last_ts = closes.index[-1]
    last_close = float(closes.iloc[-1])
    out: list[Change] = []
    for label, kind, amount in CHANGE_PERIODS:
        if kind == "days" and amount == 1:
            # previous trading session, regardless of weekends/holidays
            ref = (closes.index[-2].date(), float(closes.iloc[-2])) if len(closes) > 1 else None
        else:
            target = lookback_date(last_ts, kind, amount)
            ref = close_on_or_before(closes, target)
        out.append(_change(label, ref, last_close))
    out.append(_change("From ATH", ath, last_close))
    return out


def compute_sma(closes: pd.Series, window: int = SMA_WINDOW, slope_lookback: int = SMA_SLOPE_LOOKBACK) -> SmaStatus:
    if len(closes) < window:
        return SmaStatus(window, None, None, None, None, slope_lookback, None)
    sma = closes.rolling(window).mean()
    value = float(sma.iloc[-1])
    last_close = float(closes.iloc[-1])
    above = last_close > value
    pct = round((last_close / value - 1) * 100, 2)
    slope: str | None = None
    value_lb: float | None = None
    if len(sma.dropna()) > slope_lookback:
        value_lb = float(sma.iloc[-1 - slope_lookback])
        if value > value_lb:
            slope = "rising"
        elif value < value_lb:
            slope = "falling"
        else:
            slope = "flat"
    return SmaStatus(window, value, above, pct, slope, slope_lookback, value_lb)


def compute_ladder(last_close: float, ath_close: float) -> list[LadderRow]:
    rows = []
    for name, pct in LADDER:
        level = ath_close * (1 + pct / 100)
        rows.append(LadderRow(
            name=name,
            pct_from_ath=pct,
            level=level,
            points_from_close=level - last_close,
            pct_to_fall=round((level / last_close - 1) * 100, 2),
            fired=last_close <= level,
        ))
    return rows


def compute_recovery(sma: SmaStatus, closes: pd.Series, ladder: list[LadderRow]) -> Recovery:
    """The recovery rung fires only after a crash: armed once the arming rung has fired since the current ATH
    (reset by a new ATH), then ACTIVE when the index is above a rising SMA."""
    arm = ladder[RECOVERY_ARM_RUNG - 1]
    ath_ts = closes.idxmax()
    since_ath = closes.loc[ath_ts:]
    trough = float(since_ath.min())
    trough_pct = round((trough / float(closes.loc[ath_ts]) - 1) * 100, 2)
    armed = trough <= arm.level

    rising = None if sma.slope is None else sma.slope == "rising"
    trend_ok = bool(sma.above) and bool(rising)
    active = armed and trend_ok
    if sma.value is None:
        state = f"not enough history for the {sma.window}-day SMA"
    elif not armed:
        state = f"not armed ({arm.name} has not fired since the ATH; trough {trough_pct:+.2f}%)"
    else:
        trend = ", ".join(["above SMA" if sma.above else "below SMA",
                           f"SMA {sma.slope}" if sma.slope else "SMA slope unknown"])
        state = f"ACTIVE ({trend})" if active else f"armed, waiting ({trend})"
    return Recovery(RECOVERY_NAME, sma.above, rising, armed, arm.name, trough_pct, active, state)


def compute_etf_refs(etf_closes: dict[str, pd.Series]) -> list[EtfRef]:
    out = []
    for ticker, closes in etf_closes.items():
        if closes is None or closes.empty:
            out.append(EtfRef(ticker, None, None, None, False))
            continue
        last = float(closes.iloc[-1])
        chg = None
        if len(closes) > 1 and closes.iloc[-2]:
            chg = round((last / float(closes.iloc[-2]) - 1) * 100, 2)
        out.append(EtfRef(ticker, closes.index[-1].date(), last, chg, True))
    return out


def _fmt_pts(x: float) -> str:
    return f"{abs(x):,.0f} points"


def compute_action(closes: pd.Series, ladder: list[LadderRow], recovery: Recovery,
                   prev_logged: dict | None = None, ath_date: date | None = None) -> Action:
    """A rung is 'new' if it is fired now but was not fired at the previous close, or was not fired
    in the last run you logged (so a crossing on a day you skipped is still flagged as BUY).
    Once a rung has been logged as fired it does not re-trigger in this cycle. The logged state is
    only trusted if it belongs to the same cycle, i.e. the same all-time high; after a new ATH the
    ladder has moved and the comparison falls back to the previous close."""
    fired_prev_close: dict[str, bool] = {}
    prev_active: bool | None = None
    if len(closes) > 1:
        prev = closes.iloc[:-1]
        prev_close = float(prev.iloc[-1])
        prev_ladder = compute_ladder(prev_close, float(prev.max()))
        fired_prev_close = {l.name: l.fired for l in prev_ladder}
        prev_rec = compute_recovery(compute_sma(prev), prev, prev_ladder)
        prev_active = prev_rec.active if prev_rec.above_sma is not None else None

    logged_fired: dict[str, bool] | None = None
    logged_active: bool | None = None
    same_cycle = bool(prev_logged) and ath_date is not None and str(prev_logged.get("ath_date")) == ath_date.isoformat()
    if prev_logged and same_cycle:
        keys = [f"buy{i}_fired" for i in range(1, len(ladder) + 1)]
        if all(k in prev_logged and prev_logged[k] is not None for k in keys):
            logged_fired = {l.name: _truthy(prev_logged[k]) for l, k in zip(ladder, keys)}
        active_key = f"{recovery_key()}_active"
        if prev_logged.get(active_key) is not None:
            logged_active = _truthy(prev_logged[active_key])

    newly: list[str] = []
    already: list[str] = []
    for l in ladder:
        if not l.fired:
            continue
        if logged_fired is not None:
            was = logged_fired[l.name]
        else:
            was = fired_prev_close.get(l.name, True)
        (already if was else newly).append(l.name)

    waiting = [l for l in ladder if not l.fired]
    nearest = waiting[0] if waiting else None
    was_active = logged_active if logged_active is not None else prev_active
    recovery_new = bool(recovery.active) and was_active is False

    if newly:
        rungs = ", ".join(newly)
        lv = [l for l in ladder if l.name in newly][-1]
        signal = "BUY"
        headline = f"BUY TODAY: {rungs} fired at this close"
        detail = (f"Index closed at or below the {lv.name} level ({lv.level:,.2f}, {lv.pct_from_ath:+.0f}% from ATH). "
                  + (f"Next rung {nearest.name} is {_fmt_pts(nearest.points_from_close)} "
                     f"({abs(nearest.pct_to_fall):.2f}%) lower." if nearest else "That was the last rung."))
    elif recovery_new:
        signal = "RECOVERY"
        headline = f"RECOVERY BUY: {recovery.name} turned on today"
        detail = (f"{recovery.armed_by} fired since the ATH (trough {recovery.trough_pct_since_ath:+.2f}%) and the index "
                  f"is now above a rising {SMA_WINDOW}-day SMA, having not been at the previous check."
                  + (f" Rungs previously fired: {', '.join(already)}." if already else ""))
    elif already:
        signal = "NONE"
        headline = f"No new buy today ({', '.join(already)} already fired)"
        detail = (f"Next rung {nearest.name} is {_fmt_pts(nearest.points_from_close)} "
                  f"({abs(nearest.pct_to_fall):.2f}%) below the last close." if nearest
                  else "All rungs have fired.") + f" Recovery ({recovery.name}): {recovery.state}."
    else:
        signal = "NONE"
        headline = "No buy today"
        detail = (f"Nearest rung {nearest.name} is {_fmt_pts(nearest.points_from_close)} "
                  f"({abs(nearest.pct_to_fall):.2f}%) below the last close. Recovery ({recovery.name}): {recovery.state}.")
    return Action(signal, headline, detail, newly, already, nearest.name if nearest else None, recovery_new)


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def compute_snapshot(md: MarketData, generated_at: str, prev_logged: dict | None = None) -> Snapshot:
    closes = md.index_closes
    last_ts = closes.index[-1]
    last_close = float(closes.iloc[-1])
    ath_ts = closes.idxmax()
    ath_close = float(closes.loc[ath_ts])
    ath = (ath_ts.date(), ath_close)

    sma = compute_sma(closes)
    ladder = compute_ladder(last_close, ath_close)
    recovery = compute_recovery(sma, closes, ladder)
    return Snapshot(
        title=TITLE,
        index_ticker=md.index_ticker,
        index_name=INDEX_NAME if md.index_ticker == INDEX_TICKER else md.index_ticker,
        last_date=last_ts.date(),
        last_close=last_close,
        ath_date=ath[0],
        ath_close=ath_close,
        drawdown_pct=round((last_close / ath_close - 1) * 100, 2),
        changes=compute_changes(closes, ath),
        sma=sma,
        ladder=ladder,
        recovery=recovery,
        etfs=compute_etf_refs(md.etf_closes),
        action=compute_action(closes, ladder, recovery, prev_logged, ath[0]),
        source=md.source,
        generated_at=generated_at,
    )
