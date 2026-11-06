"""Vercel serverless entry point: serves the drop-from-ATH page (/) and its data (/data.json).

The response is cached on Vercel's CDN until the next hourly update, so Yahoo Finance is
queried at most about once an hour per region, and every visitor in that hour sees the same data.
"""

import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("XDG_CACHE_HOME", "/tmp")  # yfinance caches here; the rest of the filesystem is read-only
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bubble_detector.web import fetch_closes, next_update, public_json, render_html, summarise  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        as_json = parse_qs(urlparse(self.path).query).get("format") == ["json"]
        now = datetime.now(timezone.utc)
        try:
            s = summarise(fetch_closes(), now)
        except Exception as exc:
            self._send(503, "text/plain; charset=utf-8", f"Could not load market data, try again shortly. ({exc})",
                       "no-store")
            return
        ttl = max(60, int((next_update(now) - now).total_seconds()))
        cache = f"public, max-age=0, s-maxage={ttl}"  # browsers always revalidate; the CDN holds it until the next update
        if as_json:
            self._send(200, "application/json", public_json(s), cache)
        else:
            self._send(200, "text/html; charset=utf-8", render_html(s), cache)

    def _send(self, code, ctype, body, cache):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
