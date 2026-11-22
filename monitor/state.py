"""跨轮状态：已推事件去重 + 源健康 + 运行记录。

状态写在 state 分支（不走 Actions cache，缓存逐出会丢历史导致重复推送）。
干跑不写「已推」，否则用户还没确认推送就被标记成推过了；只有真发出去才记。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import lang, rules

UTC = timezone.utc
MAX_SEEN = 300


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


class State:
    def __init__(self, path: str = "state/state.json"):
        self.path = Path(path)
        self.seen: List[dict] = []
        self.source_failures: Dict[str, dict] = {}
        self.runs: List[dict] = []
        self.last_run: str = ""
        self.updated_at: str = ""

    def load(self) -> "State":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"状态文件损坏，拒绝静默跳过（会造成重复推送）: {exc}") from exc
        self.seen = list(raw.get("seen") or [])
        self.source_failures = dict(raw.get("source_failures") or {})
        self.runs = list(raw.get("runs") or [])[-48:]
        self.last_run = str(raw.get("last_run") or "")
        return self

    def save(self) -> None:
        self.updated_at = _now()
        payload = {
            "schema_version": "1.0.0",
            "updated_at": self.updated_at,
            "last_run": self.last_run,
            "seen": self.seen[-MAX_SEEN:],
            "source_failures": self.source_failures,
            "runs": self.runs[-48:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def record_run(self, started_at: str) -> float:
        prior = _parse(self.last_run)
        current = _parse(started_at)
        gap = round((current - prior).total_seconds() / 60, 1) if prior and current else -1
        self.runs.append({"at": started_at, "gap_minutes": gap})
        self.last_run = started_at
        return gap

    def add_source_status(self, name: str, ok: bool, error_type: str = "",
                          error: str = "") -> None:
        """只记状态，不告警：源失效那轮静默，不刷屏。"""
        if ok:
            self.source_failures.pop(name, None)
            return
        entry = dict(self.source_failures.get(name) or {})
        count = int(entry.get("count") or 0) + 1
        self.source_failures[name] = {
            "count": count, "error_type": error_type, "last_error": error[:200], "at": _now(),
        }

    def known_event(self, event) -> bool:
        """推过的同一件事不再推（标题相似度比对，跨轮）。"""
        info = lang.build(event.title)
        cutoff = datetime.now(UTC) - timedelta(days=rules.DEDUP_RETENTION_DAYS)
        for record in self.seen:
            stamp = _parse(str(record.get("at") or ""))
            if stamp is not None and stamp < cutoff:
                continue
            other = lang.build(str(record.get("title") or ""))
            if lang.same_event(info, other):
                return True
        return False

    def mark_event(self, event, status: str = "已推送") -> None:
        self.seen.append({
            "title": event.title[:300], "url": event.url,
            "platforms": event.platforms, "status": status, "at": _now(),
        })
        if len(self.seen) > MAX_SEEN:
            self.seen = self.seen[-MAX_SEEN:]

    def prune(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=rules.DEDUP_RETENTION_DAYS)
        kept = []
        removed = 0
        for record in self.seen:
            stamp = _parse(str(record.get("at") or ""))
            if stamp is not None and stamp < cutoff:
                removed += 1
                continue
            kept.append(record)
        self.seen = kept
        return removed