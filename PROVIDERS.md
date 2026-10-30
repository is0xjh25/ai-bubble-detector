# Data providers for Bubble Detector

What this tool actually needs is small:

1. Full daily close history for one US index (`^IXIC`), refreshed once a day.
2. The last two daily closes for two ASX ETFs (`HNDQ.AX`, `NDQ.AX`), reference only.

That is roughly 3 requests per day. Rate limits barely matter; reliability and ASX
coverage do.

## Comparison

| Provider | Cost | Nasdaq Composite history | ASX ETFs | Reliability | Notes |
|---|---|---|---|---|---|
| **yfinance** (Yahoo Finance, unofficial) | Free | Yes, back to 1971 | Yes | Low to medium | No API key. Scrapes Yahoo's undocumented endpoints, so it breaks whenever Yahoo changes something, and Yahoo rate-limits aggressively (HTTP 429) if you retry hard. Fine for a once-a-day manual run; expect to bump the library version now and then. |
| **Alpha Vantage** | Free tier (25 requests/day, 5/min), paid from ~US$50/mo | Indices are not reliably served on the free tier; you would proxy with an ETF such as `ONEQ` or `QQQ`, which is not the Composite | Patchy (some `.AX`/`.AUS` symbols) | Medium | Key required. Free tier is enough for 3 calls/day, but the index itself is the weak spot. |
| **Twelve Data** | Free tier (8 requests/min, 800/day), paid from ~US$29/mo | Yes (`IXIC`), but full history usually needs a paid plan (free tier caps output size) | Yes (`HNDQ:ASX`, `NDQ:ASX`) on paid plans | Medium to high | Clean JSON API, good symbol coverage. Free tier is workable if you only fetch the last few years and keep the ATH in your own log. |
| **Polygon.io** | Paid for anything useful (Indices plan is separate, from ~US$49/mo; stocks plan separate again) | Yes (`I:COMP`), good quality | **No** (US markets only) | High | Excellent for US data, but the ASX reference columns would have to come from elsewhere or be dropped. |
| **EODHD** (EOD Historical Data) | From ~US$20/mo ("All World"), free tier is demo-only | Yes (`IXIC.INDX`), full history | **Yes** (`HNDQ.AU`, `NDQ.AU`), full history | High | One key covers both halves of this tool. End-of-day focus matches the use case exactly. |

## Recommendation

- **Free: stay on yfinance.** For one manual run a day it is adequate, costs nothing, and
  already covers both the index and the ASX ETFs. Keep `history.csv` as your own record of the
  ATH so a bad day at Yahoo does not lose anything. If it breaks, `pip install -U yfinance`
  usually fixes it within days.
- **Paid: EODHD.** It is the only option in the table that serves the Nasdaq Composite and
  the ASX ETFs under one cheap key with a stable, documented API. Polygon is the better
  US-only product, but you would lose the ETF columns.

## What the swap looks like

Only `fetch_market_data()` in `bubble_detector/data.py` changes. It must return a
`MarketData` whose `index_closes` is a float `pd.Series` indexed by tz-naive daily
timestamps, sorted, with no NaNs, and whose `etf_closes` maps ticker to a Series of the
same shape (empty Series if unavailable).

### EODHD

```python
import os, requests, pandas as pd
from .data import MarketData, _normalise_closes   # reuse the normaliser

EODHD_KEY = os.environ["EODHD_API_KEY"]
SYMBOLS = {"^IXIC": "IXIC.INDX", "HNDQ.AX": "HNDQ.AU", "NDQ.AX": "NDQ.AU"}

def _eodhd_closes(symbol: str, start: str = "1970-01-01") -> pd.Series:
    r = requests.get(
        f"https://eodhd.com/api/eod/{symbol}",
        params={"api_token": EODHD_KEY, "fmt": "json", "period": "d", "from": start},
        timeout=30,
    )
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    if df.empty:
        return pd.Series(dtype="float64")
    df.index = pd.to_datetime(df["date"])
    return _normalise_closes(df.rename(columns={"close": "Close"}))

def fetch_market_data(index_ticker="^IXIC", etf_tickers=None) -> MarketData:
    etf_tickers = etf_tickers or ["HNDQ.AX", "NDQ.AX"]
    index_closes = _eodhd_closes(SYMBOLS[index_ticker])
    if index_closes.empty:
        raise RuntimeError(f"No price history returned for {index_ticker}")
    etf_closes = {t: _eodhd_closes(SYMBOLS[t], start="2024-01-01") for t in etf_tickers}
    return MarketData(index_ticker, index_closes, etf_closes, source="eodhd")
```

### Twelve Data

```python
def _twelvedata_closes(symbol: str, outputsize: int = 5000) -> pd.Series:
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": symbol, "interval": "1day", "outputsize": outputsize,
                "apikey": os.environ["TWELVEDATA_API_KEY"]},
        timeout=30,
    )
    r.raise_for_status()
    values = r.json().get("values", [])
    df = pd.DataFrame(values)
    if df.empty:
        return pd.Series(dtype="float64")
    df.index = pd.to_datetime(df["datetime"])
    return _normalise_closes(df.rename(columns={"close": "Close"}).astype({"Close": "float64"}))

# symbols: "IXIC" for the index, "HNDQ:ASX" and "NDQ:ASX" for the ETFs
```

In both cases nothing in `calc.py`, `report.py` or `main.py` needs to change, and the
`source` field flows through to the console footer, the HTML footer and the JSON output.
