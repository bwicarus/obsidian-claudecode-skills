#!/usr/bin/env python3
"""把 Tanaka 语料库(examples.utf:日英句对 + 词条索引)建成 SQLite。
之后日语词查例句 = 一次索引查询(离线秒回),AI 只负责把例句批量翻成中文。

examples.utf 格式(两行一组):
  A: <日文句子>\t<英译>#ID=...
  B: 词条(读音)[词义号]{句中词形}~ 词条2 ...   ← ~ 标记优质例句

建表:
  sent(id, ja, en)                — 句子
  wex(hw, sid, good)              — 词条 hw → 句子 sid;good=1 表示 ~ 优质
  idx wex(hw)

CLI:
  python3 scripts/vocab/build_tanaka_index.py [--src /tmp/examples.utf] [--db data/tanaka.db]
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
DEFAULT_DB = PROJECT_ROOT / "data" / "tanaka.db"
SRC_URL = "http://ftp.edrdg.org/pub/Nihongo/examples.utf.gz"

# B 行 token 取词条:词条在最前,遇到 ( [ { ~ 截断
_HW_RE = re.compile(r"^([^()\[\]{}~#]+)")
# 跳过的纯假名功能词(助词/系助词等),省得索引爆炸又无意义
_SKIP_HW = {
    "は", "が", "を", "に", "へ", "と", "の", "で", "も", "や", "か", "ね", "よ",
    "な", "ば", "から", "まで", "より", "だ", "です", "ます", "ない", "た", "て",
    "ので", "のに", "けど", "けれど", "という", "こと", "事", "もの", "物", "ん",
    "する", "ある", "いる", "なる", "れる", "られる", "せる", "この", "その", "あの",
}


def _download_if_needed(src: Path) -> Path:
    if src.exists():
        return src
    import gzip
    print(f"本地无 {src},从 {SRC_URL} 下载…", flush=True)
    gz = src.with_suffix(src.suffix + ".gz")
    urllib.request.urlretrieve(SRC_URL, gz)
    with gzip.open(gz, "rb") as f, open(src, "wb") as o:
        o.write(f.read())
    gz.unlink(missing_ok=True)
    return src


def build(src: Path, db_path: Path) -> None:
    src = _download_if_needed(src)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE sent(id INTEGER PRIMARY KEY, ja TEXT, en TEXT)")
    con.execute("CREATE TABLE wex(hw TEXT, sid INTEGER, good INTEGER)")

    sid = 0
    pending_ja = pending_en = None
    n_sent = 0
    n_wex = 0
    wex_rows = []
    sent_rows = []

    with open(src, encoding="utf-8") as f:
        for line in f:
            if line.startswith("A: "):
                body = line[3:].rstrip("\n")
                if "\t" in body:
                    ja, rest = body.split("\t", 1)
                else:
                    ja, rest = body, ""
                en = rest.split("#ID=", 1)[0].strip()
                pending_ja, pending_en = ja.strip(), en
            elif line.startswith("B: ") and pending_ja is not None:
                sid += 1
                sent_rows.append((sid, pending_ja, pending_en))
                n_sent += 1
                seen = set()
                for tok in line[3:].split():
                    good = 1 if "~" in tok else 0
                    m = _HW_RE.match(tok)
                    if not m:
                        continue
                    hw = m.group(1).strip()
                    if not hw or hw in _SKIP_HW or len(hw) > 16:
                        continue
                    key = (hw, good)
                    if key in seen:
                        continue
                    seen.add(key)
                    wex_rows.append((hw, sid, good))
                    n_wex += 1
                pending_ja = pending_en = None
                if len(sent_rows) >= 5000:
                    con.executemany("INSERT INTO sent VALUES(?,?,?)", sent_rows)
                    con.executemany("INSERT INTO wex VALUES(?,?,?)", wex_rows)
                    sent_rows.clear(); wex_rows.clear()

    if sent_rows:
        con.executemany("INSERT INTO sent VALUES(?,?,?)", sent_rows)
    if wex_rows:
        con.executemany("INSERT INTO wex VALUES(?,?,?)", wex_rows)
    print(f"建索引 wex(hw)…", flush=True)
    con.execute("CREATE INDEX idx_wex_hw ON wex(hw)")
    con.commit()
    con.close()
    sz = db_path.stat().st_size / 1024 / 1024
    print(f"完成:{n_sent} 句,{n_wex} 词条映射,db {sz:.1f}MB → {db_path}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/tmp/examples.utf")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()
    build(Path(args.src), Path(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
