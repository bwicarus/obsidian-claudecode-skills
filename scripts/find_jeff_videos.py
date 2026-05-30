#!/usr/bin/env python3
"""用 YouTube Data API v3 自动搜健身频道里每动作的教学视频。

对 fitness-plan.json 每个动作的 search_q 字段调 search.list API,
分别搜每个频道,取前 N 个 video_id + title 合并写回 plan.json 的
videos 数组。

支持的频道(--channels 用逗号分隔):
  jeff_nippard  Jeff Nippard (UC68TLK0mAEzUyHx5x5k-S1Q)  循证增肌
  athlean_x    Jeff Cavaliere ATHLEAN-X (UCe0TLA0EsQbE-MjuHXevj2A)  PT 动作机制 / 防伤

CLI:
  python find_jeff_videos.py [--channels jeff_nippard,athlean_x] [--per 5] [--dry-run]

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

CHANNELS = {
    "jeff_nippard": ("UC68TLK0mAEzUyHx5x5k-S1Q", "Jeff Nippard"),
    "athlean_x":    ("UCe0TLA0EsQbE-MjuHXevj2A", "Jeff Cavaliere"),
}


def _key() -> str:
    k = os.environ.get("GOOGLE_VISION_API_KEY") or os.environ.get("YOUTUBE_API_KEY")
    if k: return k.strip()
    if KEY_FILE.exists(): return KEY_FILE.read_text().strip()
    raise SystemExit("缺 API key(env GOOGLE_VISION_API_KEY / YOUTUBE_API_KEY 或 文件)")


def search_channel(q: str, channel_id: str, key: str, per: int) -> list[dict]:
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": q,
            "channelId": channel_id,
            "type": "video",
            "maxResults": per,
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
    ap.add_argument("--channels", default="jeff_nippard,athlean_x",
                    help="逗号分隔的频道 key,见 CHANNELS 字典")
    ap.add_argument("--per", type=int, default=5, help="每频道取前 N 个")
    args = ap.parse_args()
    key = _key()

    selected = []
    for ch in args.channels.split(","):
        ch = ch.strip()
        if ch not in CHANNELS:
            print(f"未知频道 {ch!r},可选: {list(CHANNELS)}", file=sys.stderr)
            return 1
        selected.append(ch)

    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    total_quota = 0
    for day in plan["days"]:
        for ex in day["exercises"]:
            q = ex.get("search_q")
            if not q:
                continue
            merged: list[dict] = []
            seen: set[str] = set()
            for ch in selected:
                cid, cname = CHANNELS[ch]
                try:
                    vids = search_channel(q, cid, key, args.per)
                    total_quota += 100
                except Exception as ex_err:
                    print(f"  ✗ {ex['name']} {cname}: {ex_err}", flush=True)
                    continue
                for v in vids:
                    if v["video_id"] in seen:
                        continue
                    seen.add(v["video_id"])
                    v_with_ch = {**v, "channel": cname}
                    merged.append(v_with_ch)
                time.sleep(0.3)

            print(f"\n[{ex['name']}] q={q!r}")
            for i, v in enumerate(merged):
                print(f"  [{i+1}] {v['video_id']}  ({v.get('channel', '?')})  {v['title'][:60]}")
            if not args.dry_run:
                ex["videos"] = merged

    print(f"\nquota 用量: 约 {total_quota} units(免费 10000/day)")

    if not args.dry_run:
        PLAN_PATH.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
