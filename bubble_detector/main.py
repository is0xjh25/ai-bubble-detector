"""CLI entry point: python -m bubble_detector [--no-pdf] [--no-html] [--json] [--outdir PATH]"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .calc import compute_snapshot
from .config import DEFAULT_OUTDIR, HISTORY_FILENAME, INDEX_NAME, LATEST_HTML, REPORT_RETENTION_DAYS, TAGLINE, TITLE
from .data import fetch_market_data
from . import notify
from .report import (append_history, previous_logged_row, prune_reports, read_history, render_console,
                     render_html, write_pdf)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bubble_detector",
                                description=f"{TITLE}: check the {INDEX_NAME} against a crash-buy ladder. {TAGLINE}")
    p.add_argument("--no-pdf", action="store_true", help="skip the PDF report")
    p.add_argument("--no-html", action="store_true", help="skip the HTML report (implies --no-pdf)")
    p.add_argument("--json", action="store_true", help="print core data as JSON to stdout instead of tables")
    p.add_argument("--outdir", default=DEFAULT_OUTDIR, metavar="PATH", help=f"output directory (default {DEFAULT_OUTDIR})")
    p.add_argument("--notify", action="store_true",
                   help="send a desktop notification when the signal is BUY or RECOVERY, or when the run fails")
    p.add_argument("--version", action="version", version=f"{TITLE} {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:  # anything unexpected: say so loudly if asked, never die silently
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.notify:
            notify.send("Run failed", f"{type(exc).__name__}: {exc}"[:200])
        return 1


def _run(args: argparse.Namespace) -> int:
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    log = (lambda *a: print(*a, file=sys.stderr)) if args.json else print

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        md = fetch_market_data()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.notify:
            notify.send("Data fetch failed", f"{exc}"[:200] + " Check manually.")
        return 1
    prev_logged = previous_logged_row(read_history(outdir), md.index_closes.index[-1].date())
    snap = compute_snapshot(md, generated_at, prev_logged)

    history = append_history(outdir, snap.history_row())
    written = [str(outdir / HISTORY_FILENAME)]

    if args.json:
        print(json.dumps(snap.to_dict(), indent=2))
    else:
        print(render_console(snap))
        print()

    if not args.no_html:
        html_text = render_html(snap, md.index_closes, history)
        dated = outdir / f"report_{snap.last_date.isoformat()}.html"
        dated.write_text(html_text, encoding="utf-8")
        (outdir / LATEST_HTML).write_text(html_text, encoding="utf-8")
        written += [str(dated), str(outdir / LATEST_HTML)]
        if not args.no_pdf:
            pdf_path = outdir / f"report_{snap.last_date.isoformat()}.pdf"
            engine = write_pdf(html_text, pdf_path)
            if engine:
                written.append(f"{pdf_path} ({engine})")

    log("Written:")
    for w in written:
        log(f"  {w}")
    pruned = prune_reports(outdir, REPORT_RETENTION_DAYS, snap.last_date)
    if pruned:
        log(f"Pruned {len(pruned)} report file(s) older than {REPORT_RETENTION_DAYS} days.")

    if args.notify and snap.action.signal in ("BUY", "RECOVERY"):
        report = outdir / LATEST_HTML if not args.no_html else None
        notify.send(snap.action.headline, snap.action.detail, open_path=report)
    return 0
