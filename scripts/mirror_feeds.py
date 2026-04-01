import os
from pathlib import Path
import requests

OUT_DIR = Path(os.environ.get("OUT_DIR", "site"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

FEEDS = {
    "pantheonmacro.xml": "https://politepaul.com/fd/ziIC6ajh5OfD.xml",
    "trivium_finance_regs.xml": "https://finance.triviumchina.com/feed",
    "blackrock_weekly_commentary_diffbot.xml": "https://rss.diffbot.com/rss?url=https://www.blackrock.com/corporate/insights/blackrock-investment-institute/archives%23weekly-commentary",
    "barclays_weekly_insights.xml": "https://fetchrss.com/feed/1sjHuC3vADGU1vGC1u3opETq.rss",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GitHubActions-RSS-Mirror/1.0)"
}

for filename, url in FEEDS.items():
    print(f"Fetching {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        (OUT_DIR / filename).write_bytes(r.content)
        print(f"Saved {filename}")
    except Exception as e:
        print(f"WARN: failed to fetch {url}: {e}")
