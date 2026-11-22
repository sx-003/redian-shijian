"""跨平台比对：把各平台的标题聚成「同一件事」，再按平台数判定是不是热点。

判定方法（自己算，不调外部语义服务）：
1. 每条标题先过中文过滤（纯英文挡掉）。
2. 用实词/专有名词建倒排索引，只对「有共同专有名词」的标题对算相似度，省时间。
3. 相似度 ≥ 阈值且至少有一个专有名词锚点（共用关键短语、共有缩写词、共有数值）→ 同一件事。
4. 聚类后再做一次内聚检查，把只是被通用词串起来的松散簇拆开。
5. 一个簇覆盖的平台数 ≥ 阈值才算热点；同一平台的多个源只算一个平台。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import lang, rules
from .newsnow import HotItem


@dataclass
class Event:
    """一个事件（同一件事在多个平台上的全部出现）。"""

    title: str = ""
    url: str = ""
    platforms: List[str] = field(default_factory=list)
    items: List[HotItem] = field(default_factory=list)
    event_ms: int = 0
    top_heat: Optional[int] = None
    anchor: str = ""
    score: float = 0.0

    @property
    def source_count(self) -> int:
        return len({item.source for item in self.items})

    def to_dict(self) -> Dict:
        return {
            "title": self.title, "url": self.url, "platforms": self.platforms,
            "source_count": self.source_count, "item_count": len(self.items),
            "event_ms": self.event_ms, "top_heat": self.top_heat,
            "anchor": self.anchor, "score": self.score,
            "appearances": [
                {"source": item.source, "label": item.label, "platform": item.platform,
                 "title": item.title, "url": item.url, "heat": item.heat,
                 "heat_text": item.heat_text, "published_ms": item.published_ms,
                 "updated_ms": item.updated_ms}
                for item in self.items
            ],
        }


@dataclass
class Prepared:
    item: HotItem
    info: lang.TitleInfo
    key: str          # 同源去重键


def _item_ms(item: HotItem) -> int:
    return item.published_ms or item.updated_ms or 0


def prepare(items: Sequence[HotItem], now_ms: int, window_hours: int = rules.HOT_WINDOW_HOURS) -> Tuple[List[Prepared], Dict[str, int]]:
    """中文过滤 + 题材过滤 + 时效过滤 + 同源去重。返回（可用条目，统计）。"""
    stats = {"total": len(items), "not_chinese": 0, "too_short": 0, "stale": 0,
             "duplicated": 0, "market_quote": 0, "non_news": 0}
    span_ms = window_hours * 3600 * 1000
    seen_per_source: Dict[str, Set[str]] = {}
    prepared: List[Prepared] = []
    for item in items:
        title = (item.title or "").strip()
        if len(title) < rules.TITLE_MIN_LEN:
            stats["too_short"] += 1
            continue
        info = lang.build(title)
        if not info.chinese:
            stats["not_chinese"] += 1
            continue
        if rules.FILTER_MARKET_QUOTE and lang.is_market_quote(title):
            stats["market_quote"] += 1
            continue
        if rules.FILTER_NON_NEWS and lang.is_non_news_topic(title):
            stats["non_news"] += 1
            continue
        stamp = _item_ms(item)
        if stamp and (now_ms - stamp > span_ms or stamp > now_ms + 10 * 60 * 1000):
            stats["stale"] += 1
            continue
        key = info.cjk + "|" + "|".join(sorted(info.ascii_tokens))
        bucket = seen_per_source.setdefault(item.source, set())
        if key in bucket:
            stats["duplicated"] += 1
            continue
        bucket.add(key)
        prepared.append(Prepared(item=item, info=info, key=key))
    return prepared, stats


def _candidate_pairs(prepared: Sequence[Prepared]) -> List[Tuple[int, int]]:
    """用稀有的专有名词建倒排索引，只返回可能相关的标题对。"""
    df: Dict[str, int] = {}
    for entry in prepared:
        for token in entry.info.tokens:
            df[token] = df.get(token, 0) + 1
    index: Dict[str, List[int]] = {}
    for idx, entry in enumerate(prepared):
        for token in entry.info.tokens:
            if df[token] <= rules.RARE_TOKEN_DF:
                index.setdefault(token, []).append(idx)
    pairs: Set[Tuple[int, int]] = set()
    for members in index.values():
        if len(members) < 2 or len(members) > rules.RARE_TOKEN_DF:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))
    return sorted(pairs)


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _split_loose(members: List[int], sims: Dict[Tuple[int, int], float],
                 threshold: float) -> List[List[int]]:
    """把松散簇拆成内聚的小簇：以连接度最高的点为种子，收拢与它足够像的成员。"""
    remaining = list(members)
    groups: List[List[int]] = []
    while remaining:
        degrees = {idx: sum(1 for other in remaining
                            if other != idx and sims.get((min(idx, other), max(idx, other)), 0.0) >= threshold)
                   for idx in remaining}
        seed = max(remaining, key=lambda idx: (degrees[idx], -idx))
        group = [idx for idx in remaining
                 if idx == seed or sims.get((min(seed, idx), max(seed, idx)), 0.0) >= threshold]
        groups.append(group)
        grouped = set(group)
        remaining = [idx for idx in remaining if idx not in grouped]
    return groups


def _anchor_ok(match: lang.Match, left: lang.TitleInfo, right: lang.TitleInfo,
               df_ascii: Dict[str, int], df_bigram: Dict[str, int]) -> bool:
    """语料级专有名词锚点判定。

    - 中文：最长公共子串 ≥4 字；只有 3 字时，要求还共同多个实词（二元组）。
    - 英文/缩写：共同 ≥2 个；只有 1 个时，该词在语料里不算烂大街，且两边英文词都不多。
    - 数值：共同 ≥2 个带单位的数值。
    这样「完成X轮融资」这类模板不会把两条不同新闻粘在一起。
    """
    if match.lcs_len >= rules.ANCHOR_MIN_LEN:
        return True
    if match.lcs_len == 3 and len(match.shared_bigrams) >= rules.ANCHOR_MIN_BIGRAMS:
        return True
    if len(match.shared_ascii) >= 2:
        return True
    if len(match.shared_ascii) == 1:
        token = next(iter(match.shared_ascii))
        if df_ascii.get(token, 0) <= rules.ASCII_RARE_DF and \
                min(len(left.ascii_tokens), len(right.ascii_tokens)) <= 2:
            return True
    if len(match.shared_numbers) >= 2:
        return True
    return False


def cluster(items: Sequence[HotItem], now_ms: int, *, min_platforms: int = rules.MIN_PLATFORMS,
            threshold: float = rules.SIM_THRESHOLD) -> Tuple[List[Event], List[Event], Dict[str, int]]:
    """返回（热点事件，按热度排序；差一点的热度；统计）。"""
    prepared, stats = prepare(items, now_ms)
    stats["prepared"] = len(prepared)
    if not prepared:
        return [], [], stats

    sims: Dict[Tuple[int, int], float] = {}
    df_ascii: Dict[str, int] = {}
    df_bigram: Dict[str, int] = {}
    for entry in prepared:
        for token in entry.info.ascii_tokens:
            df_ascii[token] = df_ascii.get(token, 0) + 1
        for gram in entry.info.bigrams:
            df_bigram[gram] = df_bigram.get(gram, 0) + 1
    for left, right in _candidate_pairs(prepared):
        match = lang.similarity(prepared[left].info, prepared[right].info)
        if match.score < threshold:
            continue
        if not _anchor_ok(match, prepared[left].info, prepared[right].info, df_ascii, df_bigram):
            continue
        sims[(left, right)] = match.score

    union = _UnionFind(len(prepared))
    for (left, right), score in sims.items():
        if score >= threshold:
            union.union(left, right)

    components: Dict[int, List[int]] = {}
    for idx in range(len(prepared)):
        components.setdefault(union.find(idx), []).append(idx)

    groups: List[List[int]] = []
    for members in components.values():
        groups.extend(_split_loose(members, sims, threshold))

    events: List[Event] = []
    near: List[Event] = []
    for group in groups:
        members = [prepared[idx] for idx in group]
        platforms: List[str] = []
        for entry in members:
            if entry.item.platform not in platforms:
                platforms.append(entry.item.platform)
        if len(platforms) < min_platforms - 1:
            continue
        event = _build_event(members, platforms)
        if len(platforms) >= min_platforms:
            events.append(event)
        else:
            near.append(event)

    events.sort(key=lambda e: (-len(e.platforms), -len(e.items),
                               -(e.top_heat or 0), -e.event_ms))
    stats["clusters"] = len(groups)
    stats["hot_events"] = len(events)
    stats["near_events"] = len(near)
    return events, near, stats


def _build_event(members: List[Prepared], platforms: List[str]) -> Event:
    titles: Dict[str, int] = {}
    for entry in members:
        titles[entry.item.title] = titles.get(entry.item.title, 0) + 1

    def rank(entry: Prepared) -> Tuple[int, int, int]:
        heat = entry.item.heat or 0
        duplicates = titles.get(entry.item.title, 1)
        return (duplicates, heat, -len(entry.item.title))

    best = max(members, key=rank)
    heats = [entry.item.heat for entry in members if entry.item.heat]
    stamps = [_item_ms(entry.item) for entry in members if _item_ms(entry.item)]
    event = Event(
        title=best.item.title,
        url=best.item.url,
        platforms=platforms,
        items=[entry.item for entry in members],
        event_ms=max(stamps) if stamps else 0,
        top_heat=max(heats) if heats else None,
    )
    anchor = ""
    for entry in members:
        for other in members:
            if entry is other:
                continue
            match = lang.similarity(entry.info, other.info)
            if match.score >= rules.SIM_THRESHOLD and lang.has_anchor(match) and match.anchor:
                anchor = match.anchor
                break
        if anchor:
            break
    event.anchor = anchor
    event.score = float(len(platforms)) * 10 + len(members)
    return event