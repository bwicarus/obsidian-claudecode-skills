#!/usr/bin/env python3
"""预建日语例句中文翻译:给词库里的词从 Tanaka 语料挂母语例句,把这些例句
批量翻成中文并永久缓存(state/dict-cache/jasent-*.json)。之后 PDF 点词,
例句的中文翻译离线秒出。

词源两种:
  - 给一本 PDF:全文 fugashi 分词收实词(同 prebuild_jp_dict)
  - --all-cached:遍历已有 jp 词典缓存里的所有词

只做翻译,例句本身来自离线 Tanaka 索引(data/tanaka.db,先跑 build_tanaka_index.py)。

CLI:
  python3 scripts/vocab/prebuild_jp_examples.py <pdf> [--per-word 2] [--batch 40] [--limit N] [--model sonnet] [--progress FILE]
  python3 scripts/vocab/prebuild_jp_examples.py --all-cached [...]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "vocab"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import dict_sources as ds  # noqa: E402


def _cached_words() -> list[str]:
    """遍历 jp 词典缓存,取所有词(去重保序)。"""
    words = []
    seen = set()
    for f in sorted(glob.glob(str(ds._cache_dir() / "jp-*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        w = (d.get("word") or "").strip()
        if w and w not in seen:
            seen.add(w)
            words.append(w)
    return words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", nargs="?", default="")
    ap.add_argument("--all-cached", action="store_true", help="对所有已缓存 jp 词建例句翻译")
    ap.add_argument("--per-word", type=int, default=2, help="每词取几条例句")
    ap.add_argument("--batch", type=int, default=40, help="一次 AI 翻译多少句")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个词(0=全部)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--progress", default="")
    args = ap.parse_args()

    def _prog(d):
        if args.progress:
            try:
                Path(args.progress).write_text(json.dumps(d, ensure_ascii=False), "utf-8")
            except Exception:
                pass

    # 1. 词表
    if args.all_cached:
        print("[1/3] 收集已缓存 jp 词…", flush=True)
        words = _cached_words()
    elif args.pdf:
        from prebuild_jp_dict import _collect_tokens
        print(f"[1/3] 分词收集实词: {Path(args.pdf).name}", flush=True)
        words = _collect_tokens(Path(args.pdf))
    else:
        print("需要 <pdf> 或 --all-cached", file=sys.stderr)
        return 1
    if args.limit > 0:
        words = words[: args.limit]
    print(f"  词表 {len(words)} 个", flush=True)

    # 2. 收集每词的母语例句(去重 JP 句),只留未翻译的
    print("[2/3] 查 Tanaka 例句 + 找未翻译句…", flush=True)
    sent_set = {}
    matched_words = 0
    for w in words:
        exs = ds.tanaka_examples(w, limit=args.per_word)
        if exs:
            matched_words += 1
        for e in exs:
            ja = e["ja"]
            if ja not in sent_set:
                sent_set[ja] = True
    all_sents = list(sent_set.keys())
    todo = [s for s in all_sents if ds._sent_zh(s) is None]
    print(f"  {matched_words}/{len(words)} 词有例句,去重例句 {len(all_sents)} 句,"
          f"已翻 {len(all_sents)-len(todo)},待翻 {len(todo)}", flush=True)
    _prog({"phase": "translate", "done": len(all_sents) - len(todo),
           "total": len(all_sents), "matched_words": matched_words})

    # 3. 批量翻译(translate_sentences 内部自动落库 + 跳过已缓存)
    total = len(todo)
    done = 0
    for i in range(0, total, args.batch):
        chunk = todo[i: i + args.batch]
        res = ds.translate_sentences(chunk, model=args.model)
        got = sum(1 for s in chunk if s in res)
        done += len(chunk)
        print(f"  [{done}/{total}] 本批译 {got}/{len(chunk)}", flush=True)
        _prog({"phase": "translate", "done": (len(all_sents) - total) + done,
               "total": len(all_sents), "matched_words": matched_words})
        time.sleep(0.2)

    print(f"[3/3] 完成。例句中文翻译覆盖 ~{len(all_sents)} 句", flush=True)
    _prog({"phase": "done", "done": len(all_sents), "total": len(all_sents)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
