"""热点事件监控：newsnow 公共接口取数（零第三方依赖）。

只走 newsnow 公共接口，不做直连兜底：
    https://newsnow.busiyi.world/api/s?id=<源id>

接口返回 {"status","id","updatedTime","items":[{id,title,url,extra:{info,hover}}]}，
个别源另外带 pubDate / extra.date（毫秒时间戳）。newsnow 挂了或某源失效，
那轮静默不出内容，不当故障告警、不刷屏。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import http

API_TEMPLATE = "https://newsnow.busiyi.world/api/s?id={source_id}"
DEFAULT_TIMEOUT = 20.0


@dataclass
class HotItem:
    """一条热榜/快讯条目。"""

    source: str = ""          # newsnow 源 id
    platform: str = ""        # 平台标识（同一平台多个源算一个平台）
    label: str = ""           # 源中文名
    title: str = ""
    url: str = ""
    heat: Optional[int] = None      # 热度数字（解析自 extra.info）
    heat_text: str = ""             # 热度原始文本
    summary: str = ""               # extra.hover
    published_ms: Optional[int] = None   # 条目自带时间戳（毫秒）
    updated_ms: Optional[int] = None     # 源列表更新时间（毫秒）
    rank: int = 0

    def to_dict(self) -> Dict:
        return {
            "source": self.source, "platform": self.platform, "label": self.label,
            "title": self.title, "url": self.url, "heat": self.heat,
            "heat_text": self.heat_text, "published_ms": self.published_ms,
            "updated_ms": self.updated_ms, "rank": self.rank,
        }


@dataclass
class SourceResult:
    """单个源的抓取结果。失败不抛异常，记状态即可。"""

    name: str
    label: str
    platform: str
    ok: bool = False
    status: str = ""          # ok / provider_error / blocked / missing / low_quality
    error: str = ""
    items: List[HotItem] = field(default_factory=list)
    updated_ms: Optional[int] = None
    duration_ms: int = 0

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "label": self.label, "platform": self.platform,
            "ok": self.ok, "status": self.status, "error": self.error[:200],
            "count": len(self.items), "updated_ms": self.updated_ms,
            "duration_ms": self.duration_ms,
        }


def _as_int(value) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_heat(info: str) -> Optional[int]:
    """把「395 万热度」「1.2亿热度」「409万」解析成整数热度。"""
    text = (info or "").replace(",", "").replace("，", "").strip()
    if not text:
        return None
    if "热度" not in text and "万" not in text and "亿" not in text:
        return None
    import re

    match = re.search(r"(\d+(?:\.\d+)?)\s*(万|亿|千)?", text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    unit = match.group(2) or ""
    if unit == "万":
        value *= 10_000
    elif unit == "亿":
        value *= 100_000_000
    elif unit == "千":
        value *= 1_000
    return int(value)


def parse_payload(payload, source) -> List[HotItem]:
    """解析 newsnow 返回体。字段缺失只丢条目，不让整源失败。"""
    if not isinstance(payload, dict):
        raise ValueError("返回体不是对象")
    raw_items = payload.get("items")
    if raw_items is None:
        raise ValueError("返回体缺少 items 字段")
    if not isinstance(raw_items, list):
        raise ValueError("items 不是数组")
    updated = _as_int(payload.get("updatedTime"))

    items: List[HotItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or raw.get("mobileUrl") or "").strip()
        if not title or not url:
            continue
        extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        info = str(extra.get("info") or "").strip()
        hover = str(extra.get("hover") or "").strip()
        published = _as_int(raw.get("pubDate"))
        if published is None:
            published = _as_int(extra.get("date"))
        items.append(HotItem(
            source=source.id, platform=source.platform, label=source.label,
            title=title, url=url, heat=parse_heat(info), heat_text=info,
            summary=hover, published_ms=published, updated_ms=updated,
        ))
    return items


def fetch_source(source, *, timeout: float = DEFAULT_TIMEOUT, retries: int = 1) -> SourceResult:
    """拉一个源的完整列表。每轮每源只调一次接口。"""
    result = SourceResult(name=source.id, label=source.label, platform=source.platform)
    url = API_TEMPLATE.format(source_id=source.id)
    fetch = http.fetch(url, timeout=timeout, retries=retries,
                       headers={"Accept": "application/json, text/plain, */*"})
    if not fetch.ok:
        result.status = fetch.error_type
        result.error = fetch.error
        return result
    try:
        payload = json.loads(fetch.data.decode("utf-8", "replace"))
        items = parse_payload(payload, source)
    except (ValueError, UnicodeDecodeError) as exc:
        result.status = "low_quality"
        result.error = f"解析失败: {exc}"
        return result
    result.ok = True
    result.status = "ok"
    result.items = items
    result.updated_ms = _as_int(payload.get("updatedTime"))
    result.duration_ms = fetch.duration_ms
    return result