#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chỉ mở trang deck bằng headless Chromium để lấy HTML thật (vượt qua màn
chặn bot), rồi lưu ra file. Không in PDF, không quét trang — nhẹ.
Dùng kèm slideshare_direct.py --html deck.html

Chạy được cả trên máy dev lẫn VPS: dùng Chromium mà Playwright đã cài
(không đường dẫn cứng tới trình duyệt, không proxy).
"""
import sys
import time
from playwright.sync_api import sync_playwright

if __name__ == "__main__":
    url, out = sys.argv[1], sys.argv[2]
    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = b.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        # Chờ qua màn chặn bot (nếu có).
        for _ in range(60):
            t = page.title()
            if not any(k in t for k in ("Challenge", "Just a moment",
                                        "Checking your browser",
                                        "Attention Required")):
                break
            time.sleep(2)
        html = page.content()
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"title: {page.title()} | html: {len(html) / 1024:.0f} KB")
        b.close()
