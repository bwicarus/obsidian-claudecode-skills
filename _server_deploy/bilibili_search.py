"""bilibili_search.py — 阅读器助手「搜索视频」工具的 Bilibili 源(与 youtube_search.py 并联,同一框架)。

直连 HTTP(不走 MCP):B 站 web 搜索现在要 **WBI 签名 + buvid 激活**过风控。流程:
  1) 起 session → 访问首页 + /x/frontend/finger/spi 拿 buvid3/buvid4 → 设 cookie;
  2) POST ExClimbWuzhi「激活」buvid(不激活搜索只回 v_voucher 风控凭证、没结果);
  3) /x/web-interface/nav 拿 wbi_img.img_url/sub_url → 算 mixin_key;
  4) wbi/search/type 带 wts + w_rid(md5(query+mixin_key)) 签名 → 拿结果。
激活好的 session + WBI keys 缓存在模块内(WBI keys 每日轮换,>12h 刷新);风控(只回 v_voucher)→ 重激活重试一次。
query→结果永久缓存(state/bili-search-cache/,30 天)省重复请求。B 站搜索无硬配额,不记额度。
部署:cp 到 /home/bwicarus/webapp/(跟 youtube_search.py / assistant.py 同目录)。
"""
import hashlib
import html as _html
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

import requests

_ROOT = Path(os.environ.get("CLAUDE_PROJECT") or "/home/bwicarus/claude")
_CACHE = _ROOT / "state" / "bili-search-cache"
_TTL = 30 * 86400   # 30 天
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# WBI mixin 打乱表(B 站前端固定常量;img_key+sub_key 按此重排取前 32 位)
_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
          33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
          26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

_lock = threading.Lock()
_sess = None          # 激活好的 requests.Session
_wbi = None           # {"mixin": str, "ts": float}(WBI mixin_key + 取得时刻)

_TAG_RE = re.compile(r"<[^>]+>")


def _mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN)[:32]


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Referer": "https://www.bilibili.com/",
                      "Origin": "https://www.bilibili.com"})
    return s


def _activate(s: requests.Session) -> None:
    """走完整 buvid 激活(spi 拿 buvid3/4 + ExClimbWuzhi),否则搜索被风控只回 v_voucher。"""
    try:
        s.get("https://www.bilibili.com/", timeout=10)
    except Exception:
        pass
    try:
        spi = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=10).json()
        b3 = (spi.get("data") or {}).get("b_3")
        b4 = (spi.get("data") or {}).get("b_4")
        if b3:
            s.cookies.set("buvid3", b3, domain=".bilibili.com")
        if b4:
            s.cookies.set("buvid4", b4, domain=".bilibili.com")
    except Exception:
        pass
    try:
        s.cookies.set("b_nut", str(int(time.time())), domain=".bilibili.com")
    except Exception:
        pass
    # ExClimbWuzhi:提交一份浏览器指纹 payload「激活」buvid(payload 内容不需真实,字段齐即可)
    payload = {"3064": 1, "5062": str(int(time.time() * 1000)), "03bf": "https://www.bilibili.com/",
               "39c8": "333.1007.fp.risk", "34f1": "", "d402": "", "654a": "", "6e7c": "1157x707",
               "3c43": {"2673": 0, "5766": 24, "6527": 0, "7003": 1, "807e": 1, "b8ce": _UA,
                        "641c": 0, "07a4": "zh-CN", "1c57": "not-supported", "0bd0": 16,
                        "748e": 1440, "d61f": 810, "fc9d": -480, "6aa9": "Asia/Shanghai",
                        "75b8": 1, "3b21": 1, "8a1c": 0, "d52f": "not-supported", "b7b4": "not-supported"}}
    try:
        s.post("https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi",
               json={"payload": json.dumps(payload)}, timeout=10)
    except Exception:
        pass


def _wbi_mixin(s: requests.Session) -> str:
    nav = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
    wi = nav["data"]["wbi_img"]
    ik = wi["img_url"].rsplit("/", 1)[1].split(".")[0]
    sk = wi["sub_url"].rsplit("/", 1)[1].split(".")[0]
    return _mixin_key(ik + sk)


def _ensure_session():
    """返回 (激活好的 session, wbi mixin_key)。首次或被清空 → 重激活;WBI keys >12h 刷新。"""
    global _sess, _wbi
    now = time.time()
    if _sess is None:
        _sess = _new_session()
        _activate(_sess)
        _wbi = None
    if _wbi is None or (now - _wbi.get("ts", 0)) > 12 * 3600:
        _wbi = {"mixin": _wbi_mixin(_sess), "ts": now}
    return _sess, _wbi["mixin"]


def _sign(params: dict, mixin: str) -> dict:
    p = dict(params)
    p["wts"] = int(time.time())
    p = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(p.items())}
    p["w_rid"] = hashlib.md5((urllib.parse.urlencode(p) + mixin).encode()).hexdigest()
    return p


def _raw_search(query: str, n: int, order: str = "totalrank") -> dict:
    s, mixin = _ensure_session()
    # order=totalrank(综合,relevance+质量;default)。取较大候选池,再本地按质量分排序 → 保留相关性的同时优先高质。
    p = _sign({"search_type": "video", "keyword": query, "order": order,
               "page": 1, "page_size": n}, mixin)
    r = s.get("https://api.bilibili.com/x/web-interface/wbi/search/type",
              params=p, timeout=12,
              headers={"Referer": "https://search.bilibili.com/"})
    return r.json()


def _https(u: str) -> str:
    u = (u or "").strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://"):
        return "https://" + u[7:]
    return u


def _clean_title(t: str) -> str:
    return _html.unescape(_TAG_RE.sub("", t or "")).strip()


def search(query: str, max_results: int = 6) -> dict:
    """搜 Bilibili 视频。返回 {ok, videos:[{src:'bili', id(bvid), title, channel, thumb, desc, dur, play}], cached?}
    或 {ok:False, error}。id 用 bvid(前端据 'BV' 前缀识别为 B 站、走 B 站播放器)。"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "缺 query"}
    q = q[:120]
    n = max(1, min(12, int(max_results or 6)))
    pool = max(30, n * 4)   # 多取候选,本地按质量分排序后返回 top n(相关性靠 totalrank 池 + 后续 AI 筛)
    ck = hashlib.sha1(("bili|q2|" + q.lower() + "|" + str(n)).encode("utf-8")).hexdigest()[:20]  # q2:排序算法变了 → 缓存键升版免旧结果
    cf = _CACHE / (ck + ".json")
    try:
        if cf.exists():
            d = json.loads(cf.read_text("utf-8"))
            if time.time() - d.get("ts", 0) < _TTL and d.get("videos"):
                return {"ok": True, "videos": d["videos"], "cached": True}
    except Exception:
        pass
    try:
        with _lock:
            d = _raw_search(q, pool)
            data = d.get("data") or {}
            if not (data.get("result")):
                # 只回 v_voucher / 空 = 风控或 session 失效 → 重激活重试一次
                global _sess, _wbi
                _sess = None
                _wbi = None
                d = _raw_search(q, pool)
                data = d.get("data") or {}
    except Exception as e:
        return {"ok": False, "error": "B 站请求失败:" + str(e)[:100]}
    if d.get("code") not in (0, None):
        return {"ok": False, "error": "B 站 API:" + str(d.get("message") or d.get("code"))[:80]}
    result = data.get("result") or []
    if not result:
        return {"ok": False, "error": "B 站没搜到视频(或被风控),换个关键词"}
    vids = []
    for it in result:
        bv = it.get("bvid")
        if not bv:
            continue
        vids.append({
            "src": "bili",
            "id": bv,
            "title": _clean_title(it.get("title")),
            "channel": it.get("author") or "",
            "thumb": _https(it.get("pic") or ""),
            "desc": _clean_title(it.get("description") or "")[:300],
            "tag": (it.get("tag") or "")[:120],   # 逗号分隔标签,喂 AI 筛相关性很有用
            "dur": it.get("duration") or "",     # "mm:ss" 字符串
            "play": it.get("play") or 0,         # 播放量(质量信号)
            "like": it.get("like") or 0,         # 点赞(内部质量分用)
            "favorites": it.get("favorites") or 0,   # 收藏(内部质量分用)
            "url": "https://www.bilibili.com/video/" + bv,
        })
    if not vids:
        return {"ok": False, "error": "B 站没搜到可用视频,换个关键词"}
    # 质量排序:综合 播放 + 点赞×8 + 收藏×12(点赞/收藏权重高=真被认可,非纯点击标题党)。
    #   再过滤明显低质:优先保留播放 ≥ 8000 的;不足 n 个才放低门槛补齐(避免返回冷门低质垃圾)。
    def _q(v):
        return (v.get("play") or 0) + (v.get("like") or 0) * 8 + (v.get("favorites") or 0) * 12
    vids.sort(key=_q, reverse=True)
    strong = [v for v in vids if (v.get("play") or 0) >= 8000]
    weak = [v for v in vids if (v.get("play") or 0) < 8000]
    vids = (strong if len(strong) >= n else strong + weak)[:n]
    for v in vids:   # 内部质量分字段用完即删(不进 AI 上下文/前端)
        v.pop("like", None)
        v.pop("favorites", None)
    try:
        _CACHE.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps({"query": q, "videos": vids, "ts": int(time.time())}, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return {"ok": True, "videos": vids}
