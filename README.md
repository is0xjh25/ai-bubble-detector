# Bubble Detector

**Just in case the bubble really happens.**

Everyone *says* they'll buy the dip. Then the dip arrives, the news is terrifying, and somehow
nobody does. Bubble Detector is a small command-line tool that removes you from that decision: it
checks the Nasdaq Composite against a fixed crash-buy ladder once a day, keeps a log, and tells you
in one line whether today is a day to buy or a day to go back to sleep.

Most days it says "no buy today". That is the point. It is a smoke alarm, not a slot machine.

What it does each morning:

- pulls the full daily history of `^IXIC` and finds the all-time-high close,
- checks four buy levels (**-25%, -40%, -55%, -70%** from ATH) plus a post-crash **recovery**
  signal based on the 200-day moving average,
- prints a one-line **verdict** (BUY / RECOVERY / nothing to see here) with the distance to the
  next rung,
- writes a **self-contained HTML report** with an inline chart, a **PDF** (optional), a **CSV log**
  with one row per trading day, and the same tables in the **terminal** or as **JSON**.

On macOS it can run itself every morning and send a notification only when there is something to
do. On a normal day it stays quiet, like a good smoke alarm.

> **Not financial advice.** This is a personal tool that applies fixed, mechanical rules to public
> price data. It does not know your situation, your risk tolerance, or whether this time is
> different. It was built by someone who wanted to stop refreshing charts. Decide your own levels
> and amounts before you use it, preferably while the market is boring.

---

## How it works

The rules are deliberately dumb. Dumb rules are the ones you actually follow at 3 am.

**Ladder.** Each rung's trigger level is `ATH close × (1 + level%)`. A rung **fires** when the last
close is at or below its level. The report lists, for every rung, the trigger level, the points and
percent still to fall, and whether it has fired.

**Action today.** A rung produces a BUY only on the day it crosses, so you are told once, not nagged daily while the market stays underwater. The tool compares the current
state with the last run you logged (and with the previous close if there is no log yet), so:

- a rung that crossed on a day you did not run is still flagged BUY the next time you run,
- a rung already logged as fired does not fire again, even if the index bounces above the level and
  drops back.

**Buy 5, the recovery rung.** The final buy is for after the crash, when everyone else has sworn off stocks forever. It is *armed* once Buy 1 has
fired since the current all-time high (a new ATH disarms it), and becomes ACTIVE when, while armed,
the index closes above its 200-day simple moving average and that average is rising (today's SMA
higher than 20 trading days ago). The day it flips on is reported as a RECOVERY signal. In a market
that has not crashed it simply reads "not armed".

**Changes.** 1 day, 1 week, 1 month, 3 months, year-to-date, 1 year and from ATH. The reference is
the last close on or before the lookback date (previous session for 1 day; last close of the prior
year for YTD).

**ETF reference.** HNDQ.AX and NDQ.AX last close and day change are shown for information. Triggers
are always evaluated on the index.

**Running early.** If you run while the US market is still open, the data source returns a live,
unfinished bar for today. It is dropped, so an intraday move can never fire a rung.

---

## Install

Requires Python 3.10 or newer. Takes about two minutes, which is less time than you spent reading that one bearish thread.

```bash
git clone https://github.com/is0xjh25/ai-bubble-detector.git
cd ai-bubble-detector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If your system Python is older than 3.10 (macOS ships 3.9), [uv](https://docs.astral.sh/uv/) is the
quickest route:

```bash
brew install uv
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

### Optional: PDF output

The PDF is rendered from the HTML. Install one of:

- **WeasyPrint** (preferred): it is in `requirements.txt`, but also needs system libraries:
  macOS `brew install pango`; Debian/Ubuntu `apt install libpango-1.0-0 libpangoft2-1.0-0`.
- **pdfkit + wkhtmltopdf**: `pip install pdfkit` and install
  [wkhtmltopdf](https://wkhtmltopdf.org/downloads.html).

If neither works, the run prints a one-line notice and skips the PDF. Nothing else fails.

---

## Run

```bash
./run.sh              # run, then open reports/latest.html in your browser
./run.sh --no-open    # run only
./run.sh --no-pdf     # any flag below is passed through
```

`run.sh` works from any directory, uses `.venv` (creating it on first use with uv or Python 3.10+),
and opens the report when it finishes. Or call the module directly:

```bash
source .venv/bin/activate
python -m bubble_detector [--no-pdf] [--no-html] [--json] [--notify] [--outdir PATH]
```

| Flag | Effect |
|---|---|
| `--no-pdf` | Skip the PDF |
| `--no-html` | Skip the HTML (and therefore the PDF) |
| `--json` | Print the core data as JSON to stdout instead of tables. Files are still written. |
| `--notify` | macOS notification when the signal is BUY or RECOVERY, or when the run fails. Silent otherwise. |
| `--outdir PATH` | Output directory (default `./reports`) |
| `--version` | Print the version |

### Example console output

```
========================================================================
  Bubble Detector  -  Nasdaq Composite (^IXIC)
========================================================================
Last close : 25,437.00  on 2026-10-30
ATH close  : 27,599.79  on 2026-10-06   drawdown -7.84%

>>> No buy today <<<
    Nearest rung Buy 1 is 4,737 points (18.62%) below the last close. Recovery (Buy 5): not armed ...

CHANGES
Period      Ref date  Ref close     Points  Change %
--------  ----------  ---------  ---------  --------
1 day     2026-10-29  26,184.72    -747.72    -2.86%
...
CRASH-BUY LADDER (from ATH)
Rung              Trigger level  Points from close  % still to fall  Status
----------------  -------------  -----------------  ---------------  ---------
Buy 1 (-25%)          20,699.84          -4,737.16          -18.62%  waiting
Buy 2 (-40%)          16,559.87          -8,877.12          -34.90%  waiting
...
```

---

## Outputs (in `./reports/`)

| File | What it is |
|---|---|
| `history.csv` | The long-term log, one row per trading date. Running twice on the same day overwrites that day's row. |
| `report_YYYY-MM-DD.html` | Self-contained report for that close date. Works offline, hover tooltips on the chart. |
| `latest.html` | Overwritten on every run with the newest report. |
| `report_YYYY-MM-DD.pdf` | Same report as PDF, when a renderer is installed. |

Filenames use the **last close date in the data**, not the wall-clock date, so a run on Saturday
produces the same files as Friday evening's run. Dated reports older than `REPORT_RETENTION_DAYS`
(default 90) are deleted after each run; `history.csv` and `latest.html` are kept forever.

**`history.csv` columns:**
`date, close, ath, ath_date, drawdown_pct, chg_1d_pct, chg_1w_pct, chg_1m_pct, chg_3m_pct,
chg_ytd_pct, chg_1y_pct, sma200, above_sma, sma_slope, buy1_fired, buy2_fired, buy3_fired,
buy4_fired, buy5_armed, buy5_active, hndq_close, ndq_close, signal`

`signal` is `BUY`, `RECOVERY` or `NONE` for that day. The report's drawdown-history chart is drawn
from this file once it has two or more rows.

---

## Configuration

Everything tunable is in [`bubble_detector/config.py`](bubble_detector/config.py), each setting
with an explanation, its format and examples. Highlights:

| Setting | Default | Meaning |
|---|---|---|
| `INDEX_TICKER` | `"^IXIC"` | The index everything is evaluated on |
| `EXCHANGE_TZ`, `MARKET_CLOSE_TIME` | `"America/New_York"`, `"16:00"` | Used to drop today's unfinished bar if you run while the market is open |
| `ETF_TICKERS` | `["HNDQ.AX", "NDQ.AX"]` | Reference-only instruments |
| `LADDER` | -25 / -40 / -55 / -70 % | The rungs, as % below the ATH close |
| `RECOVERY_ARM_RUNG` | `1` | Which rung must fire before Buy 5 is armed |
| `SMA_WINDOW`, `SMA_SLOPE_LOOKBACK` | `200`, `20` | Moving-average window and slope comparison |
| `FETCH_TIMEOUT_SECONDS` | `60` | Yahoo can be slow to connect on some networks |
| `REPORT_RETENTION_DAYS` | `90` | Dated reports older than this are deleted after each run; 0 keeps all. The CSV log is never pruned. |
| `SCHEDULE_HOUR`, `SCHEDULE_MINUTE`, `SCHEDULE_WEEKDAYS` | `8`, `30`, Tue–Sat | When the automatic run happens |

All settings take effect on the next run, except the `SCHEDULE_*` block, which also needs
`schedule/install-schedule.sh` re-run.

---

## Automatic morning run (macOS)

Because the one morning you forget to run it will be the morning it mattered.

```bash
schedule/install-schedule.sh          # install or update, using SCHEDULE_* from config.py
schedule/install-schedule.sh 7 45     # one-off time override
schedule/uninstall-schedule.sh        # remove
```

This installs a launchd agent that runs `run.sh --no-open --notify`. The default is Tuesday to
Saturday at 08:30 local time, which suits Asia-Pacific, where a US close lands the next morning. In
the US or Europe, set `SCHEDULE_WEEKDAYS = [1, 2, 3, 4, 5]` and an evening hour instead. If the Mac
is asleep at that time, the job runs when it next wakes. Output goes to
`~/Library/Logs/bubble-detector/launchd.log`. Test it with:

```bash
launchctl kickstart gui/$(id -u)/com.bubble-detector.daily
```

**macOS privacy note.** Background jobs cannot read `~/Documents`, `~/Desktop` or `~/Downloads`.
If the project lives in one of those, either move it (e.g. to `~/ai-bubble-detector`) or add
`/bin/bash` under *System Settings → Privacy & Security → Full Disk Access* (press ⌘⇧G in the file
dialog and type `/bin/bash`).

On Linux, use cron instead; the same wrapper works:

```
30 8 * * 2-6  /path/to/ai-bubble-detector/run.sh --no-open --notify
```

### Notifications and icon

`--notify` is quiet on a normal day. You will hear from it perhaps a handful of times per decade, and each one will be a day you remember. It speaks up for BUY, RECOVERY, a failed data fetch, or a crash,
so a silent failure on a crash day cannot slip past. On Linux it uses `notify-send` when present. On macOS, notifications sent with plain `osascript` carry
Script Editor's icon; to get the Bubble Detector icon instead, build the small notifier app once:

```bash
schedule/build-notifier.sh
```

It rasterises `assets/icons/icon.svg` (needs `rsvg-convert` from `brew install librsvg`, or Google
Chrome), compiles `assets/notifier.applescript` into `~/Applications/Bubble Detector.app`, and
`--notify` uses that app automatically when it exists. Click **Allow** on the first notification
prompt. Change the icon by editing the SVG and re-running the script.

---

## Data source

Free, like the best things and the worst APIs. [yfinance](https://github.com/ranaroussi/yfinance): free, no key, covers the index and the ASX ETFs,
but unofficial and occasionally broken by Yahoo changes. Everything downstream reads a
`MarketData` object produced by a single function, `fetch_market_data()` in
`bubble_detector/data.py`, so switching providers means replacing that one function.
[`PROVIDERS.md`](PROVIDERS.md) compares the alternatives (Alpha Vantage, Twelve Data, Polygon,
EODHD) and shows the swap.

---

## Development

```bash
python -m unittest -v        # 21 tests, synthetic data, no network
```

```
bubble_detector/
  config.py     all settings, annotated
  data.py       provider boundary (yfinance today)
  calc.py       changes, SMA, ladder, recovery, action, CSV row
  report.py     console tables, history.csv, HTML + inline SVG charts, PDF
  notify.py     macOS notifications
  main.py       CLI
assets/         icon SVG/icns and the notifier AppleScript
schedule/       launchd install/uninstall, notifier app build
tests/          unit tests
run.sh          daily wrapper
```

Dependencies: `yfinance`, `pandas`; optional `weasyprint`. Python 3.10+.

## License

[MIT](LICENSE). If it helps you buy the bottom, you owe me nothing. If it doesn't, see the disclaimer above.
