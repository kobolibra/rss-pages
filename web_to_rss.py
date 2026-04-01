#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebToRSS - 通用网页转 RSS 生成器
支持多源、正则提取、缓存、HTTP 服务
"""

import argparse
import hashlib
import json
import re
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET
from xml.dom import minidom
import html

import requests
import yaml


class TemplateEngine:
    @staticmethod
    def to_regex(pattern: str) -> str:
        if "{%}" not in pattern and "{*}" not in pattern:
            return pattern
        escaped = re.escape(pattern)
        regex = escaped.replace(r"\{%\}", r"(.+?)")
        regex = regex.replace(r"{\*}", r".*?")
        return regex

    @staticmethod
    def extract(pattern: str, content: str) -> List[Tuple[Tuple, int, int, str]]:
        """
        返回列表：[(groups_tuple, start, end, full_text), ...]
        """
        regex = TemplateEngine.to_regex(pattern)
        matches = list(re.finditer(regex, content, re.IGNORECASE | re.DOTALL | re.MULTILINE))
        result = []
        for m in matches:
            result.append((m.groups(), m.start(), m.end(), m.group(0)))
        return result


class RSSBuilder:
    def __init__(self, title: str, link: str, description: str):
        self.rss = ET.Element("rss", version="2.0")
        self.rss.set("xmlns:content", "http://purl.org/rss/1.0/modules/content/")
        self.channel = ET.SubElement(self.rss, "channel")
        self._add("title", title)
        self._add("link", link)
        self._add("description", description)
        self._add("lastBuildDate", datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"))
        self._add("generator", "WebToRSS")

    def _add(self, tag: str, text: str):
        el = ET.SubElement(self.channel, tag)
        el.text = html.unescape(str(text))

    def add_item(
        self,
        title: str,
        link: str,
        description: str,
        pub_date: Optional[str] = None,
        guid: Optional[str] = None,
        content_html: Optional[str] = None,
    ):
        item = ET.SubElement(self.channel, "item")
        ET.SubElement(item, "title").text = html.unescape(title)
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "description").text = html.unescape(description)

        if pub_date:
            ET.SubElement(item, "pubDate").text = pub_date

        if guid:
            guid_el = ET.SubElement(item, "guid")
            guid_el.text = guid
            guid_el.set("isPermaLink", "false")

        if content_html:
            content_el = ET.SubElement(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
            content_el.text = html.unescape(content_html)

    def to_xml(self) -> str:
        rough = ET.tostring(self.rss, encoding="unicode")
        pretty = minidom.parseString(rough).toprettyxml(indent="  ")
        pretty = pretty.replace('&lt;img ', '<img ')
        pretty = pretty.replace(' /&gt;', ' />')
        pretty = pretty.replace('&lt;/p&gt;', '</p>')
        pretty = pretty.replace('&lt;p&gt;', '<p>')
        pretty = pretty.replace('&lt;a ', '<a ')
        pretty = pretty.replace('&lt;/a&gt;', '</a>')
        pretty = pretty.replace('&gt;', '>')
        pretty = pretty.replace('&quot;', '"')
        return pretty


class WebToRSS:
    def __init__(self, config_path: str, base_dir: Optional[str] = None):
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config not found: {self.config_path}")

        # load feed config first, then merge with optional defaults from feeds/_defaults.yaml
        self.base_dir = Path(base_dir) if base_dir else self.config_path.parent
        defaults_path = self.base_dir / "_defaults.yaml"
        default_cfg = self._load_yaml(defaults_path) if defaults_path.exists() else {}
        self.config = self._deep_merge(default_cfg, self._load_yaml(self.config_path))

        self.cache_dir = self.base_dir / ".cache"
        self.out_dir = self.base_dir / "output"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # Cloudflare Browser Rendering optional fallback for blocked/failing pages
        cf_cfg = self.config.get("source", {}).get("cloudflare", {}) or {}
        self.cf_account_id = (
            cf_cfg.get("account_id")
            or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
            or self.config.get("env", {}).get("CLOUDFLARE_ACCOUNT_ID", "")
        )
        self.cf_api_token = (
            cf_cfg.get("api_token")
            or os.getenv("CLOUDFLARE_API_TOKEN", "")
            or self.config.get("env", {}).get("CLOUDFLARE_API_TOKEN", "")
        )
        # When enabled=true, try normal fetch first, fallback to Cloudflare only if normal fails.
        self.cf_enabled = bool(cf_cfg.get("enabled") or cf_cfg.get("use_browser_rendering"))
        self.cf_fallback = bool(cf_cfg.get("fallback", True))
        self.cf_use_if_normal_fails = self.cf_enabled and self.cf_fallback

    @staticmethod
    def _load_yaml(path: Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError("Invalid YAML config")
        return data

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Shallow + nested dict merge, where override has higher priority."""
        result = dict(base)
        for k, v in override.items():
            if (
                isinstance(result.get(k), dict)
                and isinstance(v, dict)
            ):
                result[k] = WebToRSS._deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    @staticmethod
    def _md5(s: str) -> str:
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    def _fetch_html(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        """
        Fetch strategy:
        1) Try normal path first (r.jina.ai proxy) for speed and cost.
        2) If it fails and cloudflare fallback is enabled, try Browser Rendering.
        """
        last_error = None

        # 1) normal fetch first (legacy behavior)
        try:
            if url.startswith('http://') or url.startswith('https://'):
                proxy_url = f"https://r.jina.ai/{url}"
                hdr = {"User-Agent": "Mozilla/5.0 (compatible; WebToRSS/1.0)"}
                if headers:
                    hdr.update(headers)
                r = requests.get(proxy_url, headers=hdr, timeout=30)
            else:
                hdr = headers or {"User-Agent": "Mozilla/5.0 (compatible; WebToRSS/1.0)"}
                r = requests.get(url, headers=hdr, timeout=30)

            # r.jina may still return 200 but block page; keep as content and only fallback on request exceptions
            r.raise_for_status()
            txt = r.text
            if txt and "Just a moment" not in txt and "Cloudflare" not in txt[:500]:
                return txt
            # block-like marker detected -> try cf fallback if enabled
            last_error = ValueError("normal fetch looks blocked")
        except Exception as e:
            last_error = e

        if self.cf_use_if_normal_fails:
            if not self.cf_account_id or not self.cf_api_token:
                raise ValueError("Cloudflare Browser Rendering enabled but account_id/api_token is missing")

            endpoint = f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account_id}/browser-rendering/markdown"
            hdr = {
                "Authorization": f"Bearer {self.cf_api_token}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; WebToRSS/1.0)",
            }
            if headers:
                hdr.update(headers)

            try:
                r = requests.post(endpoint, headers=hdr, json={"url": url}, timeout=60)
                r.raise_for_status()
                payload = r.json()
                if not payload.get("success"):
                    raise RuntimeError(f"Cloudflare browser-rendering error: {payload.get('errors')}")
                return payload.get("result", "")
            except Exception as e:
                # final fallback: re-raise last normal error for context if exists
                if last_error:
                    raise last_error
                raise e

        # no fallback configured: propagate normal error
        if last_error:
            raise last_error
        return ""

    def _cache_key(self, url: str) -> str:
        return self._md5(url)[:12]

    def _load_cache(self, url: str) -> Optional[str]:
        p = self.cache_dir / f"{self._cache_key(url)}.html"
        if p.exists():
            return p.read_text(encoding="utf-8")
        return None

    def _save_cache(self, url: str, html: str):
        p = self.cache_dir / f"{self._cache_key(url)}.html"
        p.write_text(html, encoding="utf-8")

    def _parse_desc_and_date(self, body: str) -> Tuple[str, Optional[str]]:
        """
        从条目后的正文片段中提取描述和日期。
        逻辑：逐行扫描，遇到日期行则停止；之前为非空行合并为描述。
        日期格式：Mar. 2026 或 March 23, 2026
        同时清理 Markdown 图片和 "Read more" 链接。
        """
        lines = body.splitlines()
        desc_lines = []
        date_str = None
        date_pattern1 = re.compile(r'^[A-Z][a-z]+\s+\d{1,2},\s+\d{4}$')
        date_pattern2 = re.compile(r'^[A-Z][a-z]+\.?\s+\d{4}$')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if not date_str and (date_pattern1.match(line) or date_pattern2.match(line)):
                date_str = line
                break
            desc_lines.append(line)
        desc = ' '.join(desc_lines).strip()
        desc = re.sub(r'\s+', ' ', desc)
        # 清理：去除 Markdown 图片和 "Read more" 链接
        desc = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\[Read more(?: about[^\]]*)?\]\([^)]+\)', '', desc, flags=re.IGNORECASE)
        desc = desc.strip()
        if len(desc) > 500:
            desc = desc[:500] + '...'
        return desc, date_str

    def _extract_fields(self, raw: str) -> Tuple[str, str, Optional[str]]:
        """
        传统格式：提取标题、描述、日期（PitchBook旧格式）
        """
        date_match = re.search(r'\|([A-Za-z]+\s+\d{1,2},\s+\d{4})', raw)
        if not date_match:
            date_match = re.search(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s+Learn more', raw)
        date_str = date_match.group(1) if date_match else None
        
        core = re.sub(r'\|[^L]*Learn more.*$', '', raw, flags=re.I).strip()
        core = re.sub(r'\s+Learn more.*$', '', core, flags=re.I).strip()
        
        if core.startswith('PitchBook Analyst Note:'):
            after = core[len('PitchBook Analyst Note:'):].strip()
            if ' This ' in after:
                title_part = after.split(' This ', 1)[0].strip()
                title = 'PitchBook Analyst Note: ' + title_part
                desc = 'This ' + after.split(' This ', 1)[1].strip()
                return title, desc, date_str
            else:
                return core, core, date_str
        
        for marker in [" The ", " This ", " These "]:
            if marker in core:
                parts = core.split(marker, 1)
                title = parts[0].strip()
                desc = marker[1:] + parts[1].strip()
                return title, desc, date_str
        
        return core, core, date_str

    def _normalize_text(self, text: str) -> str:
        text = text.replace('\r', '')
        text = re.sub(r'\xa0', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _extract_blackrock_weekly(self, html_text: str) -> Dict[str, str]:
        raw = self._normalize_text(html_text)

        m_date = re.search(r'\b([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\b', raw)
        m_title = re.search(r'\n([A-Z][^\n]{8,120})\n\n(?:To view this video|Transcript|Weekly video_)', raw)
        if not m_title:
            m_title = re.search(r'Title slide:\s*([^\n]+)', raw)
        m_pdf = re.search(r'https://www\.blackrock\.com/corporate/literature/market-commentary/weekly-investment-commentary-en-us-[^\s)\]]+\.pdf', raw)

        date_str = m_date.group(1).strip() if m_date else ''
        title = m_title.group(1).strip() if m_title else ''
        pdf_link = m_pdf.group(0).strip() if m_pdf else self.config['source']['url']

        if not title:
            raise ValueError('blackrock_weekly title not found')

        lines = [ln.strip() for ln in raw.splitlines()]
        lines = [ln for ln in lines if ln]

        # 只取标题之后，且在“Read our past weekly market commentaries”之前
        start_idx = lines.index(title) if title in lines else 0
        truncated = []
        for ln in lines[start_idx:]:
            if 'Read our past weekly market commentaries' in ln:
                break
            truncated.append(ln)

        blacklist = [
            'capital at risk', 'for public distribution', 'to view this video', 'video player is loading',
            'current time 0:00', 'loaded: 0%', 'share', 'facebook', 'twitter', 'linkedin',
            'opening frame:', 'camera frame', 'closing frame:', 'header:', 'transcript',
            'download full commentary', 'market commentary', 'asset class views', 'change location'
        ]

        def noisy(s: str) -> bool:
            low = s.lower()
            if any(b in low for b in blacklist):
                return True
            return False

        kept = []
        for ln in truncated:
            if noisy(ln):
                continue
            kept.append(ln)

        # 组装 summary：优先取三段概览块
        summary_parts = []
        summary_heads = ['Thematic opportunities', 'Market backdrop', 'Week ahead']
        for head in summary_heads:
            if head in kept:
                i = kept.index(head)
                block = [head]
                if i + 1 < len(kept):
                    block.append(kept[i + 1])
                summary_parts.append('\n'.join(block))
        description = '\n\n'.join(summary_parts).strip()

        # content 保留 summary + 正文整体结构到截断点
        content_lines = []
        skip_exact_prefixes = (
            'Weekly video_', 'Title slide:', 'URL Source:', 'Markdown Content:',
            '1: ', '2: ', '3: ', 'Outro:'
        )
        skip_exact = {
            title,
            'Weekly market commentary',
            'BlackRock Investment Institute',
            'Christopher Kaminker',
            'Head of Sustainable Investment Research and Analytics',
        }
        skip_contains = [
            'To view this video', 'Video Player is loading', 'Current Time 0:00', 'Loaded: 0%',
            'Opening frame:', 'Camera frame', 'Closing frame:', 'Read details: blackrock.com/weekly-commentary'
        ]
        for ln in kept:
            if ln in skip_exact:
                continue
            if ln.startswith(skip_exact_prefixes):
                continue
            if any(x in ln for x in skip_contains):
                continue
            content_lines.append(ln)

        # 正文尽量从 summary 概览块开始，而不是 transcript teaser 开始
        preferred_start = None
        for marker in ['Thematic opportunities', 'The economic shock emanating from the Middle East conflict']:
            if marker in content_lines:
                preferred_start = content_lines.index(marker)
                break
        if preferred_start is not None:
            content_lines = content_lines[preferred_start:]

        cleaned_lines = []
        pending_images = []
        for ln in content_lines:
            # 去 markdown 标题，但保留图片（后续转成 <img>）
            ln = re.sub(r'^#+\s*', '', ln).strip()
            # 图片先暂存，避免被紧随其后的脚注截断误伤
            if re.match(r'^!\[[^\]]*\]\([^\)]+\)$', ln):
                pending_images.append(ln)
                continue
            # 去脚注/来源/日历尾巴；截断前先把紧邻图片补回
            if ln.startswith('Past performance is not a reliable indicator') or ln.startswith('**Past performance is not a reliable indicator'):
                cleaned_lines.extend(pending_images)
                break
            if ln.startswith('Sources:'):
                cleaned_lines.extend(pending_images)
                break
            if re.match(r'^(March|April|May|June|July|August|September|October|November|December|January|February)\s+\d{1,2}$', ln):
                cleaned_lines.extend(pending_images)
                break
            if pending_images:
                cleaned_lines.extend(pending_images)
                pending_images = []
            cleaned_lines.append(ln)

        def render_line(line: str) -> str:
            m = re.match(r'^!\[([^\]]*)\]\(([^\)]+)\)$', line)
            if m:
                alt = html.escape(m.group(1).strip())
                src = html.escape(m.group(2).strip())
                return f'<p><img src="{src}" alt="{alt}" /></p>'
            return f'<p>{html.escape(line)}</p>'

        content_lines = cleaned_lines

        content_lines = cleaned_lines

        # 用 description 的首段做摘要回填；正文里仍保留 summary + 正文
        if not description:
            for ln in content_lines[:8]:
                if len(ln) > 80:
                    description = ln
                    break

        if not content_lines:
            raise ValueError('blackrock_weekly content empty')

        content_html = ''.join(render_line(x) for x in content_lines)
        guid = hashlib.md5(f'{title}|{date_str}|{pdf_link}'.encode('utf-8')).hexdigest()
        return {
            'title': title,
            'link': pdf_link,
            'description': description,
            'content_html': content_html,
            'date_str': date_str,
            'guid': guid,
        }

    def _generate_blackrock_weekly(self, html_text: str, src: str) -> str:
        ch = self.config['output']['channel']
        builder = RSSBuilder(ch['title'], ch['link'], ch['description'])

        page = html_text

        def clean_text(text: str) -> str:
            text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
            text = re.sub(r'</p\s*>', '\n\n', text, flags=re.I)
            text = re.sub(r'<[^>]+>', '', text)
            text = html.unescape(text)
            text = text.replace('\r', '')
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text.strip()

        text = clean_text(page)

        date_match = re.search(r'\b([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\b', text)
        pub_date = None
        date_str = date_match.group(1) if date_match else None
        if date_str:
            try:
                dt = datetime.strptime(date_str, '%b %d, %Y')
                pub_date = dt.strftime('%a, %d %b %Y %H:%M:%S GMT')
            except Exception:
                pub_date = None

        title = None
        m = re.search(r'\n\s*Weekly market commentary\s*\n(?:.*?\n){0,8}?\s*([A-Z][^\n]{8,140})\s*\n', text, re.S)
        if m:
            candidate = m.group(1).strip()
            if 'To view this video' not in candidate and 'BlackRock Investment Institute' not in candidate:
                title = candidate
        if not title:
            m2 = re.search(r'Title slide:\s*([^\n]+)', text)
            if m2:
                title = m2.group(1).strip()
        if not title:
            raise ValueError('blackrock_weekly: title not found')

        pdf_match = re.search(r'https://www\.blackrock\.com/corporate/literature/market-commentary/weekly-investment-commentary-en-us-[^\s\)\"]+', page)
        pdf_link = pdf_match.group(0) if pdf_match else src

        anchor_pos = text.find(title)
        if anchor_pos == -1:
            anchor_pos = 0
        window = text[anchor_pos: anchor_pos + 40000]

        stop_markers = [
            'Read our past weekly market commentaries',
            'Big calls',
            'Tactical granular views',
            'Past performance is not a reliable indicator',
        ]
        cut = len(window)
        for marker in stop_markers:
            pos = window.find(marker)
            if pos != -1:
                cut = min(cut, pos)
        window = window[:cut]

        paragraphs = [p.strip() for p in window.split('\n\n') if p.strip()]
        blacklist = [
            'capital at risk', 'for public distribution', 'video player is loading', 'current time 0:00',
            'loaded: 0%', 'weekly video_', 'share', 'facebook', 'twitter', 'linkedin', 'transcript',
            'opening frame:', 'camera frame', 'closing frame:', 'title slide:', 'download full commentary',
            'paragraph-', 'advance static table', 'image-', 'change location', 'header:'
        ]

        def is_noise(p: str) -> bool:
            low = p.lower()
            if any(b in low for b in blacklist):
                return True
            if len(p) < 40:
                return True
            return False

        candidates = []
        seen = set()
        for p in paragraphs:
            if p in seen:
                continue
            seen.add(p)
            if is_noise(p):
                continue
            candidates.append(p)

        if len(candidates) < 3:
            raise ValueError('blackrock_weekly: insufficient body paragraphs')

        # Summary: prefer the title-near blurb/lede, but skip obvious YouTube/transcript labels.
        summary = None
        for p in candidates[:8]:
            if 90 <= len(p) <= 420:
                summary = p
                break
        if not summary:
            summary = candidates[0]

        body_start = 0
        intro_markers = ['the economic shock emanating', 'highly exposed', 'ai driving power demand', 'our bottom line']
        for i, p in enumerate(candidates[:12]):
            low = p.lower()
            if any(m in low for m in intro_markers):
                body_start = i
                break

        body_paras = candidates[body_start:body_start + 18]
        content_parts = [f'<p><a href="{pdf_link}" target="_blank" rel="noopener">Download full commentary (PDF)</a></p>']
        if summary and summary not in body_paras:
            content_parts.append(f'<p>{html.escape(summary)}</p>')
        for p in body_paras:
            content_parts.append(f'<p>{html.escape(p)}</p>')
        content_html = ''.join(content_parts)

        guid = self._md5(f'blackrock-weekly|{title}|{date_str or pdf_link}')
        builder.add_item(
            title=title,
            link=pdf_link,
            description=summary,
            pub_date=pub_date or datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT'),
            guid=guid,
            content_html=content_html,
        )
        return builder.to_xml()

    def _generate_yardeni_morning_briefing(self, markdown_text: str, public_base: str = "") -> str:
        ch = self.config["output"]["channel"]
        builder = RSSBuilder(ch["title"], public_base or ch["link"], ch["description"])

        settings = self.config.get("settings", {})
        max_items = int(settings.get("max_items", 30))
        feed_name = self.config.get("name", "yardeni_morning_briefing")
        public_base = (public_base or "").rstrip('/')

        section = markdown_text.split('## Morning Briefing', 1)[1] if '## Morning Briefing' in markdown_text else markdown_text
        blocks = re.findall(
            r'^### \[(.*?)\]\((https://yardeni\.com/morning-briefing/[^)]+)\)\s*\n+(.*?)(?=^### \[|\Z)',
            section,
            re.M | re.S,
        )

        item_cache: Dict[str, Dict[str, str]] = {}
        seen = set()
        count = 0

        for title, link, body in blocks:
            title = html.unescape(title).strip()
            if not title or title.lower() == 'morning briefing':
                continue

            m_desc = re.search(r'Executive Summary:\s*(.*)', body)
            desc = m_desc.group(1).strip() if m_desc else ''
            desc = re.sub(r'\s+', ' ', desc)
            if not desc:
                desc = title

            m_date = re.search(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})', body)
            date_str = m_date.group(1) if m_date else None

            slug = link.rstrip('/').split('/')[-1]
            guid = self._md5(f'yardeni|{slug}')
            if guid in seen:
                continue
            seen.add(guid)

            local_link = f"{public_base}/item/{quote(feed_name, safe='')}/{quote(slug, safe='')}" if public_base else link

            if date_str:
                try:
                    dt = datetime.strptime(date_str, '%B %d, %Y')
                    pub_date = dt.strftime('%a, %d %b %Y %H:%M:%S GMT')
                except Exception:
                    pub_date = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
            else:
                pub_date = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')

            content_html = f'<p>{html.escape(desc)}</p>'
            builder.add_item(
                title=title,
                link=local_link,
                description=desc,
                pub_date=pub_date,
                guid=guid,
                content_html=content_html,
            )

            item_cache[slug] = {
                'title': title,
                'description': desc,
                'source_link': link,
            }
            count += 1
            if count >= max_items:
                break

        cache_path = self.out_dir / f'{feed_name}_items.json'
        cache_path.write_text(json.dumps(item_cache, ensure_ascii=False, indent=2), encoding='utf-8')
        return builder.to_xml()

    def generate(self, force: bool = False, public_base: Optional[str] = None) -> str:
        src = self.config["source"]["url"]
        headers = self.config["source"].get("headers")

        page_html = None
        if not force:
            page_html = self._load_cache(src)

        if page_html is None or force:
            page_html = self._fetch_html(src, headers=headers)
            self._save_cache(src, page_html)

        parse_mode = self.config.get("settings", {}).get("parse_mode")
        if parse_mode == "blackrock_weekly_single":
            xml = self._generate_blackrock_weekly(page_html, src)
            out_file = self.config.get("output_file")
            if out_file:
                p = self.out_dir / out_file
                p.write_text(xml, encoding="utf-8")
                print(f"[WebToRSS] saved: {p}")
            return xml

        if parse_mode == "yardeni_morning_briefing":
            xml = self._generate_yardeni_morning_briefing(page_html, public_base=public_base or os.getenv("WEB_TO_RSS_PUBLIC_BASE", ""))
            out_file = self.config.get("output_file")
            if out_file:
                p = self.out_dir / out_file
                p.write_text(xml, encoding="utf-8")
                print(f"[WebToRSS] saved: {p}")
            return xml

        extraction = self.config.get("extraction", {})
        pattern = extraction.get("pattern", "")
        link_group = int(extraction.get("link_group", 2))
        link_filter = extraction.get("link_filter")

        ch = self.config["output"]["channel"]
        item_cfg = self.config["output"].get("item", {})
        settings = self.config.get("settings", {})
        parse_mode = settings.get("parse_mode")

        if parse_mode == "blackrock_weekly_commentary":
            item = self._extract_blackrock_weekly(page_html)
            builder = RSSBuilder(ch["title"], ch["link"], ch["description"])
            if item['date_str']:
                try:
                    dt = datetime.strptime(item['date_str'], "%b %d, %Y")
                    pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
                except Exception:
                    pub_date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
            else:
                pub_date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
            builder.add_item(
                title=item['title'],
                link=item['link'],
                description=item['description'],
                pub_date=pub_date,
                guid=item['guid']
            )
            rss_item = builder.channel.findall('item')[0]
            content_elem = ET.SubElement(rss_item, '{http://purl.org/rss/1.0/modules/content/}encoded')
            content_elem.text = html.unescape(item['content_html'])
            xml = builder.to_xml()
            out_file = self.config.get("output_file")
            if out_file:
                p = self.out_dir / out_file
                p.write_text(xml, encoding="utf-8")
                print(f"[WebToRSS] saved: {p}")
            print("[WebToRSS] matched=1 emitted=1 new=1")
            return xml

        matches = TemplateEngine.extract(pattern, page_html)

        max_items = int(settings.get("max_items", 200))
        dedup = bool(settings.get("dedup", True))
        out_file = self.config.get("output_file")

        builder = RSSBuilder(ch["title"], ch["link"], ch["description"])

        seen = set()
        new_guids = set()
        count = 0

        for idx, (groups, start, end, full) in enumerate(matches):
            if count >= max_items:
                break

            # 渲染标题和链接
            title_raw = item_cfg["title"].format(*groups)
            try:
                link = item_cfg["link"].format(*groups)
            except (IndexError, KeyError):
                # 链接模板可能依赖于某些组；如果链接组索引不够，从 full 中回退
                link = groups[link_group-1] if len(groups) >= link_group else ""

            # 链接过滤
            raw_link = groups[link_group-1] if len(groups) >= link_group else link
            if link_filter and not re.search(link_filter, raw_link):
                continue

            # 提取描述和日期
            # 查找当前匹配后的内容截至下一个匹配
            next_start = matches[idx + 1][1] if idx + 1 < len(matches) else len(page_html)
            body = page_html[end:next_start].strip()

            if parse_mode == "pitchbook_short_title":
                # 从当前匹配行提取 "Learn more" 前的正文片段
                full_line = full.replace("\n", " ").strip()
                m_line = re.search(r"Research\s+###\s+", full_line)
                if m_line:
                    tail = full_line[m_line.end():]
                else:
                    tail = full_line

                # 去除末尾链接，保留 "Title The ... Learn more"
                tail = re.sub(r"\]\([^)]*\)$", "", tail)
                if "Learn more" in tail:
                    tail, _ = tail.split("Learn more", 1)
                tail = tail.strip()

                # 若正文前含标题，去掉标题前缀，取描述
                if tail.startswith(title_raw):
                    tail = tail[len(title_raw):].strip()

                desc = tail if tail else title_raw
                title_raw = title_raw.strip().split("|")[0].strip()

                # 尝试解析日期
                m_date = re.search(r"([A-Za-z]+\.?\s+\d{1,2},\s+\d{4})", full_line)
                if m_date:
                    date_str = m_date.group(1)
                else:
                    date_str = None
            else:
                if body:
                    desc, date_str = self._parse_desc_and_date(body)
                else:
                    # 回退：用传统方法从 title_raw 解析
                    title, desc, date_str = self._extract_fields(title_raw)
                    if title != title_raw:
                        title_raw = title

                    if not title_raw:
                        continue

            guid = self._md5(f"{title_raw}{link}")

            if dedup and guid in seen:
                continue
            seen.add(guid)
            new_guids.add(guid)
            count += 1

            if date_str:
                try:
                    dt = datetime.strptime(date_str, "%B %d, %Y")
                    pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
                except ValueError:
                    try:
                        dt = datetime.strptime(date_str, "%b. %Y")
                        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
                    except Exception:
                        pub_date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
            else:
                pub_date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

            builder.add_item(
                title=title_raw,
                link=link,
                description=desc,
                pub_date=pub_date,
                guid=guid
            )

            if count >= max_items:
                break

        print(f"[WebToRSS] matched={len(matches)} emitted={count} new={len(new_guids)}")
        xml = builder.to_xml()

        if out_file:
            p = self.out_dir / out_file
            p.write_text(xml, encoding="utf-8")
            print(f"[WebToRSS] saved: {p}")

        return xml

    def serve(self, host: str = "0.0.0.0", port: int = 8769, path: str = "/feed"):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import urllib.parse

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self_inner):
                parsed = urllib.parse.urlparse(self_inner.path)

                # item detail page for readers like Readwise
                if parsed.path.startswith('/item/'):
                    parts = parsed.path.split('/')
                    if len(parts) < 4:
                        self_inner.send_response(404)
                        self_inner.end_headers()
                        return
                    feed_name = unquote(parts[2])
                    slug = unquote('/'.join(parts[3:])).strip('/')
                    slug = slug.replace('\\\\', '/').replace('\\', '/')
                    if '/' in slug:
                        slug = slug.split('/')[-1]
                    cache_file = self_inner.base.parent / 'output' / f'{feed_name}_items.json'
                    if not cache_file.exists():
                        self_inner.send_response(404)
                        self_inner.end_headers()
                        return
                    try:
                        payload = json.loads(cache_file.read_text(encoding='utf-8'))
                        item = payload.get(slug)
                        if not item:
                            self_inner.send_response(404)
                            self_inner.end_headers()
                            return
                        title = item.get('title', 'Item')
                        desc = item.get('description', '')
                        source_link = item.get('source_link', '')
                        body = f"""<!doctype html><html><meta charset=\"utf-8\"><head><title>{html.escape(title)}</title></head><body><h1>{html.escape(title)}</h1><p>正文摘要（由 WebToRSS 生成）</p><p><a href=\"{html.escape(source_link)}\" target=\"_blank\">原文链接</a></p><div>{html.escape(desc)}</div></body></html>"""
                        self_inner.send_response(200)
                        self_inner.send_header('Content-Type', 'text/html; charset=utf-8')
                        self_inner.end_headers()
                        self_inner.wfile.write(body.encode('utf-8'))
                    except Exception as e:
                        self_inner.send_response(500)
                        self_inner.send_header('Content-Type', 'text/plain; charset=utf-8')
                        self_inner.end_headers()
                        self_inner.wfile.write(f'error: {e}'.encode('utf-8'))
                    return

                if parsed.path != path:
                    self_inner.send_response(404)
                    self_inner.end_headers()
                    return

                qs = urllib.parse.parse_qs(parsed.query)
                config = qs.get("config", [None])[0]
                force = qs.get("force", ["0"])[0] == "1"
                if not config:
                    self_inner.send_response(200)
                    self_inner.send_header("Content-Type", "text/html; charset=utf-8")
                    self_inner.end_headers()
                    self_inner.wfile.write(
                        f"""<html><body>
<h3>WebToRSS</h3>
<p>Usage: /feed?config=feeds/xxx.yaml</p>
<p>Example: <a href="/feed?config=feeds/pitchbook_reports.yaml">/feed?config=feeds/pitchbook_reports.yaml</a></p>
</body></html>""".encode("utf-8")
                    )
                    return

                try:
                    cfg_path = str((self_inner.base.parent / config)) if not Path(config).is_absolute() else config
                    forge = WebToRSS(cfg_path, base_dir=str(self_inner.base.parent))
                    public_base = os.getenv('WEB_TO_RSS_PUBLIC_BASE', '').rstrip('/') or f'http://{self_inner.headers.get("Host", f"127.0.0.1:{port}")}'
                    xml = forge.generate(force=force, public_base=public_base)
                    self_inner.send_response(200)
                    self_inner.send_header("Content-Type", "application/rss+xml; charset=utf-8")
                    self_inner.send_header("Access-Control-Allow-Origin", "*")
                    self_inner.end_headers()
                    self_inner.wfile.write(xml.encode("utf-8"))
                except Exception as e:
                    self_inner.send_response(500)
                    self_inner.send_header("Content-Type", "text/plain; charset=utf-8")
                    self_inner.end_headers()
                    self_inner.wfile.write(f"error: {e}".encode("utf-8"))

            def log_message(self_inner, format, *args):
                pass

        Handler.base = self.config_path.parent
        server = HTTPServer((host, port), Handler)
        print(f"[WebToRSS] server started: http://{host}:{port}{path}?config=<config.yaml>")
        print(f"[WebToRSS] example: http://127.0.0.1:{port}{path}?config=feeds/pitchbook_reports.yaml")
        server.serve_forever()


def build_parser():
    p = argparse.ArgumentParser(description="WebToRSS - generic web page to RSS generator")
    p.add_argument("--config", "-c", required=True, help="yaml config path")
    p.add_argument("--output", "-o", help="output xml path (relative to output/)")
    p.add_argument("--force", "-f", action="store_true", help="force refresh cache")
    p.add_argument("--daemon", "-d", action="store_true", help="run http server")
    p.add_argument("--host", default="0.0.0.0", help="daemon host")
    p.add_argument("--port", type=int, default=8769, help="daemon port")
    p.add_argument("--path", default="/feed", help="daemon path, default /feed")
    return p


def main():
    args = build_parser().parse_args()

    forge = WebToRSS(args.config)
    if args.output:
        forge.config["output_file"] = args.output

    if args.daemon:
        forge.serve(host=args.host, port=args.port, path=args.path)
        return

    xml = forge.generate(force=args.force)
    print(xml[:3000])
    if len(xml) > 3000:
        print("... [truncated]")

if __name__ == "__main__":
    main()
