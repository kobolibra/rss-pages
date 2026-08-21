#!/usr/bin/env python3
"""Post-process Citadel Market Insights local item pages.

Citadel's site (citadelsecurities.com) is behind Cloudflare JS Challenge, so
direct HTTP requests for images return 403. This script opens article pages in
Chromium, waits for Cloudflare clearance, then fetches images in that same
browser context and rewrites the local HTML to use the saved assets.

A single Chromium context is reused for every article in a run. This preserves
Cloudflare clearance cookies and avoids repeatedly challenging a fresh browser
session for each article.

Usage: python fix_citadel_pages.py <site_dir> [base_url]
"""
import hashlib
import html as _html
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

FEED_NAME = "citadel_market_insights"
IMG_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"', re.IGNORECASE)
CF_MARKERS = ("Just a moment...", "Checking your browser", "cf-challenge")
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def _ext_for(url: str, content_type: str) -> str:
    """Derive a file extension from a URL path or Content-Type header."""
    path = urlparse(url).path
    match = re.search(r"\.(png|jpe?g|gif|webp|svg|avif)(?:$|[?#])", path, re.IGNORECASE)
    if match:
        ext = match.group(1).lower()
        return ".jpg" if ext == "jpeg" else "." + ext
    content_type = (content_type or "").lower()
    if "svg" in content_type:
        return ".svg"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    if "avif" in content_type:
        return ".avif"
    return ".png"


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _looks_cloudflare(text: str) -> bool:
    head = text[:3000] if text else ""
    return any(marker in head for marker in CF_MARKERS)


def _wait_for_cloudflare(page, page_url: str) -> bool:
    """Wait up to 30 seconds for a Citadel page to leave the challenge screen."""
    for _ in range(60):
        try:
            if not _looks_cloudflare(page.content()):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    print(f"[fix_citadel] Cloudflare timeout for {page_url}; some images may fail")
    return False


def _download_images_via_browser(page, page_url: str, image_urls: list[str], out_dir: Path) -> dict[str, str]:
    """Download images through a Cloudflare-cleared, shared Chromium page."""
    mapping: dict[str, str] = {}
    if not image_urls:
        return mapping

    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        print(f"[fix_citadel] article navigation failed for {page_url}: {exc}")
    _wait_for_cloudflare(page, page_url)

    for remote_url in image_urls:
        real_url = _html.unescape(remote_url)
        fallback_name = "img-{}{}".format(_url_hash(real_url), _ext_for(real_url, ""))
        fallback_path = out_dir / fallback_name

        if fallback_path.exists() and fallback_path.stat().st_size >= 200:
            mapping[remote_url] = fallback_name
            print(f"[fix_citadel] cached (skip) {real_url}")
            continue

        try:
            result = page.evaluate(
                """async (url) => {
                    const response = await fetch(url, {credentials: 'include'});
                    if (!response.ok) return {error: 'HTTP ' + response.status};
                    const buffer = await response.arrayBuffer();
                    return {
                        ok: true,
                        bytes: Array.from(new Uint8Array(buffer)),
                        mime: response.headers.get('content-type') || ''
                    };
                }""",
                real_url,
            )
            if result.get("error"):
                raise RuntimeError(result["error"])
            data = bytes(result["bytes"])
            if len(data) < 200:
                raise ValueError("empty / too-small image")
            local_name = "img-{}{}".format(_url_hash(real_url), _ext_for(real_url, result.get("mime", "")))
            (out_dir / local_name).write_bytes(data)
            mapping[remote_url] = local_name
            print(f"[fix_citadel] rehosted {real_url} -> {out_dir / local_name}")
        except Exception as exc:
            print(f"[fix_citadel] keep remote (fetch failed) {remote_url}: {exc}")
            if fallback_path.exists() and fallback_path.stat().st_size >= 200:
                mapping[remote_url] = fallback_name

    return mapping


def process_file(path: Path, page) -> None:
    text = path.read_text(encoding="utf-8")
    remote = []
    for src in IMG_SRC_RE.findall(text):
        source = src.strip()
        if source.lower().startswith(("http://", "https://")) and source not in remote:
            remote.append(source)
    if not remote:
        return

    source_link = ""
    match = re.search(r'<a\b[^>]*?\bhref="(https?://www\.citadelsecurities\.com/[^"]+)"[^>]*>', text)
    if match:
        source_link = match.group(1)
    else:
        match = re.search(r'https?://www\.citadelsecurities\.com/[^"\s<>]+', text)
        if match:
            source_link = match.group(0).rstrip('"')

    if not source_link:
        print(f"[fix_citadel] no source link found for {path}; skipping browser step")
        return

    print(f"[fix_citadel] processing {path.parent.name} via {source_link}")
    mapping = _download_images_via_browser(page, source_link, remote, path.parent)

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

    # GitHub Actions enables this through CITADEL_HEADFUL=1 and xvfb-run. A
    # full browser passes Citadel's Cloudflare challenge where headless fetches
    # continue to receive 403 even with a Chrome User-Agent and Referer.
    headful = os.getenv("CITADEL_HEADFUL", "").strip().lower() in {"1", "true", "yes"}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headful)
        context = browser.new_context(user_agent=BROWSER_USER_AGENT)
        page = context.new_page()
        try:
            for path in sorted(item_root.rglob("index.html")):
                process_file(path, page)
        finally:
            context.close()
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
