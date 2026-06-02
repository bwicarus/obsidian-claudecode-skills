"""扫所有 vocab/<x>/<lemma>.md，融合多信号算 mastery 并写回 frontmatter。

信号：
  Anki 卡 state（queue/factor/lapses/reps）+ 查询次数（近 30 天，from vocab-lookups.jsonl）
  + 暴露但未查（vocab-exposure.json，阶段 E 提供；缺则不加这条信号）+ 时间衰减
  + 用户手动标 (frontmatter.user_mark = "known"|"unknown")

输出：
  - frontmatter.mastery（0.0-1.0 浮点）
  - frontmatter.mastery_label（"新词" / "见过" / "熟" / "掌握"）
  - 同时更新 tags 里的 vocab/<slug>

CLI:
  python3 scripts/vocab/compute_mastery.py            # 全扫一遍
  python3 scripts/vocab/compute_mastery.py --word construction --verbose
  python3 scripts/vocab/compute_mastery.py --dry-run  # 不写入只打印
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT_ROOT   = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
ANKI_URL     = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
CFG_PATH     = PROJECT_ROOT / "state" / "server-config.json"

sys.path.insert(0, str(Path(__file__).parent))
from build_vocab_note import _vocab_dir, _parse_simple_yaml  # noqa: E402

LABELS = [
    (0.25, "新词",   "new"),
    (0.55, "见过",   "seen"),
    (0.85, "熟",     "known"),
    (1.01, "掌握",   "mastered"),
]


def _label_for(mastery: float) -> tuple[str, str]:
    for thresh, label, slug in LABELS:
        if mastery < thresh:
            return label, slug
    return "掌握", "mastered"


def _anki_call(action: str, params: dict | None = None, timeout: int = 15):
    payload = json.dumps({"action": action, "version": 6, "params": params or {}}).encode("utf-8")
    req = urllib.request.Request(ANKI_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if body.get("error"):
        return None
    return body.get("result")


def load_anki_vocab_cards() -> dict[int, dict]:
    """返回 {note_id: {queues: [...], factors: [...], reps_total, lapses_total, due_min}}.
    扫 deck:Vocab 里所有 cards，按 note id 聚合。"""
    nids = _anki_call("findNotes", {"query": "deck:Vocab"}) or []
    if not nids:
        return {}
    notes_info = _anki_call("notesInfo", {"notes": nids}) or []
    note_to_cards: dict[int, list[int]] = {}
    for n in notes_info:
        nid = n.get("noteId")
        if nid:
            note_to_cards[nid] = n.get("cards", []) or []
    all_card_ids = [c for cs in note_to_cards.values() for c in cs]
    if not all_card_ids:
        return {}
    cards_info = _anki_call("cardsInfo", {"cards": all_card_ids}) or []
    by_card = {c["cardId"]: c for c in cards_info}
    out: dict[int, dict] = {}
    for nid, cids in note_to_cards.items():
        queues, factors, reps, lapses, dues = [], [], 0, 0, []
        for cid in cids:
            c = by_card.get(cid) or {}
            queues.append(c.get("queue", 0))
            f = c.get("factor", 0)
            if f: factors.append(f / 1000)
            reps += c.get("reps", 0)
            lapses += c.get("lapses", 0)
            dues.append(c.get("due", 0))
        out[nid] = {
            "queues": queues, "factors": factors,
            "reps": reps, "lapses": lapses,
            "due_min": min(dues) if dues else 0,
        }
    return out


def load_recent_lookups(days: int = 30) -> dict[str, int]:
    """state/vocab-lookups.jsonl 倒排：{lemma: count_in_last_N_days}."""
    log_path = PROJECT_ROOT / "state" / "vocab-lookups.jsonl"
    if not log_path.exists():
        return {}
    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).timestamp()
    out: dict[str, int] = {}
    try:
        for line in log_path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) < cutoff:
                continue
            lemma = (rec.get("lemma") or "").lower()
            if lemma:
                out[lemma] = out.get(lemma, 0) + 1
    except Exception:
        pass
    return out


def load_exposure() -> dict[str, int]:
    """state/vocab-exposure.json（阶段 E 提供）: {lemma: total_exposure_count}."""
    p = PROJECT_ROOT / "state" / "vocab-exposure.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text("utf-8"))
        # 兼容两种格式：{lemma: int} 老格式 / {lemma: {total: N, pages:[...]}}
        out = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                out[k] = int(v.get("total", 0))
            else:
                out[k] = int(v)
        return out
    except Exception:
        return {}


def compute_score(fm: dict, *, anki_data: dict, lookups_recent: int, exposure: int) -> tuple[float, dict]:
    """主算法。返回 (mastery, debug_info)."""
    # 用户手动标记 = 最高优先级，直接锁定（不被 Anki / 查词 / 暴露等信号侵蚀）
    user_mark = (fm.get("user_mark") or "").strip().lower()
    if user_mark == "known":
        return 1.0, {"user_known_lock": 1.0}
    if user_mark == "unknown":
        return 0.0, {"user_unknown_lock": 0.0}
    base = 0.50
    score = base
    debug = {"base": base}

    # 1. Anki 信号
    aid = fm.get("anki_card_id")
    try:
        aid_int = int(aid) if aid and not isinstance(aid, list) else 0
    except (TypeError, ValueError):
        aid_int = 0
    card = anki_data.get(aid_int) if aid_int else None
    if card:
        queues = card["queues"]; factors = card["factors"]
        reps = card["reps"]; lapses = card["lapses"]
        avg_ease = sum(factors) / len(factors) if factors else 2.5
        # queue: 0=new, 1=learning, 2=review, 3=day_learn, -1=suspended, -2=buried
        if any(q == 2 for q in queues) and avg_ease >= 2.5:
            score += 0.30; debug["anki_review_high"] = 0.30
        elif any(q == 2 for q in queues):
            score += 0.15; debug["anki_review_mid"]  = 0.15
        if all(q == 0 for q in queues) and reps == 0:
            score -= 0.05; debug["anki_new_untouched"] = -0.05
        if lapses:
            penalty = min(0.20, lapses * 0.04)
            score -= penalty; debug["anki_lapses"] = -penalty
    else:
        debug["anki_no_card"] = 0
        score -= 0.05   # 没 Anki 卡 = 不在学习管线，扣一点

    # 2. 查询次数（近 30 天）
    if lookups_recent >= 5:
        score -= 0.25; debug["lookups_high"] = -0.25
    elif lookups_recent >= 3:
        score -= 0.15; debug["lookups_mid"]  = -0.15
    elif lookups_recent >= 2:
        score -= 0.05; debug["lookups_low"]  = -0.05

    # 3. 暴露但未查（核心信号；exposure 由阶段 E 计算）
    if exposure > 0:
        exposed_without_lookup = max(0, exposure - lookups_recent)
        bonus = min(0.40, exposed_without_lookup * 0.05)
        score += bonus
        debug["exposure_bonus"] = bonus

    # 4. 时间衰减
    last = fm.get("last_lookup", "")
    if last:
        try:
            last_d = dt.datetime.strptime(last, "%Y-%m-%d")
            days = (dt.datetime.now() - last_d).days
            if days > 90:
                score += 0.10; debug["decay_long"] = 0.10
            elif days > 30:
                score += 0.05; debug["decay_mid"]  = 0.05
        except (ValueError, TypeError):
            pass

    # （用户手动标记已在函数开头短路处理 → 锁定 1.0 / 0.0）
    return max(0.0, min(1.0, score)), debug


def _rewrite_fm(path: Path, new_mastery: float, new_label: str, new_slug: str) -> bool:
    raw = path.read_text("utf-8")
    if not raw.startswith("---\n"):
        return False
    end = raw.find("\n---\n", 4)
    if end < 0:
        return False
    fm_text = raw[4:end]
    rest = raw[end:]
    changed = False

    def _sub(field: str, value):
        nonlocal fm_text, changed
        pattern = rf"^{field}:.*$"
        new_line = f"{field}: {value}"
        if re.search(pattern, fm_text, flags=re.M):
            if re.search(pattern, fm_text, flags=re.M).group(0) != new_line:
                fm_text = re.sub(pattern, new_line, fm_text, flags=re.M)
                changed = True
        else:
            fm_text = fm_text.rstrip() + f"\n{new_line}\n"
            changed = True

    _sub("mastery", round(new_mastery, 3))
    _sub("mastery_label", new_label)

    # tags 更新：把 vocab/<old> 改成 vocab/<new_slug>
    if "tags:" in fm_text:
        # 简易处理：检测 tag 列表中的 vocab/xxx，替换
        new_fm = re.sub(
            r"(^|\n)(  - )vocab/(new|seen|known|mastered)(?=\s|$)",
            rf"\1\2vocab/{new_slug}",
            fm_text,
        )
        if new_fm != fm_text:
            fm_text = new_fm
            changed = True

    if changed:
        path.write_text("---\n" + fm_text + rest, "utf-8")
    return changed


def _set_fm_field(path: Path, field: str, value) -> bool:
    """在 frontmatter 里设/删一个字段。value 为 "" / None → 删除该行。"""
    raw = path.read_text("utf-8")
    if not raw.startswith("---\n"):
        return False
    end = raw.find("\n---\n", 4)
    if end < 0:
        return False
    fm_text = raw[4:end]
    rest = raw[end:]
    pat = rf"^{field}:.*$"
    if value == "" or value is None:
        new_fm = re.sub(pat + r"\n?", "", fm_text, flags=re.M)   # 删字段行
    else:
        line = f"{field}: {value}"
        if re.search(pat, fm_text, flags=re.M):
            new_fm = re.sub(pat, line, fm_text, flags=re.M)
        else:
            new_fm = fm_text.rstrip() + f"\n{line}\n"
    if new_fm != fm_text:
        path.write_text("---\n" + new_fm + rest, "utf-8")
        return True
    return False


def apply_user_mark(lemma: str, mark: str) -> dict:
    """阅读器手动标「已掌握 / 没掌握 / 清除」。写 frontmatter.user_mark 并立即锁定 mastery。
    mark: "known" → 1.0；"unknown" → 0.0；"" / "clear" → 删标记（恢复算法，暂置 0.5）。
    返回 {ok, mastery, label, path}。"""
    lemma = (lemma or "").strip().lower()
    if not lemma:
        return {"ok": False, "error": "empty lemma"}
    from build_vocab_note import _word_path   # 统一分桶（日语按读音首假名，英语按首字母）
    path = _word_path(lemma)
    if not path.exists():
        return {"ok": False, "error": "note not found", "path": str(path)}
    mark = (mark or "").strip().lower()
    if mark == "known":
        mastery = 1.0
    elif mark == "unknown":
        mastery = 0.0
    else:
        mark = ""        # 清除标记
        mastery = 0.5
    label, slug = _label_for(mastery)
    _set_fm_field(path, "user_mark", mark)            # 锁定信号
    _rewrite_fm(path, mastery, label, slug)           # 立即生效（mastery + label + tags）
    return {
        "ok": True, "mastery": mastery, "label": label,
        "path": str(path.relative_to(VAULT_ROOT).as_posix()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--word", help="只跑一个词（lemma）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    vroot = _vocab_dir()
    if not vroot.exists():
        print(f"vocab dir not found: {vroot}", file=sys.stderr); return

    print("[1/3] 拉 Anki Vocab deck 数据…")
    anki_data = load_anki_vocab_cards()
    print(f"      {len(anki_data)} notes in deck Vocab")

    print("[2/3] 扫近 30 天查词日志…")
    lookups = load_recent_lookups(30)
    print(f"      {len(lookups)} lemmas 有近期查询")

    exposure = load_exposure()
    if exposure:
        print(f"      暴露数据：{len(exposure)} lemmas")

    print("[3/3] 算 mastery + 写回…")
    files = sorted(vroot.rglob("*.md"))
    if args.word:
        files = [vroot / args.word[0].lower() / f"{args.word.lower()}.md"]

    n_updated = 0
    for f in files:
        if not f.exists() or f.name.startswith("_"):
            continue
        raw = f.read_text("utf-8")
        if not raw.startswith("---\n"):
            continue
        end = raw.find("\n---\n", 4)
        if end < 0:
            continue
        fm = _parse_simple_yaml(raw[4:end])
        lemma = fm.get("lemma") or fm.get("word") or f.stem
        lookups_recent = lookups.get(lemma.lower(), 0)
        exp = exposure.get(lemma.lower(), 0) if exposure else 0
        score, debug = compute_score(fm, anki_data=anki_data, lookups_recent=lookups_recent, exposure=exp)
        label, slug = _label_for(score)

        old_score = float(fm.get("mastery") or 0.0)
        old_label = fm.get("mastery_label") or ""
        if args.verbose or args.word:
            print(f"\n{lemma}: {old_score:.2f} → {score:.2f} ({label})")
            if args.verbose:
                print("  debug:", debug)

        if not args.dry_run and (abs(score - old_score) > 0.01 or label != old_label):
            if _rewrite_fm(f, score, label, slug):
                n_updated += 1

    print(f"\n完成：更新 {n_updated} 个词")


if __name__ == "__main__":
    main()
