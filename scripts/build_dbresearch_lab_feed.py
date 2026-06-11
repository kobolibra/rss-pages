#!/usr/bin/env python3
"""EXPERIMENTAL DB Research builder: PDF -> faithful, web-reproduced HTML pages.

This is a SEPARATE copy of build_dbresearch_feed.py used to research a better
"reproduce the PDF on the web" experience. It writes its own feed
(dbresearch_lab.xml) and its own item directory (item/dbresearch_lab/...), so
the production `dbresearch` feed and pages are never touched.

Two properties this builder guarantees:

1. INCREMENTAL (same model as production): it restores the already-published
   feed + item pages from live Pages, then only fetches and renders entries it
   has not processed yet. Already-localized items are reused as-is. If nothing
   is new, the feed is left untouched.

2. FAITHFUL REPRODUCTION (the experiment): each PDF page is rendered to SVG via
   PyMuPDF with text kept as real <text> (text_as_path=False). The result
   reproduces the original layout, charts, tables and colors exactly, while the
   body text stays selectable / searchable -- i.e. the content is turned into a
   real web page, not a flat screenshot. Each page SVG is isolated in its own
   file and embedded responsively, so vector charts stay sharp at any width.

Scanned/image-only PDFs degrade to the embedded raster inside the SVG (an OCR
text layer is the natural next research step). DB Research PDFs are born-digital,
so text stays real today. See scripts/README_dbresearch_lab.md.
"""
import asyncio
import hashlib
import html
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlparse, urljoin
from xml.dom import minidom
from xml.etree import ElementTree as ET

import feedparser
import fitz  # PyMuPDF
import requests
from playwright.async_api import async_playwright

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SOURCE_FEED_URL = os.environ.get(
    "DBRESEARCH_LAB_SOURCE_FEED_URL",
    "https://rssweball.top/feed/b706ef15-fa4f-45b2-a1fd-4cd8d037e91c.xml",
)
FEED_NAME = os.environ.get("DBRESEARCH_LAB_FEED_NAME", "dbresearch_lab")
OUTPUT_FILE = os.environ.get("DBRESEARCH_LAB_OUTPUT_FILE", f"{FEED_NAME}.xml")
MAX_ITEMS = int(os.environ.get("DBRESEARCH_LAB_MAX_ITEMS", "40"))
MAX_PAGES = int(os.environ.get("DBRESEARCH_LAB_MAX_PAGES", "60"))
REQUEST_TIMEOUT = int(os.environ.get("DBRESEARCH_LAB_TIMEOUT", "60"))
USER_AGENT = os.environ.get(
    "DBRESEARCH_LAB_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
)
HEADERS = {"User-Agent": USER_AGENT}
BROWSER_FALLBACK_ENABLED = os.environ.get("DBRESEARCH_LAB_BROWSER_FALLBACK", "1") != "0"
FORCE_REBUILD = os.environ.get("DBRESEARCH_LAB_FORCE_REBUILD", "0") == "1"

# Bump when the rendering changes so already-published pages are regenerated
# (from the cached PDF, without re-downloading) on the next run.
RENDER_VERSION = 2


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def fetch_bytes(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.content


def slugify(value: str) -> str:
    value = html.unescape(value or "")
    value = unquote(value)
    value = re.sub(r"\.[Pp][Dd][Ff]$", "", value)
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def short_hash(*parts: str) -> str:
    base = "|".join(part or "" for part in parts)
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:10]


def is_pdf_url(url: str) -> bool:
    return (urlparse(url).path or "").lower().endswith(".pdf")


def normalize_space(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def shorten(value: str, max_len: int = 420) -> str:
    value = normalize_space(value)
    if len(value) <= max_len:
        return value
    cut = value[:max_len].rsplit(" ", 1)[0].strip()
    return (cut or value[:max_len]).rstrip(".,;:!?") + "\u2026"


def parse_pub_date(value: str) -> str:
    if not value:
        return format_datetime(datetime.now(timezone.utc))
    try:
        dt = parsedate_to_datetime(value)
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        return format_datetime(dt)
    except Exception:
        return format_datetime(datetime.now(timezone.utc))


def is_junk_line(line: str) -> bool:
    low = normalize_space(line).lower()
    if not low or len(low) <= 2:
        return True
    if re.fullmatch(r"\d{1,4}", low):
        return True
    if low.startswith("page ") or re.fullmatch(r"page\s+\d+", low):
        return True
    if low in {
        "deutsche bank research institute",
        "deutsche bank ag",
        "sensitivity: public",
        "source: deutsche bank research",
    }:
        return True
    if re.fullmatch(r"\d+\|\d+\|\d+", low):
        return True
    return False


def extract_text_paragraphs(raw_text: str) -> list:
    """Light paragraph extraction, only used for the RSS <description> summary."""
    raw_text = (raw_text or "").replace("\r", "\n").replace("\u00ad", "")
    raw_text = re.sub(r"-\n(?=[a-z])", "", raw_text)
    raw_text = re.sub(r"[ \t]+\n", "\n", raw_text)
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text)
    paragraphs = []
    for block in re.split(r"\n\s*\n", raw_text):
        lines = []
        for line in block.splitlines():
            line = normalize_space(line)
            if not line or is_junk_line(line):
                continue
            lines.append(line)
        if not lines:
            continue
        paragraph = normalize_space(" ".join(lines))
        if len(paragraph) >= 20:
            paragraphs.append(paragraph)
    return paragraphs


# --------------------------------------------------------------------------- #
# PDF fetching (direct -> page scrape -> browser capture; mirrors production)
# --------------------------------------------------------------------------- #
def try_fetch_binary_pdf(session: requests.Session, target_url: str, referer: str = "") -> bytes:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    response = session.get(target_url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    ctype = (response.headers.get("content-type") or "").lower()
    if "pdf" in ctype and len(response.content) > 1000:
        return response.content
    raise ValueError(f"unexpected content-type for PDF binary: {ctype or 'unknown'}")


async def _fetch_pdf_via_browser(url: str) -> bytes:
    captured = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        async def handle_response(response):
            try:
                resp_url = response.url or ""
                ctype = (response.headers.get("content-type") or "").lower()
                if ".pdf" not in resp_url.lower() and "application/pdf" not in ctype:
                    return
                body = await response.body()
                if body and len(body) > 1000 and body[:4] == b"%PDF":
                    captured.append(body)
            except Exception:
                return

        page.on("response", handle_response)
        await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
        await page.wait_for_timeout(3000)
        await browser.close()
    if captured:
        return max(captured, key=len)
    raise ValueError("browser fallback could not capture PDF response")


def fetch_pdf_bytes(url: str) -> bytes:
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        return try_fetch_binary_pdf(session, url)
    except Exception:
        pass
    response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    page_html = response.text or ""
    m_pdf = re.search(r"var\s+pdfUrl\s*=\s*'([^']+)'", page_html)
    if m_pdf:
        direct = urljoin(response.url, html.unescape(m_pdf.group(1)))
        try:
            return try_fetch_binary_pdf(session, direct, referer=response.url)
        except Exception:
            pass
    m_canon = re.search(r'<link\s+rel="canonical"\s+href="([^"]+\.pdf[^"]*)"', page_html, re.I)
    if m_canon:
        direct = urljoin(response.url, html.unescape(m_canon.group(1)))
        try:
            return try_fetch_binary_pdf(session, direct, referer=response.url)
        except Exception:
            pass
    if BROWSER_FALLBACK_ENABLED:
        try:
            print(f"INFO: browser PDF capture fallback for {url}")
            return asyncio.run(_fetch_pdf_via_browser(url))
        except Exception as exc:
            print(f"WARN: browser PDF capture failed for {url}: {exc}")
    raise ValueError("could not resolve real PDF binary URL")


# --------------------------------------------------------------------------- #
# Faithful reproduction: each page -> standalone SVG with real selectable text
# --------------------------------------------------------------------------- #
def render_pdf_faithful(pdf_bytes: bytes, out_dir: Path, max_pages: int = MAX_PAGES):
    """Render every page to its own SVG file.

    Returns (pages, plain_paragraphs) where pages is a list of dicts:
        {"name": <file>, "w": <pt>, "h": <pt>, "kind": "svg"|"png"}
    plain_paragraphs only feeds the RSS <description> summary.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    plain = []
    seen = set()
    try:
        total = min(len(doc), max_pages)
        for pi in range(total):
            page = doc.load_page(pi)
            rect = page.rect
            w = float(rect.width) or 612.0
            h = float(rect.height) or 792.0
            try:
                # text_as_path=False keeps glyphs as real, selectable <text>.
                svg = page.get_svg_image(text_as_path=False)
                svg = re.sub(r"^\s*<\?xml.*?\?>\s*", "", svg, flags=re.S)
                svg = re.sub(r"^\s*<!DOCTYPE.*?>\s*", "", svg, flags=re.S)
                name = f"page-{pi + 1:03d}.svg"
                (out_dir / name).write_text(svg, encoding="utf-8")
                pages.append({"name": name, "w": w, "h": h, "kind": "svg"})
            except Exception as exc:
                print(f"WARN: SVG render failed p{pi + 1}: {exc}; rasterizing")
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                    name = f"page-{pi + 1:03d}.png"
                    (out_dir / name).write_bytes(pix.tobytes("png"))
                    pages.append({"name": name, "w": w, "h": h, "kind": "png"})
                except Exception as exc2:
                    print(f"WARN: raster fallback failed p{pi + 1}: {exc2}")
            try:
                for para in extract_text_paragraphs(page.get_text("text") or ""):
                    key = para.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    plain.append(para)
            except Exception:
                pass
    finally:
        doc.close()
    return pages, plain


# --------------------------------------------------------------------------- #
# HTML page
# --------------------------------------------------------------------------- #
PAGE_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #111; background: #f3f4f6; }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { line-height: 1.25; margin: 0 0 12px; }
  .actions { display: flex; gap: 12px; flex-wrap: wrap; margin: 14px 0 22px; }
  .btn { display: inline-block; padding: 9px 14px; border-radius: 8px; text-decoration: none; border: 1px solid #ccc; color: #111; background: #fff; }
  .btn.primary { background: #111; color: #fff; border-color: #111; }
  .doc { display: flex; flex-direction: column; gap: 16px; }
  .pg { position: relative; width: 100%; background: #fff; box-shadow: 0 1px 5px rgba(0,0,0,0.18); border-radius: 4px; overflow: hidden; }
  .pg .pginner { position: absolute; inset: 0; }
  .pgobj { width: 100%; height: 100%; border: 0; display: block; background: #fff; }
  details.rawtext { margin: 22px 0 0; background: #fff; border: 1px solid #e3e3e3; border-radius: 8px; padding: 8px 14px; }
  details.rawtext summary { cursor: pointer; color: #444; font-size: 14px; }
  details.rawtext .text { line-height: 1.7; font-size: 15px; max-width: 820px; }
  details.rawtext .text p { margin: 0.7em 0; }
"""


def _page_block(pg: dict, index: int) -> str:
    w = pg.get("w") or 612.0
    h = pg.get("h") or 792.0
    aspect = max(h / w * 100.0, 1.0)
    name = html.escape(pg["name"])
    if pg.get("kind") == "png":
        inner = f'<img class="pgobj" src="{name}" alt="Page {index}" loading="lazy">'
    else:
        inner = (
            f'<object class="pgobj" type="image/svg+xml" data="{name}" '
            f'aria-label="Page {index}"></object>'
        )
    return (
        f'<div class="pg" data-page="{index}" style="padding-top:{aspect:.3f}%">'
        f'<div class="pginner">{inner}</div></div>'
    )


def build_local_page(title: str, source_link: str, description: str,
                     pages: list, plain: list, pdf_href) -> str:
    open_href = html.escape(pdf_href or source_link)
    if pages:
        doc_html = "\n".join("    " + _page_block(pg, i + 1) for i, pg in enumerate(pages))
    else:
        doc_html = '    <p>Faithful reproduction is currently unavailable for this item.</p>'
    raw_block = ""
    if plain:
        paras = "\n".join(f"        <p>{html.escape(p)}</p>" for p in plain[:200])
        raw_block = "\n".join([
            '    <details class="rawtext">',
            '      <summary>Plain text (extracted, for search / accessibility)</summary>',
            '      <div class="text">',
            paras,
            '      </div>',
            '    </details>',
        ])
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        f'  <meta name="render-version" content="{RENDER_VERSION}">',
        f"  <title>{html.escape(title)}</title>",
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        f"  <style>{PAGE_CSS}</style>",
        "</head>",
        "<body>",
        '  <div class="wrap">',
        f"    <h1>{html.escape(title)}</h1>",
        '    <div class="actions">',
        f'      <a class="btn primary" href="{open_href}" target="_blank" rel="noopener">Open PDF</a>',
        f'      <a class="btn" href="{open_href}" download>Download PDF</a>',
        f'      <a class="btn" href="{html.escape(source_link)}" target="_blank" rel="noopener">Original source</a>',
        '    </div>',
        '    <div class="doc">',
        doc_html,
        '    </div>',
        raw_block,
        '  </div>',
        "</body>",
        "</html>",
    ])


# --------------------------------------------------------------------------- #
# Incremental state (restore live feed + item pages, same model as production)
# --------------------------------------------------------------------------- #
def entry_slug(title: str, link: str, guid: str) -> str:
    path = (urlparse(link).path or "").strip("/")
    leaf = unquote(path.split("/")[-1]) if path else ""
    base = slugify(leaf) if leaf else slugify(title)
    return f"{base}-{short_hash(guid, link, title)}"


def parse_existing_feed(xml_path: Path) -> list:
    root = ET.parse(xml_path).getroot()
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item in channel.findall("item"):
        link = (item.findtext("link") or "").strip()
        slug = None
        parts = [p for p in urlparse(link).path.split("/") if p]
        if "item" in parts:
            idx = parts.index("item")
            if len(parts) > idx + 2 and parts[idx + 1] == FEED_NAME:
                slug = parts[idx + 2]
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": link,
            "guid": (item.findtext("guid") or "").strip(),
            "pub_date": (item.findtext("pubDate") or "").strip(),
            "slug": slug,
        })
    return items


def restore_live_feed(public_base: str, site_dir: Path) -> bool:
    feed_url = f"{public_base.rstrip('/')}/{OUTPUT_FILE}"
    output_path = site_dir / OUTPUT_FILE
    try:
        xml_bytes = fetch_bytes(feed_url, timeout=30)
    except Exception:
        return False
    output_path.write_bytes(xml_bytes)
    try:
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            return True
        local_prefix = public_base.rstrip("/") + f"/item/{FEED_NAME}/"
        for item in channel.findall("item"):
            link = (item.findtext("link") or "").strip()
            if not link.startswith(local_prefix):
                continue
            try:
                item_bytes = fetch_bytes(link, timeout=30)
            except Exception:
                continue
            item_dir = site_dir / urlparse(link).path.lstrip("/")
            item_dir.mkdir(parents=True, exist_ok=True)
            (item_dir / "index.html").write_bytes(item_bytes)
            html_text = item_bytes.decode("utf-8", errors="ignore")
            # include data= so embedded SVG/object assets are restored too
            refs = set(re.findall(r'(?:src|href|data)="([^"]+)"', html_text))
            for ref in refs:
                if not ref or ref.startswith(("http://", "https://", "#", "data:")):
                    continue
                asset_url = urljoin(link, ref)
                try:
                    asset_bytes = fetch_bytes(asset_url, timeout=30)
                except Exception:
                    continue
                asset_path = site_dir / urlparse(asset_url).path.lstrip("/")
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                asset_path.write_bytes(asset_bytes)
    except Exception:
        pass
    return True


def load_existing_items(site_dir: Path, public_base: str) -> list:
    output_path = site_dir / OUTPUT_FILE
    if not output_path.exists():
        restore_live_feed(public_base, site_dir)
    if not output_path.exists():
        return []
    try:
        return parse_existing_feed(output_path)
    except Exception:
        return []


def local_render_version(index_path: Path) -> int:
    try:
        txt = index_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0
    m = re.search(r'name="render-version"\s+content="(\d+)"', txt)
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------- #
# Feed assembly
# --------------------------------------------------------------------------- #
def build_feed(site_dir: Path, public_base: str):
    print(f"Fetching source feed: {SOURCE_FEED_URL}")
    parsed = feedparser.parse(SOURCE_FEED_URL)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise RuntimeError(f"failed to parse source feed: {getattr(parsed, 'bozo_exception', 'unknown')}")

    public_base = public_base.rstrip("/")
    item_root = site_dir / "item" / FEED_NAME
    item_root.mkdir(parents=True, exist_ok=True)
    output_path = site_dir / OUTPUT_FILE

    existing_items = load_existing_items(site_dir, public_base)
    existing_guid_map = {it["guid"]: it for it in existing_items if it.get("guid")}
    existing_slug_map = {it["slug"]: it for it in existing_items if it.get("slug")}

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = (parsed.feed.get("title") or "DB Research") + " (lab)"
    ET.SubElement(channel, "link").text = f"{public_base}/{OUTPUT_FILE}" if public_base else SOURCE_FEED_URL
    ET.SubElement(channel, "description").text = "Experimental DB Research feed: faithful per-page web reproduction with real selectable text"
    ET.SubElement(channel, "language").text = parsed.feed.get("language") or "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "generator").text = "DBResearch lab faithful reproducer"

    output_items = []
    total_count = 0
    processed_count = 0
    pdf_count = 0

    for entry in parsed.entries[:MAX_ITEMS]:
        title = normalize_space(entry.get("title", "Untitled")) or "Untitled"
        link = entry.get("link", "").strip()
        description = normalize_space(entry.get("summary", "") or entry.get("description", ""))
        guid = (entry.get("id") or entry.get("guid") or link or title).strip()
        pub_date = parse_pub_date(entry.get("published", "") or entry.get("updated", ""))

        slug = entry_slug(title, link, guid) if is_pdf_url(link) else None
        existing_item = existing_guid_map.get(guid)
        if not existing_item and slug:
            existing_item = existing_slug_map.get(slug)

        # Decide whether we can reuse the already-published page untouched.
        reuse = False
        if existing_item and not FORCE_REBUILD:
            if is_pdf_url(link) and slug:
                existing_link = (existing_item.get("link") or "").strip()
                local_index = item_root / slug / "index.html"
                if (existing_link.startswith(f"{public_base}/item/{FEED_NAME}/")
                        and local_index.exists()
                        and local_render_version(local_index) >= RENDER_VERSION):
                    reuse = True
                elif existing_link.startswith(f"{public_base}/item/{FEED_NAME}/") and local_index.exists():
                    print(f"INFO: upgrading {link} to render v{RENDER_VERSION} from cached PDF")
                else:
                    print(f"INFO: rebuilding missing local page for {link}")
            else:
                reuse = True

        if reuse:
            output_items.append({
                "title": existing_item.get("title") or title,
                "link": existing_item.get("link") or link,
                "guid": existing_item.get("guid") or guid,
                "pub_date": existing_item.get("pub_date") or pub_date,
                "is_permalink": bool((existing_item.get("link") or "").startswith("http")),
            })
            total_count += 1
            continue

        final_link, final_guid = link, guid
        is_permalink = bool(guid == link and link.startswith("http"))

        if is_pdf_url(link) and slug:
            out_dir = item_root / slug
            out_dir.mkdir(parents=True, exist_ok=True)
            local_url = f"{public_base}/item/{FEED_NAME}/{slug}/" if public_base else link
            cached_pdf = out_dir / "original.pdf"

            pdf_bytes = None
            if cached_pdf.exists() and cached_pdf.stat().st_size > 1000:
                pdf_bytes = cached_pdf.read_bytes()
                print(f"INFO: reusing cached PDF (no download) for {link}")
            else:
                try:
                    pdf_bytes = fetch_pdf_bytes(link)
                    cached_pdf.write_bytes(pdf_bytes)
                except Exception as exc:
                    print(f"WARN: fetch PDF failed for {link}: {exc}")

            pages, plain = [], []
            if pdf_bytes:
                # clear stale page assets from a previous render version
                for old in list(out_dir.glob("page-*.svg")) + list(out_dir.glob("page-*.png")) + list(out_dir.glob("p[0-9]*-*.png")):
                    try:
                        old.unlink()
                    except Exception:
                        pass
                try:
                    pages, plain = render_pdf_faithful(pdf_bytes, out_dir)
                except Exception as exc:
                    print(f"WARN: faithful render failed for {link}: {exc}")

            if not description:
                description = shorten(plain[0] if plain else title)

            (out_dir / "index.html").write_text(
                build_local_page(title, link, description, pages, plain,
                                  "original.pdf" if pdf_bytes else None),
                encoding="utf-8",
            )
            final_link = final_guid = local_url
            is_permalink = True
            pdf_count += 1

        processed_count += 1
        total_count += 1
        output_items.append({
            "title": title,
            "link": final_link,
            "guid": final_guid,
            "pub_date": pub_date,
            "is_permalink": is_permalink,
        })

    for item in output_items:
        rss_item = ET.SubElement(channel, "item")
        ET.SubElement(rss_item, "title").text = item["title"]
        ET.SubElement(rss_item, "link").text = item["link"]
        guid_el = ET.SubElement(rss_item, "guid")
        guid_el.set("isPermaLink", "true" if item["is_permalink"] else "false")
        guid_el.text = item["guid"]
        ET.SubElement(rss_item, "pubDate").text = item["pub_date"]
        desc = ET.SubElement(rss_item, "description")

    if processed_count == 0 and output_path.exists():
        print(f"no new {FEED_NAME} items; kept existing feed and pages")
        return

    xml_bytes = minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    output_path.write_bytes(xml_bytes)
    print(f"Saved {output_path} (items={total_count}, processed={processed_count}, pdf_localized={pdf_count})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_dbresearch_lab_feed.py <site_dir> <public_base>")
        sys.exit(1)
    site_dir = Path(sys.argv[1])
    site_dir.mkdir(parents=True, exist_ok=True)
    build_feed(site_dir, sys.argv[2])
