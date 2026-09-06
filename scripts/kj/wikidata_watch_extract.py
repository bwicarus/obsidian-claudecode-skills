"""等 Wikidata dump 文件就位（大小对、不再变化）就自动跑 wikidata_extract，全程写日志与状态，可脱离会话运行。

    start "" /belownormal /min python scripts/kj/wikidata_watch_extract.py --dump "C:\\迅雷下载\\wikidata-20260831-all.json.bz2" ^
        --expect-bytes 102943257005 --out state/wikidata --workers 8 --wait-hours 24

状态：<out>/watch-status.json（等待中/已开始/完成/出错），提取进度：<out>/extract-status.json。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kj import wikidata_extract as X  # type: ignore
else:
    from . import wikidata_extract as X


def _status(path: Path, **kw) -> None:
    kw["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(kw, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--expect-bytes", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--keep", default="zh,ja")
    ap.add_argument("--wait-hours", type=float, default=24)
    ap.add_argument("--poll-seconds", type=int, default=30)
    a = ap.parse_args(argv)
    dump = Path(a.dump)
    out = Path(a.out) if a.out else Path(os.environ.get("CLAUDE_PROJECT") or Path(__file__).resolve().parents[2]) / "state" / "wikidata"
    out.mkdir(parents=True, exist_ok=True)
    st = out / "watch-status.json"
    log = out / "watch.log"

    def logline(msg: str) -> None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

    deadline = time.time() + a.wait_hours * 3600
    last_size, stable = -1, 0
    logline(f"watching {dump} expect={a.expect_bytes}")
    while time.time() < deadline:
        if dump.exists():
            size = dump.stat().st_size
            ok_size = (a.expect_bytes is None) or (size == a.expect_bytes)
            stable = stable + 1 if (size == last_size and ok_size) else 0
            last_size = size
            _status(st, phase="waiting", dump=str(dump), size=size, expect=a.expect_bytes, stable_polls=stable)
            if ok_size and stable >= 2:
                break
        else:
            _status(st, phase="waiting", dump=str(dump), size=None, expect=a.expect_bytes, note="文件尚不存在")
        time.sleep(a.poll_seconds)
    else:
        _status(st, phase="timeout", dump=str(dump))
        logline("timeout: 文件没有就位")
        return 2
    logline(f"file ready size={last_size}; extracting → {out}")
    _status(st, phase="extracting", dump=str(dump), size=last_size, started=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        stats = X.extract(dump, out, workers=a.workers, keep_langs=tuple(x for x in a.keep.split(",") if x))
    except Exception:
        _status(st, phase="error", dump=str(dump), error=traceback.format_exc()[-2000:])
        logline("error:\n" + traceback.format_exc())
        return 1
    _status(st, phase="complete", dump=str(dump), items=stats.get("items"), kept=stats.get("kept"), relations=stats.get("relations"),
            minimal_utf8_bytes_all=stats.get("minimal_utf8_bytes_all"), kept_gz_bytes=stats.get("kept_gz_bytes"),
            elapsed_s=stats.get("elapsed_s"), out=stats.get("out"))
    logline(f"complete items={stats.get('items')} kept={stats.get('kept')} elapsed={stats.get('elapsed_s')}s")
    return 0


if __name__ == "__main__":
    import multiprocessing as mp
    mp.freeze_support()
    sys.exit(main())
