"""newsnow 源清单：只挑新闻类进比对池。

生活类（虎扑、贴吧、豆瓣、雪球）与纯科技社区不进池：用户目的是「了解一下有什么大事」，
生活热帖混进来会干扰跨平台比对。
36氪：newsnow 请求失败（实测 HTTP 500），先不纳入，等接口恢复再加回。

platform 用来算「几个平台」：同一个平台的多个源（财联社电报/热榜/深度、
华尔街见闻/热榜/快讯）只算一个平台，避免一家自己的多条把「跨平台」凑出来。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Source:
    id: str          # newsnow 源 id
    platform: str    # 平台标识
    label: str       # 中文名
    kind: str = "news"   # hot 热榜 / news 快讯新闻
    verified_at: str = "2026-11-18"
    enabled: bool = True
    note: str = ""


SOURCES: List[Source] = [
    Source("zhihu", "zhihu", "知乎", "hot"),
    Source("toutiao", "toutiao", "今日头条", "hot"),
    Source("weibo", "weibo", "微博", "hot"),
    Source("douyin", "douyin", "抖音", "hot"),
    Source("baidu", "baidu", "百度热搜", "hot"),
    Source("bilibili-hot-search", "bilibili", "B站热搜", "hot"),
    Source("cls-telegraph", "cls", "财联社电报", "news"),
    Source("cls-hot", "cls", "财联社热榜", "hot"),
    Source("cls-depth", "cls", "财联社深度", "news"),
    Source("wallstreetcn", "wallstreetcn", "华尔街见闻", "news"),
    Source("wallstreetcn-hot", "wallstreetcn", "华尔街见闻热榜", "hot"),
    Source("wallstreetcn-quick", "wallstreetcn", "华尔街见闻快讯", "news"),
    Source("tencent-hot", "tencent", "腾讯热榜", "hot"),
    Source("zaobao", "zaobao", "联合早报", "news"),
    Source("sputniknewscn", "sputnik", "卫星通讯社", "news"),
    Source("fastbull-express", "fastbull", "法布财经快讯", "news"),
    Source("fastbull-news", "fastbull", "法布财经新闻", "news"),
    Source("thepaper", "thepaper", "澎湃新闻", "news"),
    Source("ifeng", "ifeng", "凤凰网", "news"),
    Source("ithome", "ithome", "IT之家", "news"),
    Source("aihot", "aihot", "AIHOT", "news"),
]

# 明确不接的源（留档，防止以后误加）
EXCLUDED = {
    "hupu": "生活类（虎扑），不进比对池",
    "tieba": "生活类（贴吧），不进比对池",
    "douban": "生活类（豆瓣），不进比对池",
    "xueqiu": "生活类（雪球），不进比对池",
    "36kr": "newsnow 请求失败（实测 HTTP 500），等接口恢复再加回",
}


def active_sources() -> List[Source]:
    return [src for src in SOURCES if src.enabled]


def platforms() -> List[str]:
    seen: List[str] = []
    for src in active_sources():
        if src.platform not in seen:
            seen.append(src.platform)
    return seen