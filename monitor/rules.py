"""热点事件：判定阈值与推送格式。

所有阈值写在这里，实推之后直接改这里，不用动算法代码。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

CST = timezone(timedelta(hours=8))

# ---- 推送条件（Notion「5、热点事件」定稿）----
MIN_PLATFORMS = 3          # 同一件事至少在 3 个平台出现才算热点
MAX_ITEMS = 10             # 一轮最多推 10 条
HOT_WINDOW_HOURS = 12     # 只看最近这么多小时的内容（实时的，不要历史的）
DEDUP_RETENTION_DAYS = 7   # 推过的事件保留多久不重复推

# ---- 中文过滤（看不懂英文，英文坚决不推）----
CHINESE_MIN_RATIO = 0.30   # 标题里中文字符占有效字符的比例下限
CHINESE_MIN_CHARS = 2      # 至少要有几个中文字符
CHINESE_STRONG_CHARS = 6   # 中文字符够多就算中文（夹带大量英文产品名也不误杀）

# ---- 跨平台比对（标题实词聚类 + 专有名词匹配）----
SIM_THRESHOLD = 0.40       # 两条标题相似度达到这个值才算同一件事（实推校准过）
ANCHOR_MIN_LEN = 4         # 共用的中文关键短语至少几个字
ANCHOR_MIN_BIGRAMS = 3     # 只有 3 字短语时，还要共同这么多实词才算锚点
ASCII_RARE_DF = 24         # 英文词在语料里出现超过这么多次就不算专有名词
RARE_TOKEN_DF = 8          # 文档频率高于这个数的词当通用词，不做专有名词锚点
NUM_JOIN_THRESHOLD = 0.55  # 数值加单位（金额、百分比、版本）重合到这个比例算命中锚点

# ---- 题材过滤（可随时关掉，改这两个开关即可）----
FILTER_MARKET_QUOTE = True   # 行情报价类快讯（收盘价、涨跌幅）不算新闻事件
FILTER_NON_NEWS = True       # 体育、娱乐、追星、电竞不算「天下大事」

# ---- 运行参数 ----
FETCH_TIMEOUT = 20.0
FETCH_RETRIES = 1
MAX_WORKERS = 6
TITLE_MIN_LEN = 6          # 太短的标题判不出事件

# ---- 推送格式 ----
# 一条一行：时间｜标题｜原文地址｜平台数（平台名）｜最高热度
MESSAGE_INCLUDE_EVIDENCE = True
TIME_FORMAT = "%m-%d %H:%M"


def format_time(ms: Optional[int]) -> str:
    """毫秒时间戳 → 北京时间文本。"""
    if not ms:
        return ""
    try:
        stamp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CST)
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.strftime(TIME_FORMAT)


def format_heat(heat: Optional[int]) -> str:
    if not heat:
        return ""
    if heat >= 100_000_000:
        return f"{heat / 100_000_000:.1f}亿"
    if heat >= 10_000:
        return f"{heat / 10_000:.0f}万"
    return str(heat)


def format_event_line(event) -> str:
    """一条热点一行：时间｜标题｜原文地址｜N个平台（平台名）｜最高热度。"""
    title = event.title.replace("\n", " ").strip()
    parts = [format_time(event.event_ms) or "时间未知", title, event.url]
    if MESSAGE_INCLUDE_EVIDENCE:
        parts.append(f"{len(event.platforms)}个平台（{'/'.join(event.platforms)}）")
        heat = format_heat(event.top_heat)
        if heat:
            parts.append(f"热度{heat}")
    return "｜".join(parts)