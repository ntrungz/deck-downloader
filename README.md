# deck-downloader

Download SlideShare / Studocu / Scribd decks as PDF, from the command line
or as a Python library. No browser window needed — everything runs headless.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

Python 3.10+. Needs `ffmpeg` only if you use the dataset tools (not here).
`poppler-utils` (`pdfinfo`) is optional — used only to count PDF pages.

## Quick start

```bash
# One command, any of the three sites:
python deck_download.py "https://www.slideshare.net/slideshow/..." -o deck.pdf

# Or use the scripts directly:
python deck/slideshare_direct.py "https://www.slideshare.net/..." -o deck.pdf
python deck/bamidoc_headless.py "https://www.studocu.com/..." -o doc.pdf
```

As a library:

```python
from deck_download import download_deck

info = download_deck("https://www.slideshare.net/...", "deck.pdf")
print(info)  # {'site': 'slideshare', 'pages': 217, 'size_mb': 48.0, ...}
```

## How it works

| Site | Path | How |
|---|---|---|
| SlideShare | direct | `fetch_deck_html.py` opens the deck page in headless Chromium (gets past the bot challenge) and saves the HTML; `slideshare_direct.py` reads `__NEXT_DATA__` for the slide list, downloads each slide image at the highest resolution (2048px, with fallbacks) in parallel, and merges them into a PDF with `img2pdf` (original JPEGs embedded, no re-encode). |
| Studocu / Scribd | headless print | `bamidoc_headless.py` drives headless Chromium and prints the document straight to PDF with `Page.printToPDF` — no Print Preview dialog, which is what makes the browser extension slow on long documents. Supports `--proxy`, `--storage-state` (logged-in session), `--channel chrome`, `--no-embed`. |

## Limits (honest)

- **Bot challenges:** these sites often show a "Client Challenge" / "Just a moment" page to datacenter IPs. The scripts wait up to 120s for the challenge to clear on its own. If it doesn't, run from a residential IP / VPN, or use `--proxy`. `download_deck()` also accepts an optional `network_context` context manager (e.g. one that switches a VPN/Tailscale exit node) — it is entered for the whole download and always exited afterwards.
- **Studocu / Scribd, basic mode:** the headless print path was built around the page-scanning engine of the Bánh Mì Cẩm Phả browser extension (`deck/inject/*.js` in the original project). **Those engine files are NOT included in this repo** — they are third-party extension code with no clear license, so they cannot be republished here. Without them, `bamidoc_headless.py` runs in **basic mode**: it scrolls the page to lazy-load images and prints the original page directly. Basic mode only captures publicly visible pages; blurred / paywalled pages stay blurred in the PDF. If you have the engine files, drop them into `deck/inject/` (`inject_studocu_pdf.js`, `inject_scribd_pdf.js`, `inject_slideshare_pdf.js`) and the full engine path is used automatically.
- **Site changes:** the scrapers read the sites' current page structure (`__NEXT_DATA__`, `.pf` page elements). If a site redesigns, downloads may fail until the selectors are updated.
- **Copyright:** only download documents you have the right to save. The authors are not responsible for misuse.

## License

Apache License 2.0 — see `LICENSE`.
