#!/usr/bin/env python3
"""Post-process generated local item pages so every article that is rewritten
to a local page shows a clickable link to the ORIGINAL article at the very top
of the page body.

This runs over the built `site/item/<feed>/<slug>/index.html` files after all
feed builders have produced their pages. It is idempotent and conservative:

- The original URL is read from the page's <link rel="canonical" href="...">.
- Pages that already link to that source URL in the body, or that already carry
  an explicit source marker, are left untouched (so feeds like citadel,
  dbresearch(_lab), blackstone, yardeni, barclays and the rewrite-family that
  already include the link are never double-tagged).
- Pages whose canonical points back at our own Pages site are skipped.

Usage: python add_source_links.py <site_dir> [base_url]
"""
import html
import re
import sys
from pathlib import Path

CANONICAL_RE = re.compile(r'<link[^>]*rel="canonical"[^>]*href="([^"]+)"', re.IGNORECASE)
H1_CLOSE_RE = re.compile(r'</h1>', re.IGNORECASE)


def process_file(path: Path, base_url: str) -> str:
    text = path.read_text(encoding="utf-8")

    m = CANONICAL_RE.search(text)
    if not m:
        return "skip-no-canonical"
    source_url = m.group(1).strip()
    if not source_url:
        return "skip-empty-canonical"

    # Never point readers back at our own GitHub Pages site.
    if base_url and source_url.startswith(base_url):
        return "skip-local-canonical"

    # Determine the body region (avoid matching the <head> canonical link).
    lower = text.lower()
    body_start = lower.find("<body")
    body = text[body_start:] if body_start != -1 else text

    # Already linked to the source, or an explicit source marker is present.
    if ('href="' + source_url + '"') in body:
        return "skip-already-linked"
    if 'class="source"' in body or '原文链接' in body or 'rel="noopener"' in body:
        return "skip-has-marker"

    h1 = H1_CLOSE_RE.search(text)
    if not h1:
        return "skip-no-h1"

    esc = html.escape(source_url)
    snippet = (
        '\n    <p class="source">原文链接：'
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
