#!/usr/bin/env python3
"""用 YouTube Data API v3 自动搜 Jeff Nippard 频道里每动作的教学视频。

对 fitness-plan.json 每个动作的 search_q 字段调 search.list API:
- channelId 限定 Jeff Nippard(UC68TLK0mAEzUyHx5x5k-S1Q)
- type=video, maxResults=5
- 取前 5 个 video_id + title 写回 plan.json 的 videos 数组

CLI: python find_jeff_videos.py [--dry-run]
环境: 复用 Google Cloud Vision 同一 API key(/home/bwicarus/.config/gcp-vision-key)
       该 key 需在 Google Cloud Console "API 限制" 里勾上 YouTube Data API v3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

KEY_FILE = Path("/home/bwicarus/.config/gcp-vision-key")
PLAN_PATH = Path("/home/bwicarus/claude/_server_deploy/static/fitness-plan.json")
WEBAPP_PLAN = Path("/home/bwicarus/webapp/static/fitness-plan.json")

JEFF_CHANNEL = "UC68TLK0mAEzUyHx5x5k-S1Q"   # Jeff Nippard
MAX_RESULTS = 5


def _key() -> str:
    k = os.environ.get("GOOGLE_VISION_API_KEY") or os.environ.get("YOUTUBE_API_KEY")
    if k: return k.strip()
    if KEY_FILE.exists(): return KEY_FILE.read_text().strip()
    raise SystemExit("缺 API key(env GOOGLE_VISION_API_KEY / YOUTUBE_API_KEY 或 文件)")


def search_jeff(q: str, key: str) -> list[dict]:
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": q,
            "channelId": JEFF_CHANNEL,
            "type": "video",
            "maxResults": MAX_RESULTS,
            "key": key,
        },
        timeout=15,
    )
    data = r.json()
    if "error" in data:
        msg = data["error"].get("message", str(data["error"]))
        raise RuntimeError(f"YouTube API: {msg}")
    return [
        {"video_id": item["id"]["videoId"], "title": item["snippet"]["title"]}
        for item in data.get("items", [])
        if "videoId" in item.get("id", {})
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只搜不写回 plan.json")
    args = ap.parse_args()
    key = _key()
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))

    total_quota = 0
    for day in plan["days"]:
        for ex in day["exercises"]:
            q = ex.get("search_q")
            if not q:
                continue
            try:
                videos = search_jeff(q, key)
                total_quota += 100   # search.list = 100 units
            except Exception as ex_err:
                print(f"  ✗ {ex['name']}: {ex_err}", flush=True)
                continue
            print(f"\n[{ex['name']}] q={q!r}")
            for i, v in enumerate(videos):
                print(f"  [{i+1}] {v['video_id']}  {v['title'][:70]}")
            if not args.dry_run:
                ex["videos"] = videos
            time.sleep(0.3)   # 避免被 quota burst

    print(f"\nquota 用量: 约 {total_quota} units(免费 10000/day)")

    if not args.dry_run:
        PLAN_PATH.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 同步部署副本(if 存在)
        if WEBAPP_PLAN.parent.exists():
            WEBAPP_PLAN.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(f"已写回 {PLAN_PATH}")
        if WEBAPP_PLAN.parent.exists():
            print(f"同步到 {WEBAPP_PLAN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
