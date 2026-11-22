"""标题语言判断与「同一件事」判定。

两件事：
1. chinese_stats / is_chinese：标题里中文字符占比，纯英文挡掉（全球热点也要中文表述）。
2. similarity：判断两条标题是不是同一件事。
   做法＝标题实词聚类 + 专有名词匹配，全部自己算，不调外部语义服务：
   - 中文按字符对齐相似度（difflib 的匹配块，快速实现），长公共子串＝关键短语/专有名词。
   - 英文/缩写按词集合重合（APEC、SpaceX、iPhone 这类品牌、机构、人名缩写）。
   - 数字＋单位按重合（金额、百分比、版本号）。
   - 中文二元组集合重合（实词层面的相似度）。
   命中条件：相似度过阈值，且至少有一个「专有名词锚点」（共用关键短语、共有缩写词或共有数值）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import List, Set, Tuple

from . import rules

CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.+#\-_][A-Za-z0-9]+)*")
# 只认「数字 + 单位/百分比」，不认日期里的 11、18、2026 这种
NUMBER_UNIT_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|％|万亿|亿|万|千|百|元|美元|欧元|港元|人民币|点|倍|人|家|"
    r"台|辆|架|枚|艘|架次|吨|公里|米|小时|分钟|秒|岁|款|次|座|间|支|根|件|条|页|位|名)"
)
# 日期、时间、序号等噪音
DATE_RE = re.compile(
    r"\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{1,2}[:：]\d{2}|\d{4}年|"
    r"\d{1,2}月|第\d+季|Q[1-4]"
)
# 源自带前缀（每家格式不同，统一剥掉）
PREFIX_RE = re.compile(
    r"^(财联社|快讯|独家|独家消息|突发|刚刚|最新消息|视频|直播|海报|组图|专访|发布会|"
    r"焦点|要闻|头条|置顶|热议|辟谣|通报|官方)[：:，,、\s]*"
)
# 通用词：这些词单独出现没有信息量，不能当专有名词锚点
GENERIC_PHRASES = [
    "官方", "回应", "发布", "宣布", "表示", "记者", "报道", "消息", "据称", "据悉",
    "网传", "通报", "回应称", "独家", "视频", "直播", "专访", "最新", "消息人士",
    "相关", "问题", "情况", "目前", "今天", "昨日", "明日", "本周", "今日", "时间",
    "方式", "之后", "之前", "正式", "计划", "正在", "已经", "可能", "首个", "首次",
    "我国", "他们", "我们", "自己", "一个", "这个", "那个", "什么", "如何", "为何",
    "为什么", "怎么办", "回应了", "方面", "进行", "推出", "带来", "受到", "获得",
    "工作", "活动", "服务", "产品", "系统", "平台", "公司", "问题", "事件", "内容",
    "空间", "网友", "网友说", "知乎", "微博", "抖音", "百度", "视频号", "热榜",
    "重磅", "独家", "关注", "解读", "盘点", "汇总", "一览", "全网", "网友热议",
]
GENERIC_TOKENS: Set[str] = set()
for _phrase in GENERIC_PHRASES:
    for _size in (2, 3, 4):
        for _i in range(len(_phrase) - _size + 1):
            GENERIC_TOKENS.add(_phrase[_i:_i + _size])
GENERIC_TOKENS.add("财联社")

ASCII_GENERIC = {
    "the", "and", "for", "with", "from", "that", "this", "has", "have", "will",
    "news", "live", "video", "official", "top", "hot", "today", "update", "report",
    "says", "said", "after", "over", "into", "about", "more", "new", "now",
}

# 行情模板：报价类快讯（收盘价、涨跌幅、结算价……）。不是新闻大事，不推。
MARKET_QUOTE_PATTERNS = [
    re.compile(r"(收盘|收涨|收跌|收报|结算价|夜盘|盘中|涨跌幅|涨幅|跌幅|成交额|持仓量|交投|报价)"),
    re.compile(r"(期货|指数|股价|合约|汇率|现货|大宗商品|原油|黄金|铜价|人民币|美元兑)"),
]

# 非新闻题材：体育、娱乐、追星、电竞。同一个事件平台再多也不是「天下大事」。
NON_NEWS_PATTERNS = [
    re.compile(r"(国足|球队|联赛|球员|季后赛|夺冠|冠军|球衣|世界杯|亚冠|中超|CBA|NBA|"
               r"英雄联盟|LPL|KPL|电竞|决赛|半决赛|首发阵容|进球|射手)"),
    re.compile(r"(演唱会|综艺|明星|恋情|分手|官宣|电影|票房|短剧|选秀|网红|粉丝|爱豆|"
               r"cp|出道|新剧|追剧)"),
]


@dataclass
class TitleInfo:
    """标题的规范化表示。"""

    raw: str
    cjk: str = ""
    ascii_tokens: Set[str] = field(default_factory=set)
    numbers: Set[str] = field(default_factory=set)
    bigrams: Set[str] = field(default_factory=set)
    trigrams: Set[str] = field(default_factory=set)
    cjk_count: int = 0
    chinese_ratio: float = 0.0
    chinese: bool = False

    @property
    def tokens(self) -> Set[str]:
        return self.ascii_tokens | self.numbers | self.trigrams


def _strip_boilerplate(title: str) -> str:
    text = (title or "").strip()
    text = PREFIX_RE.sub("", text)
    text = DATE_RE.sub("", text)
    return text


def chinese_stats(title: str) -> Tuple[int, float]:
    """返回（中文字符数，中文字符占有效字符比例）。"""
    text = title or ""
    cjk = len(CJK_RE.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    digits = len(re.findall(r"\d", text))
    total = cjk + latin + digits
    ratio = (cjk / total) if total else 0.0
    return cjk, ratio


def is_chinese(title: str) -> bool:
    """纯英文挡掉；中文表述（哪怕夹带英文产品名）放行。"""
    cjk, ratio = chinese_stats(title)
    if cjk < rules.CHINESE_MIN_CHARS:
        return False
    return ratio >= rules.CHINESE_MIN_RATIO or cjk >= rules.CHINESE_STRONG_CHARS


def is_market_quote(title: str) -> bool:
    """行情报价类快讯：收盘价、涨跌幅、结算价这种，不是新闻事件。"""
    text = title or ""
    return any(pattern.search(text) for pattern in MARKET_QUOTE_PATTERNS[:1]) and \
        any(pattern.search(text) for pattern in MARKET_QUOTE_PATTERNS[1:])


def is_non_news_topic(title: str) -> bool:
    """体育、娱乐、追星、电竞：不是「天下大事」。"""
    text = title or ""
    return any(pattern.search(text) for pattern in NON_NEWS_PATTERNS)


def build(title: str) -> TitleInfo:
    """把标题拆成可比较的成分。"""
    info = TitleInfo(raw=title or "")
    text = _strip_boilerplate(title)
    cjk_chars = CJK_RE.findall(text)
    info.cjk = "".join(cjk_chars)
    info.cjk_count = len(cjk_chars)
    info.ascii_tokens = {t.lower() for t in ASCII_TOKEN_RE.findall(text)
                         if len(t) >= 2 and t.lower() not in ASCII_GENERIC}
    info.numbers = {m.group(0).replace(" ", "") for m in NUMBER_UNIT_RE.finditer(text)}
    for size, bucket in ((2, info.bigrams), (3, info.trigrams)):
        for i in range(len(info.cjk) - size + 1):
            gram = info.cjk[i:i + size]
            if gram in GENERIC_TOKENS:
                continue
            bucket.add(gram)
    info.chinese_count, info.chinese_ratio = chinese_stats(title)
    info.chinese = is_chinese(title)
    return info


def _overlap(left: Set[str], right: Set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _matching_blocks(left: str, right: str):
    if not left or not right:
        return []
    return SequenceMatcher(None, left, right, autojunk=False).get_matching_blocks()


@dataclass
class Match:
    score: float = 0.0
    anchor: str = ""       # 共用的关键短语（专有名词锚点）
    anchor_kind: str = ""  # cjk / ascii / number
    align: float = 0.0     # 中文对齐相似度
    lcs_len: int = 0       # 最长公共中文子串长度
    lcs_text: str = ""
    shared_ascii: Set[str] = field(default_factory=set)
    shared_numbers: Set[str] = field(default_factory=set)
    shared_bigrams: Set[str] = field(default_factory=set)


def similarity(left: TitleInfo, right: TitleInfo) -> Match:
    """两条标题的相似度，以及判定用的专有名词锚点。"""
    match = Match()
    if left.cjk and right.cjk:
        blocks = _matching_blocks(left.cjk, right.cjk)
        matched = sum(block.size for block in blocks)
        align = 2.0 * matched / (len(left.cjk) + len(right.cjk))
        longest = max(blocks, key=lambda b: b.size)
        lcs_len = longest.size
        lcs_text = left.cjk[longest.a:longest.a + longest.size] if longest.size else ""
    else:
        align, lcs_len, lcs_text = 0.0, 0, ""

    ascii_ov = _overlap(left.ascii_tokens, right.ascii_tokens)
    number_ov = _overlap(left.numbers, right.numbers)
    bigram_ov = _overlap(left.bigrams, right.bigrams)

    score = (0.40 * align
             + 0.20 * ascii_ov
             + 0.10 * number_ov
             + 0.15 * bigram_ov
             + 0.15 * min(1.0, lcs_len / 6.0))

    match.score = round(score, 4)
    match.shared_ascii = left.ascii_tokens & right.ascii_tokens
    match.shared_numbers = left.numbers & right.numbers
    match.shared_bigrams = left.bigrams & right.bigrams
    match.align = align
    match.lcs_len = lcs_len
    match.lcs_text = lcs_text

    if lcs_len >= rules.ANCHOR_MIN_LEN:
        match.anchor = lcs_text
        match.anchor_kind = "cjk"
    elif match.shared_ascii:
        match.anchor = sorted(match.shared_ascii)[0]
        match.anchor_kind = "ascii"
    elif len(match.shared_numbers) >= 2:
        match.anchor = sorted(match.shared_numbers)[0]
        match.anchor_kind = "number"
    return match


def has_anchor(match: Match) -> bool:
    """有没有专有名词锚点。没有锚点的相似多半只是套话，不算同一件事。"""
    if match.anchor_kind == "cjk":
        return True
    if match.anchor_kind == "ascii":
        return True
    return len(match.shared_numbers) >= 2


def same_event(left: TitleInfo, right: TitleInfo, threshold: float = rules.SIM_THRESHOLD) -> bool:
    match = similarity(left, right)
    return match.score >= threshold and has_anchor(match)