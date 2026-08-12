#!/usr/bin/env python3
"""Post-process Citadel Market Insights local item pages.

Citadel's site (citadelsecurities.com) is behind Cloudflare JS Challenge, so
direct HTTP requests for images return 403.  This script uses Playwright (a
real Chromium browser) to download images — the browser executes JavaScript
and passes the Cloudflare challenge automatically.

Strategy (per article page):
1. Launch headless Chromium and navigate to the article page.
2. Wait for Cloudflare to clear (the page becomes an actual article, not
   "Just a moment...").
3. For each remote image in the HTML, fetch it via ``page.evaluate()``
   inside the browser context that already has Cloudflare clearance.
4. Save the image bytes locally and rewrite the ``src`` attribute.

Images are cached by URL hash — re-running on the same page is a no-op.

Usage: python fix_citadel_pages.py <site_dir> [base_url]
"""
import hashlib
import html as _html
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

FEED_NAME = "citadel_market_insights"

IMG_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"', re.IGNORECASE)

# Cloudflare JS Challenge markers
CF_MARKERS = ("Just a moment...", "Checking your browser", "cf-challenge")


def _ext_for(url: str, content_type: str) -> str:
    """Derive a file extension from a URL path or Content-Type header."""
    path = urlparse(url).path
    m = re.search(r"\.(png|jpe?g|gif|webp|svg|avif)(?:$|[?#])", path, re.IGNORECASE)
    if m:
        ext = m.group(1).lower()
        return ".jpg" if ext == "jpeg" else "." + ext
    ct = (content_type or "").lower()
    if "svg" in ct:
        return ".svg"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "gif" in ct:
        return ".gif"
    if "webp" in ct:
        return ".webp"
    if "avif" in ct:
        return ".avif"
    return ".png"


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _looks_cloudflare(text: str) -> bool:
    head = text[:3000] if text else ""
    return any(m in head for m in CF_MARKERS)


def _download_images_via_browser(
    page_url: str, image_urls: list[str], out_dir: Path
) -> dict[str, str]:
    """Download images through a real browser that can pass Cloudflare.

    Returns a dict mapping remote URL -> local filename.
    """
    mapping: dict[str, str] = {}
    if not image_urls:
        return mapping

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()

        try:
            # Step 1: navigate to the article page to establish a
            # Cloudflare-cleared browser session.
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            # Wait for Cloudflare JS Challenge to resolve (max 10 seconds).
            for _ in range(20):
                body = page.content()
                if not _looks_cloudflare(body):
                    break
                time.sleep(0.5)
            else:
                print(f"[fix_citadel] Cloudflare timeout for {page_url}; some images may fail")

            # Step 2: fetch each image via JavaScript inside the browser
            # context.  The browser already has Cloudflare clearance, so
            # fetch() inside the same page context will succeed.
            for remote_url in image_urls:
                real_url = _html.unescape(remote_url)
                fname = "img-{}{}".format(_url_hash(real_url), _ext_for(real_url, ""))
                local_path = out_dir / fname

                # Cache hit: skip if already downloaded.
                if local_path.exists() and local_path.stat().st_size >= 200:
                    mapping[remote_url] = fname
                    print(f"[fix_citadel] cached (skip) {real_url}")
                    continue

                try:
                    result = page.evaluate(
                        """async (url) => {
                            const resp = await fetch(url);
                            if (!resp.ok) return {error: 'HTTP ' + resp.status};
                            const buf = await resp.arrayBuffer();
                            const bytes = Array.from(new Uint8Array(buf));
                            return {ok: true, bytes: bytes, mime: resp.headers.get('content-type') || ''};
                        }""",
                        real_url,
                    )
                    if result.get("error"):
                        raise RuntimeError(result["error"])
                    data = bytes(result["bytes"])
                    if len(data) < 200:
                        raise ValueError("empty / too-small image")
                    # Recompute extension from actual MIME type.
                    fname = "img-{}{}".format(_url_hash(real_url), _ext_for(real_url, result.get("mime", "")))
                    local_path = out_dir / fname
                    local_path.write_bytes(data)
                    mapping[remote_url] = fname
                    print(f"[fix_citadel] rehosted {real_url} -> {local_path}")
                except Exception as exc:
                    print(f"[fix_citadel] keep remote (fetch failed) {remote_url}: {exc}")
                    # Still try to map it so images that worked aren't lost.
                    if local_path.exists() and local_path.stat().st_size >= 200:
                        mapping[remote_url] = fname
        finally:
            ctx.close()
            browser.close()

    return mapping


def process_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    # Collect remote image URLs.
    remote = []
    for src in IMG_SRC_RE.findall(text):
        s = src.strip()
        if s.lower().startswith(("http://", "https://")) and s not in remote:
            remote.append(s)

    if not remote:
        return

    # Derive the article page URL from the local path.
    # path = .../site/item/citadel_market_insights/<slug>/index.html
    slug = path.parent.name
    # The article is on citadelsecurities.com; we need its URL.
    # Extract it from the source link in the page.
    source_link = ""
    m = re.search(r'<a\b[^>]*\bhref="(https?://www\.citadelsecurities\.com/[^"]+)"[^>]*>', text)
    if m:
        source_link = m.group(1)

    if not source_link:
        # Fallback: try to find any citadelsecurities.com link.
        m = re.search(r'https?://www\.citadelsecurities\.com/[^"\s<>]+', text)
        if m:
            source_link = m.group(0).rstrip('"')
        else:
            print(f"[fix_citadel] no source link found for {path}; skipping browser step")
            return

    print(f"[fix_citadel] processing {slug} via {source_link}")
    mapping = _download_images_via_browser(source_link, remote, path.parent)

    # Rewrite HTML.
    new_text = text
    for remote_url, local_name in mapping.items():
        new_text = new_text.replace('src="' + remote_url + '"', 'src="' + local_name + '"')

    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        print(f"[fix_citadel] updated {path}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python fix_citadel_pages.py <site_dir> [base_url]", file=sys.stderr)
        return 2
    site_dir = Path(sys.argv[1])
    item_root = site_dir / "item" / FEED_NAME
    if not item_root.exists():
        print(f"[fix_citadel] no citadel item dir at {item_root}; nothing to do")
        return 0
    for path in sorted(item_root.rglob("index.html")):
        process_file(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())