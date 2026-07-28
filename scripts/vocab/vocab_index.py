"""vault 内 vocab/*.md 的 in-memory 索引。

用途：PDF page-chars 后端调用时按词查 mastery，决定下划线颜色。
缓存：内存 + vault mtime 检测；vault 没改就直接读缓存。

API：
  index() -> {word_lower: {"lemma": ..., "mastery": 0.45, "label_slug": "seen"}}
"""
from __future__ import annotations

import json
import os
import re
import time
from functools import lru_cache
from pathlib import Path

VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))

_CACHE = {"data": None, "vroot_mtime": 0.0, "ts_loaded": 0.0}


def _vocab_root() -> Path:
    return VAULT_ROOT / "资源" / "vocab"


def _scan_vroot_mtime(vroot: Path) -> float:
    if not vroot.exists():
        return 0.0
    try:
        return max((p.stat().st_mtime for p in vroot.rglob("*.md")), default=0.0)
    except Exception:
        return time.time()


def _parse_frontmatter(raw: str) -> dict:
    if not raw.startswith("---\n"):
        return {}
    end = raw.find("\n---\n", 4)
    if end < 0:
        return {}
    fm: dict = {}
    cur_key = None
    for line in raw[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - "):
            v = line[4:].strip().strip("'\"")
            if cur_key:
                if not isinstance(fm.get(cur_key), list): fm[cur_key] = []
                fm[cur_key].append(v)
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not m: continue
        k, v = m.group(1), m.group(2).strip().strip("'\"")
        if v == "":
            fm[k] = ""; cur_key = k
        else:
            fm[k] = v; cur_key = None
    return fm


@lru_cache(maxsize=4096)
def _ecdict_lemma_for_form(form: str) -> str:
    """Return ECDICT's validated lemma for an English surface form.

    ``dict_sources.lookup_ecdict`` already owns the project's safeguards for
    dirty ``0:`` redirects (for example ``also -> conjurer``), so the vocab
    index must reuse it instead of growing a second lemmatizer.
    """
    if not re.fullmatch(r"[a-z][a-z'’-]*", form):
        return ""
    try:
        from dict_sources import lookup_ecdict

        entry = lookup_ecdict(form)
    except Exception:
        return ""
    return str((entry or {}).get("lemma") or "").strip().lower()


def _english_form_owner(form: str, candidates: list[dict]) -> dict:
    """Choose one vocab card when English lemmas claim the same form.

    A surface form is not globally unique (``was`` is both ``be``'s past form
    and the plural recorded for the rare abbreviation ``wa``).  Previously the
    last file returned by ``rglob`` silently won, so mastery depended on
    filesystem traversal order.  Prefer the canonical lemma from the existing
    ECDICT pipeline.  If ECDICT is unavailable, prefer the exact lemma and then
    the most frequent BNC lemma as a deterministic, conservative fallback.
    """
    canonical = _ecdict_lemma_for_form(form)
    if canonical:
        for info in candidates:
            if info["lemma"] == canonical:
                return info

    for info in candidates:
        if info["lemma"] == form:
            return info

    def frequency_rank(info: dict) -> tuple[bool, int, str]:
        freq = int(info.get("freq_bnc") or 0)
        return (freq <= 0, freq if freq > 0 else 2**31 - 1, info["lemma"])

    return min(candidates, key=frequency_rank)


def _form_owner(form: str, candidates: list[dict]) -> dict:
    if len(candidates) == 1:
        return candidates[0]
    if re.fullmatch(r"[a-z][a-z'’-]*", form):
        return _english_form_owner(form, candidates)
    # Japanese forms are already resolved by their morphology pipeline.  Keep
    # the prior last-note-wins behaviour here rather than applying English
    # frequency rules to a language where freq_bnc is intentionally zero.
    return candidates[-1]


def index(*, force_reload: bool = False) -> dict[str, dict]:
    vroot = _vocab_root()
    if not vroot.exists():
        return {}
    mtime = _scan_vroot_mtime(vroot)
    if (not force_reload) and _CACHE["data"] is not None and abs(_CACHE["vroot_mtime"] - mtime) < 0.5:
        return _CACHE["data"]
    candidates_by_form: dict[str, list[dict]] = {}
    for p in vroot.rglob("*.md"):
        try:
            raw = p.read_text("utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(raw)
        lemma = (fm.get("lemma") or fm.get("word") or p.stem).lower()
        if not lemma:
            continue
        info = {
            "lemma": lemma,
            "mastery": float(fm.get("mastery") or 0.0),
            "label_slug": _slug_from_label(fm.get("mastery_label") or ""),
            "lookup_count": int(fm.get("lookup_count") or 0),
            "freq_bnc": int(fm.get("freq_bnc") or 0),
            "anki_card_id": fm.get("anki_card_id") or "",
            "path": str(p.relative_to(VAULT_ROOT).as_posix()),
        }
        # 把 lemma + 所有 forms 都映射回同一 info
        forms = fm.get("forms") or [lemma]
        if isinstance(forms, str):
            forms = [forms]
        for f in [lemma] + list(forms):
            f = f.strip().lower()
            if f:
                candidates = candidates_by_form.setdefault(f, [])
                if not any(old["path"] == info["path"] for old in candidates):
                    candidates.append(info)
    out = {
        form: _form_owner(form, candidates)
        for form, candidates in candidates_by_form.items()
    }
    _CACHE["data"] = out
    _CACHE["vroot_mtime"] = mtime
    _CACHE["ts_loaded"] = time.time()
    return out


def _slug_from_label(label: str) -> str:
    return {
        "新词": "new", "完全不会": "new",
        "学习中": "learning",
        "见过": "seen",
        "熟": "known",
        "掌握": "mastered",
    }.get(label, "new")   # 缺省按 new 处理（确保下划线渲染）


if __name__ == "__main__":
    import sys
    idx = index(force_reload=True)
    print(f"loaded {len(idx)} entries (forms 也算)")
    if len(sys.argv) > 1:
        for w in sys.argv[1:]:
            print(w, "→", idx.get(w.lower()))
