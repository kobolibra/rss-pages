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
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET
from xml.dom import minidom
import html

import requests
import yaml
from bs4 import BeautifulSoup


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
        pretty = pretty.replace('&lt;strong&gt;', '<strong>')
        pretty = pretty.replace('&lt;/strong&gt;', '</strong>')
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

    def _fetch_html(self, url: str, headers: Optional[Dict[str, str]] = None, prefer_raw: bool = False) -> str:
        """
        Fetch strategy:
        1) Default: try r.jina.ai text extraction first for simpler parsing.
        2) For pages that need DOM/image fidelity, prefer_raw=True to fetch original HTML first.
        3) If blocked/failing and cloudflare fallback is enabled, try Browser Rendering.
        """
        last_error = None
        hdr = {"User-Agent": "Mozilla/5.0 (compatible; WebToRSS/1.0)"}
        if headers:
            hdr.update(headers)

        def _good(txt: str) -> bool:
            return bool(txt) and "Just a moment" not in txt and "Cloudflare" not in txt[:500]

        def _get_with_retries(target_url: str, timeout: int, attempts: int = 3) -> str:
            nonlocal last_error
            for attempt in range(attempts):
                try:
                    r = requests.get(target_url, headers=hdr, timeout=timeout)
                    r.raise_for_status()
                    txt = r.text
                    if _good(txt):
                        return txt
                    last_error = ValueError(f"fetch looks blocked: {target_url}")
                except Exception as e:
                    last_error = e
                    if attempt == attempts - 1:
                        break
                    time.sleep(2 * (attempt + 1))
            raise last_error

        # raw-first path for image/DOM-sensitive pages
        if prefer_raw:
            try:
                return _get_with_retries(url, timeout=45, attempts=2)
            except Exception as e:
                last_error = e

        # default / fallback text-first path via r.jina.ai
        try:
            if url.startswith('http://') or url.startswith('https://'):
                proxy_url = f"https://r.jina.ai/{url}"
                return _get_with_retries(proxy_url, timeout=60, attempts=3)
            return _get_with_retries(url, timeout=45, attempts=2)
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

        m_video_date = re.search(r'Weekly video_(\d{8})', raw)
        video_compact = m_video_date.group(1) if m_video_date else ''

        m_title = re.search(r'Title slide:\s*([^\n]+)', raw)
        if not m_title:
            m_title = re.search(r'\n([A-Z][^\n]{8,120})\n\n(?:To view this video|Transcript|Weekly video_)', raw)

        title = m_title.group(1).strip() if m_title else ''
        title = re.sub(r'[*_\s]+$', '', title).strip()
        if not title:
            raise ValueError('blackrock_weekly title not found')

        pdf_link = ''
        if video_compact:
            exact_pdf = re.search(
                rf'\[Download full commentary \(PDF\)\]\((https://www\.blackrock\.com/corporate/literature/market-commentary/weekly-investment-commentary-en-us-{video_compact}-[^)]+\.pdf)\)',
                raw,
            )
            if exact_pdf:
                pdf_link = exact_pdf.group(1).strip()
            else:
                exact_pdf = re.search(
                    rf'(https://www\.blackrock\.com/corporate/literature/market-commentary/weekly-investment-commentary-en-us-{video_compact}-[^\s)\]]+\.pdf)',
                    raw,
                )
                if exact_pdf:
                    pdf_link = exact_pdf.group(1).strip()

        if not pdf_link:
            m_pdf = re.search(r'\[Download full commentary \(PDF\)\]\((https://www\.blackrock\.com/corporate/literature/market-commentary/weekly-investment-commentary-en-us-[^)]+\.pdf)\)', raw)
            if not m_pdf:
                m_pdf = re.search(r'https://www\.blackrock\.com/corporate/literature/market-commentary/weekly-investment-commentary-en-us-[^\s)\]]+\.pdf', raw)
            pdf_link = (m_pdf.group(1) if m_pdf and m_pdf.lastindex else m_pdf.group(0)).strip() if m_pdf else self.config['source']['url']

        date_str = ''
        if video_compact:
            try:
                date_str = datetime.strptime(video_compact, '%Y%m%d').strftime('%b %d, %Y')
            except Exception:
                date_str = ''
        if not date_str:
            m_pdf_date = re.search(r'weekly-investment-commentary-en-us-(\d{8})-', pdf_link)
            compact = m_pdf_date.group(1) if m_pdf_date else ''
            if compact:
                try:
                    date_str = datetime.strptime(compact, '%Y%m%d').strftime('%b %d, %Y')
                except Exception:
                    date_str = ''
        if not date_str:
            m_date = re.search(r'\b([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\b', raw)
            date_str = m_date.group(1).strip() if m_date else ''

        raw_html = requests.get(
            self.config['source']['url'],
            headers={'User-Agent': 'Mozilla/5.0 (compatible; WebToRSS/1.0)'},
            timeout=30,
        ).text
        soup = BeautifulSoup(raw_html, 'html.parser')

        meta_title = soup.find('meta', attrs={'name': 'articleTitle'})
        if meta_title and (meta_title.get('content') or '').strip():
            title = (meta_title.get('content') or '').strip()
        meta_summary = soup.find('meta', attrs={'name': 'pageSummary'})

        body_tabs = soup.find(attrs={'data-componentname': re.compile(r'^Body Tabs$', re.I)})
        if not body_tabs:
            raise ValueError('blackrock_weekly body tabs not found')
        tab0 = body_tabs.find_next('div', attrs={'data-tab-id': '0'})
        tab0_items = [x.strip() for x in (tab0.get_text(' ', strip=True).split(',') if tab0 else []) if x.strip()]
        wrap = body_tabs.find_parent('div', class_='ls-cmp-wrap')
        siblings = []
        if wrap and tab0_items:
            for sib in wrap.find_next_siblings('div', class_='ls-cmp-wrap'):
                comp = sib.find(attrs={'data-componentname': True})
                comp_name = (comp.get('data-componentname') or '').strip().lower() if comp else ''
                if comp_name in {'paragraph', 'image'}:
                    siblings.append(sib)
                if len(siblings) >= len(tab0_items):
                    break
        if not siblings:
            raise ValueError('blackrock_weekly body components not found')

        content_parts: List[str] = []
        description = (meta_summary.get('content') or '').strip() if meta_summary else ''

        hero_bullets = []
        hero_titles_seen = set()
        week_ahead_seen_in_hero = False
        for bullet in soup.select('div.key-points div.bullet'):
            title_el = bullet.select_one('div.bullet-title span')
            body_el = bullet.select_one('div.bullet-summary p')
            btitle = re.sub(r'\s+', ' ', title_el.get_text(' ', strip=True)).strip() if title_el else ''
            bbody = re.sub(r'\s+', ' ', body_el.get_text(' ', strip=True)).strip() if body_el else ''
            if btitle or bbody:
                hero_bullets.append((btitle, bbody))
                if btitle:
                    hero_titles_seen.add(btitle.lower())
                    if btitle.lower() == 'week ahead':
                        week_ahead_seen_in_hero = True

        intro_text = ''
        download_cta = soup.find('a', attrs={'aria-label': re.compile(r'Download full commentary', re.I)})
        if not download_cta:
            download_cta = soup.find('a', string=re.compile(r'Download full commentary', re.I))
        if download_cta:
            para_wrap = download_cta.find_parent('div', class_=re.compile(r'para-content', re.I))
            if para_wrap:
                for p in para_wrap.find_all('p'):
                    text = re.sub(r'\s+', ' ', p.get_text(' ', strip=True)).strip()
                    if not text:
                        continue
                    if 'Download full commentary' in text:
                        continue
                    intro_text = text
                    break

        seed_blocks = []
        seed_blocks.append((f'<p><strong>{html.escape(title)}</strong></p>', title))
        for bullet_title, bullet_text in hero_bullets[:3]:
            if bullet_title:
                seed_blocks.append((f'<p><strong>{html.escape(bullet_title)}</strong></p>', bullet_title))
            if bullet_text:
                seed_blocks.append((f'<p>{html.escape(bullet_text)}</p>', bullet_text))
        if intro_text:
            seed_blocks.append((f'<p>{html.escape(intro_text)}</p>', intro_text))
            description = intro_text

        seen_norm = set()

        def _push_html(block_html: str, text_for_key: str = '', force: bool = False):
            key = re.sub(r'\s+', ' ', html.unescape(text_for_key or block_html)).strip().lower()
            if not key:
                return
            if (not force) and key in seen_norm:
                return
            seen_norm.add(key)
            content_parts.append(block_html)

        content_parts = []
        for block_html, key in seed_blocks:
            _push_html(block_html, key)

        def _is_chart_label(elem, text: str) -> bool:
            if elem.name != 'p':
                return False
            if elem.find('span', class_=re.compile(r'text-sm', re.I)):
                return True
            low = text.lower()
            if 'share of energy imports' in low and 'energy import dependence' in low:
                return True
            return False

        def _append_image(img_tag):
            if not img_tag:
                return
            src = (img_tag.get('data-src') or img_tag.get('src') or '').strip()
            if not src:
                return
            if src.startswith('/'):
                src = 'https://www.blackrock.com' + src
            _push_html(f'<p><img src="{html.escape(src)}" alt="" /></p>', src)

        deferred_blocks = []

        for sib in siblings:
            comp = sib.find(attrs={'data-componentname': True})
            comp_name = (comp.get('data-componentname') or '').strip() if comp else ''
            sib_text = re.sub(r'\s+', ' ', sib.get_text(' ', strip=True)).strip().lower()
            if sib_text.startswith('read our past weekly market commentaries'):
                break
            if 'big calls' in sib_text or 'tactical granular views' in sib_text:
                break

            if comp_name.lower() == 'paragraph':
                for elem in sib.find_all(['h2', 'p', 'img']):
                    if elem.name == 'img':
                        _append_image(elem)
                        continue
                    if 'footnotes' in ((elem.get('class') or [])):
                        continue
                    text = elem.get_text(' ', strip=True)
                    text = re.sub(r'\s+', ' ', text).strip()
                    if not text:
                        continue
                    if _is_chart_label(elem, text):
                        label_span = elem.find('span', class_=re.compile(r'text-sm', re.I)) if elem.name == 'p' else None
                        label_text = re.sub(r'\s+', ' ', label_span.get_text(' ', strip=True)).strip() if label_span else ''
                        if label_text:
                            _push_html(f'<p><strong>{html.escape(label_text)}</strong></p>', label_text)
                            rest = text.replace(label_text, '', 1).strip(' :-–—')
                            if rest:
                                _push_html(f'<p>{html.escape(rest)}</p>', rest)
                        elif 'share of energy imports' in text.lower() and 'energy import dependence' in text.lower():
                            _push_html(f'<p>{html.escape(text)}</p>', text)
                        continue
                    if text.lower().startswith('read our past weekly market'):
                        continue
                    if text.startswith('Source:') or text.startswith('Sources:'):
                        continue
                    if text.startswith('Past performance is not a reliable indicator'):
                        continue
                    if elem.name == 'h2':
                        force_heading = text.lower() in hero_titles_seen
                        _push_html(f'<p><strong>{html.escape(text)}</strong></p>', text, force=force_heading)
                    else:
                        if not description and len(text) > 120:
                            description = text
                        _push_html(f'<p>{html.escape(text)}</p>', text)
            elif comp_name.lower() == 'image':
                local_blocks = []
                img = sib.find('img')
                if img:
                    src = (img.get('data-src') or img.get('src') or '').strip()
                    if src:
                        if src.startswith('/'):
                            src = 'https://www.blackrock.com' + src
                        local_blocks.append((f'<p><img src="{html.escape(src)}" alt="" /></p>', src, False))
                heading = sib.find('h2')
                heading_text = ''
                if heading:
                    heading_text = re.sub(r'\s+', ' ', heading.get_text(' ', strip=True)).strip()
                    if heading_text:
                        local_blocks.append((f'<p><strong>{html.escape(heading_text)}</strong></p>', heading_text, heading_text.lower() in hero_titles_seen))
                seen_local_week_items = set()
                for elem in sib.find_all(['span', 'p']):
                    classes = ' '.join(elem.get('class') or [])
                    if 'fa ' in classes or classes.startswith('fa') or 'pseudo-mask' in classes:
                        continue
                    if 'footnotes' in classes:
                        continue
                    text = re.sub(r'\s+', ' ', elem.get_text(' ', strip=True)).strip()
                    if not text:
                        continue
                    if text.startswith('Source:') or text.startswith('Sources:'):
                        continue
                    if text.startswith('Past performance is not a reliable indicator'):
                        continue
                    if heading_text.lower() == 'week ahead' and week_ahead_seen_in_hero:
                        if re.fullmatch(r'April\s+\d{1,2}(?:-\d{1,2})?', text) or ';' in text or 'U.S.' in text or 'China ' in text:
                            if text in seen_local_week_items:
                                continue
                            seen_local_week_items.add(text)
                    local_blocks.append((f'<p>{html.escape(text)}</p>', text, False))
                if heading_text.lower() == 'week ahead':
                    deferred_blocks.extend(local_blocks)
                else:
                    for block_html, key, force in local_blocks:
                        _push_html(block_html, key, force=force)

        for block_html, key, force in deferred_blocks:
            _push_html(block_html, key, force=force)

        if not content_parts:
            raise ValueError('blackrock_weekly content empty')

        content_html = ''.join(content_parts)
        if pdf_link:
            content_html = re.sub(
                r'https://www\.blackrock\.com/corporate/literature/market-commentary/weekly-investment-commentary-en-us-[^\"\s<]+\.pdf',
                pdf_link,
                content_html,
            )
        if not description:
            description = title
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

    def _extract_pitchbook_report_blocks(self, markdown_text: str) -> List[Dict[str, str]]:
        blocks = re.findall(
            r'^\*\s+\[(.*?)\]\((https://pitchbook\.com/news/reports/[^)]+)\)',
            markdown_text,
            re.M,
        )

        items: List[Dict[str, str]] = []
        seen = set()
        stop_titles = {
            'Private Market Benchmarks Reports',
            'Venture Monitor Reports',
            'Private Equity Reports',
            'M&A Reports',
            'Fundraising Reports',
            'Fund Performance Reports',
            'Credit Reports',
            'Private Debt Reports',
        }

        for block_text, link in blocks:
            if 'Research ###' not in block_text:
                continue
            clean = re.sub(r'!\[[^\]]*\]\([^)]+\)\s*', '', block_text).strip()
            clean = re.sub(r'^Research\s+###\s+', '', clean)
            clean = re.sub(r'\s+Learn more.*$', '', clean).strip()
            clean = re.sub(r'\s+', ' ', clean).strip()
            if not clean:
                continue

            m_date = re.search(r'([A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})\s*$', clean)
            date_str = m_date.group(1) if m_date else ''
            prefix = clean[:m_date.start()].strip() if m_date else clean

            m_title = re.match(r'(.+?)\s+(The|Our|This|These|A|An)\s+(.+)$', prefix)
            if m_title:
                title = m_title.group(1).strip()
                desc = f"{m_title.group(2)} {m_title.group(3).strip()}"
            else:
                title = prefix.strip()
                desc = title

            title = re.sub(r'\s+', ' ', title).strip()
            desc = re.sub(r'\s+', ' ', desc).strip()
            if title in stop_titles:
                continue
            if not title or not link or link in seen:
                continue
            if not date_str:
                continue
            seen.add(link)
            items.append({
                'title': title,
                'link': link,
                'description': desc or title,
                'date_str': date_str,
            })
        return items

    def _extract_natixis_listing_items(self, limit: int = 50) -> List[Dict[str, str]]:
        endpoint = 'https://www.im.natixis.com/content/natixis/us/en/insights/jcr:content/root/container/listing_searchfilter.filter.json'
        resp = requests.get(endpoint, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}, timeout=30)
        resp.raise_for_status()
        payload = resp.json()

        items = []
        for item in payload[:limit]:
            if not isinstance(item, dict):
                continue
            url = str(item.get('url') or '').strip()
            title = str(item.get('title') or '').strip()
            if not url.startswith('/en-us/insights/'):
                continue
            if not title:
                continue

            full_link = f"https://www.im.natixis.com{url}"
            description = str(item.get('description') or '').strip()
            date_str = str(item.get('formattedPublishedDate') or item.get('formattedArticleDate') or '').strip()
            image = str(item.get('image') or '').strip()
            if image and image.startswith('/'):
                image = f"https://www.im.natixis.com{image}"

            item_data = {
                'title': title,
                'url': full_link,
                'description': description,
                'date_str': date_str,
                'image': image,
            }
            items.append(item_data)

        return items

    def _generate_yardeni_morning_briefing(self, markdown_text: str, public_base: str = "") -> str:
        ch = self.config["output"]["channel"]
        builder = RSSBuilder(ch["title"], ch["link"], ch["description"])

        settings = self.config.get("settings", {})
        max_items = int(settings.get("max_items", 30))
        feed_name = self.config.get("name", "yardeni_morning_briefing")
        public_base = (public_base or "").rstrip('/')

        section = markdown_text.split('## Morning Briefing', 1)[1] if '## Morning Briefing' in markdown_text else markdown_text
        links = re.findall(
            r'\[(Morning Briefing [A-Za-z]+ \d{1,2}, \d{4} ## .*?)\]\((https?://yardeni\.com/research/morning-briefing/[^)]+)\)',
            section,
            re.S,
        )

        item_cache: Dict[str, Dict[str, str]] = {}
        seen = set()
        count = 0

        for raw_text, link in links:
            slug = link.rstrip('/').split('/')[-1]
            guid = self._md5(f'yardeni|{slug}')
            if guid in seen:
                continue
            seen.add(guid)

            m_date = re.search(r'Morning Briefing ([A-Za-z]+ \d{1,2}, \d{4})', raw_text)
            date_str = m_date.group(1) if m_date else None
            title = slug.replace('-', ' ').strip().title()
            title = re.sub(r'\bUs\b', 'US', title)
            title = re.sub(r'\bAi\b', 'AI', title)
            title = re.sub(r'\bCpi\b', 'CPI', title)
            title = re.sub(r'\bPce\b', 'PCE', title)
            title = re.sub(r'\bPe\b', 'PE', title)

            desc = raw_text
            if '##' in desc:
                desc = desc.split('##', 1)[1].strip()
            desc = re.sub(r'\s+', ' ', desc).strip()
            if len(desc) > 600:
                desc = desc[:600].rsplit(' ', 1)[0].strip() + '...'
            if not desc:
                desc = title

            local_link = link

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

        parse_mode = self.config.get("settings", {}).get("parse_mode")

        page_html = None
        if not force:
            page_html = self._load_cache(src)

        prefer_raw = bool(self.config.get("source", {}).get("prefer_raw", False))
        if parse_mode in {"blackrock_weekly_commentary", "blackrock_weekly_single"}:
            prefer_raw = True

        if page_html is None or force:
            page_html = self._fetch_html(src, headers=headers, prefer_raw=prefer_raw)
            self._save_cache(src, page_html)
        if parse_mode == "blackrock_weekly_single":
            xml = self._generate_blackrock_weekly(page_html, src)
            out_file = self.config.get("output_file")
            if out_file:
                p = self.out_dir / out_file
                p.write_text(xml, encoding="utf-8")
                print(f"[WebToRSS] saved: {p}")
            return xml


        ch = self.config["output"]["channel"]
        settings = self.config.get("settings", {})
        if parse_mode == "natixis_listing_json":
            items = self._extract_natixis_listing_items(int(settings.get("max_items", 50)))
            builder = RSSBuilder(ch["title"], ch["link"], ch["description"])
            seen = set()
            for item in items:
                title = item.get("title", "")
                if not title:
                    continue
                link = item.get("url", "")
                if not link:
                    continue
                description = item.get("description", "")
                date_str = item.get("date_str", "")
                guid = self._md5(f"{title}|{link}")
                if guid in seen:
                    continue
                seen.add(guid)

                if date_str:
                    pub_date = None
                    for fmt in ["%B %d, %Y", "%b %d, %Y", "%B %d, %Y", "%b. %Y", "%Y-%m-%d"]:
                        try:
                            dt = datetime.strptime(date_str, fmt)
                            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
                            break
                        except Exception:
                            continue
                    if pub_date is None:
                        pub_date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
                else:
                    pub_date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

                # Natixis: 交给 reader 回源抓正文，避免 feed 内再塞正文
                content_html = ""

                builder.add_item(
                    title=title,
                    link=link,
                    description=description or title,
                    pub_date=pub_date,
                    guid=guid,
                    content_html=content_html,
                )

            out_file = self.config.get("output_file")
            xml = builder.to_xml()
            if out_file:
                p = self.out_dir / out_file
                p.write_text(xml, encoding="utf-8")
                print(f"[WebToRSS] saved: {p}")
            print(f"[WebToRSS] matched=natixis emitted={len(seen)}")
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
                guid=item['guid'],
                content_html=item['content_html']
            )
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
                parsed_blocks = self._extract_pitchbook_report_blocks(page_html)
                if idx >= len(parsed_blocks):
                    continue
                block = parsed_blocks[idx]
                title_raw = block['title']
                link = block['link']
                raw_link = link
                desc = block['description']
                date_str = block['date_str'] or None
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
