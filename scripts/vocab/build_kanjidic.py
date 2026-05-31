#!/usr/bin/env python3
"""下载 KANJIDIC2(EDRDG 免费汉字库)→ 解析成 data/kanjidic.json。
日语词「完整字典」页做离线汉字拆解用:每个汉字的 音読み(on)/訓読み(kun)/字义。

输出: {汉字: {"on": [...音読み], "kun": [...訓読み], "meanings": [...英文字义]}}
KANJIDIC2 XML 里 <reading r_type="ja_on"> / "ja_kun";<meaning>(无 m_lang 属性=英文)。

CLI: python3 scripts/vocab/build_kanjidic.py [--src /tmp/kanjidic2.xml] [--out data/kanjidic.json]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
SRC_URL = "http://www.edrdg.org/kanjidic/kanjidic2.xml.gz"
DEFAULT_OUT = PROJECT_ROOT / "data" / "kanjidic.json"


def _download(src: Path) -> Path:
    if src.exists():
        return src
    print(f"下载 {SRC_URL} …", flush=True)
    gz = src.with_suffix(src.suffix + ".gz")
    urllib.request.urlretrieve(SRC_URL, gz)
    with gzip.open(gz, "rb") as f, open(src, "wb") as o:
        o.write(f.read())
    gz.unlink(missing_ok=True)
    return src


def build(src: Path, out: Path) -> None:
    src = _download(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    n = 0
    # iterparse 省内存
    for ev, el in ET.iterparse(str(src), events=("end",)):
        if el.tag != "character":
            continue
        lit = el.findtext("literal")
        if not lit:
            el.clear(); continue
        on, kun, meanings = [], [], []
        rm = el.find("reading_meaning")
        if rm is not None:
            for grp in rm.findall("rmgroup"):
                for rd in grp.findall("reading"):
                    t = rd.get("r_type")
                    if t == "ja_on" and rd.text:
                        on.append(rd.text)
                    elif t == "ja_kun" and rd.text:
                        kun.append(rd.text)
                for mn in grp.findall("meaning"):
                    if mn.get("m_lang") is None and mn.text:   # 无 m_lang = 英文
                        meanings.append(mn.text)
        if on or kun or meanings:
            data[lit] = {"on": on, "kun": kun, "meanings": meanings[:4]}
            n += 1
        el.clear()
    out.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    sz = out.stat().st_size / 1024 / 1024
    print(f"完成: {n} 个汉字 → {out} ({sz:.1f}MB)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/tmp/kanjidic2.xml")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    build(Path(args.src), Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
