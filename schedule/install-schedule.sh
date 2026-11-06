#!/usr/bin/env bash
# Install a launchd agent that runs Bubble Detector on the days/time set in bubble_detector/config.py
# (a US trading day's close reaches Sydney the next morning, so Fri's close arrives Sat).
#
#   schedule/install-schedule.sh            # install / update using SCHEDULE_* in bubble_detector/config.py
#   schedule/install-schedule.sh 7 45       # one-off override of hour and minute
#
# The job runs `run.sh --no-open --notify`: it writes the reports and log, and sends a macOS
# notification only when the signal is BUY or RECOVERY, or when the run fails.
# If the Mac is asleep at 08:30, launchd runs the job when it next wakes.
#
# macOS note: the project must be readable by a background job. If it lives in ~/Documents, ~/Desktop
# or ~/Downloads, add /bin/bash under System Settings > Privacy & Security > Full Disk Access first.

set -euo pipefail
[ "$(uname -s)" = "Darwin" ] || { echo "error: launchd scheduling is macOS-only. On Linux use cron, e.g.: 30 8 * * 2-6 /path/to/run.sh --no-open --notify" >&2; exit 1; }

LABEL="com.bubble-detector.daily"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Defaults come from bubble_detector/config.py (SCHEDULE_HOUR, SCHEDULE_MINUTE, SCHEDULE_WEEKDAYS);
# command-line arguments override hour and minute for a one-off.
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
read -r CFG_HOUR CFG_MINUTE CFG_DAYS < <(cd "$HERE" && "$PY" -c '
from bubble_detector import config as c
print(c.SCHEDULE_HOUR, c.SCHEDULE_MINUTE, ",".join(str(d) for d in c.SCHEDULE_WEEKDAYS))')
HOUR="${1:-$CFG_HOUR}"
MINUTE="${2:-$CFG_MINUTE}"
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
HERE_X="$(xml "$HERE")"; LOGDIR_X="$(xml "$HOME/Library/Logs/bubble-detector")"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# Log lives outside ~/Documents: launchd itself opens it, and launchd has no Documents access.
LOGDIR="$HOME/Library/Logs/bubble-detector"
mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

cal_entries=""
for wd in ${CFG_DAYS//,/ }; do   # launchd weekdays: 0/7 = Sunday ... 6 = Saturday
  cal_entries+="      <dict><key>Weekday</key><integer>$wd</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
"
done

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$HERE_X/run.sh</string>
    <string>--no-open</string>
    <string>--notify</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
$cal_entries  </array>
  <key>StandardOutPath</key><string>$LOGDIR_X/launchd.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR_X/launchd.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST

plutil -lint "$PLIST" >/dev/null

# (re)load: bootout ignores "not loaded" errors
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

names=(Sun Mon Tue Wed Thu Fri Sat Sun); daylist=""; for wd in ${CFG_DAYS//,/ }; do daylist+="${names[$wd]} "; done
printf 'Installed %s\n  runs %sat %02d:%02d, log: %s/launchd.log\n' "$LABEL" "$daylist" "$HOUR" "$MINUTE" "$LOGDIR"

# ---- permission check -------------------------------------------------------
# macOS does not let background jobs read ~/Documents, ~/Desktop or ~/Downloads unless the
# program (here /bin/bash) has Full Disk Access. Run a throwaway launchd job that tries to read
# run.sh the same way the real job will, and explain what to do if it is blocked.
PROBE_LABEL="$LABEL.probe"
PROBE_DIR="$(mktemp -d)"; PROBE_PLIST="$PROBE_DIR/probe.plist"; PROBE_OUT="$PROBE_DIR/out.txt"
cat > "$PROBE_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$PROBE_LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>-c</string>
    <string>head -c 1 "\$1" >/dev/null 2>"\$2" &amp;&amp; echo ok >>"\$2"</string><string>--</string>
    <string>$HERE_X/run.sh</string><string>$(xml "$PROBE_OUT")</string></array>
  <key>RunAtLoad</key><false/>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$PROBE_LABEL" 2>/dev/null || true
if launchctl bootstrap "gui/$(id -u)" "$PROBE_PLIST" 2>/dev/null && launchctl kickstart "gui/$(id -u)/$PROBE_LABEL" 2>/dev/null; then
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$PROBE_OUT" ] && break; sleep 0.5; done
fi
launchctl bootout "gui/$(id -u)/$PROBE_LABEL" 2>/dev/null || true
if grep -q "^ok$" "$PROBE_OUT" 2>/dev/null; then
  echo "Permission check: OK, background jobs can read the project."
else
  cat <<MSG

PERMISSION NEEDED: a background job cannot read $HERE
  ($(grep -m1 . "$PROBE_OUT" 2>/dev/null || echo "no response from the probe job"))
  macOS blocks background jobs from ~/Documents, ~/Desktop and ~/Downloads. Fix it one of two ways:

  A) Give bash Full Disk Access (keeps the project where it is):
     1. System Settings > Privacy & Security > Full Disk Access (opening it for you now)
     2. Click "+", press Cmd+Shift+G, type  /bin/bash  and press Return, then click Open
     3. Make sure the switch next to "bash" is on
     4. Re-run this script to confirm the check passes

  B) Move the project out of those folders, e.g.  mv "$HERE" ~/bubble-detector
     and re-run this script from the new location.
MSG
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
fi
rm -rf "$PROBE_DIR"

echo "Test it now with:  launchctl kickstart gui/$(id -u)/$LABEL"
echo "Remove it with:    schedule/uninstall-schedule.sh"
