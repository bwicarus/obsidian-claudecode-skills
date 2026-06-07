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

sys.path.insert(0, str(Path(__file__).parent))
try:
    from google_api_quota import log_usage as _log_quota
except Exception:
    def _log_quota(*a, **kw): pass

KEY_FILE = Path("/home/bwicarus/.config/gcp-vision-key")
PLAN_PATH = Path("/home/bwicarus/claude/_server_deploy/static/fitness-plan.json")
WEBAPP_PLAN = Path("/home/bwicarus/webapp/static/fitness-plan.json")

CHANNELS = {
    "jeff_nippard": ("UC68TLK0mAEzUyHx5x5k-S1Q", "Jeff Nippard"),
    "athlean_x":    ("UCe0TLA0EsQbE-MjuHXevj2A", "Jeff Cavaliere"),
}

# 每动作 **必含**关键词(case-insensitive,任一命中即保留;空 list = 不限制)。
# YouTube 限定 channelId 后,搜不到精确匹配会用"频道里相关的"补,导致
# 标题完全不沾边的视频也回来。靠 must_contain 过滤掉这类噪声。
# ⚠ 键名必须跟 plan.json 里的 exercise id 完全一致(曾把侧平举写成 db_lateral_raise,
#    而 plan 里是 cable_lateral_raise → 取不到过滤词 → 侧平举什么肩部视频都进来)。
MUST_CONTAIN: dict[str, list[str]] = {
    # Push
    "db_bench_flat":         ["bench", "chest"],
    "db_bench_incline":      ["incline", "upper chest"],
    "db_shoulder_press":     [],                       # 只靠 exclude 去掉 lateral/rear,其余肩推都留
    "cable_lateral_raise":   ["lateral", "side", "delt", "raise", "shoulder"],
    "cable_tricep_pushdown": ["pushdown", "triceps", "tricep"],
    "db_overhead_tricep":    ["triceps", "tricep", "overhead"],
    "dip":                   ["dip"],
    # Pull
    "chin_up":               ["chin", "pull-up", "pull up", "chinup", "pullup"],
    "lat_pulldown":          ["pulldown", "pull-down", "pull down", "lat"],
    "db_row_single":         ["row", "back"],
    "seated_row":            ["row", "back"],
    "face_pull":             ["face pull", "rear delt"],
    "ez_curl":               ["curl", "biceps", "bicep"],
    "db_incline_curl":       ["curl", "biceps", "bicep"],
    # Legs
    "goblet_squat":          ["squat", "quad"],
    "rdl":                   ["rdl", "romanian", "deadlift", "hamstring"],
    "leg_extension":         ["leg extension", "quad"],
    "leg_curl":              ["leg curl", "hamstring"],
    "db_calf_raise":         ["calf", "calve"],        # calves 含 calve 不含 calf
    "hanging_leg_raise":     ["leg raise", "hanging", "knee raise", "ab", "abs"],
}

# 每动作 **排除**关键词(case-insensitive,命中即剔,优先级高于 must_contain)。
# 用来剔掉"标题沾边 must_contain 但其实是另一个动作/另一块肌群"的污染——
# 例如 goblet_squat 的 ["squat"] 会放行 "Bulgarian Split Squat"(另一个动作),
# 划船会混进 "Lat Pulldown"/"Rear Delt"(fallback 凑数塞的)。
MUST_EXCLUDE: dict[str, list[str]] = {
    "db_shoulder_press":   ["lateral", "rear delt"],
    "cable_lateral_raise": ["overhead press", "face pull"],
    "db_overhead_tricep":  ["pushdown"],
    "chin_up":             ["pulldown"],
    "lat_pulldown":        ["row", "rear delt"],
    "db_row_single":       ["pulldown", "rear delt"],
    "seated_row":          ["pulldown", "rear delt"],
    "goblet_squat":        ["split", "bulgarian", "leg press", "hack", "pistol"],
    "rdl":                 ["quad", "split"],
    "leg_extension":       ["hamstring", "glute", "leg press"],
    "leg_curl":            ["quad", "leg press"],
    "db_calf_raise":       ["quad"],
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
        # quota 错也算消耗(请求已发,即使失败也常计费)
        _log_quota("youtube", 100, "search.list", note=f"FAILED: {msg[:80]}")
        raise RuntimeError(f"YouTube API: {msg}")
    _log_quota("youtube", 100, "search.list", note=f"q={q[:60]}")
    return [
        {"video_id": item["id"]["videoId"], "title": item["snippet"]["title"]}
        for item in data.get("items", [])
        if "videoId" in item.get("id", {})
    ]


def _title_matches(title: str, must_contain: list[str]) -> bool:
    """case-insensitive substring 任一命中即 True;空 must_contain 视为命中。"""
    if not must_contain:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in must_contain)


def _passes(title: str, must_contain: list[str], must_exclude: list[str]) -> bool:
    """命中任一 must_exclude → 剔(优先级最高);否则按 must_contain 判定。"""
    t = title.lower()
    if must_exclude and any(kw.lower() in t for kw in must_exclude):
        return False
    return _title_matches(title, must_contain)


def _write_plans(plan: dict) -> None:
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写回 {PLAN_PATH}")
    if WEBAPP_PLAN.parent.exists():
        WEBAPP_PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"同步到 {WEBAPP_PLAN}")


def prune_existing(dry: bool) -> int:
    """不调 API:按 MUST_CONTAIN+MUST_EXCLUDE 过滤现有 plan.json 的 videos,剔掉跑偏的。
    用于规则更新后立刻清洗存量数据(YouTube 搜索结果会漂移,能不重抓就不重抓)。"""
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    for day in plan["days"]:
        for ex in day["exercises"]:
            inc = ex.get("must_contain") or MUST_CONTAIN.get(ex["id"], [])
            exc = ex.get("must_exclude") or MUST_EXCLUDE.get(ex["id"], [])
            vids = ex.get("videos", [])
            kept = [v for v in vids if _passes(v.get("title", ""), inc, exc)]
            dropped = [v for v in vids if v not in kept]
            if dropped:
                print(f"\n[{ex['name']}] 留 {len(kept)} 剔 {len(dropped)}")
                for v in dropped:
                    print(f"    ✗ {v.get('title','')[:66]}")
            ex["videos"] = kept
    if not dry:
        _write_plans(plan)
    else:
        print("\n(dry-run,未写回)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只搜不写回 plan.json")
    ap.add_argument("--channels", default="jeff_nippard,athlean_x",
                    help="逗号分隔的频道 key,见 CHANNELS 字典")
    ap.add_argument("--per", type=int, default=5, help="每频道取前 N 个")
    ap.add_argument("--prune-existing", action="store_true",
                    help="不调 API:按 MUST_CONTAIN+MUST_EXCLUDE 过滤现有 plan.json 的 videos(规则更新后清洗存量)")
    args = ap.parse_args()

    if args.prune_existing:
        return prune_existing(args.dry_run)

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
    # 搜 OVERSEARCH 倍数,然后用 must_contain 过滤后留 per 个;不够再补 fallback
    OVERSEARCH = 3
    for day in plan["days"]:
        for ex in day["exercises"]:
            q = ex.get("search_q")
            if not q:
                continue
            must_contain = ex.get("must_contain") or MUST_CONTAIN.get(ex["id"], [])
            must_exclude = ex.get("must_exclude") or MUST_EXCLUDE.get(ex["id"], [])
            merged: list[dict] = []
            seen: set[str] = set()
            for ch in selected:
                cid, cname = CHANNELS[ch]
                try:
                    vids = search_channel(q, cid, key, args.per * OVERSEARCH)
                    total_quota += 100
                except Exception as ex_err:
                    print(f"  ✗ {ex['name']} {cname}: {ex_err}", flush=True)
                    continue
                # 只留通过 include+exclude 的;**不再用跑偏视频凑数**(宁可少而准 — 凑数的
                # fallback 正是"视频和动作对不上"的根源)。
                kept: list[dict] = []
                for v in vids:
                    if v["video_id"] in seen:
                        continue
                    if _passes(v["title"], must_contain, must_exclude):
                        kept.append({**v, "channel": cname})
                for v in kept[: args.per]:
                    seen.add(v["video_id"])
                    merged.append(v)
                time.sleep(0.3)

            print(f"\n[{ex['name']}] q={q!r}  留 {len(merged)} (include={must_contain} exclude={must_exclude})")
            for i, v in enumerate(merged):
                print(f"  [{i+1}] {v['video_id']}  ({v.get('channel', '?')})  {v['title'][:60]}")
            if not args.dry_run:
                ex["videos"] = [
                    {"video_id": v["video_id"], "title": v["title"], "channel": v.get("channel", "")}
                    for v in merged
                ]

    print(f"\nquota 用量: 约 {total_quota} units(免费 10000/day)")
    if not args.dry_run:
        _write_plans(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
