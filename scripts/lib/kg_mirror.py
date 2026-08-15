"""读本地的知识图谱副本。

**唯一的读取入口。** 权威 KG 那边的教训是：18 个文件各自
`json.loads(kg_path.read_text())`，于是没有一处知道另一处在假设什么。副本这边
从一开始就只留一条路。

这条路强制带上新鲜度，因为副本和权威文件有一个本质区别：**它可能是旧的，
而且它自己知道有多旧**。丢掉这个信息，AI 就会拿一份三天前的图去回答
"这本书讲了什么"，语气和拿着最新的图完全一样。所以 `load()` 返回的是
图**和**新鲜度，而不是只有图 —— 调用方想忽略新鲜度得自己动手，不能顺手忽略。

同理，**没有副本要跟"这本书没有图"分开报**。前者是同步没跑过或失败了，
后者是关于用户书架的事实。混成一个 `None`，AI 就会把"我没同步"说成
"你这本书没有知识点"。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "_mirror.json"

# 超过这个时长就在读出来时标出来。不是硬失败 —— 旧图仍然有用，
# 但 AI 应该知道自己手上这份可能落后于用户刚学完的东西。
STALE_AFTER_SECONDS = 48 * 3600


class MirrorMissing(Exception):
    """副本不在。**不是**"这本书没有知识点"。"""


def mirror_dir() -> Path:
    override = os.environ.get("CLAUDE_PROJECT")
    root = Path(override) if override else Path(__file__).resolve().parents[2]
    return root / "state" / "kg-mirror"


@dataclass(frozen=True)
class Freshness:
    """副本对自己的说明。"""

    book: str
    revision: str
    synced_at_epoch_seconds: int
    status: str          # "ok" | "stale"
    age_seconds: int
    last_error: str | None

    @property
    def is_current(self) -> bool:
        return self.status == "ok" and self.age_seconds <= STALE_AFTER_SECONDS

    def describe(self) -> str:
        """一句可以直接讲给用户听的话。

        存在的理由：让"这份有多旧"以自然语言出现在答案里，而不是留给
        每个调用方各自把秒数翻译成人话（翻译不一致就等于没说）。
        """
        hours = self.age_seconds / 3600.0
        if hours < 1:
            when = "刚同步过"
        elif hours < 48:
            when = f"约 {int(hours)} 小时前同步"
        else:
            when = f"约 {int(hours / 24)} 天前同步"
        if self.status != "ok":
            reason = f"，上次同步失败（{self.last_error}）" if self.last_error else "，上次同步失败"
            return f"{when}{reason}，内容可能不是最新的"
        if not self.is_current:
            return f"{when}，可能已落后"
        return when


def _load_manifest() -> dict:
    path = mirror_dir() / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value.get("books") or {} if isinstance(value, dict) else {}


def _freshness_of(book: str, entry: dict) -> Freshness:
    synced = int(entry.get("syncedAtEpochSeconds") or 0)
    return Freshness(
        book=book,
        revision=str(entry.get("revision") or ""),
        synced_at_epoch_seconds=synced,
        status=str(entry.get("status") or "ok"),
        age_seconds=max(0, int(time.time()) - synced) if synced else 10**9,
        last_error=entry.get("lastError"),
    )


def available_books() -> list[str]:
    """本地有副本的书。

    以磁盘上的文件为准而不是以清单为准：清单丢了但文件还在时，图仍然可读，
    只是不知道多旧 —— 那种情况该让调用方看到一本"新鲜度未知"的书，
    而不是看不到这本书。
    """
    directory = mirror_dir()
    if not directory.is_dir():
        return []
    return sorted(
        path.stem
        for path in directory.glob("*.json")
        if path.name != MANIFEST_NAME
    )


def load(book: str) -> tuple[dict, Freshness]:
    """读一本书的图，连同它的新鲜度。

    副本不在时抛 `MirrorMissing`，而不是返回空图。空图会被当成
    "这本书没有知识点"讲给用户听，那是一句关于用户书架的假话。
    """
    path = mirror_dir() / f"{book}.json"
    if not path.is_file():
        raise MirrorMissing(
            f"本地没有《{book}》的图谱副本。"
            f"这表示同步没跑过或这本书不在同步范围内，"
            f"不表示这本书没有知识点。"
        )
    graph = json.loads(path.read_text(encoding="utf-8"))
    return graph, _freshness_of(book, _load_manifest().get(book) or {})


def status() -> dict:
    """整体同步状态 —— 给"你现在能看到什么"这类问题用。

    尤其是启动阶段：同步还没跑完时，AI 应该说"还在同步"，
    而不是照着一个空目录回答"你没有任何图谱"。
    """
    books = available_books()
    manifest = _load_manifest()
    entries = [_freshness_of(name, manifest.get(name) or {}) for name in books]
    return {
        "books": books,
        "count": len(books),
        "stale": [f.book for f in entries if not f.is_current],
        # 看清单**文件在不在**，不是看它有没有内容 —— 同步跑过但一本书都没有，
        # 跟从没跑过，是两句不同的话。前者该说"你还没有建过图"，
        # 后者该说"还在同步，稍等"。
        "neverSynced": not (mirror_dir() / MANIFEST_NAME).is_file() and not books,
        "directory": str(mirror_dir()),
    }
