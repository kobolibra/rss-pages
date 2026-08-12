#!/usr/bin/env python3
"""Post-process GSAM Insights local item pages.

GSAM pages are generated from `am.gs.com` JSON data and embed remote CDN
images (`am.gs.com/cms-assets/...`).  While the CDN is currently reachable
from third-party readers, its access policy may change at any time.

This script rehosts those images onto our own GitHub Pages domain so reading
apps such as Readwise Reader can always fetch them reliably.  It also performs
self-healing: if a local `img-NNN` file is missing (e.g. after a preserve
restore), it is re-downloaded from the live Pages URL.

Usage: python fix_gsam_pages.py <site_dir> [base_url]
"""
import html as _html
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

FEED_NAME = "gsam"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Referer": "https://am.gs.com/",
}

IMG_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"', re.IGNORECASE)


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


def rehost_remote_images(text: str, out_dir: Path) -> str:
    """Download every remote image and rewrite its src to a local filename.

    Returns the modified HTML.  Failures are non-fatal: the original remote
    URL is kept so the page is never left broken.
    """
    remote = []
    for src in IMG_SRC_RE.findall(text):
        s = src.strip()
        if s.lower().startswith(("http://", "https://")) and s not in remote:
            remote.append(s)
    n = 0
    for src in remote:
        n += 1
        try:
            real_url = _html.unescape(src)
            resp = requests.get(real_url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            if len(resp.content) < 200:
                raise ValueError("empty / too-small image")
            fname = "img-{:03d}{}".format(n, _ext_for(real_url, resp.headers.get("content-type", "")))
            (out_dir / fname).write_bytes(resp.content)
            text = text.replace('src="' + src + '"', 'src="' + fname + '"')
            print("[fix_gsam] rehosted {} -> {}".format(real_url, out_dir / fname))
        except Exception as exc:
            print("[fix_gsam] keep remote (fetch failed) {}: {}".format(src, exc))
    return text


def recover_local_images(text: str, out_dir: Path, asset_base: str) -> None:
    """Self-heal: re-download missing local img-NNN files from the live Pages URL.

    This handles the case where a page was restored from a live Pages copy
    (preserve step) but its local image assets were not carried over.
    """
    if not asset_base:
        return
    for src in IMG_SRC_RE.findall(text):
        s = src.strip()
        if not s or s.lower().startswith(("http://", "https://", "data:", "#")):
            continue
        local = out_dir / s
        if local.exists():
            continue
        url = asset_base.rstrip("/") + "/" + s.lstrip("/")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(resp.content)
            print("[fix_gsam] recovered missing asset {} -> {}".format(url, local))
        except Exception as exc:
            print("[fix_gsam] could not recover {}: {}".format(url, exc))


def process_file(path: Path, site_dir: Path, base_url: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text = rehost_remote_images(text, path.parent)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        print("[fix_gsam] updated {}".format(path))
    # Self-heal local img-NNN assets
    asset_base = ""
    if base_url:
        rel_dir = path.parent.relative_to(site_dir).as_posix()
        asset_base = base_url.rstrip("/") + "/" + rel_dir
    recover_local_images(new_text, path.parent, asset_base)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python fix_gsam_pages.py <site_dir> [base_url]", file=sys.stderr)
        return 2
    site_dir = Path(sys.argv[1])
    base_url = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else ""
    item_root = site_dir / "item" / FEED_NAME
    if not item_root.exists():
        print("[fix_gsam] no gsam item dir at {}; nothing to do".format(item_root))
        return 0
    for path in sorted(item_root.rglob("index.html")):
        process_file(path, site_dir, base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())