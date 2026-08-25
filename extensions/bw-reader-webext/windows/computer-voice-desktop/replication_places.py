#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常在位置 —— 从位置记录里自动聚出「我常在哪」，可命名，供快照判断在场。

设计（2026-08-25 用户拍板）：

- **自动聚类**：扫 dwell 记录里的坐标，贪心聚类（半径 120m —— 防微小
  漂移被当成多个位置），按停留秒数排序 = 常在位置候选。
- **命名是派生层**（activity-ledger-design 的别名原则）：别名表
  `place-aliases.json` 独立于聚类结果，按坐标就近命中；聚类重算随时
  重放，命名永不丢，且**追溯适用于全部历史**。
- **当前位置**：最近一条带坐标的 dwell（≤30 分钟）→ 命中别名（或退
  反解地名）→ 导出 `current-place.json`，桥合并进快照 `currentPlace`
  节 —— AI 据此判断通知该不该提醒（在公司就别提倒垃圾）。

用法::

    python replication_places.py analyze          # 看常在位置候选
    python replication_places.py name 1 家        # 给候选 #1 命名
    python replication_places.py aliases          # 看已命名的
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

ALIASES_FILE_NAME = "place-aliases.json"
CURRENT_PLACE_FILE_NAME = "current-place.json"
ALIASES_CONTRACT = "reader-place-aliases/1"
CLUSTER_RADIUS_M = 120.0     # 微小位置变化不分裂成多个位置
ALIAS_HIT_RADIUS_M = 200.0   # 别名命中的宽容半径
CURRENT_WINDOW_MINUTES = 30  # 「当前位置」的新鲜度窗口


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # equirectangular 近似:城市尺度误差可忽略,不引依赖。
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6371000.0


def _load_located_dwell(root: Path) -> list[dict[str, Any]]:
    """全部带坐标的 dwell 行（旁路进来的 loc）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import replication_activity
    rows = []
    data_dir = root / "replication-data"
    if not data_dir.is_dir():
        return rows
    for book_dir in sorted(data_dir.iterdir()):
        path = book_dir / replication_activity.ACTIVITY_FILE_NAME
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            body = record.get("body") or {}
            loc = body.get("loc")
            if body.get("kind") != "dwell" or not isinstance(loc, dict):
                continue
            if not (
                isinstance(loc.get("lat"), (int, float))
                and isinstance(loc.get("lon"), (int, float))
            ):
                continue
            secs = sum(
                int(item.get("secs") or 0)
                for item in body.get("entries") or []
                if isinstance(item, dict)
            )
            rows.append({
                "atUtcMs": int(record.get("receivedAtUtcMs") or 0),
                "lat": float(loc["lat"]),
                "lon": float(loc["lon"]),
                "name": str(loc.get("name") or ""),
                "secs": secs,
            })
    return rows


def cluster(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """贪心聚类：常在位置候选，按累计停留秒数降序。"""
    clusters: list[dict[str, Any]] = []
    for row in rows:
        target = None
        for candidate in clusters:
            if _distance_m(
                row["lat"], row["lon"],
                candidate["lat"], candidate["lon"],
            ) <= CLUSTER_RADIUS_M:
                target = candidate
                break
        if target is None:
            clusters.append({
                "lat": row["lat"], "lon": row["lon"],
                "seconds": row["secs"],
                "lastSeenUtcMs": row["atUtcMs"],
                "geoName": row["name"],
            })
            continue
        total = target["seconds"] + row["secs"]
        if total > 0:
            weight = row["secs"] / total
            target["lat"] += (row["lat"] - target["lat"]) * weight
            target["lon"] += (row["lon"] - target["lon"]) * weight
        target["seconds"] = total
        target["lastSeenUtcMs"] = max(
            target["lastSeenUtcMs"], row["atUtcMs"])
        if row["name"]:
            target["geoName"] = row["name"]
    clusters.sort(key=lambda c: -c["seconds"])
    return clusters


def load_aliases(root: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(
            (root / ALIASES_FILE_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if (
        not isinstance(value, dict)
        or value.get("contract") != ALIASES_CONTRACT
        or not isinstance(value.get("aliases"), list)
    ):
        return []
    return value["aliases"]


def save_alias(root: Path, name: str, lat: float, lon: float) -> None:
    aliases = load_aliases(root)
    for alias in aliases:
        if _distance_m(lat, lon, alias["lat"], alias["lon"]) \
                <= ALIAS_HIT_RADIUS_M:
            alias["name"] = name
            alias["lat"] = lat
            alias["lon"] = lon
            break
    else:
        aliases.append({"name": name, "lat": lat, "lon": lon})
    path = root / ALIASES_FILE_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "contract": ALIASES_CONTRACT,
        "aliases": aliases,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    temporary.replace(path)


def resolve_alias(
    root: Path, lat: float, lon: float
) -> str | None:
    best: tuple[float, str] | None = None
    for alias in load_aliases(root):
        d = _distance_m(lat, lon, alias["lat"], alias["lon"])
        if d <= ALIAS_HIT_RADIUS_M and (best is None or d < best[0]):
            best = (d, alias["name"])
    return best[1] if best else None


def export_current_place(root: Path, export_path: Path) -> dict | None:
    """最近 30 分钟内的位置 → 别名优先 → 导出给桥的快照 join。

    没有新鲜位置时**删除导出文件**（缺席=不知道在哪；一个两小时前的
    位置冒充"当前"比不知道更糟 —— 拿旧状态冒充当前是本仓库反复吃亏
    的形态）。
    """
    rows = _load_located_dwell(root)
    cutoff = int((time.time() - CURRENT_WINDOW_MINUTES * 60) * 1000)
    fresh = [r for r in rows if r["atUtcMs"] >= cutoff]
    if not fresh:
        try:
            export_path.unlink()
        except FileNotFoundError:
            pass
        return None
    latest = max(fresh, key=lambda r: r["atUtcMs"])
    value = {
        "contract": "reader-current-place/1",
        "lat": round(latest["lat"], 6),
        "lon": round(latest["lon"], 6),
        "geoName": latest["name"],
        "alias": resolve_alias(root, latest["lat"], latest["lon"]),
        "observedAtUtcMs": latest["atUtcMs"],
    }
    temporary = export_path.with_suffix(".tmp")
    export_path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(export_path)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("analyze", help="聚类展示常在位置候选")
    name = sub.add_parser("name", help="给候选编号命名（如：name 1 家）")
    name.add_argument("index", type=int)
    name.add_argument("alias")
    sub.add_parser("aliases", help="看已命名的位置")
    args = parser.parse_args()
    root = args.root or default_root()

    if args.command == "aliases":
        aliases = load_aliases(root)
        if not aliases:
            print("还没有命名任何位置。")
        for alias in aliases:
            print("  %s  (%.5f, %.5f)" % (
                alias["name"], alias["lat"], alias["lon"]))
        return 0

    clusters = cluster(_load_located_dwell(root))
    if args.command == "analyze":
        if not clusters:
            print("还没有带坐标的记录（App 开「记录学习地点」后积累）。")
            return 0
        print("常在位置候选（按停留时长）：")
        for index, c in enumerate(clusters[:10], 1):
            alias = resolve_alias(root, c["lat"], c["lon"])
            label = alias or c["geoName"] or "（未命名）"
            print("  #%d %s  %.1f 分钟  (%.5f, %.5f)" % (
                index, label, c["seconds"] / 60, c["lat"], c["lon"]))
        return 0
    if args.command == "name":
        if not (1 <= args.index <= len(clusters)):
            print("候选编号超界（先跑 analyze 看编号）")
            return 2
        chosen = clusters[args.index - 1]
        save_alias(root, args.alias, chosen["lat"], chosen["lon"])
        print("已命名：%s → (%.5f, %.5f)" % (
            args.alias, chosen["lat"], chosen["lon"]))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
