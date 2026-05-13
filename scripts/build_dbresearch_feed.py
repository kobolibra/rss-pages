#!/usr/bin/env python3
import asyncio
import hashlib
import html
import io
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlparse, urljoin
from xml.dom import minidom
from xml.etree import ElementTree as ET

import feedparser
import fitz
import requests
from playwright.async_api import async_playwright
from pypdf import PdfReader

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ET.register_namespace("content", CONTENT_NS)

SOURCE_FEED_URL = os.environ.get(
    "DBRESEARCH_SOURCE_FEED_URL",
    "https://rssweball.top/feed/b706ef15-fa4f-45b2-a1fd-4cd8d037e91c.xml",
)
FEED_NAME = os.environ.get("DBRESEARCH_FEED_NAME", "dbresearch")
OUTPUT_FILE = os.environ.get("DBRESEARCH_OUTPUT_FILE", f"{FEED_NAME}.xml")
LEGACY_FEED_NAMES = [name.strip() for name in os.environ.get("DBRESEARCH_LEGACY_FEED_NAMES", "dbresearch_global_search").split(",") if name.strip()]
MAX_ITEMS = int(os.environ.get("DBRESEARCH_MAX_ITEMS", "40"))
REQUEST_TIMEOUT = int(os.environ.get("DBRESEARCH_TIMEOUT", "60"))
USER_AGENT = os.environ.get(
    "DBRESEARCH_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
)

HEADERS = {
    "User-Agent": USER_AGENT,
}

BROWSER_FALLBACK_ENABLED = os.environ.get("DBRESEARCH_BROWSER_FALLBACK", "1") != "0"


def qname_local(tag: str) -> str:
    return f"{{{CONTENT_NS}}}{tag}"


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
    path = (urlparse(url).path or "").lower()
    return path.endswith(".pdf")


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
    return (cut or value[:max_len]).rstrip(".,;:!?") + "…"


def parse_pub_date(value: str) -> str:
    if not value:
        return format_datetime(datetime.now(timezone.utc))
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return format_datetime(dt)
    except Exception:
        return format_datetime(datetime.now(timezone.utc))


def extract_text_paragraphs(raw_text: str) -> list[str]:
    raw_text = raw_text or ""
    raw_text = raw_text.replace("\r", "\n")
    raw_text = raw_text.replace("\u00ad", "")
    raw_text = re.sub(r"-\n(?=[a-z])", "", raw_text)
    raw_text = re.sub(r"[ \t]+\n", "\n", raw_text)
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text)

    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", raw_text):
        lines = []
        for line in block.splitlines():
            line = normalize_space(line)
            if not line:
                continue
            if re.fullmatch(r"\d{1,4}", line):
                continue
            lines.append(line)
        if not lines:
            continue
        paragraph = normalize_space(" ".join(lines))
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def is_junk_pdf_line(line: str) -> bool:
    low = normalize_space(line).lower()
    if not low:
        return True
    if re.fullmatch(r"\d{1,4}", low):
        return True
    if low in {
        "deutsche bank research institute",
        "deutsche bank ag",
        "sensitivity: public",
        "source: deutsche bank research",
    }:
        return True
    if low.startswith("page "):
        return True
    if low.startswith("authors") and len(low) < 80:
        return True
    if low.startswith("important research disclosures"):
        return True
    if low.startswith("appendix "):
        return True
    if any(x in low for x in [
        "deep blue", "dark blue", "bright blue", "vibrant cyan", "autumn green",
        "lucent yellow", "first level, 14 pt", "standard text formats"
    ]):
        return True
    if re.fullmatch(r"\d+\|\d+\|\d+", low):
        return True
    if len(low) <= 2:
        return True
    return False


def extract_pdf_paragraphs(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    paragraphs: list[str] = []
    current: list[str] = []

    def flush():
        nonlocal current
        if current:
            paragraph = normalize_space(" ".join(current))
            if paragraph:
                paragraphs.append(paragraph)
            current = []

    for page in reader.pages:
        text = page.extract_text() or ""
        if not text:
            continue
        for raw_line in text.replace("\r", "\n").splitlines():
            line = normalize_space(raw_line)
            if not line:
                flush()
                continue
            if is_junk_pdf_line(line):
                flush()
                continue
            if len(line) < 80 and (
                line.endswith(":")
                or re.match(r"^(\d{2}\.|\d+\.|[A-Z][a-z]+:)", line)
                or line.lower().startswith("key highlights")
            ):
                flush()
                current.append(line)
                flush()
                continue
            current.append(line)
        flush()

    return paragraphs


def extract_pdf_paragraphs_via_fitz(pdf_bytes: bytes) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        text = "\n\n".join((page.get_text("text") or "") for page in doc)
    finally:
        doc.close()
    return extract_text_paragraphs(text)


def clean_article_paragraphs(paragraphs: list[str], title: str, description: str) -> list[str]:
    cleaned: list[str] = []
    title_n = normalize_space(title)
    desc_n = normalize_space(description)
    stop_markers = [
        "important research disclosures",
        "the above information has been obtained",
        "all opinions and claims are based upon data",
        "this material has been prepared by the deutsche bank research institute",
        "copyright ©",
    ]
    skip_prefixes = [
        "title:",
        "url source:",
        "markdown content:",
        "don't show this message anymore",
        "ihre sitzung",
        "mit neuer sitzung fortfahren",
        "bringing the world to europe",
    ]
    junk_markers = [
        "deep blue",
        "dark blue",
        "bright blue",
        "vibrant cyan",
        "autumn green",
        "lucent yellow",
        "first level, 14 pt",
        "second level, 14 pt",
        "sixth level, footnote",
        "seventh level, footnote",
        "standard text formats",
    ]

    for idx, para in enumerate(paragraphs):
        p = normalize_space(para)
        if not p:
            continue
        low = p.lower()

        if any(marker in low for marker in stop_markers):
            if idx >= max(3, len(cleaned)):
                break
            continue
        if any(low.startswith(prefix) for prefix in skip_prefixes):
            continue
        if any(marker in low for marker in junk_markers):
            continue
        if p.startswith("## [") or p.startswith("# "):
            continue
        if "http://" in p or "https://" in p:
            continue
        if title_n and p == title_n:
            continue
        if title_n and title_n.lower() in low and len(p) < max(120, len(title_n) + 40):
            continue
        if desc_n and p == desc_n:
            continue
        if low == "deutsche bank research institute":
            continue
        if low.endswith("research analyst") and len(p) < 80:
            continue
        if low.startswith("analysts") and len(p) < 120:
            continue
        if re.fullmatch(r"\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4}\s+.*", p) and len(p) < 80:
            continue
        if low.startswith("über diesen link verlassen sie"):
            continue
        if low.startswith("die seite xxxx wird geöffnet"):
            continue
        if low.startswith("1. [null]"):
            continue
        if re.fullmatch(r"[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}.*", p) and len(p) < 60:
            continue
        if re.fullmatch(r"page\s+\d+", low):
            continue
        if re.fullmatch(r"deutsche bank [^.]{0,60} page\s+\d+", low):
            continue
        if low.startswith("source:") or low == "source" or low == "sources":
            continue
        if low.startswith("figure "):
            continue
        if low.startswith("deutsche bank research institute ") and " – " in p:
            continue
        if " | " in p and len(re.findall(r"\d+\|\d+\|\d+", p)) >= 3:
            continue
        if len(re.findall(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", p)) >= 5 and len(p) < 600:
            continue
        if re.fullmatch(r"\d+\s+review of .*", low):
            continue
        if len(p) < 20:
            continue
        cleaned.append(p)

    deduped: list[str] = []
    seen = set()
    for p in cleaned:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)

    return deduped


def extract_best_pdf_paragraphs(pdf_bytes: bytes, title: str, description: str) -> list[str]:
    candidates: list[list[str]] = []
    try:
        candidates.append(clean_article_paragraphs(extract_pdf_paragraphs(pdf_bytes), title, description))
    except Exception:
        pass
    try:
        candidates.append(clean_article_paragraphs(extract_pdf_paragraphs_via_fitz(pdf_bytes), title, description))
    except Exception:
        pass
    candidates = [c for c in candidates if c]
    if not candidates:
        return []
    return max(candidates, key=lambda paras: sum(len(p) for p in paras))


def fetch_jina_text(url: str) -> str:
    jina_url = f"https://r.jina.ai/http://{url}"
    response = requests.get(jina_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def is_bad_jina_payload(raw_text: str) -> bool:
    text = (raw_text or "").lower()
    bad_markers = [
        "title: deutsche bank research institute",
        "bringing the world to europe and europe to the world",
        "über diesen link verlassen sie die informationsseiten der deutsche bank gruppe",
        "don't show this message anymore",
        "ihre sitzung wird in 180 sekunden beendet",
        "mit neuer sitzung fortfahren",
        "1. [null](https://www.dbresearch.com/prod/ie-prod/navigation.alias)",
    ]
    if any(marker in text for marker in bad_markers):
        return True
    if text.count("## [") >= 3:
        return True
    return False


def extract_article_text_from_jina(raw_text: str, title: str, description: str) -> list[str]:
    text = raw_text or ""
    if is_bad_jina_payload(text):
        return []
    if title:
        marker = f"# {title}"
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx:]

    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            lines.append("")
            continue
        low = s.lower()
        if s.startswith("!"):
            continue
        if s.startswith("[") and "](" in s:
            continue
        if s.startswith("## ["):
            continue
        if low.startswith("you are here"):
            continue
        if low.startswith("title:") or low.startswith("url source:") or low.startswith("markdown content:"):
            continue
        if s in {"Analysts", "Topics", "Publication Type", "Regions"}:
            continue
        if s.startswith("*   "):
            continue
        lines.append(s)

    paragraphs = extract_text_paragraphs("\n".join(lines))
    return clean_article_paragraphs(paragraphs, title, description)


def fallback_jina_paragraphs(link: str, title: str, description: str) -> list[str]:
    try:
        jina_text = fetch_jina_text(link)
        paragraphs = extract_article_text_from_jina(jina_text, title, description)
        if paragraphs:
            print(f"INFO: used Jina text fallback for {link}")
        return paragraphs
    except Exception as exc:
        print(f"WARN: Jina fallback also failed for {link}: {exc}")
        return []


def load_legacy_pdf_bytes(site_dir: Path, slug: str, link: str) -> bytes | None:
    for legacy_name in LEGACY_FEED_NAMES:
        legacy_pdf_path = site_dir / "item" / legacy_name / slug / "original.pdf"
        if legacy_pdf_path.exists() and legacy_pdf_path.stat().st_size > 1000:
            print(f"INFO: reusing legacy local PDF for {link} from {legacy_pdf_path}")
            return legacy_pdf_path.read_bytes()
    return None


def render_pdf_pages(pdf_bytes: bytes, out_dir: Path, max_pages: int = 80) -> list[str]:
    page_hrefs: list[str] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = min(len(doc), max_pages)
        for i in range(total):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            name = f"page-{i+1:03d}.png"
            (out_dir / name).write_bytes(pix.tobytes("png"))
            page_hrefs.append(name)
    finally:
        doc.close()
    return page_hrefs


def build_local_page(
    title: str,
    source_link: str,
    description: str,
    paragraphs: list[str],
    embedded_pdf_href: str | None,
    rendered_page_hrefs: list[str] | None = None,
) -> str:
    escaped_title = html.escape(title)
    escaped_source = html.escape(source_link)
    text_block = []
    if paragraphs:
        for para in paragraphs:
            text_block.append(f"  <p>{html.escape(para)}</p>")
    else:
        text_block.append("  <p>Full-text extraction is currently unavailable for this item.</p>")

    pdf_block = [
        '    <div class="notice">Embedded preview unavailable for this item. Use the buttons above to open the PDF.</div>'
    ]
    if rendered_page_hrefs:
        rendered = ['    <div class="page-images">']
        for idx, href in enumerate(rendered_page_hrefs, start=1):
            rendered.append('      <figure class="page-image">')
            rendered.append(f'        <img src="{html.escape(href)}" alt="PDF page {idx}" loading="lazy">')
            rendered.append(f'        <figcaption>Page {idx}</figcaption>')
            rendered.append('      </figure>')
        rendered.append('    </div>')
        pdf_block = rendered
    elif embedded_pdf_href:
        escaped_embedded_pdf = html.escape(embedded_pdf_href)
        pdf_block = [
            '    <div class="pdfbox">',
            f'      <object data="{escaped_embedded_pdf}#view=FitH" type="application/pdf">',
            f'        <iframe src="{escaped_embedded_pdf}#view=FitH" loading="lazy"></iframe>',
            '      </object>',
            '    </div>',
        ]

    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        f"  <title>{escaped_title}</title>",
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        '  <style>',
        '    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #111; background: #fff; }',
        '    .wrap { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }',
        '    h1 { line-height: 1.2; margin: 0 0 12px; }',
        '    .actions { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0 20px; }',
        '    .btn { display: inline-block; padding: 10px 14px; border-radius: 8px; text-decoration: none; border: 1px solid #ccc; color: #111; }',
        '    .btn.primary { background: #111; color: #fff; border-color: #111; }',
        '    .pdfbox { margin: 20px 0 28px; border: 1px solid #ddd; border-radius: 10px; overflow: hidden; background: #f6f6f6; }',
        '    iframe, embed, object { width: 100%; height: 80vh; border: 0; display: block; background: white; }',
        '    .page-images { display: grid; gap: 18px; margin: 20px 0 28px; }',
        '    .page-image { margin: 0; border: 1px solid #ddd; border-radius: 10px; overflow: hidden; background: #fafafa; }',
        '    .page-image img { display: block; width: 100%; height: auto; background: white; }',
        '    .page-image figcaption { padding: 8px 12px; font-size: 13px; color: #555; border-top: 1px solid #eee; }',
        '    .notice { margin: 20px 0 28px; padding: 14px 16px; border-radius: 10px; background: #fff8e1; border: 1px solid #f0d98c; color: #5f4700; }',
        '    h2 { margin-top: 32px; }',
        '    .text { max-width: 820px; line-height: 1.7; font-size: 16px; }',
        '    .text p { margin: 0 0 1em; }',
        '  </style>',
        "</head>",
        "<body>",
        '  <div class="wrap">',
        f"    <h1>{escaped_title}</h1>",
        '    <div class="actions">',
        f"      <a class=\"btn primary\" href=\"{html.escape(embedded_pdf_href or source_link)}\" target=\"_blank\" rel=\"noopener\">Open PDF</a>",
        f"      <a class=\"btn\" href=\"{html.escape(embedded_pdf_href or source_link)}\" download>Download PDF</a>",
        f"      <a class=\"btn\" href=\"{escaped_source}\" target=\"_blank\" rel=\"noopener\">Open original source</a>",
        '    </div>',
        *pdf_block,
        '    <h2>Extracted text</h2>',
        '    <div class="text">',
        *text_block,
        '    </div>',
        '  </div>',
        "</body>",
        "</html>",
    ])


def try_fetch_binary_pdf(session: requests.Session, target_url: str, referer: str = "") -> bytes:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    response = session.get(target_url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if "pdf" in content_type and len(response.content) > 1000:
        return response.content
    raise ValueError(f"unexpected content-type for PDF binary: {content_type or 'unknown'}")


async def _fetch_pdf_via_browser(url: str) -> bytes:
    viewer_url = url
    if is_pdf_url(url):
        parsed = urlparse(url)
        prod_id = re.search(r"(PROD\d+)", parsed.path or "")
        if prod_id:
            viewer_url = f"https://www.dbresearch.com/PROD/IE-PROD/PDFVIEWER.calias?pdfViewerPdfUrl={prod_id.group(1)}&rwnode=REPORT"

    captured: list[bytes] = []

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
        await page.goto(viewer_url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
        await page.wait_for_timeout(3000)
        await browser.close()

    if captured:
        return max(captured, key=len)
    raise ValueError("browser fallback could not capture PDF response")


def fetch_pdf_via_browser(url: str) -> bytes:
    return asyncio.run(_fetch_pdf_via_browser(url))


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
        direct_url = urljoin(response.url, html.unescape(m_pdf.group(1)))
        try:
            return try_fetch_binary_pdf(session, direct_url, referer=response.url)
        except Exception:
            pass

    m_canonical = re.search(r'<link\s+rel="canonical"\s+href="([^"]+\.pdf[^"]*)"', page_html, re.I)
    if m_canonical:
        direct_url = urljoin(response.url, html.unescape(m_canonical.group(1)))
        try:
            return try_fetch_binary_pdf(session, direct_url, referer=response.url)
        except Exception:
            pass

    if BROWSER_FALLBACK_ENABLED:
        try:
            print(f"INFO: trying browser PDF capture fallback for {url}")
            return fetch_pdf_via_browser(url)
        except Exception as exc:
            print(f"WARN: browser PDF capture fallback failed for {url}: {exc}")

    raise ValueError("could not resolve real PDF binary URL from DB viewer page")


def entry_slug(entry_title: str, entry_link: str, entry_guid: str) -> str:
    path = (urlparse(entry_link).path or "").strip("/")
    leaf = unquote(path.split("/")[-1]) if path else ""
    base = slugify(leaf) if leaf else slugify(entry_title)
    suffix = short_hash(entry_guid, entry_link, entry_title)
    return f"{base}-{suffix}"


def build_feed(site_dir: Path, public_base: str):
    print(f"Fetching source feed: {SOURCE_FEED_URL}")
    parsed = feedparser.parse(SOURCE_FEED_URL)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise RuntimeError(f"failed to parse source feed: {getattr(parsed, 'bozo_exception', 'unknown error')}")

    public_base = public_base.rstrip("/")
    channel_title = parsed.feed.get("title") or "DB Research Global Search"
    channel_description = parsed.feed.get("subtitle") or parsed.feed.get("description") or "DB Research feed with local PDF full text pages"
    channel_language = parsed.feed.get("language") or "en"
    channel_link = f"{public_base}/{OUTPUT_FILE}" if public_base else SOURCE_FEED_URL
    item_root = site_dir / "item" / FEED_NAME
    item_root.mkdir(parents=True, exist_ok=True)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = channel_title
    ET.SubElement(channel, "link").text = channel_link
    ET.SubElement(channel, "description").text = channel_description
    ET.SubElement(channel, "language").text = channel_language
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "generator").text = "DBResearch PDF localizer"

    pdf_count = 0
    total_count = 0
    current_slugs: set[str] = set()

    for entry in parsed.entries[:MAX_ITEMS]:
        title = normalize_space(entry.get("title", "Untitled")) or "Untitled"
        link = entry.get("link", "").strip()
        description = normalize_space(entry.get("summary", "") or entry.get("description", ""))
        guid = (entry.get("id") or entry.get("guid") or link or title).strip()
        pub_date = parse_pub_date(entry.get("published", "") or entry.get("updated", ""))

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title

        if is_pdf_url(link):
            slug = entry_slug(title, link, guid)
            local_url = f"{public_base}/item/{FEED_NAME}/{slug}/" if public_base else link
            current_slugs.add(slug)
            out_dir = item_root / slug
            out_dir.mkdir(parents=True, exist_ok=True)

            extract_error = None
            fetch_error = None
            pdf_bytes: bytes | None = None
            existing_pdf_path = out_dir / "original.pdf"

            try:
                pdf_bytes = fetch_pdf_bytes(link)
            except Exception as exc:
                fetch_error = exc
                extract_error = exc
                print(f"WARN: failed to fetch PDF binary for {link}: {exc}")

            if pdf_bytes:
                existing_pdf_path.write_bytes(pdf_bytes)
            elif existing_pdf_path.exists() and existing_pdf_path.stat().st_size > 1000:
                pdf_bytes = existing_pdf_path.read_bytes()
                print(f"INFO: reusing existing local PDF for {link}")
            else:
                pdf_bytes = load_legacy_pdf_bytes(site_dir, slug, link)
                if pdf_bytes:
                    existing_pdf_path.write_bytes(pdf_bytes)

            paragraphs: list[str] = []
            if pdf_bytes:
                try:
                    paragraphs = extract_best_pdf_paragraphs(pdf_bytes, title, description)
                except Exception as exc:
                    extract_error = exc
                    print(f"WARN: failed to extract PDF text for {link}: {exc}")
                if not paragraphs:
                    paragraphs = fallback_jina_paragraphs(link, title, description)
            else:
                paragraphs = fallback_jina_paragraphs(link, title, description)

            if extract_error and not description:
                description = "PDF item. Local full-text extraction fallback was limited."

            if not description:
                description = shorten(paragraphs[0] if paragraphs else title)

            local_pdf_href = None
            rendered_page_hrefs: list[str] = []
            if pdf_bytes:
                local_pdf_href = "original.pdf"
                try:
                    rendered_page_hrefs = render_pdf_pages(pdf_bytes, out_dir)
                except Exception as exc:
                    print(f"WARN: failed to render PDF pages for {link}: {exc}")
            elif fetch_error and not paragraphs:
                paragraphs = ["Full-text extraction is currently unavailable for this item because the source PDF could not be captured and no local PDF cache was available."]

            (out_dir / "index.html").write_text(
                build_local_page(title, link, description, paragraphs, local_pdf_href, rendered_page_hrefs),
                encoding="utf-8",
            )

            ET.SubElement(item, "link").text = local_url
            guid_el = ET.SubElement(item, "guid")
            guid_el.set("isPermaLink", "true")
            guid_el.text = local_url
            pdf_count += 1
        else:
            ET.SubElement(item, "link").text = link
            guid_el = ET.SubElement(item, "guid")
            guid_el.set("isPermaLink", "true" if guid == link and link.startswith("http") else "false")
            guid_el.text = guid

        ET.SubElement(item, "pubDate").text = pub_date
        total_count += 1

    for child in item_root.iterdir():
        if child.is_dir() and child.name not in current_slugs:
            shutil.rmtree(child)
            print(f"removed stale item dir: {child}")

    xml_bytes = minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    output_path = site_dir / OUTPUT_FILE
    output_path.write_bytes(xml_bytes)
    print(f"Saved {output_path} (items={total_count}, pdf_localized={pdf_count})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_dbresearch_feed.py <site_dir> <public_base>")
        sys.exit(1)

    site_dir = Path(sys.argv[1])
    site_dir.mkdir(parents=True, exist_ok=True)
    public_base = sys.argv[2]
    build_feed(site_dir, public_base)
