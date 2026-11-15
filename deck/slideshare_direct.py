#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tải trực tiếp slide SlideShare rồi ghép thành PDF — KHÔNG qua Chrome Print.

Vì sao cần bản này: với deck dài (vd. 101 trang, ảnh 2048px), bước
Page.printToPDF của Chrome phải rasterize toàn bộ trang trên máy yếu
(1.5 CPU / RAM ít) nên quay rất lâu, có khi không xong. Cách này bỏ qua
hoàn toàn trình duyệt:
  1. Tải HTML trang deck, đọc __NEXT_DATA__ lấy slides{host, imageLocation,
     title, imageSizes} + totalSlides (đúng nguồn mà code inject của
     extension vẫn dùng).
  2. Tải từng ảnh slide ở độ phân giải cao nhất (2048px, fallback 1024/638/320).
  3. Ghép ảnh thành PDF bằng img2pdf (nhúng JPEG nguyên gốc, không nén lại).

Cách dùng:
  python slideshare_direct.py "https://www.slideshare.net/slideshow/.../72620514" -o sach.pdf
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}
FALLBACK_SIZES = [
    {"quality": 75, "width": 2048},
    {"quality": 95, "width": 1024},
    {"quality": 85, "width": 1024},
    {"quality": 85, "width": 638},
    {"quality": 85, "width": 320},
]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_slides_meta(html):
    """Tìm slides{host, imageLocation, title} + totalSlides trong __NEXT_DATA__."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if m:
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            data = None
        if data is not None:
            found = {}

            def scan(obj):
                if isinstance(obj, dict):
                    s = obj.get("slides")
                    if (
                        isinstance(s, dict)
                        and s.get("host")
                        and s.get("imageLocation")
                        and s.get("title")
                    ):
                        found.setdefault("meta", s)
                        if isinstance(obj.get("totalSlides"), int):
                            found["total"] = obj["totalSlides"]
                    for v in obj.values():
                        scan(v)
                elif isinstance(obj, list):
                    for v in obj:
                        scan(v)

            scan(data)
            if found.get("meta") and found.get("total"):
                return found["meta"], found["total"]

    # Fallback: quét regex trực tiếp trên HTML.
    loc = re.search(r'"imageLocation"\s*:\s*"([^"]+)"', html)
    title = re.search(r'"title"\s*:\s*"([^"]+)"\s*,\s*"imageLocation"', html)
    total = re.search(r'"totalSlides"\s*:\s*(\d+)', html)
    if loc and title and total:
        meta = {
            "host": "https://image.slidesharecdn.com",
            "imageLocation": loc.group(1),
            "title": title.group(1),
            "imageSizes": FALLBACK_SIZES,
        }
        return meta, int(total.group(1))
    return None, None


def is_image(data):
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"


def main():
    ap = argparse.ArgumentParser(description="SlideShare direct-to-PDF")
    ap.add_argument("url", help="Link deck SlideShare")
    ap.add_argument("-o", "--output", required=True, help="File PDF đầu ra")
    ap.add_argument("--workdir", default=None, help="Thư mục chứa ảnh slide")
    ap.add_argument("--html", default=None,
                    help="Dùng HTML trang deck đã lưu sẵn thay vì tải lại "
                         "(khi bị chặn bot)")
    ap.add_argument("--jobs", type=int, default=8, help="Số luồng tải song song")
    args = ap.parse_args()

    t_start = time.time()
    out_dir = os.path.dirname(os.path.abspath(args.output))
    deck_id = re.sub(r"\D", "", args.url.rstrip("/").split("/")[-1]) or "deck"
    workdir = args.workdir or os.path.join(out_dir, f"slides-{deck_id}")
    os.makedirs(workdir, exist_ok=True)

    sess = requests.Session()
    sess.headers.update(UA)
    if args.html:
        with open(args.html, encoding="utf-8") as f:
            html = f.read()
        log(f"Đọc HTML trang deck từ file: {len(html) / 1024:.0f} KB")
    else:
        r = sess.get(args.url, timeout=60)
        log(f"GET trang deck: HTTP {r.status_code}, {len(r.content) / 1024:.0f} KB")
        if r.status_code != 200:
            print(f"Không tải được trang deck (HTTP {r.status_code}).",
                  file=sys.stderr)
            return 2
        html = r.text

    meta, total = find_slides_meta(html)
    if not meta or not total:
        print("Không tìm thấy thông tin slide trong trang (có thể bị chặn bot).",
              file=sys.stderr)
        return 3
    host = meta["host"].rstrip("/")
    loc, title = meta["imageLocation"], meta["title"]
    sizes = sorted(
        meta.get("imageSizes") or FALLBACK_SIZES, key=lambda d: -d["width"]
    )
    # Thêm các mức fallback nếu metadata thiếu.
    seen = {(s["quality"], s["width"]) for s in sizes}
    for s in FALLBACK_SIZES:
        if (s["quality"], s["width"]) not in seen:
            sizes.append(s)
    log(f"Deck: {total} slide | ảnh gốc {sizes[0]['width']}px "
        f"(quality {sizes[0]['quality']}) | thư mục ảnh: {workdir}")

    def fetch_slide(i):
        for s in sizes:
            url = f"{host}/{loc}/{s['quality']}/{title}-{i}-{s['width']}.jpg"
            try:
                resp = sess.get(url, timeout=30)
            except requests.RequestException:
                continue
            if resp.status_code == 200 and is_image(resp.content):
                path = os.path.join(workdir, f"slide-{i:03d}.jpg")
                with open(path, "wb") as f:
                    f.write(resp.content)
                return i, path, s["width"]
        return i, None, None

    t0 = time.time()
    paths = [None] * (total + 1)
    used_width = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for i, path, w in ex.map(fetch_slide, range(1, total + 1)):
            paths[i] = path
            if w:
                used_width[w] = used_width.get(w, 0) + 1
            if i % 20 == 0 or i == total:
                done = sum(1 for p in paths[1:] if p)
                log(f"  ... đã tải {done}/{total} slide ({time.time() - t0:.0f}s)")
    missing = [i for i in range(1, total + 1) if not paths[i]]
    if missing:
        log(f"Còn thiếu {len(missing)} slide: {missing[:20]}"
            f"{'...' if len(missing) > 20 else ''} — thử tải lại 1 lần...")
        for i in missing:
            _, path, w = fetch_slide(i)
            paths[i] = path
            if w:
                used_width[w] = used_width.get(w, 0) + 1
        missing = [i for i in range(1, total + 1) if not paths[i]]
    ok_paths = [p for p in paths[1:] if p]
    log(f"Đã tải: {len(ok_paths)}/{total} slide "
        f"(độ phân giải: {used_width}) trong {time.time() - t0:.1f}s")
    if not ok_paths:
        print("Không tải được slide nào.", file=sys.stderr)
        return 4
    if missing:
        log(f"CẢNH BÁO: PDF sẽ thiếu các slide: {missing}")

    import img2pdf

    t0 = time.time()
    with open(args.output, "wb") as f:
        f.write(img2pdf.convert(ok_paths))
    size_mb = os.path.getsize(args.output) / 1024 / 1024
    log(f"Đã ghi PDF: {args.output} | {size_mb:.1f} MB | "
        f"{len(ok_paths)} trang | ghép mất {time.time() - t0:.1f}s")

    summary = {
        "url": args.url,
        "output": os.path.abspath(args.output),
        "size_mb": round(size_mb, 2),
        "slides_total": total,
        "slides_ok": len(ok_paths),
        "missing_slides": missing,
        "widths": used_width,
        "total_seconds": round(time.time() - t_start, 1),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not missing else 5


if __name__ == "__main__":
    sys.exit(main())
