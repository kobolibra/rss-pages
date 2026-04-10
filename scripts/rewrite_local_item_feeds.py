import hashlib
import html
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from xml.dom import minidom
from xml.etree import ElementTree as ET

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ET.register_namespace("content", CONTENT_NS)

SOURCE_LINK_FEEDS = {"carlyle_insights", "pitchbook_reports"}
SUMMARY_LOCAL_FEEDS = {"pantheonmacro", "trivium_finance_regs", "yardeni_morning_briefing"}


def slugify_from_link_or_title(link: str, title: str) -> str:
    if link:
        parsed = urlparse(link)
        path = (parsed.path or "").strip("/")
        if path:
            last = path.split("/")[-1]
            if last and "." not in last:
                return last
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return slug or hashlib.md5((link + title).encode("utf-8")).hexdigest()[:12]


def build_page(title: str, body_html: str, source_link: str) -> str:
    # 不在本地 item 页里放外站超链接，尽量避免阅读器继续回源抓正文
    source_html = (
        f'<p>来源：{html.escape(source_link)}</p>'
        if source_link
        else ""
    )
    return f"""<!doctype html>
<html>
<meta charset="utf-8">
<head>
  <title>{html.escape(title)}</title>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  {source_html}
  <div>{body_html}</div>
</body>
</html>
"""


def qname_local(tag: str) -> str:
    return f"{{{CONTENT_NS}}}{tag}"


def html_to_text(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def get_text(el, tag: str) -> str:
    node = el.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def get_content_encoded(el) -> str:
    node = el.find(qname_local("encoded"))
    if node is None:
        return ""
    # content:encoded 里可能是真正的嵌套 HTML，而不只是纯文本；不能只取 node.text
    parts = []
    if node.text and node.text.strip():
        parts.append(node.text)
    for child in list(node):
        parts.append(ET.tostring(child, encoding="unicode", method="xml"))
        if child.tail and child.tail.strip():
            parts.append(child.tail)
    return "".join(parts).strip()


def sanitize_feed_html(feed_name: str, value: str) -> str:
    if not value:
        return ""
    if feed_name == "trivium_finance_regs":
        value = re.sub(
            r"<p>\s*The post\s+.*?\s+appeared first on\s+.*?</p>",
            "",
            value,
            flags=re.I | re.S,
        )
        value = re.sub(
            r"The post\s+.*?\s+appeared first on\s+.*?\.",
            "",
            value,
            flags=re.I | re.S,
        )
        value = re.sub(r"\n\s*\n+", "\n", value).strip()
    if feed_name == "pantheonmacro":
        paragraphs = re.findall(r"<p\b[^>]*>.*?</p>", value, flags=re.I | re.S)
        if paragraphs:
            value = "\n".join(p.strip() for p in paragraphs)
        else:
            value = re.sub(r"<h4\b[^>]*>.*?</h4>", "", value, flags=re.I | re.S)
            value = re.sub(r"<div\b[^>]*class=\"item_info\"[^>]*>.*?</div>", "", value, flags=re.I | re.S)
            value = value.strip()
    return value


def rewrite_feed(xml_path: Path, site_dir: Path, public_base: str, feed_name: str):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"No channel in {xml_path}")

    channel_title = get_text(channel, "title")
    channel_link = get_text(channel, "link") or f"{public_base.rstrip('/')}/{feed_name}.xml"
    channel_desc = get_text(channel, "description")
    channel_lang = get_text(channel, "language") or "en"
    channel_build = get_text(channel, "lastBuildDate") or get_text(channel, "pubDate")

    items = []
    for item in channel.findall("item"):
        title = get_text(item, "title")
        link = get_text(item, "link")
        desc = sanitize_feed_html(feed_name, get_text(item, "description"))
        guid = get_text(item, "guid")
        pub_date = get_text(item, "pubDate")
        author = get_text(item, "author")
        source_content_html = sanitize_feed_html(feed_name, get_content_encoded(item) or desc)
        if feed_name in {"pitchbook_reports", "carlyle_insights", "blackrock_weekly_commentary"}:
            # 这些 feed 需要让阅读器优先根据链接抓正文；
            # 不再把摘要/正文重复塞进 content:encoded。
            content_html = ""
        else:
            content_html = desc if feed_name in SUMMARY_LOCAL_FEEDS else source_content_html
        full_text = html_to_text(content_html)
        if feed_name != "blackrock_weekly_commentary" and len(full_text) > len(desc or ""):
            desc = full_text
        slug = slugify_from_link_or_title(link or guid, title)
        local_url = f"{public_base.rstrip('/')}/item/{feed_name}/{slug}/"

        out_dir = site_dir / "item" / feed_name / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(
            build_page(title, content_html or html.escape(desc), link),
            encoding="utf-8",
        )

        item_link = (link or local_url) if feed_name in SOURCE_LINK_FEEDS else local_url
        item_guid = (guid or item_link) if feed_name in SOURCE_LINK_FEEDS else local_url
        item_desc = desc

        items.append(
            {
                "title": title,
                "link": item_link,
                "guid": item_guid,
                "pub_date": pub_date,
                "author": author,
                "description": item_desc,
                "content_html": content_html,
            }
        )

    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = channel_title
    ET.SubElement(ch, "link").text = channel_link
    ET.SubElement(ch, "description").text = channel_desc
    ET.SubElement(ch, "language").text = channel_lang
    if channel_build:
        ET.SubElement(ch, "lastBuildDate").text = channel_build
    ET.SubElement(ch, "generator").text = "GitHub Pages RSS rewrite"

    for item in items:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = item["title"]
        ET.SubElement(it, "link").text = item["link"]
        guid_el = ET.SubElement(it, "guid")
        is_permalink = str(item["guid"]).startswith("http://") or str(item["guid"]).startswith("https://")
        guid_el.set("isPermaLink", "true" if is_permalink else "false")
        guid_el.text = item["guid"]
        if item["pub_date"]:
            ET.SubElement(it, "pubDate").text = item["pub_date"]
        if item["author"]:
            ET.SubElement(it, "author").text = item["author"]
        if item["description"]:
            ET.SubElement(it, "description").text = item["description"]
        if item["content_html"]:
            ET.SubElement(it, qname_local("encoded")).text = item["content_html"]

    pretty = minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    xml_path.write_bytes(pretty)
    print(f"rewritten: {xml_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python rewrite_local_item_feeds.py <site_dir> <public_base> <feed1,feed2,...>")
        sys.exit(1)

    site_dir = Path(sys.argv[1])
    public_base = sys.argv[2]
    feed_names = [x.strip() for x in sys.argv[3].split(",") if x.strip()]
    for feed_name in feed_names:
        rewrite_feed(site_dir / f"{feed_name}.xml", site_dir, public_base, feed_name)
