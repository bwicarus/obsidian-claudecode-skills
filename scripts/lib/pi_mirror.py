"""Pi 阅读器数据镜像的唯一读取入口（与 kg_mirror 同一套哲学）。

为什么读取必须走这里而不是直接 open 那些 JSON：镜像必须能回答"我这份是什么
时候的"。不知道自己多旧的数据比明确过时的数据更危险 —— AI 会拿它下断言。

第 17 条铁律在读取端的落点：**同步未完成时不得下否定性结论**。PC 刚开机、
追赶还在跑时查"昨天划的那句"，本地没有 ≠ 用户没划过 —— 数据没错、代码没错、
时机错了。所以 load() 永远连新鲜度一起给；镜像不存在时抛 MirrorMissing 而不是
返回空列表，让"没同步"和"确实没有"在类型上就分开。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

CONTRACT = "reader-pi-mirror/1"
MANIFEST_NAME = "_mirror.json"

# SSE 断开重连是日常，但断了太久说明守护死了/机器睡了 —— 超过这个秒数标 stale。
STALE_AFTER_SECONDS = 30 * 60


class MirrorMissing(RuntimeError):
    """镜像还没建立（守护从未跑过 / 该书该域从未拉过）。

    捕获方唯一正确的回答是"还没同步，暂时查不到"，不是"没有"。"""


@dataclass
class Freshness:
    domain: str
    rel: str | None
    synced_at_epoch_seconds: int
    age_seconds: int
    sse_status: str
    stale: bool

    def describe(self) -> str:
        if self.stale:
            return (f"镜像已 {self.age_seconds // 60} 分钟未更新"
                    f"(sse={self.sse_status})，结论请打折扣")
        return f"镜像新鲜(约 {self.age_seconds}s 前，sse={self.sse_status})"


def _root() -> Path:
    override = os.environ.get("CLAUDE_PROJECT")
    base = Path(override) if override else Path(__file__).resolve().parents[2]
    return base / "state" / "pi-mirror"


def _book_key(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def _manifest() -> dict:
    path = _root() / MANIFEST_NAME
    if not path.is_file():
        raise MirrorMissing(
            "Pi 数据镜像从未同步过：先跑 scripts/pi_mirror_daemon.py --once")
    return json.loads(path.read_text(encoding="utf-8"))


def _freshness(domain: str, rel: str | None, synced_at: int, sse: str) -> Freshness:
    age = max(0, int(time.time()) - int(synced_at or 0))
    return Freshness(domain=domain, rel=rel,
                     synced_at_epoch_seconds=int(synced_at or 0),
                     age_seconds=age,
                     sse_status=sse,
                     stale=(not synced_at) or age > STALE_AFTER_SECONDS)


def status() -> dict:
    """守护/镜像的总体状态；neverSynced 看 manifest 文件本身的存在。"""
    path = _root() / MANIFEST_NAME
    if not path.is_file():
        return {"neverSynced": True, "directory": str(_root())}
    m = json.loads(path.read_text(encoding="utf-8"))
    return {
        "neverSynced": False,
        "sse": m.get("sse") or {},
        "positionsSyncedAt": m.get("positionsSyncedAt") or 0,
        "books": len(m.get("books") or {}),
        "directory": str(_root()),
    }


def load_positions() -> tuple[dict, Freshness]:
    m = _manifest()
    path = _root() / "positions.json"
    if not path.is_file():
        raise MirrorMissing("positions 尚未镜像")
    data = json.loads(path.read_text(encoding="utf-8"))
    sse = str((m.get("sse") or {}).get("status") or "unknown")
    return data, _freshness("pos", None, m.get("positionsSyncedAt") or 0, sse)


def load_book(rel: str, domain: str) -> tuple[dict | list, Freshness]:
    """domain ∈ hl / epub-hl / note。返回该书该域的镜像内容 + 新鲜度。"""
    m = _manifest()
    entry = (m.get("books") or {}).get(_book_key(rel)) or {}
    dom = (entry.get("domains") or {}).get(domain) or {}
    path = _root() / "books" / _book_key(rel) / f"{domain}.json"
    if not path.is_file():
        raise MirrorMissing(f"《{rel}》的 {domain} 尚未镜像"
                            "（守护会在首次事件或下轮追赶时拉取）")
    data = json.loads(path.read_text(encoding="utf-8"))
    sse = str((m.get("sse") or {}).get("status") or "unknown")
    return data, _freshness(domain, rel, dom.get("syncedAt") or 0, sse)
