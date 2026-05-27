"""扫所有 vault PDF，对每个 vocab lemma 计算"暴露次数"（在 PDF 中出现的页数）。

输出：
  state/vocab-exposure.json：
    {
      "lemma_lower": {
        "total_pages": 12,
        "pages": [{"pdf": "...", "page": 18}, ...],  # 含位置，给反向索引用
      }
    }

性能：每个 PDF 用 PyMuPDF page.get_text("words") 拿单词，按 lemma 化（用 ECDICT exchange）；
      只统计 vocab_index 里有的词。

CLI:
  python3 scripts/vocab/build_exposure.py             # 全扫
  python3 scripts/vocab/build_exposure.py --pdf X.pdf # 只一个 PDF
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
VAULT_ROOT   = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
OUT_PATH     = PROJECT_ROOT / "state" / "vocab-exposure.json"

sys.path.insert(0, str(Path(__file__).parent))
import vocab_index   # noqa: E402
import dict_sources  # noqa: E402


def scan_pdf(abs_pdf: Path, form_to_lemma: dict[str, str]) -> dict[str, list[dict]]:
    """对一个 PDF：返回 {lemma: [{page, count_in_page}]}.

    性能优化：不做 ECDICT lemma 化（每个 token 1 次 sqlite 太慢）。
    依赖 vocab_index 已经把所有 forms 都映射到 lemma — 只匹配 form_to_lemma 字典命中的。
    """
    try:
        import fitz
    except ImportError:
        return {}
    out: dict[str, list[dict]] = {}
    try:
        doc = fitz.open(str(abs_pdf))
    except Exception:
        return {}
    try:
        for pno, page in enumerate(doc, start=1):
            words_on_page = page.get_text("words")
            lemma_counts: dict[str, int] = {}
            for w in words_on_page:
                token = w[4] if len(w) > 4 else ""
                # 去标点 + lower
                token = re.sub(r"[^\w'-]", "", token).strip("'-").lower()
                if not token or len(token) < 2: continue
                lemma = form_to_lemma.get(token)
                if not lemma: continue
                lemma_counts[lemma] = lemma_counts.get(lemma, 0) + 1
            for lemma, cnt in lemma_counts.items():
                out.setdefault(lemma, []).append({"page": pno, "count": cnt})
    finally:
        doc.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="只扫一个 PDF（vault 相对路径）")
    args = ap.parse_args()

    idx = vocab_index.index(force_reload=True)
    if not idx:
        print("vocab dir 空，无需扫描"); return
    # form_to_lemma：每个 form 映射到其 lemma（vocab_index 已经构造好）
    form_to_lemma = {form: info["lemma"].lower() for form, info in idx.items()}
    print(f"vocab forms: {len(form_to_lemma)} (lemmas {len({v for v in form_to_lemma.values()})})")

    out: dict[str, dict] = {}
    if OUT_PATH.exists():
        try: out = json.loads(OUT_PATH.read_text("utf-8"))
        except Exception: out = {}

    if args.pdf:
        pdfs = [VAULT_ROOT / args.pdf]
    else:
        pdfs = list(VAULT_ROOT.rglob("*.pdf"))
    print(f"将扫 {len(pdfs)} 个 PDF")

    t0 = time.time()
    for i, p in enumerate(pdfs, 1):
        if not p.exists():
            print(f"  [{i}/{len(pdfs)}] {p.name} 不存在，跳过"); continue
        rel = str(p.relative_to(VAULT_ROOT).as_posix())
        print(f"  [{i}/{len(pdfs)}] {rel}", flush=True)
        result = scan_pdf(p, form_to_lemma)
        for lemma, page_list in result.items():
            entry = out.setdefault(lemma, {"total": 0, "pages": []})
            # 清这本 PDF 旧记录
            entry["pages"] = [pg for pg in entry["pages"] if pg.get("pdf") != rel]
            for pg in page_list:
                entry["pages"].append({"pdf": rel, "page": pg["page"], "count": pg["count"]})
            entry["total"] = sum(p["count"] for p in entry["pages"])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n✓ {len(out)} lemmas / {sum(e['total'] for e in out.values())} 总暴露 → {OUT_PATH}")
    print(f"耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
