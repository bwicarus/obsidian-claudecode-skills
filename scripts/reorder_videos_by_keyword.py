#!/usr/bin/env python3
"""本地对 fitness-plan.json 已有视频列表重排序,把标题包含 must_contain
关键词的视频排前。不调 YouTube API,纯本地处理。

用 find_jeff_videos.py 里的 MUST_CONTAIN 表。
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from find_jeff_videos import MUST_CONTAIN  # noqa: E402

PLAN = Path("/home/bwicarus/claude/_server_deploy/static/fitness-plan.json")
WEBAPP = Path("/home/bwicarus/webapp/static/fitness-plan.json")


def main(drop_irrelevant: bool = False):
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    n_reordered = 0
    n_dropped = 0
    for day in plan["days"]:
        for ex in day["exercises"]:
            vids = ex.get("videos") or []
            if not vids:
                continue
            kws = MUST_CONTAIN.get(ex["id"]) or []
            if not kws:
                continue
            kws_lc = [k.lower() for k in kws]

            def matches(v):
                t = (v.get("title") or "").lower()
                return any(k in t for k in kws_lc)

            primary = [v for v in vids if matches(v)]
            fallback = [v for v in vids if not matches(v)]
            print(f"\n[{ex['name']}] kws={kws}")
            print(f"  ✓ 命中 {len(primary)}: {[v['title'][:50] for v in primary]}")
            print(f"  ✗ 未命中 {len(fallback)}: {[v['title'][:50] for v in fallback]}")
            if drop_irrelevant:
                new_list = primary
                n_dropped += len(fallback)
            else:
                new_list = primary + fallback   # 命中的在前
            if [v["video_id"] for v in new_list] != [v["video_id"] for v in vids]:
                n_reordered += 1
            ex["videos"] = new_list

    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if WEBAPP.parent.exists():
        WEBAPP.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n重排 {n_reordered} 个动作 / 删除 {n_dropped} 个不相关视频")


if __name__ == "__main__":
    drop = "--drop" in sys.argv
    main(drop_irrelevant=drop)
