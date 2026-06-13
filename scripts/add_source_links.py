#!/usr/bin/env python3
"""Post-process generated local item pages so every article that is rewritten
to a local page shows a clickable link to the ORIGINAL article, in TEXT form,
at the very top of the page body (right under the <h1>).

Runs over the built site/item/<feed>/<slug>/index.html files after all feed
builders have produced their pages. Idempotent and conservative:

- The original URL is taken from <link rel="canonical"> when present; otherwise
  it falls back to the first external "open original / source" button in the
  body (an <a> carrying rel="noopener" that points off our Pages site). This
  covers feeds like dbresearch / dbresearch_lab / blackstone whose pages expose
  the source only as an action button rather than a text link.
- Pages that already show an explicit text-form source link (they contain
  '\u539f\u6587\u94fe\u63a5' or class="source") are left untouched, so feeds that already
  embed the link (the rewrite family, etc.) are never double-tagged.
- Links back to our own Pages site are never used as the "original" URL.

Usage: python add_source_links.py <site_dir> [base_url]
"""
import html
import re
import sys
from pathlib import Path

CANONICAL_RE = re.compile(r'<link[^>]*rel="canonical"[^>]*href="([^"]+)"', re.IGNORECASE)
H1_CLOSE_RE = re.compile(r'</h1>', re.IGNORECASE)
ANCHOR_RE = re.compile(r'<a\b[^>]*>', re.IGNORECASE)
HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


def _resolve_source_url(text: str, body: str, base_url: str) -> str:
    """Best-effort original-article URL for a local item page."""
    # Prefer an explicit canonical that is not our own Pages site.
    m = CANONICAL_RE.search(text)
    if m:
        canonical = m.group(1).strip()
        if canonical and not (base_url and canonical.startswith(base_url)):
            return canonical
    # Fall back to the first external action button (rel="noopener") in the body,
    # e.g. "Open original source" / "Source" / "View on ...". This is how the
    # button-only feeds (dbresearch, dbresearch_lab, blackstone) expose the link.
    for tag in ANCHOR_RE.findall(body):
        if 'rel="noopener"' not in tag.lower():
            continue
        hm = HREF_RE.search(tag)
        if not hm:
            continue
        href = hm.group(1).strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        if base_url and href.startswith(base_url):
            continue
        return href
    return ""


def process_file(path: Path, base_url: str) -> str:
    text = path.read_text(encoding="utf-8")

    # Determine the body region (avoid matching the <head> canonical link).
    lower = text.lower()
    body_start = lower.find("<body")
    body = text[body_start:] if body_start != -1 else text

    # Already shows an explicit text-form source link; never double-tag.
    if 'class="source"' in body or '\u539f\u6587\u94fe\u63a5' in body:
        return "skip-has-marker"

    source_url = _resolve_source_url(text, body, base_url)
    if not source_url:
        return "skip-no-source"

    h1 = H1_CLOSE_RE.search(text)
    if not h1:
        return "skip-no-h1"

    esc = html.escape(source_url)
    snippet = (
        '\n    <p class="source">\u539f\u6587\u94fe\u63a5\uff1a'
        '<a href="' + esc + '" target="_blank" rel="noopener">' + esc + '</a></p>'
    )
    new_text = text[: h1.end()] + snippet + text[h1.end():]
    path.write_text(new_text, encoding="utf-8")
    return "patched"


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python add_source_links.py <site_dir> [base_url]", file=sys.stderr)
        return 2

    site_dir = Path(sys.argv[1])
    base_url = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else ""

    item_root = site_dir / "item"
    if not item_root.exists():
        print(f"[add_source_links] no item dir at {item_root}; nothing to do")
        return 0

    counts: dict[str, int] = {}
    for path in sorted(item_root.rglob("index.html")):
        result = process_file(path, base_url)
        counts[result] = counts.get(result, 0) + 1
        if result == "patched":
            print(f"[add_source_links] patched {path}")

    print(f"[add_source_links] summary: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
