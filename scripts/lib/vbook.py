#!/usr/bin/env python3
"""vbook.py — 虚拟合并书**领域服务**(转换层 v2 第 1 步;规格 references/虚拟合并书-转换层方案-2026-07.md v2 节)。

坐标铁律:ViewLocation(vbook:<group_id>, global_page, revision)=视图;SourceLocation(member_rel,
local_page)=**唯一持久真相**。本服务是两者之间**唯一**的双向翻译点,全进程共享(webapp/assistant/
voice/task/脚本都 import 它,不是 Flask middleware)。

- group_id 稳定:sha1(dir|base|ext) —— 加卷/删卷/改页数不换身份;改名=新组(合理)。
- revision:成员结构指纹(num/rel/pages/mtime_ns/size)。请求 revision 不匹配 → VbookStale
  (对应 HTTP 409 manifest_stale,绝不静默重解释后写入)。
- fail-closed:未适配代码见到 vbook: 前缀应立即报错(assert_not_view_ref),宁缺勿污染。
- 拒绝合并:卷号重复 / 任一成员页数 0。
- manifest 持久化 state/reader-vbooks.json(原子写);内存缓存按 store mtime 重载(多 worker 安全)。
"""
import hashlib
import json
import threading
import time
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import book_groups as BG  # noqa: E402   (分卷识别/成员扫描复用;页数 fitz 权威)

STORE = Path(config.PROJECT_DIR) / "state" / "reader-vbooks.json"
VIEW_PREFIX = "vbook:"

_lock = threading.RLock()
_cache = {"data": None, "mtime": 0.0}


# ── 异常(调用方按类型转 HTTP 语义) ─────────────────────────────────────────────
class VbookError(Exception):
    """vbook 领域错误基类。"""


class VbookUnknown(VbookError):
    """view_ref 不存在/不可合并。→ 404"""


class VbookStale(VbookError):
    """revision 不匹配(成员结构已变)。→ 409 manifest_stale;客户端须重载映射。"""


class VbookRange(VbookError):
    """global_page 越界。→ 400"""


class VbookUnadapted(VbookError):
    """未适配的代码收到了 vbook: 引用(fail-closed,防静默把全局页写进某一卷)。→ 501"""


def is_view_ref(s):
    return isinstance(s, str) and s.startswith(VIEW_PREFIX)


def assert_not_view_ref(rel, where=""):
    """未适配 handler 的护栏:见 vbook: 立即炸,绝不当真实文件用。"""
    if is_view_ref(rel):
        raise VbookUnadapted("unadapted code received %r%s" % (rel, (" at " + where) if where else ""))


# ── manifest 存取 ─────────────────────────────────────────────────────────────
def _load():
    with _lock:
        try:
            mt = STORE.stat().st_mtime
        except OSError:
            _cache["data"] = {"groups": {}}
            _cache["mtime"] = 0.0
            return _cache["data"]
        if _cache["data"] is None or mt != _cache["mtime"]:
            try:
                _cache["data"] = json.loads(STORE.read_text("utf-8"))
            except Exception:
                _cache["data"] = {"groups": {}}
            _cache["mtime"] = mt
        return _cache["data"]


def _save(data):
    with _lock:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(STORE)
        try:
            _cache["data"] = data
            _cache["mtime"] = STORE.stat().st_mtime
        except OSError:
            pass


def _gid(dirpart, base, ext):
    return "g_" + hashlib.sha1(("%s|%s|%s" % (dirpart, base, ext.lower())).encode("utf-8")).hexdigest()[:10]


def _revision(members):
    sig = "|".join("%s:%s:%s:%s:%s" % (m["num"], m["rel"], m["pages"], m["mtime_ns"], m["size"])
                   for m in members)
    return "r_" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:10]


def _scan_group(rel):
    """从磁盘按命名约定扫 rel 所在组 → (group dict, reject_reason)。不落盘。"""
    mem = BG.members_of(rel)
    if not mem:
        return None, "not_a_group"
    nums = [n for n, _ in mem]
    if len(set(nums)) != len(nums):
        return None, "duplicate_part_number"
    p = Path(rel)
    base = BG.split_part(p.stem)[0]
    members = []
    off = 0
    for num, r in mem:
        ap = BG.VAULT / r
        try:
            st = ap.stat()
        except OSError:
            return None, "member_missing:%s" % r
        pages = BG._page_count(r)
        if not pages:
            return None, "member_zero_pages:%s" % r
        members.append({"num": num, "rel": r, "pages": pages,
                        "mtime_ns": st.st_mtime_ns, "size": st.st_size, "offset": off})
        off += pages
    g = {"group_id": _gid(str(p.parent), base, p.suffix),
         "base": base, "dir": str(p.parent), "ext": p.suffix.lower(),
         "members": members, "total": off, "built": int(time.time())}
    g["revision"] = _revision(members)
    return g, ""


def refresh(rel):
    """按成员 rel 重建该组 manifest 并落盘。返回 group dict;不可合并 → None。"""
    g, why = _scan_group(rel)
    data = _load()
    if g is None:
        return None
    with _lock:
        data.setdefault("groups", {})[g["group_id"]] = g
        _save(data)
    return g


def group_for_rel(rel):
    """真实成员 rel → 它所属组(store 有且磁盘未变则直接用;否则重扫)。非分卷 → None。"""
    g, _ = _scan_group(rel)
    if g is None:
        return None
    data = _load()
    stored = (data.get("groups") or {}).get(g["group_id"])
    if stored and stored.get("revision") == g["revision"]:
        return stored
    return refresh(rel)


def get(view_ref):
    """view_ref → group dict(store)。未知 → VbookUnknown。"""
    if not is_view_ref(view_ref):
        raise VbookUnknown("not a view_ref: %r" % view_ref)
    gid = view_ref[len(VIEW_PREFIX):]
    g = (_load().get("groups") or {}).get(gid)
    if not g:
        raise VbookUnknown("unknown vbook %r" % view_ref)
    return g


def validate(view_ref):
    """按磁盘重算当前 revision(轻:stat;页数仅在 mtime_ns/size 变时重取)。返回最新 group。"""
    g = get(view_ref)
    fresh, _ = _scan_group(g["members"][0]["rel"])
    if fresh is None:
        raise VbookUnknown("group no longer mergeable: %s" % view_ref)
    if fresh["revision"] != g["revision"]:
        data = _load()
        with _lock:
            data["groups"][fresh["group_id"]] = fresh
            _save(data)
    return fresh


def _check_rev(g, revision):
    if revision is not None and revision != g["revision"]:
        raise VbookStale("manifest_stale: have %s, request %s" % (g["revision"], revision))


# ── 双向 resolver ─────────────────────────────────────────────────────────────
def resolve_view(view_ref, global_page, revision=None):
    """(vbook, 全局页) → SourceLocation(member_rel, local_page)。stale → VbookStale;越界 → VbookRange。"""
    g = get(view_ref)
    _check_rev(g, revision)
    try:
        gp = int(global_page)
    except (TypeError, ValueError):
        raise VbookRange("bad global_page %r" % (global_page,))
    for m in g["members"]:
        if m["offset"] < gp <= m["offset"] + m["pages"]:
            return m["rel"], gp - m["offset"]
    raise VbookRange("global_page %s out of 1..%s" % (gp, g["total"]))


def resolve_pages(view_ref, global_pages, revision=None):
    return [resolve_view(view_ref, gp, revision) for gp in global_pages]


def to_view(member_rel, local_page):
    """SourceLocation → (view_ref, global_page, revision)(反译:响应/SSE/搜索/job 结果 globalize)。
    非分卷成员 → None。"""
    g = group_for_rel(member_rel)
    if g is None:
        return None
    for m in g["members"]:
        if m["rel"] == member_rel:
            try:
                lp = int(local_page)
            except (TypeError, ValueError):
                raise VbookRange("bad local_page %r" % (local_page,))
            if not (1 <= lp <= m["pages"]):
                raise VbookRange("local_page %s out of 1..%s for %s" % (lp, m["pages"], member_rel))
            return VIEW_PREFIX + g["group_id"], m["offset"] + lp, g["revision"]
    return None


def members(view_ref):
    return get(view_ref)["members"]


def revision_of(view_ref):
    return get(view_ref)["revision"]
