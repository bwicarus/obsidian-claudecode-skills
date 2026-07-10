#!/usr/bin/env python3
"""跨书图像描述去重缓存:同一/极相似插图(常出现在同书多版本、或不同书引用同一经典图)只描述一次。

做法(业界通用感知哈希 + 汉明距离近邻):
  ① 每张图算 **dHash**(差分哈希,64-bit;对缩放/重压缩鲁棒 → 不同版本同图也命中);
  ② SQLite 存 hash→description;描述前先查「汉明距离 ≤ 阈值」的已描述图,命中就复用、省一次 AI 视觉调用;
  ③ 描述完落库,供后续(任何书)命中。

用法(被 describe_figures_batch.py / born-digital 图提取共用):
  from figure_dedup import lookup, store
  desc = lookup(png_bytes)            # 命中近邻 → 返回已有描述;否则 None
  ... 没命中才调 AI ...
  store(png_bytes, desc, book=rel, page=n)
"""
import os
import io
import sqlite3
import time
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
DB_PATH = CLAUDE_DIR / "state" / "figure-desc-cache.db"
HAMMING_MAX = 5          # ≤5 bit 差 视作"同/极相似图"(64-bit dHash 经验值)
_MIN_DESC = 8            # 太短的描述(NONE/空)不入库,免污染


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS figdesc(
        hash INTEGER PRIMARY KEY, description TEXT NOT NULL,
        book TEXT, page INTEGER, created REAL)""")
    return c


def dhash(png_bytes, size=8) -> int:
    """差分哈希:缩到 (size+1)×size 灰度,逐行比较相邻像素亮度 → size*size bit 整数。
    对分辨率/JPEG 重压缩鲁棒(同图不同版本仍近邻)。失败返回 None。"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert("L").resize((size + 1, size), Image.LANCZOS)
        px = list(im.getdata())
        h = 0; bit = 0
        for r in range(size):
            row = r * (size + 1)
            for col in range(size):
                h |= (1 << bit) if px[row + col] > px[row + col + 1] else 0
                bit += 1
        return h
    except Exception:
        return None


def _ham(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def lookup(png_bytes, max_dist=HAMMING_MAX):
    """查近邻已描述图。命中返回 (description, dist);否则 None。
    精确命中走主键秒回;否则线性扫已存 hash 比汉明距离(库到上万行仍毫秒级)。"""
    h = dhash(png_bytes)
    if h is None:
        return None
    try:
        c = _conn()
        row = c.execute("SELECT description FROM figdesc WHERE hash=?", (h,)).fetchone()
        if row:
            c.close(); return (row[0], 0)
        best = None; bestd = max_dist + 1
        for (hh, desc) in c.execute("SELECT hash, description FROM figdesc"):
            d = _ham(h, hh)
            if d < bestd:
                bestd = d; best = desc
                if d == 0:
                    break
        c.close()
        return (best, bestd) if best is not None and bestd <= max_dist else None
    except Exception:
        return None


def store(png_bytes, description, book="", page=0):
    """落库(已存 hash 不覆盖)。描述过短不入。"""
    if not description or len(description.strip()) < _MIN_DESC:
        return False
    h = dhash(png_bytes)
    if h is None:
        return False
    try:
        c = _conn()
        c.execute("INSERT OR IGNORE INTO figdesc(hash,description,book,page,created) VALUES(?,?,?,?,?)",
                  (h, description.strip(), book, int(page or 0), time.time()))
        c.commit(); c.close()
        return True
    except Exception:
        return False


def stats():
    try:
        c = _conn(); n = c.execute("SELECT COUNT(*) FROM figdesc").fetchone()[0]; c.close()
        return {"count": n, "db": str(DB_PATH)}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    print(stats())
