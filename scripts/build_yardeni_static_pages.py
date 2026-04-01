import json
import html
import sys
from pathlib import Path

if len(sys.argv) != 4:
    print("Usage: python build_yardeni_static_pages.py <items_json> <site_dir> <public_base>")
    sys.exit(1)

items_json = Path(sys.argv[1])
site_dir = Path(sys.argv[2])
public_base = sys.argv[3].rstrip("/")

if not items_json.exists():
    print(f"items json not found: {items_json}")
    sys.exit(1)

data = json.loads(items_json.read_text(encoding="utf-8"))

for slug, item in data.items():
    title = item.get("title", "Item")
    desc = item.get("description", "")
    source_link = item.get("source_link", "")

    out_dir = site_dir / "item" / "yardeni_morning_briefing" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    html_text = f"""<!doctype html>
<html>
<meta charset="utf-8">
<head>
  <title>{html.escape(title)}</title>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p>正文摘要（静态页面）</p>
  <p><a href="{html.escape(source_link)}" target="_blank">原文链接</a></p>
  <div>{html.escape(desc)}</div>
</body>
</html>
"""
    (out_dir / "index.html").write_text(html_text, encoding="utf-8")
    print(f"built: {slug}")
