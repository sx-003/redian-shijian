"""编排：抓源 → 中文过滤 → 跨平台比对 → 去重 → 一条消息 → 干跑/推送 → 状态。

推送纪律：
- 一轮合成一条消息，最多 10 条；没有热点就静默，不发心跳。
- 某个源失效只是那轮少一块内容，静默不告警；全部源失效同样静默（接口挂了不当故障）。
- 推送失败不写「已推」，下一轮重试。
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from . import cluster, newsnow, rules, sources, state as state_mod, telegram

EXIT_OK, EXIT_CONFIG = 0, 5


def _now_ms() -> int:
    return int(time.time() * 1000)


def fetch_all() -> List[newsnow.SourceResult]:
    """每轮每个源只拉一次。单个源失败不影响其他源。"""
    active = sources.active_sources()
    with ThreadPoolExecutor(max_workers=rules.MAX_WORKERS) as pool:
        return list(pool.map(
            lambda src: newsnow.fetch_source(src, timeout=rules.FETCH_TIMEOUT,
                                             retries=rules.FETCH_RETRIES),
            active,
        ))


def _load_fixtures(path: str) -> List[newsnow.SourceResult]:
    """离线样例：{"sources":[{"source","platform","label","items":[{title,url,heat,published_ms}]}]}"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    results: List[newsnow.SourceResult] = []
    for row in raw.get("sources", []):
        source = sources.Source(row.get("source", "fixture"), row.get("platform", "fixture"),
                                row.get("label", "样例"))
        updated = row.get("updated_ms")
        items: List[newsnow.HotItem] = []
        for entry in row.get("items", []):
            items.append(newsnow.HotItem(
                source=source.id, platform=source.platform, label=source.label,
                title=str(entry.get("title", "")), url=str(entry.get("url", "")),
                heat=entry.get("heat"), heat_text=str(entry.get("heat_text", "")),
                summary=str(entry.get("summary", "")),
                published_ms=entry.get("published_ms"), updated_ms=updated,
            ))
        results.append(newsnow.SourceResult(name=source.id, label=source.label,
                                            platform=source.platform, ok=True, status="ok",
                                            items=items, updated_ms=updated))
    return results


def build_message(events) -> str:
    header = "🔥 热点事件"
    lines = [rules.format_event_line(event) for event in events]
    return "\n".join([header] + lines)


def run(*, fixtures: str = "", state_path: str = "state/state.json", report_path: str = "",
        send: bool = False, now_ms: Optional[int] = None,
        print_candidates: bool = False) -> int:
    started_dt = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    stamp = now_ms if now_ms is not None else _now_ms()
    st = state_mod.State(state_path).load()
    gap = st.record_run(started_dt)

    if fixtures:
        reports = _load_fixtures(fixtures)
        print(f"使用本地样例：{len(reports)} 个源")
    else:
        reports = fetch_all()

    ok_reports = [r for r in reports if r.ok]
    failed = [r for r in reports if not r.ok]
    for report in reports:
        st.add_source_status(report.name, report.ok, report.status, report.error)
    if failed:
        # 源失效只记状态，不刷屏、不告警
        print(f"源失效 {len(failed)} 个（静默跳过）："
              + "、".join(f"{r.name}:{r.status}" for r in failed))

    all_items: List[newsnow.HotItem] = []
    for report in ok_reports:
        all_items.extend(report.items)

    events, near, stats = cluster.cluster(all_items, stamp)
    hot = [e for e in events]

    fresh: List = []
    skipped_seen = 0
    for event in hot:
        if st.known_event(event):
            skipped_seen += 1
            continue
        fresh.append(event)
    selected = fresh[:rules.MAX_ITEMS]

    message = build_message(selected) if selected else ""
    pushed = False
    if send:
        if not selected:
            print("本轮无热点，静默不推送。")
        else:
            token, chat_id = telegram.credentials()
            if not token or not chat_id:
                print("缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，拒绝推送。", file=sys.stderr)
                return EXIT_CONFIG
            try:
                telegram.send_message(token, chat_id, message)
            except telegram.PushError as exc:
                # 推送失败不写「已推」，下一轮重试；错误写清楚，不打印任何密钥
                print(f"Telegram 推送失败：{exc}", file=sys.stderr)
                print("本轮不记为已推，下一轮会重推。", file=sys.stderr)
                return EXIT_CONFIG
            pushed = True
            for event in selected:
                st.mark_event(event, "已推送")
    elif selected:
        print("\n--- 干跑：不会发送 Telegram ---\n" + message + "\n")
    else:
        print("本轮无热点（跨平台未达阈值），静默。")

    if print_candidates:
        _print_candidates(events, near, fresh, selected)

    st.prune()
    st.save()

    report = {
        "started_at": started_dt, "mode": "send" if send else "dry-run",
        "gap_minutes": gap, "window_hours": rules.HOT_WINDOW_HOURS,
        "min_platforms": rules.MIN_PLATFORMS, "sim_threshold": rules.SIM_THRESHOLD,
        "sources": [r.to_dict() for r in reports],
        "source_failures": st.source_failures,
        "items_total": len(all_items), "stats": stats,
        "clusters_hot": len(events), "skipped_seen": skipped_seen,
        "selected": [e.to_dict() for e in selected],
        "near_miss": [e.to_dict() for e in near],
        "message": message, "telegram_sent": pushed,
    }
    if report_path:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return EXIT_OK


def _print_candidates(events, near, fresh, selected) -> None:
    print("\n=== 跨平台候选（平台数排序）===")
    for event in events:
        mark = "★推送" if event in selected else ("·见过" if event not in fresh else "·未入选")
        print(f"[{len(event.platforms)}平台/{event.source_count}源] {mark} {event.title}")
        print(f"    锚点: {event.anchor or '-'}｜时间: {rules.format_time(event.event_ms) or '-'}"
              f"｜热度: {rules.format_heat(event.top_heat) or '-'}")
        for item in event.items:
            heat = rules.format_heat(item.heat) or (item.heat_text or "-")
            print(f"    - {item.label}({item.source}) {heat} {item.title[:60]}")
            print(f"      {item.url}")
    if near:
        print("\n=== 差一点（2 个平台，未达阈值）===")
        for event in near:
            print(f"[{len(event.platforms)}平台] {event.title}")
            for item in event.items:
                print(f"    - {item.label}({item.source}) {item.title[:60]}")
            print(f"      {event.url}")