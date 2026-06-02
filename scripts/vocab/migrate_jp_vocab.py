#!/usr/bin/env python3
"""一次性迁移：state/jp-vocab.json（日语生词旧 store：looks/last_ts/mastered）
→ vault vocab 笔记（资源/vocab/<首字>/<lemma>.md，跟英语同一套库/算法/工具）。

迁移后日语词跟英语词共用 vocab_index / compute_mastery / apply_user_mark / paragraph_exposure。
**非破坏**：不删 jp-vocab.json（保留做备份/回滚）；已存在的笔记跳过（幂等可重跑）。

用法：python3 scripts/vocab/migrate_jp_vocab.py [--dry-run]
"""
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_vocab_note as B
import compute_mastery as CM

JP_VOCAB = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "state" / "jp-vocab.json"


def _jp_mastery(e: dict) -> float:
    """跟 pdf_reader._jp_mastery 一致：用户锁 > 查询次数 > 时间衰减。"""
    um = (e.get("user_mark") or "").strip().lower()
    if um == "known" or e.get("mastered"):
        return 1.0
    if um == "unknown":
        return 0.0
    score = 0.50
    looks = int(e.get("looks", 0))
    if looks >= 5: score -= 0.25
    elif looks >= 3: score -= 0.15
    elif looks >= 2: score -= 0.05
    last = int(e.get("last_ts", 0) or 0)
    if last:
        days = (time.time() - last) / 86400.0
        if days > 90: score += 0.10
        elif days > 30: score += 0.05
    return max(0.0, min(1.0, score))


def main():
    dry = "--dry-run" in sys.argv
    store = json.loads(JP_VOCAB.read_text("utf-8")) if JP_VOCAB.exists() else {}
    print(f"jp-vocab 词数: {len(store)}  dry_run={dry}")
    created = skipped = mastered_n = 0
    for word, e in store.items():
        word = (word or "").strip()
        if not word:
            continue
        path = B._word_path(word)
        if path.exists():
            skipped += 1
            continue
        m = _jp_mastery(e)
        if dry:
            print(f"  would create {path.name}  mastery={m:.2f}")
            created += 1
            continue
        # ① 建笔记（_new_source=False → 不触发「查词重置 0」，下面再按旧数据落 mastery）
        B.update_jp_word_note(word, _new_source=False)
        # ② 落旧 store 的时间/次数信号
        if e.get("first_ts"):
            CM._set_fm_field(path, "first_seen",
                             time.strftime("%Y-%m-%d", time.localtime(int(e["first_ts"]))))
        if e.get("last_ts"):
            CM._set_fm_field(path, "last_lookup_ts", int(e["last_ts"]))
            CM._set_fm_field(path, "last_lookup",
                             time.strftime("%Y-%m-%d", time.localtime(int(e["last_ts"]))))
        CM._set_fm_field(path, "lookup_count", int(e.get("looks", 1)))
        # ③ mastery：已掌握 → user_mark known 锁 1.0；否则按算出的分数落库
        if e.get("mastered") or (e.get("user_mark") == "known"):
            CM.apply_user_mark(word, "known")
            mastered_n += 1
        else:
            lbl, slug = CM._label_for(m)
            CM._rewrite_fm(path, round(m, 3), lbl, slug)
        created += 1
    print(f"完成：新建 {created}，跳过(已存在) {skipped}，其中已掌握 {mastered_n}")


if __name__ == "__main__":
    main()
