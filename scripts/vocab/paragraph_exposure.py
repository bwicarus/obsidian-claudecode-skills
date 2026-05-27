"""查询事件触发的局部段落扫描：用户查 W 时，扫该词所在段落、句子之前的部分，
对其中已查过的词加 mastery（说明用户读到那里没查 = 认识它）。

入口：process_lookup(pdf_rel, page, lookup_word) → {affected: [{lemma, delta, new_mastery}, ...]}

实现：复用 PyMuPDF rawdict（跟 page-chars 一致）+ vocab_index 找已查词。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT_ROOT   = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))

sys.path.insert(0, str(Path(__file__).parent))
import vocab_index  # noqa: E402

# 每次"暴露但未查"加的 mastery 量
EXPOSURE_DELTA = 0.03


def _safe_pdf(rel: str) -> Path | None:
    rel = (rel or "").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    p = (VAULT_ROOT / rel).resolve()
    try:
        p.relative_to(VAULT_ROOT.resolve())
    except ValueError:
        return None
    return p if p.exists() else None


def _scan_chars(pdf_path: Path, page_num: int) -> list[dict]:
    try:
        import fitz
    except ImportError:
        return []
    chars = []
    try:
        doc = fitz.open(str(pdf_path))
        if page_num < 1 or page_num > len(doc):
            return []
        p = doc[page_num - 1]
        raw = p.get_text("rawdict")
        for b in raw.get("blocks", []):
            if b.get("type") != 0: continue
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    for ch in sp.get("chars", []):
                        bbox = ch.get("bbox")
                        if not bbox: continue
                        c = ch.get("c", "")
                        if not c: continue
                        chars.append({
                            "c": c, "x0": bbox[0], "y0": bbox[1],
                            "x1": bbox[2], "y1": bbox[3],
                            "sp": 1 if c.isspace() else 0,
                        })
    except Exception:
        pass
    finally:
        try: doc.close()
        except Exception: pass
    # sort 同 page-chars 前端逻辑：baseline + max height 阈值
    chars.sort(key=lambda c: (c["y0"] + (c["y1"] - c["y0"]), c["x0"]))
    return chars


def _words_with_positions(chars: list[dict]) -> list[dict]:
    """从 chars 流识别词。返回 [{start_idx, end_idx, word_lower}]."""
    out = []
    cur_start = None
    cur_letters = []
    def _flush(end_idx):
        nonlocal cur_start, cur_letters
        if cur_start is None or not cur_letters:
            cur_start = None; cur_letters = []; return
        w = "".join(cur_letters).lower()
        out.append({"start": cur_start, "end": end_idx, "word": w})
        cur_start = None; cur_letters = []
    for i, c in enumerate(chars):
        ch = c["c"]
        if c["sp"]:
            _flush(i - 1); continue
        if ch.isalpha() or ch in "'-":
            if cur_start is None: cur_start = i
            cur_letters.append(ch)
        else:
            _flush(i - 1)
    if cur_letters:
        _flush(len(chars) - 1)
    return out


def _find_lookup_word_pos(words: list[dict], lookup_word: str, form_to_lemma: dict[str, str]) -> int:
    """在 words 列表里找 lookup_word（或其屈折形）的第一次出现 idx。"""
    lookup_lemma = form_to_lemma.get(lookup_word.lower(), lookup_word.lower())
    for i, w in enumerate(words):
        wl = form_to_lemma.get(w["word"], w["word"])
        if wl == lookup_lemma:
            return i
    return -1


def _paragraph_bounds(chars: list[dict], anchor_idx: int) -> tuple[int, int]:
    """同 PDF reader 的 _paragraphExpandFromChar：连续行 + 行间距 < 2.2 行高."""
    if anchor_idx < 0 or anchor_idx >= len(chars):
        return -1, -1
    refH = chars[anchor_idx]["y1"] - chars[anchor_idx]["y0"]
    s = e = anchor_idx
    cur_top = chars[anchor_idx]["y0"]
    while s > 0:
        t = chars[s - 1]["y0"]
        if abs(t - cur_top) > refH * 2.2: break
        s -= 1
        if abs(t - cur_top) > refH * 0.4: cur_top = t
    cur_top = chars[anchor_idx]["y0"]
    while e < len(chars) - 1:
        t = chars[e + 1]["y0"]
        if abs(t - cur_top) > refH * 2.2: break
        e += 1
        if abs(t - cur_top) > refH * 0.4: cur_top = t
    return s, e


def _sentence_start_before(chars: list[dict], anchor_idx: int, para_start: int) -> int:
    """向左扩到当前句子起点（. ! ? 或段落起点）。返回句子起 idx。"""
    s = anchor_idx
    while s > para_start:
        prev_c = chars[s - 1]["c"]
        if prev_c in ".!?。！？":
            break
        s -= 1
    return s


def _bump_mastery(lemma: str, delta: float) -> tuple[float, float] | None:
    """改 vocab .md 的 frontmatter.mastery，clamp 0~1。
    返回 (old, new) 或 None（笔记不存在/失败）。"""
    info = vocab_index.index().get(lemma.lower())
    if not info or not info.get("path"):
        return None
    path = VAULT_ROOT / info["path"]
    if not path.exists():
        return None
    raw = path.read_text("utf-8")
    if not raw.startswith("---\n"):
        return None
    end = raw.find("\n---\n", 4)
    if end < 0:
        return None
    fm_text = raw[4:end]
    rest = raw[end:]
    m = re.search(r"^mastery:\s*(.*)$", fm_text, flags=re.M)
    if not m:
        return None
    try:
        old = float(m.group(1).strip() or 0.0)
    except ValueError:
        old = 0.0
    new = max(0.0, min(1.0, old + delta))
    if abs(new - old) < 0.001:
        return (old, new)
    fm_text = re.sub(r"^mastery:.*$", f"mastery: {round(new, 3)}", fm_text, flags=re.M)
    # 同步 label
    label, _ = _label_for(new)
    if re.search(r"^mastery_label:", fm_text, flags=re.M):
        fm_text = re.sub(r"^mastery_label:.*$", f"mastery_label: {label}", fm_text, flags=re.M)
    # 同步 last_exposure 时间
    today = time.strftime("%Y-%m-%d")
    if re.search(r"^last_exposure:", fm_text, flags=re.M):
        fm_text = re.sub(r"^last_exposure:.*$", f"last_exposure: {today}", fm_text, flags=re.M)
    else:
        fm_text = fm_text.rstrip() + f"\nlast_exposure: {today}\n"
    path.write_text("---\n" + fm_text + rest, "utf-8")
    return (old, new)


LABELS_NEW = [
    (0.10, "完全不会", "new"),     # 刚查过 → 0
    (0.40, "学习中",   "learning"),
    (0.70, "见过",     "seen"),
    (0.90, "熟",       "known"),
    (1.01, "掌握",     "mastered"),
]
def _label_for(m: float) -> tuple[str, str]:
    for thresh, label, slug in LABELS_NEW:
        if m < thresh:
            return label, slug
    return "掌握", "mastered"


def reset_to_zero(lemma: str) -> tuple[float, float] | None:
    """用户查询某词：mastery 重置到一个低值（不一定 0；首次查 = 0，再次查 = max(old-0.2, 0)）"""
    info = vocab_index.index().get(lemma.lower())
    if not info:
        return None
    path = VAULT_ROOT / info["path"]
    if not path.exists():
        return None
    raw = path.read_text("utf-8")
    end = raw.find("\n---\n", 4)
    if end < 0:
        return None
    fm_text = raw[4:end]
    rest = raw[end:]
    m = re.search(r"^mastery:\s*(.*)$", fm_text, flags=re.M)
    try:
        old = float(m.group(1).strip()) if m else 1.0
    except ValueError:
        old = 1.0
    new = 0.0   # 用户主动查 = 重置
    if old == new:
        return (old, new)
    fm_text = re.sub(r"^mastery:.*$", f"mastery: 0.0", fm_text, flags=re.M)
    label, _ = _label_for(new)
    if re.search(r"^mastery_label:", fm_text, flags=re.M):
        fm_text = re.sub(r"^mastery_label:.*$", f"mastery_label: {label}", fm_text, flags=re.M)
    path.write_text("---\n" + fm_text + rest, "utf-8")
    return (old, new)


def process_lookup(pdf_rel: str, page: int, lookup_word: str) -> dict:
    """主入口：用户在 pdf_rel:page 查 lookup_word 时调用。
    1. 扫该页 chars
    2. 找 lookup_word 第一次出现位置
    3. 算所在段落 + 当前句子起点
    4. 段落起 ~ 当前句子起 之间的所有词 → 命中 vocab_index 的加 mastery
    5. 返回 affected list（调试 / 前端显示用）
    """
    out = {"affected": [], "found_position": False}
    pdf_path = _safe_pdf(pdf_rel)
    if not pdf_path or page < 1:
        return out
    idx = vocab_index.index()
    if not idx:
        return out
    form_to_lemma = {form: info["lemma"].lower() for form, info in idx.items()}

    chars = _scan_chars(pdf_path, page)
    if not chars:
        return out
    words = _words_with_positions(chars)
    if not words:
        return out
    pos = _find_lookup_word_pos(words, lookup_word, form_to_lemma)
    if pos < 0:
        return out   # 查询词不在该页（极少见，可能 PyMuPDF char 序跟 word 序不同）
    out["found_position"] = True
    anchor_char_idx = words[pos]["start"]

    para_s, para_e = _paragraph_bounds(chars, anchor_char_idx)
    if para_s < 0:
        return out
    sent_start = _sentence_start_before(chars, anchor_char_idx, para_s)

    # 扫 para_s ~ sent_start 之前的词
    seen_lemmas: set[str] = set()   # 同段落同 lemma 只加一次
    for w in words:
        if w["start"] < para_s: continue
        if w["start"] >= sent_start: break   # 句子内不算
        lemma = form_to_lemma.get(w["word"])
        if not lemma:
            continue   # 不在 vocab_index → 默认掌握，不动
        if lemma in seen_lemmas:
            continue
        seen_lemmas.add(lemma)
        result = _bump_mastery(lemma, EXPOSURE_DELTA)
        if result:
            old, new = result
            if abs(new - old) > 0.001:
                out["affected"].append({"lemma": lemma, "old": round(old, 3),
                                         "new": round(new, 3), "delta": round(new - old, 3)})
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("page", type=int); ap.add_argument("word")
    args = ap.parse_args()
    print(json.dumps(process_lookup(args.pdf, args.page, args.word), ensure_ascii=False, indent=2))
