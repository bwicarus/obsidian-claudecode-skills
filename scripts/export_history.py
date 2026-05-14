#!/usr/bin/env python3
"""从本地 SQLite 导出问答历史到 history/ 目录，供上传到网站"""

import sqlite3, json, shutil, os, sys
from pathlib import Path

_localappdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
STORE_DIR    = Path(_localappdata) / "截图问答"
DB_PATH      = STORE_DIR / "history.db"
HIST_IMG_DIR = STORE_DIR / "images"
PROJECT_DIR  = Path(os.environ.get("CLAUDE_PROJECT", Path(__file__).resolve().parents[1]))
OUTPUT_DIR   = PROJECT_DIR / "history"


def export():
    if not DB_PATH.exists():
        print("未找到历史数据库，跳过导出。")
        sys.exit(0)

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "images").mkdir(exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, timestamp, img_fname, note, messages, record_type, related_cards "
        "FROM conversations ORDER BY timestamp DESC"
    ).fetchall()
    conn.close()

    entries = []
    copied = skipped = 0
    for r in rows:
        msgs = json.loads(r["messages"] or "[]")
        entries.append({
            "id":            r["id"],
            "timestamp":     r["timestamp"],
            "img_fname":     r["img_fname"] or "",
            "note":          r["note"] or "",
            "msg_count":     len(msgs),
            "record_type":   r["record_type"] or "normal",
            "messages":      msgs,
            "related_cards": json.loads(r["related_cards"] or "[]"),
        })
        if r["img_fname"]:
            src = HIST_IMG_DIR / r["img_fname"]
            dst = OUTPUT_DIR / "images" / r["img_fname"]
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                copied += 1
            else:
                skipped += 1

    total = len(entries)
    wrong = sum(1 for e in entries if e["record_type"] == "wrong")
    (OUTPUT_DIR / "history.json").write_text(
        json.dumps(
            {"entries": entries, "stats": {"total": total, "normal": total - wrong, "wrong": wrong}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"已导出 {total} 条记录（{wrong} 条错题），新图片 {copied} 张，已有 {skipped} 张跳过")


if __name__ == "__main__":
    export()
