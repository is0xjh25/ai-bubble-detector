"""Bubble Detector settings.

Every tunable value lives in this file. Edit, save, and the next run picks it up.
The only exception is the SCHEDULE block at the bottom, which also needs
`schedule/install-schedule.sh` re-run because launchd keeps its own copy of the timing.

Formatting rules that apply throughout:
  * Text goes in quotes:            "like this"
  * Numbers do not:                 200   or   -25.0
  * Lists use square brackets with commas:   ["A", "B"]   or   [2, 3, 4]
  * True / False are capitalised:   True
  * Anything after a # is a comment and is ignored.
"""

# =============================================================================
# TITLE
# -----------------------------------------------------------------------------
# Shown as the heading of the HTML/PDF report, the console header and the
# notification. Also used as the name of the notifier app in ~/Applications.
# Format: text in quotes.            Example: "Bubble Detector"
TITLE = "Bubble Detector"

# One-liner shown in the CLI help and the report footer. Format: text in quotes.
TAGLINE = "Just in case the bubble really happens."


# =============================================================================
# INSTRUMENTS
# -----------------------------------------------------------------------------
# INDEX_TICKER: the Yahoo Finance symbol the whole tool is evaluated on. The ATH,
#   the ladder triggers, the SMA and the change table all use this series.
#   Index symbols on Yahoo start with a caret.
#   Format: text in quotes.          Examples: "^IXIC" (Nasdaq Composite),
#                                              "^GSPC" (S&P 500), "^NDX" (Nasdaq 100)
INDEX_TICKER = "^IXIC"

# INDEX_NAME: human-readable name printed next to the ticker in reports.
#   Format: text in quotes.          Example: "Nasdaq Composite"
INDEX_NAME = "Nasdaq Composite"

# EXCHANGE_TZ / MARKET_CLOSE_TIME: where and when the index's market closes. If you run the
#   tool while that market is still open, the data source returns a live, unfinished bar for
#   today; it is dropped so a half-day move can never fire a rung. US exchanges close 16:00
#   New York time.
#   Format: IANA time zone in quotes / "HH:MM" 24h in quotes.
#   Examples: "America/New_York", "16:00"   |   "Australia/Sydney", "16:00"   |   "Europe/London", "16:30"
EXCHANGE_TZ = "America/New_York"
MARKET_CLOSE_TIME = "16:00"

# ETF_TICKERS: instruments shown in the "ETF reference" table for information
#   only. They never affect any trigger. Each one also gets a `<name>_close`
#   column in history.csv, e.g. "HNDQ.AX" -> hndq_close. ASX symbols end in .AX.
#   Format: list of quoted symbols.  Example: ["HNDQ.AX", "NDQ.AX"]
#   An empty list [] removes the table and the columns.
ETF_TICKERS = ["HNDQ.AX", "NDQ.AX"]


# =============================================================================
# CRASH-BUY LADDER
# -----------------------------------------------------------------------------
# LADDER: the rungs, in order from shallowest to deepest. Each entry is
#   (label, percent below the all-time-high close). The trigger level is
#   ATH x (1 + percent/100); a rung FIRES when the last close is at or below it.
#   Percentages are negative numbers with a decimal point. Labels appear in the
#   reports; history.csv gets one buyN_fired column per rung (N = position).
#   Format: list of ("label", -percent) pairs, one per line.
#   Example: [("Buy 1", -25.0), ("Buy 2", -40.0), ("Buy 3", -55.0), ("Buy 4", -70.0)]
LADDER = [
    ("Buy 1", -25.0),
    ("Buy 2", -40.0),
    ("Buy 3", -55.0),
    ("Buy 4", -70.0),
]

# RECOVERY_NAME: label of the final, post-crash rung. It is not a price level;
#   it turns ACTIVE when the index is back above a rising SMA (see TREND below).
#   Its history.csv columns are derived from it: "Buy 5" -> buy5_armed, buy5_active.
#   Format: text in quotes.          Example: "Buy 5"
RECOVERY_NAME = "Buy 5"

# RECOVERY_ARM_RUNG: the recovery rung is only "armed" once this ladder rung has
#   fired since the current all-time high. 1 = Buy 1 (a 25% fall arms it),
#   2 = Buy 2, and so on. A new ATH disarms it again.
#   Format: whole number from 1 to the number of LADDER entries.   Example: 1
RECOVERY_ARM_RUNG = 1


# =============================================================================
# TREND (moving average)
# -----------------------------------------------------------------------------
# SMA_WINDOW: how many trading days the simple moving average covers. 200 is the
#   conventional long-term trend line (about ten months). The history.csv column
#   is named after it: sma200.
#   Format: whole number of trading days.       Example: 200
SMA_WINDOW = 200

# SMA_SLOPE_LOOKBACK: the SMA is called "rising" if today's value is higher than
#   it was this many trading days ago, "falling" if lower. 20 is about one month.
#   Format: whole number of trading days.       Example: 20
SMA_SLOPE_LOOKBACK = 20


# =============================================================================
# CHANGE TABLE
# -----------------------------------------------------------------------------
# CHANGE_PERIODS: the rows of the "Changes" table. Each entry is
#   (label, kind, amount) where kind is one of:
#     "days"   -> amount is calendar days back   (1 day is special-cased to the previous session)
#     "months" -> amount is calendar months back
#     "years"  -> amount is calendar years back
#     "ytd"    -> last close of the previous year; amount is ignored, use 0
#   The reference close is the last close on or before the lookback date.
#   "From ATH" is always appended automatically and does not need listing.
#   history.csv columns are derived from kind+amount, not the label:
#     ("days",1)->chg_1d_pct  ("days",7)->chg_1w_pct  ("months",3)->chg_3m_pct
#     ("years",1)->chg_1y_pct  ("ytd",0)->chg_ytd_pct
#   Format: list of ("label", "kind", amount) triples, one per line.
#   Example row: ("6 months", "months", 6)
CHANGE_PERIODS = [
    ("1 day", "days", 1),
    ("1 week", "days", 7),
    ("1 month", "months", 1),
    ("3 months", "months", 3),
    ("YTD", "ytd", 0),
    ("1 year", "years", 1),
]


# =============================================================================
# CHART
# -----------------------------------------------------------------------------
# CHART_LOOKBACK_DAYS: how much history the price chart in the report shows,
#   in calendar days back from the last close. 365 = one year, 730 = two years.
#   Format: whole number of calendar days.      Example: 365
CHART_LOOKBACK_DAYS = 365


# =============================================================================
# OUTPUT
# -----------------------------------------------------------------------------
# DEFAULT_OUTDIR: folder for history.csv and the reports, relative to where you
#   run the tool (run.sh always runs from the project folder). The --outdir flag
#   overrides it for a single run.
#   Format: path in quotes.          Examples: "./reports"   or   "/Users/me/Dropbox/bubble"
DEFAULT_OUTDIR = "./reports"

# HISTORY_FILENAME: name of the long-term CSV log inside the output folder.
#   Format: filename in quotes.      Example: "history.csv"
HISTORY_FILENAME = "history.csv"

# LATEST_HTML: name of the always-overwritten copy of the newest report.
#   Format: filename in quotes.      Example: "latest.html"
LATEST_HTML = "latest.html"

# REPORT_RETENTION_DAYS: after each run, dated reports (report_YYYY-MM-DD.html/.pdf) older
#   than this many days are deleted. history.csv and latest.html are never touched, so the
#   long-term log is unaffected. 0 keeps every report forever.
#   Format: whole number of days.    Examples: 90 (about a quarter), 365, 0 (never prune)
REPORT_RETENTION_DAYS = 90


# =============================================================================
# NETWORK (Yahoo Finance via yfinance)
# -----------------------------------------------------------------------------
# FETCH_RETRIES: how many times to attempt each download before giving up.
#   Yahoo rate-limits aggressively, so keep this small (2 to 5).
#   Format: whole number.            Example: 4
FETCH_RETRIES = 4

# FETCH_RETRY_DELAY_SECONDS: pause between attempts.
#   Format: whole number of seconds. Example: 3
FETCH_RETRY_DELAY_SECONDS = 3

# FETCH_TIMEOUT_SECONDS: how long one request may take before it counts as
#   failed. Yahoo can be slow to connect on some networks; 60 is generous.
#   Format: whole number of seconds. Example: 60
FETCH_TIMEOUT_SECONDS = 60


# =============================================================================
# SCHEDULE (automatic morning run on macOS)
# -----------------------------------------------------------------------------
# These are read by schedule/install-schedule.sh. After changing any of them run:
#     schedule/install-schedule.sh
# so launchd picks up the new timing. Times are your Mac's local time, 24-hour.
#
# SCHEDULE_HOUR:   hour of the day, 0 to 23.        Example: 8   (for 08:30)   or   18  (for 6 pm)
SCHEDULE_HOUR = 8

# SCHEDULE_MINUTE: minute of the hour, 0 to 59.     Example: 30
SCHEDULE_MINUTE = 30

# SCHEDULE_WEEKDAYS: which days to run, using launchd numbering:
#     0 = Sunday, 1 = Monday, 2 = Tuesday, 3 = Wednesday, 4 = Thursday, 5 = Friday, 6 = Saturday
#   Pick days so the run happens AFTER the market has closed in your local time.
#   In Asia-Pacific a US close lands the next morning, so Tue-Sat covers Mon-Fri
#   closes. In the US or Europe, run Mon-Fri in the evening instead.
#   Format: list of numbers.         Examples: [2, 3, 4, 5, 6] (Tue-Sat)
#                                              [1, 2, 3, 4, 5] (Mon-Fri)
#                                              [6]             (Saturday only, weekly check)
SCHEDULE_WEEKDAYS = [2, 3, 4, 5, 6]
