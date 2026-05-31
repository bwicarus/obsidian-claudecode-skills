"""例句翻译（英→中），多源优先级：DeepL (有 key) > MyMemory (免费) > 不译。

API：translate(text, target='zh-CN') → str  (空 = 失败)
缓存：state/dict-cache/tr-<sha>.json，TTL 90 天
配置：state/server-config.json 的 dict.deepl_key（可选）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
CFG_PATH     = PROJECT_ROOT / "state" / "server-config.json"
CACHE_DIR    = PROJECT_ROOT / "state" / "dict-cache"

_CFG: dict | None = None
def _cfg() -> dict:
    global _CFG
    if _CFG is None:
        try: _CFG = json.loads(CFG_PATH.read_text("utf-8"))
        except Exception: _CFG = {}
    return _CFG.get("dict", {})


def _cache_path(text: str, target: str) -> Path:
    sha = hashlib.sha1(f"{target}::{text}".encode("utf-8")).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"tr-{sha}.json"


def _cache_get(text: str, target: str, ttl_days: int = 36500) -> str | None:
    """翻译缓存读取。默认 TTL 100 年（实际永久；用户手动删 cache 文件才会重翻）。"""
    p = _cache_path(text, target)
    if not p.exists(): return None
    try:
        if (time.time() - p.stat().st_mtime) > ttl_days * 86400:
            return None
        d = json.loads(p.read_text("utf-8"))
        return d.get("tr") or ""
    except Exception:
        return None


def _cache_put(text: str, target: str, tr: str, source: str):
    try:
        _cache_path(text, target).write_text(
            json.dumps({"src": text, "tr": tr, "target": target, "source": source}, ensure_ascii=False, indent=2),
            "utf-8")
    except Exception:
        pass


def _gcp_key() -> str:
    """GCP API key:env 优先(GOOGLE_VISION_API_KEY/GCP_API_KEY),回落 ~/.config/gcp-vision-key
    (跟 Vision/STT 共用同一 key)。"""
    k = os.environ.get("GOOGLE_VISION_API_KEY") or os.environ.get("GCP_API_KEY")
    if k:
        return k.strip()
    try:
        from pathlib import Path
        return Path("/home/bwicarus/.config/gcp-vision-key").read_text("utf-8").strip()
    except Exception:
        return ""


def _gtranslate(text: str, target: str = "zh-CN") -> str | None:
    """Google Cloud Translation v2。~0.3s、质量高、走 GCP 赠金。
    需在 GCP 控制台:① 启用 Cloud Translation API ② 给该 key 的「API 限制」放行 Translation。
    未放行会 403(API_KEY_SERVICE_BLOCKED)→ 返回 None,auto 链自动回落 mymemory。
    source 不指定 → 让 Google 自动检测(en/ja/… 都准),target=zh-CN。"""
    key = _gcp_key()
    if not key:
        return None
    tgt = "zh-CN" if target.startswith("zh") else target
    try:
        data = urllib.parse.urlencode(
            {"q": text, "target": tgt, "format": "text", "key": key}).encode("utf-8")
        req = urllib.request.Request(
            "https://translation.googleapis.com/language/translate/v2", data=data)
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        trs = ((d.get("data") or {}).get("translations") or [])
        tr = (trs[0].get("translatedText", "").strip() if trs else "")
        return tr or None
    except Exception:
        return None


def _deepl(text: str, target: str = "zh-CN") -> str | None:
    key = _cfg().get("deepl_key", "").strip()
    if not key:
        return None
    # free key 用 api-free.deepl.com，pro 用 api.deepl.com
    base = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
    data = urllib.parse.urlencode({
        "auth_key": key,
        "text": text,
        "target_lang": "ZH" if target.startswith("zh") else target.upper(),
    }).encode("utf-8")
    try:
        req = urllib.request.Request(f"{base}/v2/translate", data=data)
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        items = d.get("translations") or []
        if items: return items[0].get("text", "").strip()
    except Exception:
        return None
    return None


def _ai_translate(text: str, target: str = "zh-CN", model: str = "sonnet", effort: str = "low") -> str | None:
    """用 AI 后端翻译（claude_cli / codex_cli / openai_api / ollama）。
    比 MyMemory 质量高，但耗 AI 额度。"""
    try:
        import sys as _sys
        _sys.path.insert(0, str(PROJECT_ROOT / "_client" / "core"))
        _sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        _sys.path.insert(0, str(PROJECT_ROOT / "_server_deploy"))
        from ai_backends import make_backend
        try:
            from qa_server import get_cfg
            cfg_all = get_cfg()
        except Exception:
            cfg_all = json.loads(CFG_PATH.read_text("utf-8")) if CFG_PATH.exists() else {}
        backend_name = cfg_all.get("ai_backend", "claude_cli")
        settings = dict((cfg_all.get("ai") or {}).get(backend_name, {}))
        if model:  settings["model"] = model
        if effort: settings["effort"] = effort
        ad = make_backend(backend_name, settings)
        target_zh = "中文" if target.startswith("zh") else target
        sys_msg = {"role": "system", "content": "你是专业英汉翻译助手。只输出译文，不要解释或注释。"}
        user_msg = {"role": "user", "content": f"把下面这句翻译成{target_zh}：\n\n{text}"}
        zh = ad.chat([sys_msg, user_msg])
        if zh:
            zh = zh.strip()
            # 去掉 AI 可能加的引号 / 前缀
            if zh.startswith(('"', '"', "'", "「")) and zh.endswith(('"', '"', "'", "」")):
                zh = zh[1:-1].strip()
            for prefix in ["译文：", "翻译：", "Translation:", "Answer:"]:
                if zh.startswith(prefix):
                    zh = zh[len(prefix):].strip()
                    break
            return zh or None
    except Exception:
        return None
    return None


def _detect_src(text: str) -> str:
    """粗判源语言(给免费翻译 API 用):含假名→ja;纯汉字无拉丁→ja(本项目场景是日语书);否则 en。"""
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    if re.search(r"[㐀-鿿一-鿿]", text) and not re.search(r"[A-Za-z]", text):
        return "ja"
    return "en"


def _mymemory(text: str, target: str = "zh-CN") -> str | None:
    """MyMemory free。匿名 5000 字/天；带 de=email 50000 字/天。"""
    # source 之前写死 en → 日语句子被当英语翻、出乱码/空,导致退化到慢的 AI。改自动判源。
    src_target = f"{_detect_src(text)}|{target}"
    params = {"q": text, "langpair": src_target}
    email = (_cfg().get("mymemory_email") or "").strip()
    if email:
        params["de"] = email   # 提配额到 50K/day
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        if d.get("responseStatus") != 200 and d.get("responseStatus") != "200":
            # quota / err
            return None
        tr = (d.get("responseData", {}) or {}).get("translatedText", "").strip()
        if not tr or tr.upper().startswith("MYMEMORY WARNING"):
            return None
        return tr
    except Exception:
        return None


def translate(text: str, target: str = "zh-CN",
              backend: str = "", model: str = "", effort: str = "") -> str:
    """主入口。返回中文翻译；失败返回空。

    backend 优先级：
      - 显式参数 > server-config.json dict.translate_backend > 默认 'mymemory'
      - 'deepl' / 'ai' / 'mymemory' / 'auto'（'auto' = deepl → mymemory）
    """
    text = (text or "").strip()
    if not text:
        return ""
    # 至少含一个「可翻译字符」(拉丁字母 / 假名 / 汉字)。之前只判 [A-Za-z] →
    # 整句日语/中文(无拉丁)被误判为无内容直接返回空,导致日语多选翻译永远失败。
    if not re.search(r"[A-Za-z぀-ヿ㐀-鿿一-鿿々ー]", text):
        return ""
    cached = _cache_get(text, target)
    if cached is not None:
        return cached

    cfg = _cfg()
    if not backend:
        backend = (cfg.get("translate_backend") or "auto").strip().lower()
    if not model:
        model = (cfg.get("translate_model") or "sonnet").strip()
    if not effort:
        effort = (cfg.get("translate_effort") or "low").strip()

    sources = []
    if backend == "deepl":
        sources = ["deepl"]
    elif backend == "ai":
        sources = ["ai"]
    elif backend == "mymemory":
        sources = ["mymemory"]
    elif backend in ("gtranslate", "google"):
        sources = ["gtranslate"]
    else:  # auto:Google 翻译优先(快~0.3s+质量高+走赠金,key 未放行则自动跳过)→ mymemory → AI 兜底
        sources = ["gtranslate", "deepl", "mymemory", "ai"]

    for src in sources:
        tr = None
        if src == "gtranslate":
            tr = _gtranslate(text, target)
        elif src == "deepl":
            tr = _deepl(text, target)
        elif src == "ai":
            tr = _ai_translate(text, target, model=model, effort=effort)
        elif src == "mymemory":
            tr = _mymemory(text, target)
        if tr:
            _cache_put(text, target, tr, src if src != "ai" else f"ai-{model}")
            return tr
    return ""


def translate_examples(examples: list[str], target: str = "zh-CN", limit: int = 6) -> list[dict]:
    """批量翻译例句（仅前 limit 条），每条返回 {en, zh}。"""
    out = []
    for ex in examples[:limit]:
        zh = translate(ex, target=target)
        out.append({"en": ex, "zh": zh})
    return out


if __name__ == "__main__":
    import sys
    for line in sys.argv[1:]:
        print(line)
        print(" →", translate(line))
