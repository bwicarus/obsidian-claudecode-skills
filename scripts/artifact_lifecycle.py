#!/usr/bin/env python3
"""生成物生命周期：判定哪些已经冷掉，冷归档而不是删除。

活动账本设计稿（`references/activity-ledger-design.md` §3.1）里最大的那块空白：

    现状：id 有两三套，**自动回收一套都没有**。全仓只有 6 处在做过期删除，
    且全是「时长/数量硬上限」，**没有一处是按「是否被固定保存」判定的**。

而「是否被固定保存」的判据**不用发明** —— 实体注册表里已经有 `local` 字段
（是否已贴页并本地化，本地化时机就是"贴到页面时"）。那天然就是
「用户把它固定下来了」的信号。

## 三条保留判据，命中任意一条就留

1. `local` 为真 —— 用户已经把它贴到页面上
2. 被任何一条持久记录引用（便签 / 高亮 / 笔记 / 卡片 / 插入页 / 收藏夹）
3. 还不够老（未过冷却期）

三条都不命中 → **冷归档**。

## ⚠ 为什么是归档不是删除

设计稿 §6 拍板的就是冷归档。理由很直接：**采集不可重来**
（`references/evidence-quality-lessons.md`）。一条记录删错了没有第二次机会，
而磁盘是便宜的。归档之后它仍然在 `state/cold-archive/` 里，
只是不再占用运行时的读取路径和 AI 的上下文预算。

## ⚠ 默认 dry-run

不带 `--archive` 时**只报告、不动任何文件**。这个脚本会碰用户数据，
默认必须是只读的。

用法::

    python3 scripts/artifact_lifecycle.py                 # 只看报告
    python3 scripts/artifact_lifecycle.py --days 90       # 换冷却期
    python3 scripts/artifact_lifecycle.py --archive       # 真的归档
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import time

ROOT = Path(os.environ.get("CLAUDE_PROJECT", Path(__file__).resolve().parents[1]))
STATE = ROOT / "state"
ASSET_REG = STATE / "assets" / "registry.json"
ARCHIVE = STATE / "cold-archive"

# 冷却期。90 天是个保守起点：比"对话纯文本归档"的 180 天短，
# 比"任务 run"的 7 天长得多 —— 生成物介于两者之间。
DEFAULT_COLD_DAYS = 90

# 会引用生成物编号的持久记录。命中即保留。
# ⚠ 这张表**少一处就会误归档**。新增一类持久记录时必须同时加进来 ——
#   所以它有一条契约测试盯着（tests/test_artifact_lifecycle.py）。
REFERENCE_SOURCES = (
    ("便签", STATE / "reader-notes"),
    ("插入页", STATE / "reader-userpages"),
    ("收藏夹", STATE / "reader-favorites.json"),
    ("高亮", STATE / "reader-highlights"),
    ("墨迹", STATE / "reader-ink"),
    ("卡片", STATE / "reader-cards"),
)


def _load_registry(path: Path = ASSET_REG) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _iter_entries(reg: dict):
    """注册表按 identity 分区存过，也可能是平铺的。两种都认。"""
    for key, value in reg.items():
        if not isinstance(value, dict):
            continue
        if "kind" in value or "ts" in value:
            yield key, value                      # 平铺
            continue
        for sub_key, sub in value.items():        # 按 identity 分区
            if isinstance(sub, dict):
                yield sub_key, sub


def collect_referenced_ids(sources=REFERENCE_SOURCES) -> set[str]:
    """扫一遍持久记录，把里面出现过的编号全收起来。

    ⚠ 用**朴素的子串包含**而不是解析各家结构：编号形如 `img_3f9a1c`，
      在 markdown 正文里以 `#img_3f9a1c` 出现、在 JSON 里以字段值出现，
      形态不一。少认一处的代价是误归档用户还在用的东西，
      而多认一处只是少归档一点 —— 两边的代价完全不对称。
    """
    blobs: list[str] = []
    for _label, path in sources:
        if path.is_file():
            try:
                blobs.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
        elif path.is_dir():
            for child in path.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    blobs.append(child.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
    return set(), "".join(blobs)


def classify(reg: dict, corpus: str, cold_days: int, now: int | None = None):
    """把注册表分成 保留 / 冷 两堆，并说清每条为什么。"""
    now = int(time.time()) if now is None else now
    cutoff = now - cold_days * 86400
    keep, cold = [], []
    for aid, entry in _iter_entries(reg):
        ts = 0
        try:
            ts = int(entry.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        pinned = bool(entry.get("local"))
        referenced = bool(aid) and aid in corpus
        fresh = ts >= cutoff
        row = {
            "id": aid,
            "kind": entry.get("kind") or "",
            "ts": ts,
            "pinned": pinned,
            "referenced": referenced,
            "fresh": fresh,
        }
        if pinned:
            row["why"] = "已贴页（local）"
            keep.append(row)
        elif referenced:
            row["why"] = "被持久记录引用"
            keep.append(row)
        elif fresh:
            row["why"] = "还不够老"
            keep.append(row)
        else:
            row["why"] = "没贴页、没被引用、已过冷却期"
            cold.append(row)
    return keep, cold


def archive(cold: list[dict], reg_path: Path = ASSET_REG,
            archive_root: Path = ARCHIVE, now: int | None = None) -> Path | None:
    """把冷条目挪进冷归档。**不删除任何东西。**

    先把整份注册表原样存一份，再从活的那份里摘掉冷条目 —— 顺序不能反：
    崩在中间时宁可归档里多一份副本，也不要活的那份已经少了而归档还没写成。
    """
    if not cold:
        return None
    now = int(time.time()) if now is None else now
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    out_dir = archive_root / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    # ① 整份快照（取证用：事后要能还原"当时归档了什么、活的那份长什么样"）
    try:
        shutil.copy2(reg_path, out_dir / "registry.before.json")
    except OSError:
        pass

    reg = _load_registry(reg_path)
    cold_ids = {row["id"] for row in cold}
    moved = {}

    def _prune(node: dict) -> dict:
        out = {}
        for key, value in node.items():
            if not isinstance(value, dict):
                out[key] = value
                continue
            if ("kind" in value or "ts" in value):
                if key in cold_ids:
                    moved[key] = value
                    continue
                out[key] = value
            else:
                out[key] = _prune(value)
        return out

    pruned = _prune(reg)
    with open(out_dir / "cold.json", "w", encoding="utf-8") as fh:
        json.dump(moved, fh, ensure_ascii=False, indent=1)

    # ② 原子替换活的那份
    tmp = reg_path.with_suffix(".tmp." + str(os.getpid()))
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pruned, fh, ensure_ascii=False)
    os.replace(tmp, reg_path)
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=DEFAULT_COLD_DAYS,
                    help="冷却期（天），默认 %d" % DEFAULT_COLD_DAYS)
    ap.add_argument("--archive", action="store_true",
                    help="真的归档（不带就只报告，什么都不动）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    reg = _load_registry()
    if not reg:
        print("注册表为空或读不到：%s" % ASSET_REG)
        return 0

    _unused, corpus = collect_referenced_ids()
    keep, cold = classify(reg, corpus, args.days)

    if args.json:
        print(json.dumps({"keep": len(keep), "cold": cold}, ensure_ascii=False))
        return 0

    print("生成物生命周期  冷却期=%d 天" % args.days)
    print("  保留 %d 条：" % len(keep))
    reasons: dict[str, int] = {}
    for row in keep:
        reasons[row["why"]] = reasons.get(row["why"], 0) + 1
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("    %-16s %d" % (why, n))
    print("  冷 %d 条（没贴页、没被引用、已过冷却期）" % len(cold))
    for row in cold[:12]:
        age = (int(time.time()) - row["ts"]) // 86400 if row["ts"] else "?"
        print("    %-14s %-6s %s 天前" % (row["id"], row["kind"], age))
    if len(cold) > 12:
        print("    …另有 %d 条" % (len(cold) - 12))

    if not args.archive:
        print()
        print("（只报告，什么都没动。要真的归档加 --archive）")
        return 0

    out = archive(cold)
    if out:
        print()
        print("已冷归档到 %s —— **没有删除任何东西**，随时可以取回。" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
