"""热点事件监控入口：扫多平台热榜，跨平台比对，只推多个平台同时出现的中文热点。

python main.py --self-check                 离线自检
python main.py --fixtures tests/fixtures/sample.json --dry-run
python main.py --list-sources               列出全部源与平台归属
python main.py --dry-run --candidates        真实抓源；只打印不推送
python main.py --check-telegram             自查 Telegram 凭据（不发消息）
python main.py --send                       用户说「接推送」后才用
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor import cluster, lang, newsnow, rules, sources, state as state_mod, telegram  # noqa: E402


def _self_check() -> int:
    failures = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # 源清单：新闻类进池，生活类不进池，同平台多源算一个平台
    active = sources.active_sources()
    check(len(active) == 21, f"源条目应为 21，实际 {len(active)}")
    for banned in ("hupu", "tieba", "douban", "xueqiu"):
        check(all(src.id != banned for src in active), f"生活类源混进了比对池: {banned}")
    check(all(src.id != "36kr" for src in active), "36kr 请求失败，不该在池子里")
    check("36kr" in sources.EXCLUDED, "36kr 的排除原因没有留档")
    check(sources.platforms().count("cls") == 1, "财联社多个源应算一个平台")
    check(len({src.platform for src in active}) == 16, "平台数不对")

    # newsnow 接口解析
    payload = {
        "status": "cache", "id": "zhihu", "updatedTime": 1794945316764,
        "items": [
            {"id": "1", "title": "美商务部发布限制远程获取先进半导体相关措施 中方回应",
             "url": "https://www.zhihu.com/question/1",
             "extra": {"info": "395 万热度", "hover": "摘要"}},
            {"id": "2", "title": "US stocks rally as Fed signals rate cut",
             "url": "https://example.com/en", "extra": {}},
            {"id": "3", "title": "", "url": "https://example.com/empty", "extra": {}},
        ],
    }
    source = sources.Source("zhihu", "zhihu", "知乎", "hot")
    items = newsnow.parse_payload(payload, source)
    check(len(items) == 2, f"解析应保留 2 条，实际 {len(items)}")
    check(items[0].heat == 3_950_000, f"热度解析错误: {items[0].heat}")
    check(items[0].updated_ms == 1794945316764, "源更新时间没解析出来")
    check(items[0].summary == "摘要", "摘要没解析出来")
    check(newsnow.parse_heat("1.2亿热度") == 120_000_000, "亿单位热度解析错误")
    try:
        newsnow.parse_payload({"items": "x"}, source)
        check(False, "字段缺失时应当抛错")
    except ValueError:
        pass

    # 非中文标题过滤
    check(lang.is_chinese("美商务部发布限制远程获取先进半导体相关措施，中方回应"), "中文标题被挡")
    check(lang.is_chinese("小米回应雷军APEC发言紧张"), "夹英文的中文标题被挡")
    check(not lang.is_chinese("US stocks rally as Fed signals rate cut"), "纯英文没挡住")
    check(not lang.is_chinese("Tesla Model Y"), "短英文没挡住")
    check(not lang.is_chinese("APEC"), "纯缩写没挡住")

    # 跨平台比对：同一件事 ≥3 平台才算热点
    def make(source_id, platform, title, url, heat=None):
        return newsnow.HotItem(source=source_id, platform=platform, label=platform,
                               title=title, url=url, heat=heat, updated_ms=1794945316764)

    same = [
        make("zhihu", "zhihu", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://a/1"),
        make("cls-hot", "cls", "美商务部发布限制远程获取先进半导体措施 中方回应", "https://b/1"),
        make("tencent-hot", "tencent", "美商务部发布限制远程获取先进半导体相关措施，中方回应", "https://c/1"),
        make("weibo", "weibo", "美商务部发布限制远程获取先进半导体相关措施", "https://d/1"),
    ]
    events, near, stats = cluster.cluster(same, 1794945316764)
    check(len(events) == 1, f"同一件事应聚成 1 个热点，实际 {len(events)}")
    check(len(events[0].platforms) == 4, f"平台数应为 4，实际 {len(events[0].platforms)}")
    check(events[0].source_count == 4, "源数不对")

    three = same[:3]
    events1, near1, _ = cluster.cluster(three, 1794945316764)
    check(len(events1) == 1 and len(events1[0].platforms) == 3, "3 个平台应算热点")
    check(not near1, "3 个平台的事件不该进 near 列表")

    two = same[:2] + [make("weibo", "weibo", "小米回应雷军APEC发言紧张", "https://d/2")]
    events2, near2, _ = cluster.cluster(two, 1794945316764)
    check(not events2, "只有 2 个平台却判成热点")
    check(len(near2) == 1, "差一点的事件没有进 near 列表")

    same_platform = [
        make("cls-telegraph", "cls", "浑水做空英伟达", "https://a/1"),
        make("cls-hot", "cls", "浑水做空英伟达", "https://b/1"),
        make("cls-depth", "cls", "浑水做空英伟达", "https://c/1"),
    ]
    events3, _, _ = cluster.cluster(same_platform, 1794945316764)
    check(not events3, "同一平台多个源不该凑成 3 个平台")

    # 专有名词聚类
    info_a = lang.build("特朗普启程亚洲行，准备参与 APEC 会议")
    info_b = lang.build("特朗普启程亚洲行 将参加 APEC 会议")
    info_c = lang.build("小米回应雷军APEC发言紧张")
    check(lang.same_event(info_a, info_b), "同事件不同写法没认出来")
    check(not lang.same_event(info_a, info_c), "不同事件被误合并")
    check(lang.similarity(info_a, info_b).anchor != "", "没抽到专有名词锚点")
    check(not lang.has_anchor(lang.Match(score=0.9)), "没有锚点也算同一件事")

    # 10 条上限：15 件互不相同的事，每件 3 个平台
    def unique_chars(index: int) -> str:
        return "".join(chr(0x4E00 + index * 8 + offset) for offset in range(4))

    many = []
    for i in range(15):
        chars = unique_chars(i)
        for platform in ("zhihu", "cls", "tencent"):
            many.append(make(platform, platform, f"{chars}公司{chars}", f"https://x/{i}/{platform}"))
    events4, _, _ = cluster.cluster(many, 1794945316764)
    check(len(events4) == 15, f"应聚成 15 个事件，实际 {len(events4)}")
    selected = events4[:rules.MAX_ITEMS]
    check(len(selected) == 10, f"上限应为 10，实际 {len(selected)}")

    # 静默逻辑 + 去重
    st = state_mod.State("state/selfcheck-state.json")
    check(not st.known_event(events[0]), "没推过的事件被当成见过")
    st.mark_event(events[0], "已推送")
    check(st.known_event(events[0]), "推过的事件没被记住")

    if failures:
        for item in failures:
            print("自检失败：" + item, file=sys.stderr)
        return 5
    print(f"自检通过：{len(active)} 个源／{len(sources.platforms())} 个平台／"
          "接口解析、中文过滤、≥3 平台判定、专有名词聚类、10 条上限、去重均正常")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="热点事件监控")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选，不发送（默认）")
    parser.add_argument("--send", action="store_true", help="显式启用 Telegram 推送")
    parser.add_argument("--self-check", action="store_true", help="离线自检，不联网不写状态")
    parser.add_argument("--list-sources", action="store_true", help="列出源、平台与地址")
    parser.add_argument("--fixtures", default="", help="本地 JSON 样例，替代真实网络")
    parser.add_argument("--state", default="state/state.json", help="状态文件路径")
    parser.add_argument("--report", default="", help="运行报告路径")
    parser.add_argument("--candidates", action="store_true", help="打印每个候选的来源与热度明细")
    parser.add_argument("--check-telegram", action="store_true",
                        help="自查 Telegram 凭据：token 有效性、bot 名字、chat 能不能找到（不发消息）")
    args = parser.parse_args()

    if args.self_check:
        return _self_check()
    if args.check_telegram:
        token, chat_id = telegram.credentials()
        report = telegram.check_credentials(token, chat_id)
        print(f"token 有效：{report['token_ok']}"
              f"｜bot：@{report['bot_username']}（id {report['bot_id']}）")
        print(f"chat 可用：{report['chat_ok']}"
              f"｜类型：{report['chat_type'] or '-'}｜名称：{report['chat_title'] or '-'}")
        if report["error"]:
            print(f"问题：{report['error']}")
        return 0 if (report["token_ok"] and report["chat_ok"]) else 5
    if args.list_sources:
        for src in sources.active_sources():
            print(f"{src.id}\t{src.platform}\t{src.label}\t{src.kind}\t{newsnow.API_TEMPLATE.format(source_id=src.id)}")
        return 0
    if args.send and args.dry_run:
        print("--send 与 --dry-run 不能同时用", file=sys.stderr)
        return 5

    from monitor import run as monitor_run

    return monitor_run.run(fixtures=args.fixtures, state_path=args.state,
                           report_path=args.report, send=args.send,
                           print_candidates=args.candidates)


if __name__ == "__main__":
    sys.exit(main())