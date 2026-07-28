#!/usr/bin/env python3
"""全局 PDF 全文搜索索引构建器(成熟方案:SQLite FTS5 + trigram 分词器)。

跨 vault 所有 PDF 书的**逐页**纯文本建索引,供 `/pdf/search` 全局搜索 + 深链到阅读器对应页。
- trigram 分词器:中/日/英统一子串匹配(避开 unicode61 把整段 CJK 当一个 token 的坑),≥3 字走 FTS5,
  <3 字前端不触发(由 API 兜底 LIKE)。bm25 排序。
- external-content + 触发器:`pages_data` 是真源,FTS 自动同步;删书 = `DELETE FROM pages_data WHERE file=?`。
- 增量:`meta` 记每本 mtime,只重建新增/改动的书;磁盘上消失的书清出索引。
- 页文本复用 `state/pdf-text-index/<sha1(rel)[:16]>-<mtime>.json`(与阅读器 F4 全文搜索共享缓存)。

用法:
  python3 scripts/build_search_index.py            # 增量(默认)
  python3 scripts/build_search_index.py --rebuild  # 全量重建
  python3 scripts/build_search_index.py --stats     # 只看统计
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
OBSIDIAN_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
DB_PATH = CLAUDE_DIR / "state" / "pdf-search.db"
TEXT_INDEX_DIR = CLAUDE_DIR / "state" / "pdf-text-index"
sys.path.insert(0, str(CLAUDE_DIR / "_server_deploy"))
from web_cache_store import iter_account_web_cache, normalize_user_id  # noqa: E402


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _init_schema(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages_data(
            id   INTEGER PRIMARY KEY,
            file TEXT NOT NULL,
            page INTEGER NOT NULL,
            body TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pages_file ON pages_data(file);
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            body,
            content='pages_data', content_rowid='id',
            tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages_data BEGIN
            INSERT INTO pages_fts(rowid, body) VALUES (new.id, new.body);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages_data BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, body) VALUES('delete', old.id, old.body);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages_data BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, body) VALUES('delete', old.id, old.body);
            INSERT INTO pages_fts(rowid, body) VALUES (new.id, new.body);
        END;
        CREATE TABLE IF NOT EXISTS meta(
            file       TEXT PRIMARY KEY,
            name       TEXT,
            dir        TEXT,
            mtime      INTEGER,
            pages      INTEGER,
            indexed_at INTEGER
        );
        """
    )
    con.commit()


_UPAGES_DIR = CLAUDE_DIR / "state" / "reader-userpages"


def _userpages_sidecar_mtime(rel: str) -> int:
    """插入页 overlay sidecar 的 mtime。折进书的变更签名 → sidecar 文字改了(PDF mtime 没变)也触发本书重建索引。"""
    try:
        sha = hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16]
        return int((_UPAGES_DIR / (sha + ".json")).stat().st_mtime)
    except OSError:
        return 0


def _overlay_records(rel: str) -> list:
    try:
        sha = hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16]
        items = json.loads((_UPAGES_DIR / (sha + ".json")).read_text("utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _apply_overlay_supplement(texts: dict, rel: str) -> dict:
    """v4 批次2:未同步(脏)的 overlay 插入页,PDF 那页空白 → 用 sidecar md 顶上,让全文搜索能命中用户内容。
    已同步的(md_ver==synced_ver)PDF 已含文字,走 get_text,不覆盖。"""
    for it in _overlay_records(rel):
        if not isinstance(it, dict) or it.get("mode") != "overlay":
            continue
        pg = it.get("page")
        if not isinstance(pg, int) or isinstance(pg, bool):
            continue
        if int(it.get("md_ver", 0) or 0) <= int(it.get("synced_ver", 0) or 0):
            continue
        md = (it.get("md") or "").strip()
        if not md:
            continue
        ttl = (it.get("title") or "").strip()
        texts[str(pg)] = (ttl + "\n" + md) if ttl else md
    return texts


def _list_pdfs():
    """扫 vault 下所有 PDF(排除 .orig/.compressed 备份),返回 [{rel,name,dir,mtime}]。
    mtime = max(PDF mtime, overlay sidecar mtime) → 插入页文字改动也纳入增量重建。"""
    out = []
    for p in OBSIDIAN_ROOT.rglob("*.pdf"):
        if "/.sandbox/" in p.as_posix():
            continue   # 工具库沙盒副本不进全文搜索
        if p.name.endswith((".orig.pdf", ".compressed.pdf")):
            continue
        try:
            rel = p.relative_to(OBSIDIAN_ROOT).as_posix()
            if rel.startswith("资源/收藏夹/"):
                continue   # 收藏夹物化书是原书内容的重复副本 → 不进全文搜索(否则同一页文字原书+收藏夹双份命中)
            out.append({
                "rel": rel,
                "name": p.name,
                "dir": str(Path(rel).parent),
                "mtime": max(int(p.stat().st_mtime), _userpages_sidecar_mtime(rel)),
                "abs": p,
            })
        except OSError:
            continue
    return out


def _private_web_index_owner() -> str:
    """Return the sole account allowed in the legacy shared search database.

    ``pdf-search.db`` predates accounts and its readers do not yet carry a uid.
    Feeding every account's private web cache into that shared database would
    silently bridge accounts.  Preserve the single-account deployment, and
    require an explicit owner once more than one account cache exists.
    """
    selected = normalize_user_id(os.environ.get("READER_SEARCH_OWNER_UID"))
    if selected:
        return selected
    base = CLAUDE_DIR / "state" / "web-cache" / "by-user"
    try:
        users = sorted(
            path.name
            for path in base.iterdir()
            if path.is_dir() and normalize_user_id(path.name)
        )
    except OSError:
        users = []
    return users[0] if len(users) == 1 else ""


def _list_webs():
    """浏览过的网页→ 与书同构的条目，且绝不跨账户汇入共享索引。

    当前搜索数据库仍是单 owner。单账户部署自动沿用；多账户部署必须设置
    ``READER_SEARCH_OWNER_UID``。未选择 owner 时网页条目会从共享索引移除，
    PDF/EPUB 搜索不受影响。
    """
    out = []
    owner = _private_web_index_owner()
    if not owner:
        return out
    root = CLAUDE_DIR / "state" / "web-cache"
    for uid, f, j in iter_account_web_cache(root, user_id=owner):
        try:
            url, txt = j.get("url"), (j.get("text") or "").strip()
            if not url or len(txt) < 80:
                continue
            out.append({"rel": "web:" + url, "name": (j.get("title") or url)[:120],
                        "dir": "web", "mtime": int(f.stat().st_mtime_ns // 1000) ^ int(uid),
                        "abs": None, "_web_text": txt, "_web_owner_uid": uid})
        except Exception:
            continue
    return out


def _page_texts(abs_path: Path, rel: str) -> dict:
    """{page_str: text},复用 pdf-text-index 缓存(与阅读器 F4 共享),缺则用 fitz 抽 + 写缓存。"""
    try:
        mtime = int(abs_path.stat().st_mtime)
    except OSError:
        mtime = 0
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    cpath = TEXT_INDEX_DIR / f"{sha}-{mtime}-v2.json"   # v2:OCR 竖线串清洗
    if cpath.exists():
        try:
            return json.loads(cpath.read_text("utf-8"))
        except Exception:
            pass
    import fitz
    import re as _re
    _noise = _re.compile(r"(?:[|│丨︱‖∥┃┆┇┊┋╎╏]\s*){2,}")   # OCR 把插图边框认成的竖线串
    out = {}
    doc = fitz.open(str(abs_path))
    try:
        for i in range(len(doc)):
            try:
                out[str(i + 1)] = _noise.sub(" ", doc[i].get_text("text"))
            except Exception:
                out[str(i + 1)] = ""
    finally:
        doc.close()
    try:
        TEXT_INDEX_DIR.mkdir(parents=True, exist_ok=True)
        for old in TEXT_INDEX_DIR.glob(f"{sha}-*.json"):
            if old != cpath:
                try:
                    old.unlink()
                except OSError:
                    pass
        cpath.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return out


def _index_book(con, bk: dict) -> int:
    """重建单本书的页行。返回插入的页数。"""
    rel = bk["rel"]
    con.execute("DELETE FROM pages_data WHERE file=?", (rel,))
    if bk.get("_web_text") is not None:          # 网页材料:单文档一页
        texts = {"1": bk["_web_text"]}
    else:
        texts = _apply_overlay_supplement(_page_texts(bk["abs"], rel), rel)   # 未同步 overlay 插入页补 sidecar md
    n = 0
    for pg_str, text in texts.items():
        text = (text or "").strip()
        if not text:
            continue
        try:
            pg = int(pg_str)
        except ValueError:
            continue
        con.execute("INSERT INTO pages_data(file, page, body) VALUES (?,?,?)", (rel, pg, text))
        n += 1
    con.execute(
        "INSERT OR REPLACE INTO meta(file,name,dir,mtime,pages,indexed_at) VALUES (?,?,?,?,?,?)",
        (rel, bk["name"], bk["dir"], bk["mtime"], n, int(time.time())),
    )
    return n


def build(rebuild=False):
    con = _connect()
    _init_schema(con)
    if rebuild:
        con.execute("DELETE FROM pages_data")
        con.execute("DELETE FROM meta")
        con.commit()
    pdfs = _list_pdfs() + _list_webs()   # 网页与书同权进索引(审计 #16)
    on_disk = {bk["rel"] for bk in pdfs}
    have = {row[0]: row[1] for row in con.execute("SELECT file, mtime FROM meta").fetchall()}
    # 清理磁盘上已消失的书
    for gone in set(have) - on_disk:
        con.execute("DELETE FROM pages_data WHERE file=?", (gone,))
        con.execute("DELETE FROM meta WHERE file=?", (gone,))
        print(f"  ✗ 移除(已删除): {gone}")
    con.commit()
    changed = 0
    for bk in pdfs:
        if not rebuild and have.get(bk["rel"]) == bk["mtime"]:
            continue  # 未变,跳过
        t0 = time.time()
        n = _index_book(con, bk)
        con.commit()
        changed += 1
        print(f"  ✓ {bk['rel']}  {n} 页  ({time.time()-t0:.1f}s)")
    # 优化 FTS(合并 b-tree,加快查询)——仅在有变动时,避免每 15min 空转
    if changed:
        try:
            con.execute("INSERT INTO pages_fts(pages_fts) VALUES('optimize')")
            con.commit()
        except Exception:
            pass
    total_books = con.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    total_pages = con.execute("SELECT COUNT(*) FROM pages_data").fetchone()[0]
    con.close()
    print(f"完成:{changed} 本变动 / 共 {total_books} 本 {total_pages} 页 → {DB_PATH}")


def stats():
    if not DB_PATH.exists():
        print("索引不存在,先运行 build")
        return
    con = _connect()
    rows = con.execute("SELECT name, pages, indexed_at FROM meta ORDER BY indexed_at DESC").fetchall()
    print(f"共 {len(rows)} 本:")
    for name, pages, ts in rows:
        print(f"  {pages:>5} 页  {name}")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="全量重建(删旧表)")
    ap.add_argument("--stats", action="store_true", help="只看统计")
    args = ap.parse_args()
    if args.stats:
        stats()
    else:
        build(rebuild=args.rebuild)
