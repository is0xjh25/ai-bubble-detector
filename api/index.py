"""Vercel serverless entry point: the drop-from-ATH page (/), its data (/data.json), icons and the app files.

The response is cached on Vercel's CDN until the next update (hourly while the market is open, otherwise
until the next open), so Yahoo Finance is queried rarely and every visitor sees the same data. When a
copy expires the CDN keeps serving it while it fetches a new one, and keeps serving it if the fetch fails.
"""

import os
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("XDG_CACHE_HOME", "/tmp")  # yfinance caches here; the rest of the filesystem is read-only
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bubble_detector.web import (  # noqa: E402
    APP_ICON_SIZES, ICON_SVG, MANIFEST, STATIC_DIR, SW_JS, WEB_FETCH_RETRIES, WEB_FETCH_TIMEOUT_SECONDS,
    fetch_closes, next_update, public_json, render_error, render_html, robots_txt, sitemap_xml, summarise,
)

STALE_SECONDS = 7 * 24 * 3600  # how long the CDN may serve an expired copy while refreshing or when Yahoo fails


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle(head=False)

    def do_HEAD(self):
        self._handle(head=True)

    def _handle(self, head):
        query = parse_qs(urlparse(self.path).query)
        fmt = (query.get("format") or [""])[0]
        day = "public, max-age=86400"
        if fmt == "icon":
            self._send(200, "image/svg+xml", ICON_SVG, day, head)
            return
        if fmt == "png":
            size = (query.get("size") or [""])[0]
            if size.isdigit() and int(size) in APP_ICON_SIZES:
                self._send(200, "image/png", (STATIC_DIR / f"icon-{size}.png").read_bytes(), day, head)
            else:
                self._send(404, "text/plain; charset=utf-8", "not found", day, head)
            return
        if fmt == "manifest":
            self._send(200, "application/manifest+json", MANIFEST, day, head)
            return
        if fmt == "robots":
            self._send(200, "text/plain; charset=utf-8", robots_txt(), day, head)
            return
        if fmt == "sitemap":
            self._send(200, "application/xml; charset=utf-8", sitemap_xml(datetime.now(timezone.utc)),
                       "public, max-age=3600", head)
            return
        if fmt == "sw":  # browsers check for a new version on every visit, so never let it go stale
            self._send(200, "application/javascript; charset=utf-8", SW_JS, "no-cache", head)
            return
        now = datetime.now(timezone.utc)
        try:
            s = summarise(fetch_closes(WEB_FETCH_RETRIES, WEB_FETCH_TIMEOUT_SECONDS), now)
        except Exception:
            traceback.print_exc()  # goes to the Vercel function logs; visitors get a plain message
            self._send(503, "text/html; charset=utf-8", render_error(), "no-store", head)
            return
        ttl = max(60, int((next_update(now) - now).total_seconds()))
        # Browsers always revalidate; the CDN holds it until the next update, then serves it stale while refetching.
        cache = f"public, max-age=0, s-maxage={ttl}, stale-while-revalidate={STALE_SECONDS}, stale-if-error={STALE_SECONDS}"
        if fmt == "json":
            self._send(200, "application/json", public_json(s), cache, head, {"Access-Control-Allow-Origin": "*", "X-Robots-Tag": "noindex"})
        else:
            self._send(200, "text/html; charset=utf-8", render_html(s), cache, head)

    def _send(self, code, ctype, body, cache, head=False, extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(data)
