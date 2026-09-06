"""Wikidata 全量 dump（all.json.bz2，约 103 GB 压缩 / 1.4 TB 展开）流式提取。

一次扫描同时得到：
1. **规模实测**（文档 §七要的数字）：实体数、实体值关系数、度分布、"最简行"展开后的 UTF-8 字节数 —— 对**全部** item 统计。
2. **可导入子集** ``minimal-index.<tag>.jsonl.gz``：只留有 zh/ja 标签（可改）的 item，行格式与 Codex 侧 wikidata_measure.py 一致
   （{id, labels{lang}, descriptions{lang}, aliases{lang:[..]}, relations[[prop,target,rank]]}），可直接喂 ``kj wikidata-import``。

流式：bz2 逐块解压（磁盘上从不出现 1.4 TB），主进程切行、按批发给进程池做 json 解析，进度写 ``extract-status.json``。
截断的 bz2（下载未完的文件）也能读到截断点为止 —— 用来做吞吐基准。

    python scripts/kj/wikidata_extract.py DUMP.bz2 --out state/wikidata [--workers 8] [--keep zh,ja] [--limit-bytes 1073741824]
"""
from __future__ import annotations

import argparse
import bz2
import collections
import gzip
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

KEEP_LANGS_DEFAULT = ("zh", "ja")
_ZH = ("zh", "zh-hans", "zh-cn", "zh-hant", "zh-tw", "zh-hk")
LABEL_LANGS = ("en",) + _ZH + ("ja",)


def _minimal_row(obj: dict) -> tuple[dict, int]:
    """完整 item → 最简行；返回 (行, 实体值关系数)。"""
    row = {"id": obj["id"]}
    for key in ("labels", "descriptions"):
        src = obj.get(key) or {}
        row[key] = {lang: v["value"] for lang, v in src.items() if lang in LABEL_LANGS and isinstance(v, dict) and v.get("value")}
    al = {}
    for lang, vals in (obj.get("aliases") or {}).items():
        if lang in LABEL_LANGS and vals:
            al[lang] = [v["value"] for v in vals if isinstance(v, dict) and v.get("value")]
    row["aliases"] = al
    rel = []
    for prop, statements in (obj.get("claims") or {}).items():
        for s in statements or []:
            val = ((s.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if isinstance(val, dict) and "id" in val:
                rel.append([prop, val["id"], s.get("rank", "normal")])
    row["relations"] = rel
    return row, len(rel)


def _has_lang(row: dict, langs: tuple[str, ...]) -> bool:
    labels = row.get("labels") or {}
    for lang in langs:
        if lang == "zh":
            if any(labels.get(z) for z in _ZH):
                return True
        elif labels.get(lang):
            return True
    return False


def _work(batch: list[bytes], keep_langs: tuple[str, ...]) -> tuple[int, int, list[int], int, list[bytes], int]:
    """一批原始行 → (item 数, 关系数, 度列表, 全部最简行字节数, 保留的行, 保留数)。"""
    items = rels = min_bytes = kept_n = 0
    degrees: list[int] = []
    kept: list[bytes] = []
    for line in batch:
        line = line.strip()
        if line.endswith(b","):
            line = line[:-1]
        if not line or line in (b"[", b"]"):
            continue
        if b'"type":"item"' not in line and b'"type": "item"' not in line:   # dump 是紧凑 JSON；测试/他人产物可能带空格
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        row, nrel = _minimal_row(obj)
        text = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        items += 1
        rels += nrel
        degrees.append(nrel)
        min_bytes += len(text) + 1
        if _has_lang(row, keep_langs):
            kept.append(text)
            kept_n += 1
    return items, rels, degrees, min_bytes, kept, kept_n


def extract(dump: Path, out_dir: Path, *, workers: int = 6, keep_langs: tuple[str, ...] = KEEP_LANGS_DEFAULT,
            limit_bytes: int | None = None, batch_lines: int = 800, tag: str | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = tag or "".join(keep_langs)
    out_path = out_dir / f"minimal-index.{tag}.jsonl.gz"
    status_path = out_dir / "extract-status.json"
    total_c = dump.stat().st_size
    t0 = time.time()
    stats = {"phase": "extracting", "dump": str(dump), "dump_bytes": total_c, "compressed_read": 0, "items": 0, "relations": 0,
             "kept": 0, "minimal_utf8_bytes_all": 0, "kept_utf8_bytes": 0, "keep_langs": list(keep_langs), "workers": workers,
             "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    degree_hist: collections.Counter = collections.Counter()

    def flush_status(final: bool = False) -> None:
        el = time.time() - t0
        stats["elapsed_s"] = round(el)
        read = stats["compressed_read"]
        stats["compressed_mb_s"] = round(read / 1e6 / el, 2) if el else 0
        stats["eta_s"] = round((total_c - read) / (read / el)) if read and el and not final else 0
        stats["degree_histogram"] = {str(k): v for k, v in sorted(degree_hist.items())[:60]}
        stats["phase"] = "complete" if final else "extracting"
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, status_path)

    dec = bz2.BZ2Decompressor()
    pending = b""
    batch: list[bytes] = []
    inflight = []
    last_status = time.time()
    with open(dump, "rb") as fh, gzip.open(out_path, "wb", compresslevel=1) as out, mp.Pool(workers) as pool:
        def collect(block: bool) -> None:
            nonlocal inflight
            keep = []
            for ar in inflight:
                if block or ar.ready():
                    items, rels, degrees, mb, kept, kept_n = ar.get()
                    stats["items"] += items
                    stats["relations"] += rels
                    stats["minimal_utf8_bytes_all"] += mb
                    for d in degrees:
                        degree_hist[min(d, 200)] += 1
                    for text in kept:
                        out.write(text)
                        out.write(b"\n")
                        stats["kept_utf8_bytes"] += len(text) + 1
                    stats["kept"] += kept_n
                else:
                    keep.append(ar)
            inflight = keep

        while True:
            chunk = fh.read(8 * 1024 * 1024)
            if not chunk:
                break
            stats["compressed_read"] += len(chunk)
            try:
                data = dec.decompress(chunk)
            except (EOFError, OSError) as e:  # 截断/多流：能读多少算多少
                stats["decompress_error"] = str(e)[:120]
                break
            if dec.eof and dec.unused_data:  # 多流 bz2：接着下一流
                rest = dec.unused_data
                dec = bz2.BZ2Decompressor()
                data += dec.decompress(rest)
            if not data:
                continue
            pending += data
            lines = pending.split(b"\n")
            pending = lines.pop()
            batch.extend(lines)
            while len(batch) >= batch_lines:
                inflight.append(pool.apply_async(_work, (batch[:batch_lines], keep_langs)))
                batch = batch[batch_lines:]
                if len(inflight) >= workers * 3:
                    collect(block=False)
                    if len(inflight) >= workers * 3:
                        inflight[0].wait()
                        collect(block=False)
            if time.time() - last_status > 20:
                flush_status()
                last_status = time.time()
            if limit_bytes and stats["compressed_read"] >= limit_bytes:
                stats["limited"] = True
                break
        if pending.strip():
            batch.append(pending)
        if batch:
            inflight.append(pool.apply_async(_work, (batch, keep_langs)))
        collect(block=True)
    stats["kept_gz_bytes"] = out_path.stat().st_size
    stats["out"] = str(out_path)
    flush_status(final=True)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wikidata dump 流式提取 + 规模实测")
    ap.add_argument("dump")
    ap.add_argument("--out", default=None, help="输出目录（默认 $CLAUDE_PROJECT/state/wikidata）")
    ap.add_argument("--workers", type=int, default=max(2, min(8, (os.cpu_count() or 4) - 2)))
    ap.add_argument("--keep", default=",".join(KEEP_LANGS_DEFAULT), help="保留有这些语言标签的 item，逗号分隔")
    ap.add_argument("--limit-bytes", type=int, default=None, help="只读前 N 压缩字节（基准/试跑）")
    ap.add_argument("--tag", default=None)
    a = ap.parse_args(argv)
    if a.out:
        out = Path(a.out)
    else:
        root = Path(os.environ.get("CLAUDE_PROJECT") or Path(__file__).resolve().parents[2])
        out = root / "state" / "wikidata"
    st = extract(Path(a.dump), out, workers=a.workers, keep_langs=tuple(x.strip() for x in a.keep.split(",") if x.strip()),
                 limit_bytes=a.limit_bytes, tag=a.tag)
    print(json.dumps({k: v for k, v in st.items() if k != "degree_histogram"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
