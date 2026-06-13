#!/usr/bin/env python3
"""Post-process BlackRock weekly-commentary local item pages.

This runs AFTER the preserve / validate steps in the workflow, so its changes
apply to the CURRENTLY published article (not only future ones) and survive the
"restore live copy when unchanged" preserve behaviour.

Two fixes, both scoped to site/item/blackrock_weekly_commentary/* ONLY:

1. Inject a small reader-friendly stylesheet so the page renders as a centered,
   readable column and charts are capped to the column width. Fixes oversized,
   unharmonious images when the page is opened directly.

2. Rehost remote BlackRock chart images onto our own Pages site (download +
   rewrite the <img> src to a local file) and wrap lone images in <figure> so
   they load reliably and reading apps such as Readwise Reader can fetch them
   (BlackRock-hosted images are not reliably fetched by third-party readers).
   - Failures are non-fatal: the original remote URL is kept.
   - Self-heal: if a page already points at a local img-NNN file (e.g. it was
     restored from live Pages by the preserve step) but the file is missing
     from the build, it is re-downloaded from the live Pages URL.

No other feed and no feed XML is touched, so the validator rule that BlackRock
must not embed content:encoded stays satisfied.

Usage: python fix_blackrock_pages.py <site_dir> [base_url]
"""
import html as _html
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

FEED_NAME = "blackrock_weekly_commentary"
STYLE_MARKER = "data-blackrock-reader"
STYLE_BLOCK = (
    '<style data-blackrock-reader="1">\n'
    '  body { max-width: 760px; margin: 0 auto; padding: 28px 18px 64px;'
    ' font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;'
    ' color: #1a1a1a; line-height: 1.7; }\n'
    '  h1 { line-height: 1.25; font-size: 1.7em; margin: 0 0 12px; }\n'
    '  p { margin: 0.7em 0; }\n'
    '  img { max-width: 100%; height: auto; display: block; margin: 18px auto; }\n'
    '  figure { margin: 18px 0; }\n'
    '  figure img { margin: 0 auto; }\n'
    '  table { border-collapse: collapse; width: 100%; font-size: 0.95em; }\n'
    '  th, td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }\n'
    '</style>'
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Referer": "https://www.blackrock.com/",
}

IMG_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"', re.IGNORECASE)
P_IMG_RE = re.compile(r'<p>\s*(<img\b[^>]*?>)\s*</p>', re.IGNORECASE)


def _ext_for(url: str, content_type: str) -> str:
    path = urlparse(url).path
    m = re.search(r"\.(png|jpe?g|gif|webp|svg)(?:$|[?#])", path, re.IGNORECASE)
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
    return ".png"


def inject_style(text: str) -> str:
    if STYLE_MARKER in text:
        return text
    m = re.search(r"</head>", text, re.IGNORECASE)
    if m:
        return text[: m.start()] + "  " + STYLE_BLOCK + "\n" + text[m.start():]
    m = re.search(r"<body[^>]*>", text, re.IGNORECASE)
    if m:
        return text[: m.end()] + "\n" + STYLE_BLOCK + text[m.end():]
    return STYLE_BLOCK + text


def figurify(text: str) -> str:
    # Wrap lone <p><img></p> in <figure> so readability parsers keep the image.
    return P_IMG_RE.sub(lambda m: "<figure>" + m.group(1) + "</figure>", text)


def rehost_remote_images(text: str, out_dir: Path) -> str:
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
            print("[fix_blackrock] rehosted {} -> {}".format(real_url, out_dir / fname))
        except Exception as exc:
            print("[fix_blackrock] keep remote (fetch failed) {}: {}".format(src, exc))
    return text


def recover_local_images(text: str, out_dir: Path, asset_base: str) -> None:
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
            print("[fix_blackrock] recovered missing asset {} -> {}".format(url, local))
        except Exception as exc:
            print("[fix_blackrock] could not recover {}: {}".format(url, exc))


def process_file(path: Path, site_dir: Path, base_url: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text = inject_style(text)
    new_text = figurify(new_text)
    new_text = rehost_remote_images(new_text, path.parent)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        print("[fix_blackrock] updated {}".format(path))
    # Ensure any local img-NNN assets actually exist (heal preserve-restored pages).
    asset_base = ""
    if base_url:
        rel_dir = path.parent.relative_to(site_dir).as_posix()
        asset_base = base_url.rstrip("/") + "/" + rel_dir
    recover_local_images(new_text, path.parent, asset_base)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python fix_blackrock_pages.py <site_dir> [base_url]", file=sys.stderr)
        return 2
    site_dir = Path(sys.argv[1])
    base_url = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else ""
    item_root = site_dir / "item" / FEED_NAME
    if not item_root.exists():
        print("[fix_blackrock] no blackrock item dir at {}; nothing to do".format(item_root))
        return 0
    for path in sorted(item_root.rglob("index.html")):
        process_file(path, site_dir, base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
