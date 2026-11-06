"""Data layer.

`fetch_market_data()` is the single provider boundary. Everything downstream
consumes a `MarketData` object and never touches yfinance directly, so swapping
providers means replacing the body of that one function (see PROVIDERS.md).

Contract of the returned object:
  * `index_closes`: pd.Series of float closes, indexed by tz-naive daily
    Timestamps (one row per trading day), sorted ascending, no NaNs, full history.
  * `etf_closes`: dict ticker -> pd.Series with the same shape (recent closes
    only are needed). A ticker that could not be fetched maps to an empty Series.
"""

from __future__ import annotations

import logging
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from .config import (
    ETF_TICKERS,
    EXCHANGE_TZ,
    FETCH_RETRIES,
    FETCH_RETRY_DELAY_SECONDS,
    FETCH_TIMEOUT_SECONDS,
    INDEX_TICKER,
    MARKET_CLOSE_TIME,
)


@dataclass
class MarketData:
    index_ticker: str
    index_closes: pd.Series
    etf_closes: dict[str, pd.Series] = field(default_factory=dict)
    source: str = "yfinance"


def _normalise_closes(df: pd.DataFrame) -> pd.Series:
    """Turn a provider OHLC frame into the tz-naive, NaN-free close Series we need."""
    if df is None or df.empty or "Close" not in df.columns:
        return pd.Series(dtype="float64")
    closes = df["Close"].astype("float64")
    idx = pd.DatetimeIndex(closes.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    closes.index = idx.normalize()
    closes = closes[~closes.index.duplicated(keep="last")].dropna().sort_index()
    closes.name = "close"
    return closes


def drop_live_bar(closes: pd.Series, tz: str = EXCHANGE_TZ, close_time: str = MARKET_CLOSE_TIME) -> pd.Series:
    """Remove today's row if that market has not closed yet (the provider returns a live partial bar)."""
    if closes.empty:
        return closes
    now = datetime.now(ZoneInfo(tz))
    hh, mm = (int(x) for x in close_time.split(":"))
    market_closed = (now.hour, now.minute) >= (hh, mm)
    if closes.index[-1].date() == now.date() and not market_closed:
        return closes.iloc[:-1]
    return closes


def _yf_history(ticker: str, period: str, retries: int = FETCH_RETRIES,
                timeout: float = FETCH_TIMEOUT_SECONDS) -> pd.Series:
    """Fetch daily closes from yfinance with simple retries. Returns empty Series on failure."""
    import yfinance as yf  # imported lazily so the module is importable without network/yfinance

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # we report failures ourselves
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # yfinance emits pandas deprecation noise
                df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False,
                                                 timeout=timeout, raise_errors=True)
            closes = _normalise_closes(df)
            if not closes.empty:
                return closes
            last_err = RuntimeError("empty response")
        except Exception as exc:  # network hiccups, rate limits, parsing issues
            last_err = exc
        if attempt < retries:
            time.sleep(FETCH_RETRY_DELAY_SECONDS)
    print(f"warning: could not fetch {ticker} ({last_err})", file=sys.stderr)
    return pd.Series(dtype="float64")


def fetch_market_data(
    index_ticker: str = INDEX_TICKER,
    etf_tickers: list[str] | None = None,
) -> MarketData:
    """Provider boundary. Replace this body to switch data sources."""
    etf_tickers = ETF_TICKERS if etf_tickers is None else etf_tickers

    index_closes = drop_live_bar(_yf_history(index_ticker, period="max"))
    if index_closes.empty:
        raise RuntimeError(f"No price history returned for {index_ticker}; cannot continue.")

    etf_closes = {t: _yf_history(t, period="1mo") for t in etf_tickers}
    return MarketData(index_ticker=index_ticker, index_closes=index_closes, etf_closes=etf_closes)
