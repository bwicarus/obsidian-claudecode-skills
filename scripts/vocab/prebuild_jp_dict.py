#!/usr/bin/env python3
"""预建日语词库:对一本 PDF 全文 fugashi 分词,收集实词,批量 AI 查中文释义,
写进 dict_sources 的 jp 缓存(state/dict-cache/jp-*.json)。之后 PDF 阅读器点词
全部离线秒出(命中缓存,不再现调 AI)。

幂等:已缓存的词跳过,可随时重跑续传。
批量:一次 AI 调用查 BATCH 个词(远快于逐词)。

CLI:
  python3 scripts/vocab/prebuild_jp_dict.py <pdf_path> [--batch 40] [--limit N] [--progress FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "vocab"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import dict_sources as ds  # noqa: E402

_KANA = re.compile(r"[぀-ゟ゠-ヿ]")
_KANJI = re.compile(r"[一-鿿㐀-䶿]")
# 跳过的词性(助词/助动词/标点/符号/空白/补助记号)+ 代词等
_SKIP_POS = {"助詞", "助動詞", "補助記号", "記号", "空白", "接続詞", "代名詞", "フィラー"}


def _collect_tokens(pdf_path: Path) -> list[str]:
    """全文 fugashi 分词,收集实词 lemma(去重,保序)。"""
    import fitz
    from fugashi import Tagger
    tagger = Tagger()
    seen = {}
    doc = fitz.open(str(pdf_path))
    for pno in range(doc.page_count):
        text = doc[pno].get_text("text")
        if not text:
            continue
        # 只喂含 CJK 的行,省时
        for line in text.splitlines():
            if not (_KANA.search(line) or _KANJI.search(line)):
                continue
            try:
                toks = tagger(line)
            except Exception:
                continue
            for w in toks:
                pos1 = getattr(w.feature, "pos1", "") or ""
                if pos1 in _SKIP_POS:
                    continue
                lemma = getattr(w.feature, "lemma", None) or w.surface
                lemma = (lemma or "").strip()
                # 只要含假名或汉字、长度合理的实词
                if not lemma or len(lemma) > 12:
                    continue
                if not (_KANA.search(lemma) or _KANJI.search(lemma)):
                    continue
                if lemma not in seen:
                    seen[lemma] = True
    doc.close()
    return list(seen.keys())


_BATCH_PROMPT = (
    "你是日中词典。为下面每个日语词给词典条目。严格只输出 JSON 数组,每项格式:\n"
    '{"word":"原词(照抄)","reading":"假名读音","romaji":"罗马字","pos":"词性",'
    '"zh":"简洁中文释义,多义用;分隔"}\n'
    "不要例句,不要解释,数组顺序对应词表。词表:\n"
)


def _batch_lookup(words: list[str], model: str = "haiku") -> dict:
    """一次 AI 调用查一批词 → {word: entry}。失败返回 {}。"""
    from ai_client import ask
    listing = "\n".join(f"{i+1}. {w}" for i, w in enumerate(words))
    prompt = _BATCH_PROMPT + listing + "\n\n只输出 JSON 数组。"
    try:
        resp = ask(prompt, claude_model=model, claude_effort="low")
    except Exception as e:
        print(f"  batch ai 失败: {e}", flush=True)
        return {}
    m = re.search(r"\[.*\]", resp or "", re.DOTALL)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        w = (item.get("word") or "").strip()
        if w and item.get("zh"):
            out[w] = item
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个新词(0=全部)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--progress", default="", help="进度写到这个 JSON 文件")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"找不到 PDF: {pdf_path}", file=sys.stderr)
        return 1

    def _prog(d):
        if args.progress:
            try:
                Path(args.progress).write_text(json.dumps(d, ensure_ascii=False), "utf-8")
            except Exception:
                pass

    print(f"[1/3] 分词收集实词: {pdf_path.name}", flush=True)
    _prog({"phase": "tokenize", "done": 0, "total": 0})
    tokens = _collect_tokens(pdf_path)
    print(f"  全文实词去重 {len(tokens)} 个", flush=True)

    # 过滤已缓存
    todo = [w for w in tokens if ds._cache_load("jp", w, ttl_days=3650) is None]
    cached_n = len(tokens) - len(todo)
    print(f"  已缓存 {cached_n},待查 {len(todo)}", flush=True)
    if args.limit > 0:
        todo = todo[: args.limit]

    total = len(todo)
    done = 0
    saved = 0
    _prog({"phase": "lookup", "done": cached_n, "total": len(tokens), "new": total})
    for i in range(0, total, args.batch):
        chunk = todo[i: i + args.batch]
        res = _batch_lookup(chunk, model=args.model)
        for w in chunk:
            entry = res.get(w)
            if entry:
                entry["word"] = w
                entry["source"] = "jp_ai_batch"
                entry.setdefault("examples", [])
                ds._cache_save("jp", w, entry)
                saved += 1
        done += len(chunk)
        print(f"  [{done}/{total}] 已存 {saved}  (本批 {len(res)}/{len(chunk)})", flush=True)
        _prog({"phase": "lookup", "done": cached_n + done, "total": len(tokens),
               "new": total, "new_done": done, "saved": saved})
        time.sleep(0.2)

    print(f"[3/3] 完成。新缓存 {saved} 词,词库共覆盖 ~{cached_n + saved}/{len(tokens)}", flush=True)
    _prog({"phase": "done", "done": len(tokens), "total": len(tokens), "saved": saved})
    return 0


if __name__ == "__main__":
    sys.exit(main())
