"""Desktop notifications (macOS). Used by --notify: only speaks up on BUY / RECOVERY or on failure."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import TITLE


def _osascript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


NOTIFIER_APP = Path.home() / "Applications" / f"{TITLE}.app"   # built by schedule/build-notifier.sh


def send(subtitle: str, message: str, open_path: Path | None = None, sound: bool = True) -> bool:
    """Show a notification. Returns True if a notifier ran. Never raises.

    Order of preference: the branded notifier app (shows the Bubble Detector icon),
    terminal-notifier (click opens the report), plain osascript (Script Editor icon)."""
    if sys.platform != "darwin":
        ns = shutil.which("notify-send")  # Linux desktops
        if ns:
            try:
                subprocess.run([ns, f"{TITLE}: {subtitle}", message], check=False, timeout=15, capture_output=True)
                return True
            except Exception:
                pass
        print(f"[notify] {TITLE} - {subtitle}: {message}", file=sys.stderr)
        return False
    try:
        applet = NOTIFIER_APP / "Contents" / "MacOS" / "applet"
        if applet.exists():
            env = dict(os.environ, BD_TITLE=TITLE, BD_SUBTITLE=subtitle, BD_MESSAGE=message, BD_SOUND="" if sound else "silent")
            r = subprocess.run([str(applet)], env=env, check=False, timeout=20, capture_output=True, text=True)
            if r.returncode == 0:
                return True
            print(f"warning: notifier app failed ({r.stderr.strip()[:200]}); falling back", file=sys.stderr)
        tn = shutil.which("terminal-notifier")
        if tn:  # optional: brew install terminal-notifier -> clicking the banner opens the report
            cmd = [tn, "-title", TITLE, "-subtitle", subtitle, "-message", message, "-group", "bubble-detector"]
            if sound:
                cmd += ["-sound", "default"]
            if open_path is not None:
                cmd += ["-open", open_path.resolve().as_uri()]
            subprocess.run(cmd, check=False, timeout=15, capture_output=True)
            return True
        script = (f'display notification "{_osascript_escape(message)}" with title "{_osascript_escape(TITLE)}" '
                  f'subtitle "{_osascript_escape(subtitle)}"' + (' sound name "default"' if sound else ""))
        r = subprocess.run(["osascript", "-e", script], check=False, timeout=15, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"warning: notification failed: {r.stderr.strip()}", file=sys.stderr)
            return False
        return True
    except Exception as exc:
        print(f"warning: notification failed ({exc})", file=sys.stderr)
        return False
