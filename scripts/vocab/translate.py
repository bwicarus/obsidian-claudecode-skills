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


def _cache_get(text: str, target: str, ttl_days: int = 90) -> str | None:
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


def _mymemory(text: str, target: str = "zh-CN") -> str | None:
    """MyMemory free / no key / 5000 chars/day anonymous."""
    src_target = f"en|{target if target.startswith('zh') else target}"
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode({
        "q": text, "langpair": src_target,
    })
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


def translate(text: str, target: str = "zh-CN") -> str:
    """主入口。返回中文翻译；失败返回空。"""
    text = (text or "").strip()
    if not text:
        return ""
    # 已是中文（无英文字母）→ 不译
    if not re.search(r"[A-Za-z]", text):
        return ""
    cached = _cache_get(text, target)
    if cached is not None:
        return cached
    tr = _deepl(text, target)
    if tr:
        _cache_put(text, target, tr, "deepl")
        return tr
    tr = _mymemory(text, target)
    if tr:
        _cache_put(text, target, tr, "mymemory")
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
