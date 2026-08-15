"""把「用户正看着的这一页」对到知识图谱上的节点。

为什么需要它：快照现在告诉助手用户在第几页、正文是什么，但不告诉它**这页在
讲什么概念**。助手要知道就得再问一轮 —— 而这是最常问的一件事。带上它，
一次往返变零次。

这个映射此前不存在。系统里已有的 `kg:<book>#<node>` 引用是反过来用的：
`attention_profile._kg_all()` 把所有书的节点合并成一张全局表，按**概念名**跨书
搜。从"打开的这本 PDF"走到"它的图"，之前没有人需要过。

两处地方决定了这个模块的形状：

  · **宁可说不知道，也不能对错书。** 快照带上别的书的知识点，比什么都不带糟
    得多 —— 助手会拿它当这页的内容讲出来，而用户没有办法看出这是张冠李戴。
    所以匹配必须**唯一**：命中零本或多本，一律返回 None 并说明原因。

  · **每翻一页都会调。** 书架上二十几本书，图文件不小。每次全读一遍会让翻页
    变慢，而翻页慢是用户唯一能感觉到的事。所以映射与页索引都按 mtime 缓存。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

# 目录里不止书（照 kg_export 的排除表；两处若分歧，会出现"索引里没有、
# 这里却匹配上了"的怪事）。
_NON_BOOK_MARKERS = (".bak", ".pre", ".scan", "_archive")
_NON_BOOK_NAMES = frozenset({"kg_audit.json"})

# 一页最多带几个概念。带全了会把快照撑爆，而助手真正用得上的是"这页在讲什么"，
# 不是穷举。超出时截断并说出来。
MAX_CONCEPTS_PER_PAGE = 6
SUMMARY_LIMIT = 160

_lock = threading.Lock()
_book_cache: dict[str, tuple[float, str | None]] = {}   # kg 路径 → (mtime, pdf 文件名)
_index_cache: dict[str, tuple[float, dict]] = {}        # kg 路径 → (mtime, 页索引)


def _kg_dir() -> Path:
    override = os.environ.get("CLAUDE_PROJECT")
    root = Path(override) if override else Path(__file__).resolve().parent.parent
    return root / "knowledge_graph"


def _book_files() -> list[Path]:
    directory = _kg_dir()
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.glob("*.json")
        if path.name not in _NON_BOOK_NAMES
        and not any(marker in path.name for marker in _NON_BOOK_MARKERS)
    )


def _source_pdf_name(path: Path) -> str | None:
    """这本图是从哪个 PDF 建的。取文件名，不比路径。

    建图时记的是建图那台机器上的绝对路径，跟阅读器看到的 vault 相对路径不同源。
    文件名是两边唯一都成立的东西。
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    with _lock:
        cached = _book_cache.get(str(path))
        if cached and cached[0] == stat.st_mtime:
            return cached[1]
    name = None
    try:
        kg = json.loads(path.read_text(encoding="utf-8"))
        source = kg.get("pdf") or kg.get("source") or ""
        if source:
            name = Path(str(source).replace("\\", "/")).name.lower()
    except Exception:
        name = None
    with _lock:
        _book_cache[str(path)] = (stat.st_mtime, name)
    return name


def book_for_file(rel: str) -> tuple[str | None, str]:
    """打开的这本书对应哪张图。返回 (book_id, 原因)。

    匹配不唯一时返回 None —— 见模块头：对错书比不给更糟。
    """
    target = Path(str(rel or "").replace("\\", "/")).name.lower()
    if not target:
        return None, "没有文件名"
    matches = [
        path.stem for path in _book_files() if _source_pdf_name(path) == target
    ]
    if len(matches) == 1:
        return matches[0], "按源文件名匹配"
    if not matches:
        return None, "这本书还没有建过知识图谱"
    # 两张图声称来自同一个文件。猜哪张都可能把另一本书的概念讲成这页的内容。
    return None, f"有 {len(matches)} 张图都声称来自这个文件，无法确定是哪一张"


def _page_index(path: Path) -> dict:
    """{页码: {"section": L1节点, "concepts": [L2节点]}}，按 mtime 缓存。

    展开成按页查是因为这个函数每翻一页都会走一次。L1 带的是区间、L2 带的是
    离散页码，每次现算要遍历全部节点。
    """
    try:
        stat = path.stat()
    except OSError:
        return {}
    with _lock:
        cached = _index_cache.get(str(path))
        if cached and cached[0] == stat.st_mtime:
            return cached[1]
    index: dict[int, dict] = {}
    try:
        kg = json.loads(path.read_text(encoding="utf-8"))
        nodes = kg.get("nodes") or []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            pages = node.get("pages") or []
            if not isinstance(pages, list) or not pages:
                continue
            level = node.get("level")
            if level == 1 and len(pages) >= 2:
                try:
                    start, end = int(pages[0]), int(pages[1])
                except (TypeError, ValueError):
                    continue
                if end < start:
                    start, end = end, start
                # 一节动辄几十页，逐页展开是有意的：换来的是翻页时的 O(1)。
                for page in range(start, end + 1):
                    index.setdefault(page, {}).setdefault("section", node)
            elif level == 2:
                for raw in pages:
                    try:
                        page = int(raw)
                    except (TypeError, ValueError):
                        continue
                    index.setdefault(page, {}).setdefault(
                        "concepts", []).append(node)
    except Exception:
        index = {}
    with _lock:
        _index_cache[str(path)] = (stat.st_mtime, index)
    return index


def _trim(node: dict) -> dict:
    out = {"name": str(node.get("name") or "").strip()}
    summary = str(node.get("summary") or "").strip()
    if summary:
        out["summary"] = summary[:SUMMARY_LIMIT]
        if len(summary) > SUMMARY_LIMIT:
            out["summary_truncated"] = True
    node_id = node.get("id")
    if node_id:
        out["id"] = str(node_id)
    kind = node.get("type")
    if kind:
        out["type"] = str(kind)
    return out


def knowledge_for_page(rel: str, page) -> dict:
    """这一页上有哪些知识点。

    永远返回一个 dict 并说明情况，不返回 None —— 上游分不清"这页没有节点"
    和"这段代码没跑"时，会把后者当成前者讲给用户听。
    """
    out = {"available": False, "reason": None, "book": None,
           "section": None, "concepts": []}
    book, reason = book_for_file(rel)
    out["reason"] = reason
    if not book:
        return out
    out["book"] = book
    try:
        number = int(page)
    except (TypeError, ValueError):
        out["reason"] = "页码不是数字，无法定位"
        return out
    entry = _page_index(_kg_dir() / f"{book}.json").get(number) or {}
    section = entry.get("section")
    concepts = entry.get("concepts") or []
    if not section and not concepts:
        out["reason"] = "这一页在图上没有对应节点"
        return out
    out["available"] = True
    out["reason"] = reason
    if section:
        out["section"] = _trim(section)
    if concepts:
        out["concepts"] = [_trim(node) for node in concepts[:MAX_CONCEPTS_PER_PAGE]]
        if len(concepts) > MAX_CONCEPTS_PER_PAGE:
            out["concepts_truncated"] = len(concepts) - MAX_CONCEPTS_PER_PAGE
    # 摘要是建图时写的概括，不是原文。不说清楚，助手会拿它当原文引用，
    # 而用户翻到那页会发现书上没有这句话。
    out["note"] = "以上是知识图谱里的概括，不是本页原文；要精确引用请读正文"
    return out


def _reset_caches_for_tests() -> None:
    with _lock:
        _book_cache.clear()
        _index_cache.clear()
