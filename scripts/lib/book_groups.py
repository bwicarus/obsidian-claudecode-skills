#!/usr/bin/env python3
"""book_groups.py — 虚拟合并书(用户设计:命名约定即合并)。

同目录、同扩展名、stem 匹配 `<base>part<N>` 的文件 = 一本虚拟书的各卷。
原文件不动;本模块只提供**映射**:成员/页数/偏移/连续页码/canonical 身份。
页数来源:pdf-search.db(MAX page)→ fitz 兜底;缓存 state/reader-book-groups.json(按 mtime 失效)。
消费方:pdf_reader(书架合并+阅读器连续页码+边界翻卷)、propose_concept_notes(概念网按组归一+跨卷搜索)。
"""
import json
import re
import sqlite3
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

VAULT = Path(getattr(config, "VAULT_ROOT", "/home/bwicarus/obsidian"))
if not VAULT.exists():   # env 缺失退回 Windows 默认的老坑 → 按项目根自愈(同 attention_profile)
    _v = Path(config.PROJECT_DIR).parent / "obsidian"
    if _v.exists():
        VAULT = _v
SEARCH_DB = Path(config.PROJECT_DIR) / "state" / "pdf-search.db"
CACHE = Path(config.PROJECT_DIR) / "state" / "reader-book-groups.json"

PART_RE = re.compile(r"^(?P<base>.+?)[\s_\-]*part[\s_\-]*(?P<num>\d+)$", re.I)


def split_part(stem):
    """'料理师part2' → ('料理师', 2);非分卷 → None。"""
    m = PART_RE.match(stem or "")
    if not m:
        return None
    base = m.group("base").strip()
    return (base, int(m.group("num"))) if base else None


def _load_cache():
    try:
        return json.loads(CACHE.read_text("utf-8"))
    except Exception:
        return {}


def _page_count(rel):
    """该卷页数:缓存(mtime 失效)→ pdf-search.db → fitz。取不到返 0。"""
    ap = VAULT / rel
    try:
        mt = int(ap.stat().st_mtime)
    except OSError:
        return 0
    cache = _load_cache()
    c = cache.get(rel)
    if c and c.get("mtime") == mt and c.get("pages"):
        return int(c["pages"])
    pages = 0
    if SEARCH_DB.exists():
        try:
            con = sqlite3.connect("file:%s?mode=ro" % SEARCH_DB, uri=True)
            r = con.execute("SELECT MAX(page) FROM pages_data WHERE file=?", (rel,)).fetchone()
            con.close()
            pages = int(r[0] or 0)
        except Exception:
            pages = 0
    if not pages:
        try:
            import fitz
            with fitz.open(str(ap)) as d:
                pages = d.page_count
        except Exception:
            pages = 0
    if pages:
        cache[rel] = {"mtime": mt, "pages": pages}
        try:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
            tmp.replace(CACHE)
        except Exception:
            pass
    return pages


def members_of(rel):
    """同组成员 [(num, rel)] 升序;非分卷或孤卷 → []。同目录+同扩展名+同 base。"""
    p = Path(rel)
    sp = split_part(p.stem)
    if not sp:
        return []
    base, _ = sp
    out = []
    d = VAULT / p.parent
    if not d.is_dir():
        return []
    for f in d.iterdir():
        if f.suffix.lower() != p.suffix.lower() or not f.is_file():
            continue
        sp2 = split_part(f.stem)
        if sp2 and sp2[0] == base:
            out.append((sp2[1], (Path(rel).parent / f.name).as_posix() if str(p.parent) != "." else f.name))
    out.sort()
    return out if len(out) >= 2 else []


def group_info(rel):
    """完整组信息(供阅读器/搜索):None=非合并书。
    {base, members:[{rel,num,pages,offset}], total, self:{num,pages,offset,index}, prev, next}"""
    mem = members_of(rel)
    if not mem:
        return None
    members = []
    off = 0
    for num, r in mem:
        pg = _page_count(r)
        members.append({"rel": r, "num": num, "pages": pg, "offset": off})
        off += pg
    me = next((m for i, m in enumerate(members) if m["rel"] == rel), None)
    if me is None:
        return None
    idx = members.index(me)
    base = split_part(Path(rel).stem)[0]
    return {"base": base, "members": members, "total": off,
            "self": {"num": me["num"], "pages": me["pages"], "offset": me["offset"], "index": idx},
            "prev": members[idx - 1]["rel"] if idx > 0 else None,
            "next": members[idx + 1]["rel"] if idx + 1 < len(members) else None}


def canonical_rel(rel):
    """组身份 = 最小卷号成员的 rel(注册表/概念网/科目 按这个归一);非分卷返回原 rel。"""
    mem = members_of(rel)
    return mem[0][1] if mem else rel
