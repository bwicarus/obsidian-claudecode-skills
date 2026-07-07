"""youtube_search.py — 阅读器助手「搜索视频」工具的后端(YouTube Data API v3 search.list)。

配额:search.list = 100 units/次,GCP 项目每天 10k(跟健身视频轮播共享同一配额)→ **永久缓存**
query→结果(state/yt-search-cache/,30 天)省配额、也让重复问同一主题秒回。
key 来源:env GOOGLE_VISION_API_KEY / YOUTUBE_API_KEY(跟 scripts/find_jeff_videos 一致)。
videoEmbeddable=true:只返回**能内嵌播放**的视频(否则前端 iframe 会「视频不可用」)。
部署:cp 到 /home/bwicarus/webapp/(跟 assistant.py 同目录)。
"""
import hashlib
import json
import os
import time
from pathlib import Path

import requests

_ROOT = Path(os.environ.get("CLAUDE_PROJECT") or "/home/bwicarus/claude")
_CACHE = _ROOT / "state" / "yt-search-cache"
_TTL = 30 * 86400   # 30 天


def _key() -> str:
    k = os.environ.get("GOOGLE_VISION_API_KEY") or os.environ.get("YOUTUBE_API_KEY")
    if k:
        return k.strip()
    kf = Path("/home/bwicarus/.config/gcp-vision-key")
    try:
        if kf.exists():
            return kf.read_text().strip()
    except Exception:
        pass
    return ""


def _log(units: int, note: str):
    """记进本地配额计数器(跟健身视频共享 youtube 配额,方便控制面板看用量)。"""
    try:
        import sys
        sp = str(_ROOT / "scripts")
        if sp not in sys.path:
            sys.path.insert(0, sp)
        import google_api_quota
        google_api_quota.log_usage("youtube", units, "search.list", note)
    except Exception:
        pass


def search(query: str, max_results: int = 6, lang: str = None) -> dict:
    """搜 YouTube 视频。返回 {ok, videos:[{id,title,channel,thumb}], cached?} 或 {ok:False, error}。"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "缺 query"}
    q = q[:120]
    n = max(1, min(10, int(max_results or 6)))
    ck = hashlib.sha1((q.lower() + "|" + str(n) + "|" + (lang or "")).encode("utf-8")).hexdigest()[:20]
    cf = _CACHE / (ck + ".json")
    try:
        if cf.exists():
            d = json.loads(cf.read_text("utf-8"))
            if time.time() - d.get("ts", 0) < _TTL and d.get("videos"):
                return {"ok": True, "videos": d["videos"], "cached": True}
    except Exception:
        pass
    key = _key()
    if not key:
        return {"ok": False, "error": "服务器没配 YouTube API key"}
    params = {"part": "snippet", "q": q, "type": "video", "videoEmbeddable": "true",
              "maxResults": n, "safeSearch": "moderate", "key": key}
    if lang:
        params["relevanceLanguage"] = lang
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=15)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": "请求失败:" + str(e)[:100]}
    if "error" in data:
        msg = data["error"].get("message", "未知")
        _log(100, "FAILED: " + msg[:80])
        if "quota" in msg.lower():
            return {"ok": False, "error": "今日视频搜索配额已用完(每天上限),明天再试"}
        return {"ok": False, "error": "YouTube API: " + msg[:100]}
    _log(100, "q=" + q[:50])
    vids = []
    for it in data.get("items", []):
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if not vid:
            continue
        th = sn.get("thumbnails") or {}
        thumb = (th.get("medium") or th.get("high") or th.get("default") or {}).get("url", "")
        vids.append({"id": vid, "title": sn.get("title", ""),
                     "channel": sn.get("channelTitle", ""), "thumb": thumb,
                     "desc": (sn.get("description") or "")[:300]})   # 描述节选(search snippet 免费带,供 AI 判断相关性)
    if not vids:
        return {"ok": False, "error": "没搜到可嵌入的视频,换个关键词"}
    try:
        _CACHE.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps({"query": q, "videos": vids, "ts": int(time.time())}, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return {"ok": True, "videos": vids}
