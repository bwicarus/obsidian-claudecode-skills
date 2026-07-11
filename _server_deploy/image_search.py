"""image_search.py — 配图搜**真实图片**(非 AI 生成),按关键词搜(脱开上下文),多源 + 格式过滤 + 缓存。

源(按优先级):
  1) Wikimedia Commons 全库搜(免 key,教育/科学/数学示意图覆盖好;不止条目首图)
  2) Google Custom Search 图片(可选,更宽、生僻词也能找到)——需 **两样都配好**才启用:
     · GCP 项目启用 "Custom Search API"
     · 一个 Programmable Search Engine 的 cx(env GOOGLE_CSE_ID 或 server-config.image.cse_id)
     没配 → 静默跳过(只用 Commons)。

过滤:只留 jpg/png/svg/gif(踢掉 .djvu/.pdf/.tif 书扫、音视频)、太小的图(<120px)剔除。
缓存:query→结果 30 天(state/img-search-cache/)。
部署:cp 到 /home/bwicarus/webapp/(跟 assistant.py 同目录)。
"""
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(os.environ.get("CLAUDE_PROJECT") or "/home/bwicarus/claude")
_CACHE = _ROOT / "state" / "img-search-cache"
_TTL = 30 * 86400
_UA = "study-reader/1.0 (bwicarus; educational)"

_GOOD = re.compile(r"\.(jpe?g|png|svg|gif)(\?|$)", re.I)      # 真图片格式
_BAD = re.compile(r"\.(djvu|pdf|tiff?|ogv|webm|ogg|mid|xcf)(\?|$)", re.I)  # 书扫/音视频/工程文件


def _get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _key() -> str:
    # 专用 key 优先:server-config.image.api_key(Custom Search 若配在别的 GCP 项目,给一把那个项目的 AIzaSy* key)。
    try:
        cfg = json.loads((_ROOT / "state" / "server-config.json").read_text("utf-8"))
        ik = ((cfg.get("image") or {}).get("api_key") or "").strip()
        if ik:
            return ik
    except Exception:
        pass
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


def _cse_id() -> str:
    v = (os.environ.get("GOOGLE_CSE_ID") or "").strip()
    if v:
        return v
    try:
        cfg = json.loads((_ROOT / "state" / "server-config.json").read_text("utf-8"))
        return ((cfg.get("image") or {}).get("cse_id") or cfg.get("google_cse_id") or "").strip()
    except Exception:
        return ""


def _ok_img(url: str, w=0, h=0) -> bool:
    if not url or _BAD.search(url) or not _GOOD.search(url):
        return False
    if w and h and (w < 120 or h < 120):   # 太小的多是图标/占位
        return False
    return True


def search_commons(query: str, n: int = 6) -> list:
    """Wikimedia Commons 全库图片搜(File 命名空间)。免 key。"""
    p = {"action": "query", "format": "json", "generator": "search", "gsrsearch": query,
         "gsrnamespace": "6", "gsrlimit": str(min(n * 3, 30)), "prop": "imageinfo",
         "iiprop": "url|size|mime", "iiurlwidth": "640"}
    try:
        d = _get_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(p))
    except Exception:
        return []
    out = []
    for pg in (d.get("query", {}).get("pages", {}) or {}).values():
        ii = (pg.get("imageinfo") or [{}])[0]
        u = ii.get("url", "")
        if not (ii.get("mime", "").startswith("image")):
            continue
        if not _ok_img(u, ii.get("width", 0) or 0, ii.get("height", 0) or 0):
            continue
        ttl = (pg.get("title", "") or "")
        out.append({"image_url": ii.get("thumburl") or u, "full_url": u,
                    "title": ttl.replace("File:", "")[:80],
                    "page_url": "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(ttl),
                    "source": "commons"})
        if len(out) >= n:
            break
    return out


_CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def search_bing_scrape(query: str, n: int = 4) -> list:
    """搜索引擎图搜结果页直爬(零 key,用户提议"最原始的浏览器地址式"):Google 对无浏览器请求
    甩 JS challenge(实测重定向占位页,需无头浏览器不值得),Bing 一次 HTTP 就给全量
    `"murl":"原图URL"`(media url)+`"purl":"宿主页"`,结构稳定反爬松——用它当无 API 兜底。"""
    url = "https://www.bing.com/images/search?" + urllib.parse.urlencode({"q": query, "count": "35"})
    req = urllib.request.Request(url, headers={"User-Agent": _CHROME_UA, "Accept-Language": "ja,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception as ex:
        import sys
        sys.stderr.write(f"[image_search] bing fail: {str(ex)[:120]}\n")
        return []
    import html as _hm
    txt = _hm.unescape(html)
    murls = re.findall(r'"murl":"(https?://[^"]+?)"', txt)
    purls = re.findall(r'"purl":"(https?://[^"]+?)"', txt)
    out, seen = [], set()
    for i, u in enumerate(murls):
        u = u.replace("\\/", "/")
        if u in seen or not _ok_img(u):   # 无扩展名的间接链(instagram lookaside 等)一并被格式过滤踢掉
            continue
        seen.add(u)
        out.append({"image_url": u, "full_url": u, "title": "",
                    "page_url": (purls[i].replace("\\/", "/") if i < len(purls) else u),
                    "source": "bing"})
        if len(out) >= n:
            break
    return out


def search_web(query: str, n: int = 5) -> list:
    """通用网页搜索(同一个 Programmable Search Engine,不带 searchType 即网页结果)。
    与图搜共享每日 100 次免费池;没配 key/cx 返回 []。返回 [{title, snippet, url}]。"""
    cx, key = _cse_id(), _key()
    if not cx or not key:
        return []
    p = {"key": key, "cx": cx, "q": query, "num": str(max(1, min(n, 10))), "safe": "active"}
    try:
        d = _get_json("https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(p))
    except Exception:
        return []
    try:
        import sys
        sp = str(_ROOT / "scripts")
        if sp not in sys.path:
            sys.path.insert(0, sp)
        import google_api_quota
        google_api_quota.log_usage("customsearch", 1, "web", query[:40])
    except Exception:
        pass
    if d.get("error"):
        return []
    return [{"title": (it.get("title") or "")[:120],
             "snippet": (it.get("snippet") or "").replace("\n", " ")[:300],
             "url": it.get("link") or ""}
            for it in (d.get("items") or [])[:n]]


def search_google(query: str, n: int = 4) -> list:
    """Google Custom Search 图片。需 key + cx + 已启用 Custom Search API,否则返回 []。"""
    cx, key = _cse_id(), _key()
    if not cx or not key:
        return []
    p = {"key": key, "cx": cx, "searchType": "image", "q": query,
         "num": str(max(1, min(n, 10))), "safe": "active", "imgSize": "medium"}
    try:
        d = _get_json("https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(p))
    except Exception as ex:
        import sys
        sys.stderr.write(f"[image_search] google fail: {str(ex)[:120]} (403=Custom Search API 未启用/额度尽)\n")
        return []
    try:   # 记本地配额(每天 100 免费)
        import sys
        sp = str(_ROOT / "scripts")
        if sp not in sys.path:
            sys.path.insert(0, sp)
        import google_api_quota
        google_api_quota.log_usage("customsearch", 1, "image", query[:40])
    except Exception:
        pass
    if d.get("error"):
        return []
    out = []
    for it in d.get("items", []):
        u = it.get("link", "")
        img = it.get("image", {}) or {}
        if not _ok_img(u, img.get("width", 0) or 0, img.get("height", 0) or 0):
            continue
        out.append({"image_url": img.get("thumbnailLink") or u, "full_url": u,
                    "title": (it.get("title", "") or "")[:80],
                    "page_url": img.get("contextLink", ""), "source": "google"})
        if len(out) >= n:
            break
    return out


def search_images(query: str, n: int = 2, want_google: bool = True) -> list:
    """按关键词搜真实图片:Commons 优先(教育质量),不足 n 张再用 Google 补(若已配好)。去重、缓存。"""
    q = (query or "").strip()
    if not q:
        return []
    q = q[:120]
    ck = hashlib.sha1(("img|" + q.lower() + "|" + str(n)).encode("utf-8")).hexdigest()[:20]
    cf = _CACHE / (ck + ".json")
    try:
        if cf.exists():
            d = json.loads(cf.read_text("utf-8"))
            # partial=Google 源没干活(未启用/额度尽)时的残缺结果:只信 1 天,别把断腿期的空缓存钉 30 天
            ttl = 86400 if d.get("partial") else _TTL
            if time.time() - d.get("ts", 0) < ttl and d.get("images") is not None:
                return d["images"][:n]
    except Exception:
        pass
    imgs = search_commons(q, n)
    google_ok = True
    if want_google and len(imgs) < n:
        seen = {i["full_url"] for i in imgs}
        gs = search_google(q, n - len(imgs) + 2)
        if not gs:
            gs = search_bing_scrape(q, n - len(imgs) + 2)   # 零 key 兜底(Google API 未启用/额度尽)
        if not gs:
            google_ok = False   # Commons 之外的第二腿(Google API+Bing 爬取)都没跑成
        for x in gs:
            if x["full_url"] not in seen:
                imgs.append(x)
                seen.add(x["full_url"])
    imgs = imgs[:n]
    partial = bool(want_google and len(imgs) < n and not google_ok)
    try:
        _CACHE.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps({"q": q, "images": imgs, "ts": int(time.time()), "partial": partial}, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return imgs
