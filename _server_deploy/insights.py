"""学习数据看板（/insights）—— 跨 Anki / vocab / 知识图谱 / PDF 阅读 的统一学习分析。

设计:自包含 blueprint(register_insights 风格,同 control/skilltree/pdf_reader),**请求时实时聚合 + 120s 进程缓存**。
不耦合 daily 流水线 → 永远新鲜、不会拖垮夜间任务。所有数据都是本机文件 / SQLite,聚合 ~1-2s,缓存后秒开。

数据源(全部只读):
  • Anki   collection.anki2(immutable=1 只读):revlog 留存曲线/活跃度,cards 漏斗/到期排期,cards.data FSRS 强度
  • vocab  vault 资源/vocab/*/*.md frontmatter + state/vocab-lookups.jsonl
  • KG     knowledge_graph/*.json(排除 .bak/_archive)
  • PDF    state/pdf-prefs/<user>.json 阅读进度 + state/pdf-search.db meta 总页数
  • notes  dashboard/dashboard.json 的 notes[](复用现成 retention/mastery/lapses 字段做薄弱点/科目汇总)

时区:服务器 JST,但显式用 +9h(32400s)算"日历天",不依赖服务器 TZ 配置。
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import jsonify, render_template, session

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
OBSIDIAN_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
ANKI_COLLECTION = Path(os.environ.get(
    "ANKI_COLLECTION",
    str(Path.home() / ".local/share/Anki2/User 1/collection.anki2"),
))
JST_OFF = 32400  # +9h，把 epoch 当作 UTC+9 算日历天，与服务器 TZ 解耦

_CACHE = {}        # 按 username 分键:{user: {ts, payload}}(多用户下不串数据)
_CACHE_TTL = 120   # 秒
_CACHE_MAX = 32    # 用户槽上限,防膨胀

# 旧卡片:早已存在的大批量日语沉浸牌组(默认 新/漢字/意味,~4500 卡/44k 复习)会淹没真实新学习,
# 看板的 Anki 统计按用户要求**忽略这些旧卡**,只算我们新工作流的学习(数学/CS/生词/语法/项目…)。
# 可用 env INSIGHTS_LEGACY_DECKS(逗号分隔顶层牌组名)覆盖。
_LEGACY_DECKS = set(filter(None, (os.environ.get("INSIGHTS_LEGACY_DECKS") or "新,漢字,意味").split(",")))


# ── 工具 ──────────────────────────────────────────────────────────────
def _jst_day(epoch_s: float) -> str:
    """epoch 秒 → JST 日历日 'YYYY-MM-DD'(显式 +9h,与服务器 TZ 解耦)。"""
    return datetime.fromtimestamp(epoch_s + JST_OFF, tz=timezone.utc).strftime("%Y-%m-%d")


def _jst_date(epoch_s: float):
    return datetime.fromtimestamp(epoch_s + JST_OFF, tz=timezone.utc).date()


def _anki_con():
    return sqlite3.connect(f"file:{ANKI_COLLECTION}?immutable=1", uri=True)


def _legacy_deck_ids(con) -> list:
    """返回应忽略的'旧卡片'牌组(含子牌组)的 deck id 列表。decks.name 用 \\x1f 分层,按顶层名匹配。"""
    out = []
    for did, name in con.execute("SELECT id, name FROM decks"):
        if (name or "").split("\x1f")[0] in _LEGACY_DECKS:
            out.append(did)
    return out


def _safe(fn, default):
    """跑一个聚合函数,失败返回 default(+ 记 error),保证单块挂掉不拖垮整体。"""
    try:
        return fn()
    except Exception as ex:  # noqa: BLE001
        return {"_error": f"{type(ex).__name__}: {ex}", **(default if isinstance(default, dict) else {})} if isinstance(default, dict) else default


# ── Anki ──────────────────────────────────────────────────────────────
def _anki_stats() -> dict:
    con = _anki_con()
    try:
        crt = con.execute("SELECT crt FROM col").fetchone()[0]
        today_off = int((time.time() - crt) // 86400)
        # 忽略的旧牌组:取**实际存在**且顶层名在 _LEGACY_DECKS 的 deck(删光后自动为空 → 不再显示"已忽略"说明)
        legacy_pairs = [(did, name) for did, name in con.execute("SELECT id, name FROM decks")
                        if (name or "").split("\x1f")[0] in _LEGACY_DECKS]
        legacy = [d for d, _ in legacy_pairs]
        excluded_names = sorted({(n or "").split("\x1f")[0] for _, n in legacy_pairs})
        ph = ",".join("?" * len(legacy))
        # 排除旧卡的 SQL 片段:基于 cards.did(直接 cards 表用 col="did";revlog join 用 col="c.did")
        c_notin = (f" AND did NOT IN ({ph})") if legacy else ""
        rc_notin = (f" AND c.did NOT IN ({ph})") if legacy else ""
        lg = legacy

        # 留存曲线:review 阶段(type=1) 按区间分桶,pass=ease>=2(仅本工作流的卡)
        BUCKETS = [(1, 1, "1d"), (2, 3, "2-3d"), (4, 7, "4-7d"), (8, 14, "1-2w"),
                   (15, 30, "2-4w"), (31, 60, "1-2m"), (61, 180, "2-6m"), (181, 10**9, "6m+")]
        curve = []
        for lo, hi, label in BUCKETS:
            row = con.execute(
                "SELECT COUNT(*), AVG(CASE WHEN r.ease>=2 THEN 1.0 ELSE 0 END) "
                "FROM revlog r JOIN cards c ON c.id=r.cid "
                "WHERE r.type=1 AND r.lastIvl>=? AND r.lastIvl<=?" + rc_notin, (lo, hi, *lg)).fetchone()
            n = row[0] or 0
            curve.append({"bucket": label, "n": n, "pass": round(row[1], 4) if row[1] is not None else None})
        yr = con.execute("SELECT AVG(CASE WHEN r.ease>=2 THEN 1.0 ELSE 0 END) FROM revlog r JOIN cards c ON c.id=r.cid "
                         "WHERE r.type=1 AND r.lastIvl>0 AND r.lastIvl<21" + rc_notin, lg).fetchone()[0]
        mr = con.execute("SELECT AVG(CASE WHEN r.ease>=2 THEN 1.0 ELSE 0 END) FROM revlog r JOIN cards c ON c.id=r.cid "
                         "WHERE r.type=1 AND r.lastIvl>=21" + rc_notin, lg).fetchone()[0]

        # 卡片漏斗:按 queue(排除旧卡)
        qd = dict(con.execute("SELECT queue, COUNT(*) FROM cards WHERE 1=1" + c_notin + " GROUP BY queue", lg).fetchall())
        funnel = {
            "new": qd.get(0, 0),
            "learning": qd.get(1, 0) + qd.get(3, 0),
            "review": qd.get(2, 0),
            "suspended": qd.get(-1, 0),
            "buried": qd.get(-2, 0) + qd.get(-3, 0),
        }

        # 到期排期:queue=2,due ∈ [today_off, today_off+13](排除旧卡)。按原始 due 分组,Python 里减偏移
        forecast = []
        fraw = con.execute(
            "SELECT due, COUNT(*) FROM cards WHERE queue=2 AND due BETWEEN ? AND ?" + c_notin + " GROUP BY due",
            (today_off, today_off + 13, *lg)).fetchall()
        fmap = {int(due) - today_off: cnt for due, cnt in fraw}
        for d in range(14):
            forecast.append({"day": d, "count": fmap.get(d, 0)})
        due_today = con.execute("SELECT COUNT(*) FROM cards WHERE queue=2 AND due<=?" + c_notin, (today_off, *lg)).fetchone()[0]
        overdue = con.execute("SELECT COUNT(*) FROM cards WHERE queue=2 AND due<?" + c_notin, (today_off, *lg)).fetchone()[0]

        # FSRS 记忆强度(stability)分布:cards.data JSON 的 's'(天)。最低桶下界=0(含 0<s<1 的学习阶段卡)
        SB = [0, 7, 30, 90, 180, 365, 10**9]
        SB_LABEL = ["<1w", "1w-1m", "1-3m", "3-6m", "6m-1y", "1y+"]
        shist = [0] * (len(SB) - 1)
        for (data,) in con.execute("SELECT data FROM cards WHERE data NOT IN ('', '{}')" + c_notin, lg):
            try:
                s = json.loads(data).get("s")
            except Exception:
                continue
            if not s:
                continue
            for i in range(len(SB) - 1):
                if SB[i] <= s < SB[i + 1]:
                    shist[i] += 1
                    break
        stability = [{"bucket": SB_LABEL[i], "count": shist[i]} for i in range(len(shist))]

        # per-deck 复习量 + 留存(排除旧牌组;decks.name 用 \x1f 分层 → 显示转 ::)
        per_deck = []
        try:
            d_notin = (f" AND d.id NOT IN ({ph})") if legacy else ""
            drows = con.execute(
                # pass 仅在 type=1(review) 行上算:内层 CASE 给 1/0,非 review 行给 NULL → AVG 忽略
                "SELECT d.name, COUNT(*), "
                "AVG(CASE WHEN r.type=1 THEN (CASE WHEN r.ease>=2 THEN 1.0 ELSE 0.0 END) END) "
                "FROM revlog r JOIN cards c ON c.id=r.cid JOIN decks d ON d.id=c.did "
                "WHERE r.type!=3" + d_notin + " GROUP BY d.id ORDER BY COUNT(*) DESC LIMIT 12", lg).fetchall()
            for name, cnt, passr in drows:
                per_deck.append({"deck": (name or "").replace("\x1f", "::"),
                                 "reviews": cnt, "pass": round(passr, 4) if passr is not None else None})
        except Exception:
            pass

        total_cards = con.execute("SELECT COUNT(*) FROM cards WHERE 1=1" + c_notin, lg).fetchone()[0]
        return {
            "retention_curve": curve,
            "retention_young": round(yr, 4) if yr is not None else None,
            "retention_mature": round(mr, 4) if mr is not None else None,
            "funnel": funnel, "due_forecast": forecast, "due_today": due_today, "overdue": overdue,
            "stability": stability, "per_deck": per_deck, "total_cards": total_cards,
            "excluded_legacy_decks": excluded_names,
        }
    finally:
        con.close()


def _anki_reviews_by_day() -> dict:
    """{date: review_count}(排除 cram type=3 + 旧牌组),JST 日历天。"""
    con = _anki_con()
    try:
        legacy = _legacy_deck_ids(con)
        notin = (" AND c.did NOT IN (%s)" % ",".join("?" * len(legacy))) if legacy else ""
        rows = con.execute(
            "SELECT date((r.id/1000)+?, 'unixepoch'), COUNT(*) "
            "FROM revlog r JOIN cards c ON c.id=r.cid WHERE r.type!=3" + notin + " GROUP BY 1",
            (JST_OFF, *legacy)).fetchall()
        return {d: c for d, c in rows}
    finally:
        con.close()


# ── vocab ─────────────────────────────────────────────────────────────
def _parse_frontmatter(text: str) -> dict:
    """极简 YAML frontmatter 解析(只取顶层 key: value 标量)。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fm = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.startswith(" ") or line.startswith("\t"):
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


_LABEL_ORDER = ["完全不会", "见过", "学习中", "熟", "掌握"]


def _vocab_summary() -> dict:
    vdir = OBSIDIAN_ROOT / "资源" / "vocab"
    total = 0
    by_lang = {"en": 0, "ja": 0}
    mastery_hist = [0] * 10
    by_label = Counter()
    first_seen = []  # [date_str]
    for mdp in glob.glob(str(vdir / "*" / "*.md")):
        try:
            txt = Path(mdp).read_text("utf-8", errors="ignore")[:1500]
        except OSError:
            continue
        fm = _parse_frontmatter(txt)
        if not fm:
            continue
        total += 1
        lang = (fm.get("lang") or "en").strip().lower()
        lang = "ja" if lang.startswith("ja") else "en"
        by_lang[lang] += 1
        try:
            mv = float(fm.get("mastery", 0) or 0)
            mastery_hist[min(int(min(mv, 0.999) * 10), 9)] += 1
        except (ValueError, TypeError):
            pass
        lab = (fm.get("mastery_label") or "").strip()
        if lab == "新词":
            lab = "完全不会"
        if lab:
            by_label[lab] += 1
        fs = (fm.get("first_seen") or "").strip()[:10]
        if fs and fs[:4].isdigit():
            first_seen.append(fs)

    # 词汇量累计增长曲线(按 first_seen 排序累加)
    cumulative = []
    if first_seen:
        cnt_by_day = Counter(first_seen)
        run = 0
        for d in sorted(cnt_by_day):
            run += cnt_by_day[d]
            cumulative.append({"date": d, "total": run})

    # 查词趋势 + top 来源书(vocab-lookups.jsonl)
    lookups_per_day = Counter()
    top_books = Counter()
    lp = CLAUDE_DIR / "state" / "vocab-lookups.jsonl"
    if lp.exists():
        with lp.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if isinstance(ts, (int, float)):
                    lookups_per_day[_jst_day(ts)] += 1
                pdf = rec.get("pdf") or rec.get("book") or ""
                if pdf:
                    top_books[os.path.basename(str(pdf))] += 1

    label_sorted = [{"label": lab, "count": by_label[lab]}
                    for lab in _LABEL_ORDER if by_label.get(lab)]
    for lab, c in by_label.items():  # 兜底:不在标准序里的标签
        if lab not in _LABEL_ORDER:
            label_sorted.append({"label": lab, "count": c})

    return {
        "total": total, "by_lang": by_lang,
        "mastery_hist": mastery_hist,
        "by_label": label_sorted,
        "cumulative": cumulative[-180:],
        "lookups_per_day": dict(lookups_per_day),
        "top_books": [{"book": b, "count": c} for b, c in top_books.most_common(8)],
    }


# ── 知识图谱 ──────────────────────────────────────────────────────────
def _kg_progress() -> dict:
    kg_dir = CLAUDE_DIR / "knowledge_graph"
    books = []
    recently = []
    next_up = []
    for kgp in sorted(kg_dir.glob("*.json")):
        if kgp.name.endswith(".bak.json"):
            continue
        try:
            kg = json.loads(kgp.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        nodes = kg.get("nodes", [])
        l2 = [n for n in nodes if n.get("level") == 2]
        if not l2:
            continue
        kind = kg.get("kind", "")
        states = Counter(n.get("state", "locked") for n in l2)
        tracked = sum(1 for n in l2 if n.get("tracked"))
        title = kg.get("book") or kg.get("title") or kgp.stem
        is_grammar = kind == "grammar"
        denom = len(l2)
        done = tracked if is_grammar else states.get("mastered", 0)
        books.append({
            "book": kgp.stem, "title": title, "kind": kind,
            "l2_total": denom, "mastered": states.get("mastered", 0),
            "unlockable": states.get("unlockable", 0), "locked": states.get("locked", 0),
            "tracked": tracked, "is_grammar": is_grammar,
            "pct": round(100.0 * done / denom, 1) if denom else 0,
        })
        # 最近掌握(用 containing_notes mtime 近似)
        for n in l2:
            if n.get("state") != "mastered":
                continue
            mt = 0
            for nt in (n.get("containing_notes") or []):
                p = OBSIDIAN_ROOT / nt
                try:
                    mt = max(mt, p.stat().st_mtime)
                except OSError:
                    pass
            if mt:
                recently.append({"label": n.get("numeric_label", ""), "name": n.get("name", ""),
                                 "title": title, "mtime": int(mt)})
        # 下一步可学(unlockable)
        for n in l2:
            if n.get("state") == "unlockable":
                next_up.append({"label": n.get("numeric_label", ""), "name": n.get("name", ""),
                                "title": title, "book": kgp.stem})
    recently.sort(key=lambda x: -x["mtime"])
    return {
        "books": books,
        "recently_mastered": recently[:8],
        "next_up": next_up[:10],
        "total_l2": sum(b["l2_total"] for b in books),
        "total_mastered": sum(b["mastered"] for b in books),
    }


# ── PDF 阅读进度 ──────────────────────────────────────────────────────
def _reading_progress(username: str) -> dict:
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(username))[:64] or "anon"   # 防路径穿越,与 pdf_reader._prefs_path 一致
    prefs_p = CLAUDE_DIR / "state" / "pdf-prefs" / f"{safe}.json"
    if not prefs_p.exists():
        return {"books": []}
    try:
        prefs = json.loads(prefs_p.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"books": []}
    raw = prefs.get("pdf-last-positions")
    if isinstance(raw, str):  # localStorage dump → 值本身是字符串,二次 json.loads
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        return {"books": []}

    # 总页数:读 pdf-search.db meta(file→pages),没有则未知
    total_by_file = {}
    sdb = CLAUDE_DIR / "state" / "pdf-search.db"
    if sdb.exists():
        try:
            con = sqlite3.connect(f"file:{sdb}?mode=ro", uri=True)
            total_by_file = dict(con.execute("SELECT file, pages FROM meta").fetchall())
            con.close()
        except Exception:
            pass

    out = []
    for rel, pos in raw.items():
        if not isinstance(pos, dict):
            continue
        page = pos.get("page") or 0
        ts = pos.get("ts") or 0
        if ts > 1e12:  # 毫秒 → 秒
            ts = ts / 1000.0
        total = total_by_file.get(rel) or 0
        out.append({
            "file": rel, "title": os.path.basename(rel), "page": page, "total": total,
            "pct": round(100.0 * page / total, 1) if total else None, "ts": int(ts),
        })
    out.sort(key=lambda x: -x["ts"])
    return {"books": out}


# ── notes[](复用 dashboard.json)→ 薄弱点 + 科目汇总 ───────────────────
def _notes_insights() -> dict:
    dp = CLAUDE_DIR / "dashboard" / "dashboard.json"
    if not dp.exists():
        return {"by_lapse": [], "by_retention": [], "subjects": []}
    try:
        notes = json.loads(dp.read_text("utf-8")).get("notes", [])
    except (json.JSONDecodeError, OSError):
        return {"by_lapse": [], "by_retention": [], "subjects": []}

    by_lapse, by_ret = [], []
    subj = defaultdict(lambda: {"notes": 0, "cards": 0, "mastery_sum": 0.0, "retention_sum": 0.0, "ret_n": 0})
    for nt in notes:
        reps = nt.get("reps_total") or 0
        lapses = nt.get("lapses_total") or 0
        if reps >= 10:  # 复习够多才谈 lapse 率,避免新卡噪声
            by_lapse.append({"name": nt.get("name", ""), "subject": nt.get("subject", ""),
                             "lapse_rate": round(lapses / reps, 3), "reps": reps, "lapses": lapses,
                             "url": nt.get("obsidian_url", "")})
        ret = nt.get("retention")
        if isinstance(ret, (int, float)) and (nt.get("anki_review") or 0) > 0:
            by_ret.append({"name": nt.get("name", ""), "subject": nt.get("subject", ""),
                           "retention": round(ret, 3), "url": nt.get("obsidian_url", "")})
        s = nt.get("subject") or "未分类"
        d = subj[s]
        d["notes"] += 1
        d["cards"] += nt.get("anki_total") or 0
        if isinstance(nt.get("mastery"), (int, float)):
            d["mastery_sum"] += nt["mastery"]
        if isinstance(nt.get("retention"), (int, float)):
            d["retention_sum"] += nt["retention"]; d["ret_n"] += 1

    by_lapse.sort(key=lambda x: -x["lapse_rate"])
    by_ret.sort(key=lambda x: x["retention"])
    subjects = []
    for s, d in subj.items():
        subjects.append({
            "subject": s, "notes": d["notes"], "cards": d["cards"],
            "avg_mastery": round(d["mastery_sum"] / d["notes"], 3) if d["notes"] else 0,
            "avg_retention": round(d["retention_sum"] / d["ret_n"], 3) if d["ret_n"] else None,
        })
    subjects.sort(key=lambda x: -x["cards"])
    return {"by_lapse": by_lapse[:12], "by_retention": by_ret[:12], "subjects": subjects}


# ── 组装 ──────────────────────────────────────────────────────────────
def _build_payload(username: str) -> dict:
    anki = _safe(_anki_stats, {})
    reviews_by_day = _safe(_anki_reviews_by_day, {})
    vocab = _safe(_vocab_summary, {})
    kg = _safe(_kg_progress, {})
    reading = _safe(lambda: _reading_progress(username), {"books": []})
    notes = _safe(_notes_insights, {})

    # 活跃度热力图:近 26 周(182 天),每天 = Anki 复习(已排除旧卡) + 查词
    lookups_by_day = vocab.get("lookups_per_day", {}) if isinstance(vocab, dict) else {}
    today = _jst_date(time.time())
    DAYS = 182
    activity = []
    streak = 0
    streak_live = True
    for i in range(DAYS):
        d = today - timedelta(days=DAYS - 1 - i)
        ds = d.strftime("%Y-%m-%d")
        rv = reviews_by_day.get(ds, 0) if isinstance(reviews_by_day, dict) else 0
        lk = lookups_by_day.get(ds, 0)
        activity.append({"date": ds, "reviews": rv, "lookups": lk, "total": rv + lk})
    # streak:从今天往回数连续有活动的天(今天没活动则从昨天起算)
    for a in reversed(activity):
        if a["total"] > 0:
            if streak_live:
                streak += 1
        else:
            if a["date"] == today.strftime("%Y-%m-%d"):
                continue  # 今天还没学不算断
            break
    today_str = today.strftime("%Y-%m-%d")
    today_reviews = reviews_by_day.get(today_str, 0) if isinstance(reviews_by_day, dict) else 0
    today_lookups = lookups_by_day.get(today_str, 0)

    kpis = {
        "streak_days": streak,
        "today_reviews": today_reviews,
        "today_lookups": today_lookups,
        "total_vocab": vocab.get("total", 0) if isinstance(vocab, dict) else 0,
        "total_cards": anki.get("total_cards", 0) if isinstance(anki, dict) else 0,
        "mastered_nodes": kg.get("total_mastered", 0) if isinstance(kg, dict) else 0,
        "due_today": anki.get("due_today", 0) if isinstance(anki, dict) else 0,
        "overdue": anki.get("overdue", 0) if isinstance(anki, dict) else 0,
        "week_reviews": sum(a["reviews"] for a in activity[-7:]),
        "week_lookups": sum(a["lookups"] for a in activity[-7:]),
    }

    return {
        "ok": True,
        "generated_at": _jst_day(time.time()) + datetime.fromtimestamp(time.time() + JST_OFF, tz=timezone.utc).strftime(" %H:%M"),
        "kpis": kpis,
        "activity": activity,
        "anki": anki,
        "vocab": vocab,
        "kg": kg,
        "reading": reading,
        "notes": notes,
    }


def register_insights(app):
    @app.route("/insights/")
    def insights_home():
        return render_template("insights.html")

    @app.route("/insights/api/data")
    def insights_data():
        now = time.time()
        username = session.get("username") or "anon"
        slot = _CACHE.get(username)   # 按用户分键,避免多用户串数据(per-user 阅读进度等)
        if slot and (now - slot["ts"]) < _CACHE_TTL:
            return jsonify(slot["payload"])
        payload = _build_payload(username)
        if len(_CACHE) >= _CACHE_MAX and username not in _CACHE:   # 朴素逐出最旧,防膨胀
            try:
                _CACHE.pop(min(_CACHE, key=lambda u: _CACHE[u]["ts"]))
            except ValueError:
                pass
        _CACHE[username] = {"ts": now, "payload": payload}
        return jsonify(payload)
