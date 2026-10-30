#!/usr/bin/env bash
# Remove the Bubble Detector launchd agent.
set -euo pipefail
[ "$(uname -s)" = "Darwin" ] || { echo "error: macOS-only" >&2; exit 1; }
LABEL="com.bubble-detector.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Removed $LABEL"
