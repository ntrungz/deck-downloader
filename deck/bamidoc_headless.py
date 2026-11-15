#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BamiDoc Headless — tải PDF Studocu / Scribd / SlideShare bằng Chrome không giao diện.

Vì sao bản extension trên Chrome thường "load quá lâu" ở bước lưu PDF?
  Extension không tự tạo file PDF. Nó chỉ:
    1. cuộn hết các trang để load ảnh,
    2. convert toàn bộ ảnh sang Base64 data-URI rồi nhét vào một "Trang Tải",
    3. bấm nút In -> gọi window.print() -> Chrome mở hộp thoại Print Preview.
  Chính Print Preview mới là chỗ chậm: nó phải render thumbnail xem trước cho
  TỪNG trang (ảnh base64 phình ~33%, DOM nặng hàng chục MB) ngay trên luồng UI,
  nên với tài liệu dài nó quay mãi không xong.

Bản headless này dùng LẠI Y NGUYÊN code inject của extension (quét trang +
dựng Trang Tải), nhưng thay bước window.print() bằng lệnh in trực tiếp của
Chrome DevTools (Page.printToPDF): không hộp thoại, không render preview,
ghi thẳng ra file .pdf. Nhanh hơn hẳn, và chạy ngầm không cần mở cửa sổ.

Cách dùng (trên máy của bạn — Windows/macOS/Linux đều được):
  pip install playwright
  playwright install chromium        # hoặc dùng Chrome sẵn có: thêm --channel chrome
  python bamidoc_headless.py "https://www.studocu.com/.../..." -o tailieu.pdf

  Tài liệu cần đăng nhập: đăng nhập trước trong Chrome rồi xuất storage state,
  hoặc chạy với --storage-state state.json (xem README).

CHẾ ĐỘ CƠ BẢN (không có code inject):
  Bản này vốn dùng lại code inject của extension Bánh Mì Cẩm Phả để quét
  trang và dựng "Trang Tải". Các file inject đó KHÔNG kèm trong repo này
  (code của extension khác, không rõ giấy phép — xem README). Khi thiếu
  file inject, script tự chạy chế độ cơ bản: cuộn hết trang để load ảnh
  rồi in trực tiếp trang gốc ra PDF. Chế độ cơ bản chỉ lấy được các trang
  hiển thị công khai; trang bị mờ/khóa vẫn mờ trong PDF.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INJECT_DIR = os.path.join(HERE, "inject")

SITE_FILES = {
    "studocu": "inject_studocu_pdf.js",
    "scribd": "inject_scribd_pdf.js",
    "slideshare": "inject_slideshare_pdf.js",
}
RUNNING_FLAG = {
    "studocu": "__banhmi_studocu_running",
    "scribd": "__banhmi_scribd_running",
    "slideshare": "__banhmi_slideshare_running",
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def detect_site(url):
    u = url.lower()
    if "studocu" in u or "studeersnel" in u:
        return "studocu"
    if "scribd.com" in u:
        return "scribd"
    if "slideshare.net" in u:
        return "slideshare"
    return None


def replace_function_body(js, func_name, new_body):
    """Thay toàn bộ thân của `function <func_name>(...) {...}` bằng new_body.

    Dùng cho --no-embed: biến hàm fetch ảnh thành hàm trả về rỗng ngay, để
    Trang Tải giữ nguyên URL ảnh gốc thay vì nhúng Base64 (nhẹ hơn, in nhanh
    hơn). Chỉ áp dụng cho Studocu/Scribd (fetchDataUri) và SlideShare
    (preloadSingleSlide).
    """
    m = re.search(r"function\s+" + re.escape(func_name) + r"\s*\([^)]*\)\s*\{", js)
    if not m:
        return js, False
    start = m.start()
    i = m.end() - 1  # vị trí của '{'
    depth = 0
    j = i
    while j < len(js):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0:
        return js, False
    header = js[start : i + 1]  # "function name(...) {"
    return js[:start] + header + "\n" + new_body + "\n}" + js[j + 1 :], True


def find_chromium():
    """Tìm Chromium/Chrome để chạy headless. Ưu tiên Chrome hệ thống."""
    candidates = [
        os.environ.get("BAMIDOC_CHROME", ""),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        os.path.expanduser("~/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome"),
        os.path.expanduser(
            "~/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell"
        ),
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def probe_doc(page, site):
    """Đọc nhanh thông tin tài liệu trước khi chạy inject."""
    if site == "studocu":
        return page.evaluate(
            """() => {
            let total = null, gated = false;
            try {
                const el = document.querySelector('#__NEXT_DATA__');
                const d = JSON.parse(el.textContent);
                const pp = (d.props && d.props.pageProps) || {};
                const doc = pp.document || {};
                total = doc.numberOfPages || doc.pageCount || null;
                const da = pp.documentAccess || {};
                gated = !!(da.hasBlurredPages || doc.hasBlurredPages || pp.hasBlurredPages);
            } catch (e) {}
            if (!gated) gated = !!document.querySelector('.blurred-page, img[src*="/pages/blurred/"], [class*="blurred"]');
            return {total, gated, pf: document.querySelectorAll('.pf').length,
                    title: document.title};
        }"""
        )
    if site == "scribd":
        return page.evaluate(
            """() => ({total: null, gated: !!document.querySelector('[class*="blur"]'),
                      pf: document.querySelectorAll('.pf, .page').length, title: document.title})"""
        )
    return page.evaluate(
        """() => ({total: null, gated: false,
                  pf: document.querySelectorAll('img[src*="slidesharecdn"]').length,
                  title: document.title})"""
    )


def viewer_stats(page):
    return page.evaluate(
        """() => {
        const v = document.getElementById('clean-viewer-container');
        if (!v) return null;
        const imgs = [...v.querySelectorAll('img')];
        const dataImgs = imgs.filter(i => (i.getAttribute('src') || '').startsWith('data:'));
        const dataBytes = dataImgs.reduce((s, i) => s + (i.getAttribute('src') || '').length, 0);
        return {pages: v.querySelectorAll('.pf').length,
                slides: v.querySelectorAll('.clean-viewer-slide, .slide').length,
                imgs: imgs.length, dataImgs: dataImgs.length,
                dataMB: Math.round(dataBytes / 1024 / 1024 * 10) / 10,
                gatedNotes: v.querySelectorAll('.banhmi-gated-note').length};
    }"""
    )


def pdf_info(path):
    try:
        out = subprocess.run(
            ["pdfinfo", path], capture_output=True, text=True, timeout=30
        ).stdout
        pages = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(pages.group(1)) if pages else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="BamiDoc headless PDF downloader")
    ap.add_argument("url", help="Link Studocu / Scribd / SlideShare")
    ap.add_argument("-o", "--output", required=True, help="File PDF đầu ra, vd. tailieu.pdf")
    ap.add_argument("--site", choices=["auto", "studocu", "scribd", "slideshare"], default="auto")
    ap.add_argument("--lang", default="vi")
    ap.add_argument("--timeout", type=int, default=600, help="Giây chờ Trang Tải xong (mặc định 600)")
    ap.add_argument("--proxy", default=None, help="Proxy http://host:port (nếu mạng cần)")
    ap.add_argument("--ignore-https-errors", action="store_true",
                    help="Bỏ qua lỗi chứng chỉ (chỉ dùng khi test sau proxy chặn)")
    ap.add_argument("--storage-state", default=None, help="File storage_state JSON của Playwright (đã đăng nhập)")
    ap.add_argument("--executable", default=None, help="Đường dẫn Chrome/Chromium")
    ap.add_argument("--channel", default=None, help='Dùng Chrome đã cài: --channel chrome')
    ap.add_argument("--headed", action="store_true", help="Hiện cửa sổ Chrome (để debug)")
    ap.add_argument("--no-embed", action="store_true",
                    help="THỬ NGHIỆM: bỏ qua bước nhúng ảnh Base64, giữ URL ảnh gốc (nhẹ hơn, có thể in nhanh hơn nữa)")
    ap.add_argument("--fresh", action="store_true",
                    help="Xóa cookie/storage của trang rồi tải lại trước khi quét (mẹo mở khóa Studocu của extension)")
    args = ap.parse_args()

    t_start = time.time()
    site = args.site if args.site != "auto" else detect_site(args.url)
    if not site:
        print("Không nhận ra trang (chỉ hỗ trợ Studocu / Scribd / SlideShare).", file=sys.stderr)
        return 2
    inject_path = os.path.join(INJECT_DIR, SITE_FILES[site])
    if os.path.isfile(inject_path):
        inject_js = open(inject_path, encoding="utf-8").read()
        basic_mode = False
    else:
        # No inject engine available (not shipped in this repo — see README).
        # Fall back to basic mode: print the original page directly.
        log(f"Không thấy file inject {inject_path} — chạy chế độ cơ bản "
            "(in trực tiếp, không có engine quét trang).")
        inject_js = None
        basic_mode = True

    if args.no_embed and not basic_mode:
        if site in ("studocu", "scribd"):
            inject_js, ok = replace_function_body(
                inject_js, "fetchDataUri", "return Promise.resolve(null);"
            )
        else:
            inject_js, ok = replace_function_body(
                inject_js, "preloadSingleSlide", "return Promise.resolve(url);"
            )
        log(f"--no-embed: đã patch hàm fetch ảnh ({'ok' if ok else 'KHÔNG tìm thấy hàm, chạy như bình thường'})")
    elif args.no_embed:
        log("--no-embed chỉ có tác dụng khi có code inject; chế độ cơ bản bỏ qua.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Chưa cài playwright. Chạy: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    exe = args.executable or find_chromium()
    log(f"Site: {site} | Chrome: {exe or args.channel or 'playwright-bundled'} | headless: {not args.headed}")

    with sync_playwright() as pw:
        launch_kw = {"headless": not args.headed, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if args.proxy:
            launch_kw["proxy"] = {"server": args.proxy}
        if args.channel:
            launch_kw["channel"] = args.channel
        elif exe:
            launch_kw["executable_path"] = exe
        browser = pw.chromium.launch(**launch_kw)

        ctx_kw = {"viewport": {"width": 1440, "height": 900},
                  "ignore_https_errors": args.ignore_https_errors}
        if args.storage_state:
            ctx_kw["storage_state"] = args.storage_state
        context = browser.new_context(**ctx_kw)
        page = context.new_page()
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

        t0 = time.time()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)
        log(f"Mở trang xong: {page.title()[:80]} ({time.time() - t0:.1f}s)")

        # Một số trang (đặc biệt SlideShare) thỉnh thoảng trả về màn hình thử
        # thách bot ("Client Challenge" / "Just a moment") rồi mới tự chuyển
        # về tài liệu thật sau vài giây. Đợi trang thật hiện ra rồi mới quét.
        challenge_words = ("Challenge", "Just a moment", "Checking your browser",
                           "Attention Required")
        waited = 0
        while any(w in page.title() for w in challenge_words) and waited < 120:
            if waited == 0:
                log("Trang đang hiện màn hình thử thách bot, đợi nó tự chuyển về tài liệu...")
            page.wait_for_timeout(2000)
            waited += 2
        if any(w in page.title() for w in challenge_words):
            log("Sau 120s trang vẫn ở màn hình thử thách — site đang chặn máy này. Dừng lại, thử lại sau.")
            page.screenshot(path=args.output + ".debug.png")
            browser.close()
            return 4
        if waited:
            log(f"Đã qua màn thử thách sau {waited}s: {page.title()[:80]}")
            page.wait_for_timeout(3000)

        info = probe_doc(page, site)
        log(f"Tài liệu: {info.get('title', '')[:80]} | tổng số trang (khai báo): {info.get('total')} "
            f"| trang đã render .pf: {info.get('pf')} | bị mờ/khóa: {info.get('gated')}")
        if dialogs:
            log(f"Dialog đã tự đóng: {dialogs}")

        # Nếu chưa thấy trang tài liệu nào thì đợi thêm một lúc cho trang load
        # (tránh đốt 600s chờ Trang Tải trên một trang lỗi/chặn).
        if (info.get("pf") or 0) == 0:
            log("Chưa thấy trang tài liệu nào, đợi thêm 30s cho trang load...")
            page.wait_for_timeout(30000)
            info = probe_doc(page, site)
            log(f"Sau khi đợi thêm: trang đã render .pf: {info.get('pf')}")
            if (info.get("pf") or 0) == 0:
                log("Không tìm thấy trang tài liệu — có thể trang đổi cấu trúc hoặc vẫn bị chặn.")
                page.screenshot(path=args.output + ".debug.png")
                browser.close()
                return 3

        if args.fresh or (site == "studocu" and info.get("gated")):
            log("Tài liệu bị khóa/mờ -> xóa cookie + storage rồi tải lại (mẹo mở khóa của extension)...")
            context.clear_cookies()
            try:
                page.evaluate("localStorage.clear(); sessionStorage.clear();")
            except Exception:
                pass
            page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)
            info = probe_doc(page, site)
            log(f"Sau khi tải lại: tổng trang {info.get('total')} | bị mờ/khóa: {info.get('gated')}")

        if basic_mode:
            # Basic mode (no inject engine): scroll the whole page to
            # lazy-load images, then print the original page directly.
            # Only publicly visible pages are captured; blurred/locked
            # pages stay blurred in the PDF.
            log("Chế độ cơ bản: cuộn trang để load ảnh, rồi in trực tiếp...")
            t0 = time.time()
            for _ in range(40):
                h1 = page.evaluate("document.body.scrollHeight")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(800)
                h2 = page.evaluate("document.body.scrollHeight")
                if h2 == h1:
                    break
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(3000)
            info = probe_doc(page, site)
            log(f"Sau khi cuộn: trang đã render .pf: {info.get('pf')}")
            viewer_ready = (info.get("pf") or 0) > 0
            if not viewer_ready:
                log("Không tìm thấy trang tài liệu ở chế độ cơ bản.")
                page.screenshot(path=args.output + ".debug.png")
                browser.close()
                return 3
            log(f"Trang đã load xong sau {time.time() - t0:.1f}s")
            stats = {}
        else:
            # Vô hiệu alert() (inject dùng alert khi lỗi) + đặt cờ như popup extension đặt.
            page.evaluate(
                """(cfg) => {
                    window.alert = (m) => console.log('[bamidoc-alert]', m);
                    window.__banhmi_lang = cfg.lang;
                    window.__banhmi_autoprint = false;
                    window.__banhmi_logo_url = '';
                    window.__banhmi_locket_logo = '';
                    window.__banhmi_locket_qr = '';
                }""",
                {"lang": args.lang},
            )
            t0 = time.time()
            page.evaluate(inject_js)  # chạy y nguyên code inject của extension
            log("Đã inject code quét trang của extension, đang chờ Trang Tải...")

            last_toast = ""
            deadline = time.time() + args.timeout
            viewer_ready = False
            while time.time() < deadline:
                try:
                    toast = page.evaluate(
                        """() => { const t = document.querySelector('#banhmi-scan-toast-text');
                                   return t ? t.textContent.trim() : ''; }"""
                    )
                    if toast and toast != last_toast:
                        log(f"  ... {toast}")
                        last_toast = toast
                    if page.evaluate("!!document.getElementById('clean-viewer-container')"):
                        viewer_ready = True
                        break
                except Exception:
                    pass
                page.wait_for_timeout(2000)
            if not viewer_ready:
                log("HẾT THỜI GIAN chờ Trang Tải — trang có thể đổi cấu trúc, hoặc mạng chậm.")
                page.screenshot(path=args.output + ".debug.png")
                log(f"Đã chụp màn hình debug: {args.output}.debug.png")
                browser.close()
                return 3
            log(f"Trang Tải xong sau {time.time() - t0:.1f}s")
            page.wait_for_timeout(1500)

            stats = viewer_stats(page) or {}
        if stats:
            log(f"Trang Tải: {stats.get('pages') or stats.get('slides')} trang/slide | "
                f"{stats.get('imgs')} ảnh (Base64: {stats.get('dataImgs')} ảnh, ~{stats.get('dataMB')} MB) | "
                f"trang mờ: {stats.get('gatedNotes')}")
            if stats.get("gatedNotes"):
                log("Lưu ý: vẫn còn trang bị mờ — PDF sẽ giữ nguyên các trang mờ đó.")
        else:
            log("Chế độ cơ bản: in trực tiếp trang gốc (trang mờ — nếu có — giữ nguyên trong PDF).")

        # In trực tiếp ra PDF, KHÔNG qua hộp thoại Print Preview.
        t0 = time.time()
        page.pdf(path=args.output, prefer_css_page_size=True, print_background=True,
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        pdf_seconds = time.time() - t0
        size_mb = os.path.getsize(args.output) / 1024 / 1024
        pages = pdf_info(args.output)
        log(f"Đã ghi PDF: {args.output} | {size_mb:.1f} MB | {pages} trang | in mất {pdf_seconds:.1f}s")
        browser.close()

    total = time.time() - t_start
    summary = {"site": site, "url": args.url, "output": os.path.abspath(args.output),
               "size_mb": round(size_mb, 2), "pdf_pages": pages,
               "viewer": stats, "mode": "basic" if basic_mode else "engine",
               "print_seconds": round(pdf_seconds, 1),
               "total_seconds": round(total, 1), "no_embed": args.no_embed}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
