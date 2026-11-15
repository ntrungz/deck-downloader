#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Download SlideShare / Studocu / Scribd decks as PDF.

Two download paths:
- SlideShare: direct — fetch the deck HTML with headless Chromium
  (fetch_deck_html.py, gets past the bot challenge), then download the
  slide images at full resolution and merge them into a PDF with img2pdf
  (deck/slideshare_direct.py). Fast and light, no browser printing.
- Studocu / Scribd: headless print — deck/bamidoc_headless.py drives
  headless Chromium and prints the document straight to PDF with
  Page.printToPDF (no Print Preview dialog).

Each downloader prints a JSON summary as its last stdout line; this
module parses it back to get page counts / file size, and recounts the
real page count from the PDF with PyMuPDF.

Network note: these sites often challenge datacenter IPs ("Client
Challenge" / "Just a moment"). If downloads fail from a server, run from
a residential IP / VPN, or pass a proxy to the headless scripts
(bamidoc_headless.py --proxy), or pass a ``network_context`` context
manager (e.g. one that switches a VPN/Tailscale exit node) to
``download_deck`` — it is entered for the whole download and always
exited afterwards, even on error.

Usage as a library:
    from deck_download import download_deck
    info = download_deck("https://www.slideshare.net/...", "deck.pdf")

Usage as a CLI:
    python deck_download.py "https://www.slideshare.net/..." -o deck.pdf
"""
import argparse
import contextlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("deck_downloader")

BASE_DIR = Path(__file__).resolve().parent
DECK_DIR = BASE_DIR / "deck"

DECK_RE = re.compile(
    r"(slideshare\.net|studocu\.com|studeersnel\.com|scribd\.com)", re.I
)


def detect_deck_site(url):
    """slideshare | studocu | scribd | None."""
    u = (url or "").lower()
    if "slideshare.net" in u:
        return "slideshare"
    if "studocu.com" in u or "studeersnel.com" in u:
        return "studocu"
    if "scribd.com" in u:
        return "scribd"
    return None


def _parse_summary(stdout):
    """Get the JSON summary the download scripts print at end of stdout."""
    idx = stdout.rfind("\n{")
    if idx == -1:
        return {}
    try:
        return json.loads(stdout[idx + 1:])
    except json.JSONDecodeError:
        return {}


def _count_pdf_pages(pdf_path):
    try:
        import fitz  # PyMuPDF

        with fitz.open(pdf_path) as doc:
            return len(doc)
    except Exception:
        return None


def download_deck(url, out_pdf, timeout=1800, network_context=None):
    """Download a deck as PDF. Returns dict {site, pages, size_mb, output, summary}.
    Raises RuntimeError / ValueError on failure.

    ``network_context``: optional context manager entered for the whole
    download (e.g. to route traffic through a VPN / exit node). Defaults
    to a no-op.
    """
    ctx = network_context or contextlib.nullcontext()
    with ctx:
        return _download_deck_inner(url, out_pdf, timeout=timeout)


def _download_deck_inner(url, out_pdf, timeout=1800):
    """The actual download (already inside the network context)."""
    site = detect_deck_site(url)
    if not site:
        raise ValueError("Link is not SlideShare / Studocu / Scribd.")
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    log.info("Downloading deck %s: %s", site, url)

    if site == "slideshare":
        html_path = out_pdf.with_suffix(".html")
        r = subprocess.run(
            [py, str(DECK_DIR / "fetch_deck_html.py"), url, str(html_path)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0 or not html_path.is_file():
            raise RuntimeError(
                "Could not fetch the deck HTML "
                f"(exit {r.returncode}): {(r.stdout + r.stderr)[-500:]}"
            )
        r = subprocess.run(
            [py, str(DECK_DIR / "slideshare_direct.py"), url,
             "-o", str(out_pdf), "--html", str(html_path)],
            capture_output=True, text=True, timeout=timeout,
        )
        summary = _parse_summary(r.stdout)
        if r.returncode != 0 or not out_pdf.is_file():
            raise RuntimeError(
                "SlideShare download failed "
                f"(exit {r.returncode}): {(r.stdout + r.stderr)[-800:]}"
            )
        pages = _count_pdf_pages(out_pdf) or summary.get("slides_ok")
    else:
        r = subprocess.run(
            [py, str(DECK_DIR / "bamidoc_headless.py"), url,
             "-o", str(out_pdf), "--timeout", "900"],
            capture_output=True, text=True, timeout=timeout,
        )
        summary = _parse_summary(r.stdout)
        if r.returncode != 0 or not out_pdf.is_file():
            raise RuntimeError(
                f"{site} download failed "
                f"(exit {r.returncode}): {(r.stdout + r.stderr)[-800:]}"
            )
        pages = _count_pdf_pages(out_pdf) or summary.get("pdf_pages")

    size_mb = out_pdf.stat().st_size / (1024 * 1024)
    if size_mb < 0.05:
        raise RuntimeError("Downloaded PDF is too small / empty.")
    result = {
        "site": site,
        "pages": pages,
        "size_mb": round(size_mb, 2),
        "output": str(out_pdf),
        "summary": summary,
    }
    log.info("Deck downloaded: %s pages, %.1f MB", pages, size_mb)
    return result


def main():
    ap = argparse.ArgumentParser(description="Download a SlideShare / Studocu / Scribd deck as PDF")
    ap.add_argument("url", help="Deck link")
    ap.add_argument("-o", "--output", required=True, help="Output PDF file")
    ap.add_argument("--timeout", type=int, default=1800, help="Download timeout in seconds")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        info = download_deck(args.url, args.output, timeout=args.timeout)
    except (ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
