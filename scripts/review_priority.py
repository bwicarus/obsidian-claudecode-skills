"""
知识图谱复习优先级计算脚本（PageRank + FSRS retrievability 版）。

最终评分：priority = (1 - R) × activation × importance

  R           — Anki 平均留存率（来自 anki_status 写入的 retention_avg），
                FSRS/Ebbinghaus 风格：R = 0.9^(t/interval)，越低越需要复习
  activation  — Personalized PageRank，种子为各笔记的近期编辑权重
                （指数衰减，半衰期 7 天），表示与当前学习焦点的图距离
  importance  — 标准 PageRank（均匀种子），表示节点在知识图谱中的中心性

图的边来源：
  ① 关键词 Jaccard 相似度（从科目索引解析）
  ② 显式 [[link]]（从笔记正文解析，权重 1.0）

no_cards 的笔记保留在图中参与传播，但不出现在输出排名里。

用法：
  python scripts/review_priority.py
  python scripts/review_priority.py --top 5 --subject 数学
  python scripts/review_priority.py --damping 0.85 --half-life 7
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).resolve().parents[1]
VAULT_ROOT   = Path(r"C:\obsidian")
INDEX_DIR    = PROJECT_DIR / "index"
RECORDS_DIR  = PROJECT_DIR / "anki" / "records"

# [[note-name]] または [[note-name|alias]] または [[note-name#heading]]
_LINK_RE  = re.compile(r"\[\[([^\]#|]+)")
# 索引条目: - [[note]] `kw1, kw2` — summary
_ENTRY_RE = re.compile(r"-\s+\[\[([^\]]+)\]\]\s+`([^`]*)`\s+—\s+(.+)")


# ── データ型 ──────────────────────────────────────────────────────────────────

class NoteInfo(NamedTuple):
    name:        str
    keywords:    frozenset[str]
    subject:     str
    note_path:   Path | None
    record:      dict | None
    record_path: Path | None


# ── パース ────────────────────────────────────────────────────────────────────

def parse_index(index_dir: Path) -> dict[str, tuple[frozenset[str], str]]:
    """返回 {note_name: (keywords, subject)}，从各科目索引文件解析。"""
    result: dict[str, tuple[frozenset[str], str]] = {}
    for f in sorted(index_dir.glob("*.md")):
        if f.name == "knowledge-index.md":
            continue
        subject = f.stem
        try:
            content = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _ENTRY_RE.finditer(content):
            name = m.group(1).strip()
            kws  = frozenset(k.strip() for k in m.group(2).split(",") if k.strip())
            result[name] = (kws, subject)
    return result


def load_records(records_dir: Path) -> dict[str, tuple[dict, Path]]:
    """返回 {note_name: (record, record_path)}，从 anki/records/*.json 加载。"""
    result: dict[str, tuple[dict, Path]] = {}
    for rf in sorted(records_dir.glob("*.json")):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
            sn  = rec.get("source_note", "")
            if sn:
                result[Path(sn).stem] = (rec, rf)
        except (json.JSONDecodeError, OSError):
            pass
    return result


def resolve_note_path(name: str, record: dict | None, vault_root: Path) -> Path | None:
    if record:
        sn = record.get("source_note", "")
        if sn:
            p = (vault_root / sn).resolve()
            if p.exists():
                return p
    p = (vault_root / f"{name}.md").resolve()
    return p if p.exists() else None


def parse_links(note_path: Path) -> frozenset[str]:
    try:
        return frozenset(_LINK_RE.findall(note_path.read_text(encoding="utf-8")))
    except OSError:
        return frozenset()


# ── スコアリング ───────────────────────────────────────────────────────────────

def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def mtime_seed(note_path: Path | None, half_life_days: float = 7.0) -> float:
    """笔记最近编辑时间 → 指数衰减权重（半衰期 half_life_days 天）。"""
    if note_path is None:
        return 0.0
    try:
        days = (time.time() - note_path.stat().st_mtime) / 86400
    except OSError:
        return 0.0
    return 0.5 ** (max(days, 0.0) / half_life_days)


def weakness_score(record: dict | None) -> float:
    """1 - retention_avg（FSRS 留存率）。

    新版 anki_status 写入 status_snapshot.retention_avg。
    旧 record 无该字段时退化为 (relearning + new) / total。
    """
    if record is None or record.get("status") == "no_cards":
        return 0.0
    snap = record.get("status_snapshot")
    if not snap:
        return 0.5

    if "retention_avg" in snap:
        return max(0.0, min(1.0, 1.0 - snap["retention_avg"]))

    total = snap.get("total", 0)
    if total == 0:
        return 0.5
    return (snap.get("relearning", 0) + snap.get("new", 0)) / total


def has_cards(record: dict | None) -> bool:
    if record is None or record.get("status") == "no_cards":
        return False
    return bool(record.get("cards"))


# ── グラフ & アクティベーション ───────────────────────────────────────────────

def build_adjacency(notes: list[NoteInfo]) -> dict[str, dict[str, float]]:
    """构建邻接表 {name: {neighbor: max_weight}}。"""
    name_set = {n.name for n in notes}
    adj: dict[str, dict[str, float]] = {n.name: {} for n in notes}

    def add_edge(a: str, b: str, w: float) -> None:
        adj[a][b] = max(adj[a].get(b, 0.0), w)
        adj[b][a] = max(adj[b].get(a, 0.0), w)

    # ① 关键词 Jaccard 边
    for i, a in enumerate(notes):
        for b in notes[i + 1:]:
            w = jaccard(a.keywords, b.keywords)
            if w > 0:
                add_edge(a.name, b.name, w)

    # ② 显式 [[link]] 边（权重 1.0）
    for note in notes:
        if note.note_path is None:
            continue
        for target in parse_links(note.note_path):
            target = target.strip()
            if target in name_set and target != note.name:
                add_edge(note.name, target, 1.0)

    return adj


def personalized_pagerank(
    adj:      dict[str, dict[str, float]],
    seeds:    dict[str, float],
    damping:  float = 0.85,
    max_iter: int   = 100,
    tol:      float = 1e-6,
) -> dict[str, float]:
    """Personalized PageRank（带重启的随机游走）。

    seeds 全 0 时退化为均匀种子（标准 PageRank，给出图中心性）。
    返回稳定分布（和为 1）。
    """
    nodes = list(adj.keys())
    n = len(nodes)
    if n == 0:
        return {}

    s_total = sum(seeds.values())
    if s_total <= 0:
        p = {k: 1.0 / n for k in nodes}
    else:
        p = {k: seeds.get(k, 0.0) / s_total for k in nodes}

    out_weight = {k: sum(adj[k].values()) for k in nodes}
    r = dict(p)

    for _ in range(max_iter):
        new_r = {k: (1 - damping) * p[k] for k in nodes}
        for k in nodes:
            if out_weight[k] == 0:
                share = damping * r[k] / n
                for nb in nodes:
                    new_r[nb] += share
            else:
                contrib = damping * r[k] / out_weight[k]
                for nb, w in adj[k].items():
                    new_r[nb] += contrib * w
        diff = sum(abs(new_r[k] - r[k]) for k in nodes)
        r = new_r
        if diff < tol:
            break
    return r


def _normalize_max(d: dict[str, float]) -> dict[str, float]:
    """归一化到 [0, 1]，最大值映射为 1。"""
    if not d:
        return {}
    max_val = max(d.values())
    if max_val <= 0:
        return {k: 0.0 for k in d}
    return {k: v / max_val for k, v in d.items()}


def compute_activation(
    notes:          list[NoteInfo],
    adj:            dict[str, dict[str, float]],
    half_life_days: float,
    damping:        float,
) -> dict[str, float]:
    """近期学习焦点的图传播：mtime 衰减种子 → PPR → 归一化到 max=1。"""
    seeds = {n.name: mtime_seed(n.note_path, half_life_days) for n in notes}
    raw = personalized_pagerank(adj, seeds, damping=damping)
    return _normalize_max(raw)


def compute_importance(
    adj:     dict[str, dict[str, float]],
    damping: float,
) -> dict[str, float]:
    """图中心性：均匀种子的 PageRank → 归一化到 max=1。"""
    raw = personalized_pagerank(adj, {}, damping=damping)
    return _normalize_max(raw)


# ── Frontmatter 写入 ─────────────────────────────────────────────────────────

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_PRIORITY_FM_KEYS = (
    "review_priority", "review_activation", "review_importance",
    "review_weakness", "review_date",
)


def _set_frontmatter_keys(content: str, kv: dict[str, Any]) -> str:
    m = _FM_RE.match(content)
    if m:
        fm = m.group(1)
        for key, value in kv.items():
            pat  = re.compile(rf"^{re.escape(key)}\s*:.*$", re.MULTILINE)
            line = f"{key}: {value}"
            fm   = pat.sub(line, fm) if pat.search(fm) else fm.rstrip("\n") + f"\n{line}"
        return f"---\n{fm}\n---\n" + content[m.end():]
    else:
        block = "\n".join(f"{k}: {v}" for k, v in kv.items())
        return f"---\n{block}\n---\n\n" + content


def write_priority_record(
    record_path: Path,
    score: float,
    activation: float,
    importance: float,
    weakness: float,
    dry_run: bool,
) -> None:
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARN 读取 record 失败：{e}", file=sys.stderr)
        return
    snapshot = {
        "computed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "score":       round(score, 4),
        "activation":  round(activation, 4),
        "importance":  round(importance, 4),
        "weakness":    round(weakness, 4),
        "formula":     "(1-R) * activation * importance",
    }
    record["priority_snapshot"] = snapshot
    if dry_run:
        print(f"  [dry-run] {record_path.name}: priority_snapshot = {json.dumps(snapshot, ensure_ascii=False)}")
    else:
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"  record 已写入：{record_path.name}")


def write_priority_frontmatter(
    note_path: Path,
    score: float,
    activation: float,
    importance: float,
    weakness: float,
    dry_run: bool,
) -> None:
    kv: dict[str, Any] = {
        "review_priority":   round(score, 3),
        "review_activation": round(activation, 2),
        "review_importance": round(importance, 2),
        "review_weakness":   round(weakness, 2),
        "review_date":       dt.date.today().isoformat(),
    }
    try:
        content = note_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"  WARN 读取失败：{e}", file=sys.stderr)
        return
    new_content = _set_frontmatter_keys(content, kv)
    if dry_run:
        m = _FM_RE.match(new_content)
        preview = f"---\n{m.group(1)}\n---" if m else new_content[:200]
        print(f"  [dry-run] {note_path.name}:\n{preview}")
    else:
        note_path.write_text(new_content, encoding="utf-8")
        print(f"  frontmatter 已写入：{note_path.name}")


# ── 出力 ──────────────────────────────────────────────────────────────────────

def fmt_snap(record: dict | None) -> str:
    snap = (record or {}).get("status_snapshot", {})
    if not snap:
        return "无状态"
    return (
        f"新 {snap.get('new',0)}"
        f" 学 {snap.get('learning',0)}"
        f" 复 {snap.get('review',0)}"
        f" 重学 {snap.get('relearning',0)}"
    )


# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="知识图谱复习优先级（PageRank + FSRS）")
    parser.add_argument("--top",       type=int,   default=10,  help="显示前 N 条（默认 10）")
    parser.add_argument("--subject",               default=None, help="只显示指定科目（如 数学）")
    parser.add_argument("--damping",   type=float, default=0.85, help="PageRank 衰减系数（默认 0.85）")
    parser.add_argument("--half-life", type=float, default=7.0,  help="mtime 半衰期天数（默认 7）")
    parser.add_argument("--vault-root",        type=Path, default=VAULT_ROOT)
    parser.add_argument("--write-frontmatter", action="store_true",
                        help="将分数写入所有有卡笔记的 frontmatter")
    parser.add_argument("--write-record",      action="store_true",
                        help="将分数写入对应 anki/records/*.json")
    parser.add_argument("--dry-run",           action="store_true",
                        help="只预览写入内容，不实际修改文件")
    args = parser.parse_args()

    index_data = parse_index(INDEX_DIR)
    records    = load_records(RECORDS_DIR)
    all_names  = set(index_data) | set(records)

    notes: list[NoteInfo] = []
    for name in sorted(all_names):
        kws, subject         = index_data.get(name, (frozenset(), ""))
        rec_tuple            = records.get(name)
        record, record_path  = rec_tuple if rec_tuple else (None, None)
        note_path            = resolve_note_path(name, record, args.vault_root)
        notes.append(NoteInfo(name, kws, subject, note_path, record, record_path))

    if not notes:
        print("未找到笔记。")
        sys.exit(0)

    adj        = build_adjacency(notes)
    activation = compute_activation(notes, adj, args.half_life, args.damping)
    importance = compute_importance(adj, args.damping)

    # 全量评分（所有有卡笔记，不受 --subject 和 --top 限制）
    # priority = (1 - R) × activation × importance
    all_scored: list[tuple[float, float, float, float, NoteInfo]] = []
    for note in notes:
        if not has_cards(note.record):
            continue
        act   = activation[note.name]
        imp   = importance[note.name]
        wk    = weakness_score(note.record)
        score = wk * act * imp
        all_scored.append((score, act, imp, wk, note))

    # 写入（全量，不受过滤影响）
    if args.write_frontmatter or args.write_record:
        label = " + ".join(filter(None, [
            "frontmatter" if args.write_frontmatter else "",
            "record"      if args.write_record      else "",
        ]))
        print(f"写入 {label}...")
        for score, act, imp, wk, note in all_scored:
            if args.write_frontmatter:
                if note.note_path is None:
                    print(f"  SKIP frontmatter（找不到文件）：{note.name}", file=sys.stderr)
                else:
                    write_priority_frontmatter(note.note_path, score, act, imp, wk, args.dry_run)
            if args.write_record:
                if note.record_path is None:
                    print(f"  SKIP record（无 record 文件）：{note.name}", file=sys.stderr)
                else:
                    write_priority_record(
                        note.record_path, score, act, imp, wk, args.dry_run,
                    )

    # 过滤显示（受 --subject 限制）
    scored = [
        (score, act, imp, wk, note)
        for score, act, imp, wk, note in all_scored
        if not args.subject or note.subject == args.subject
    ]
    scored.sort(reverse=True)
    top = scored[:args.top]

    # 图统计
    edge_count     = sum(len(v) for v in adj.values()) // 2
    explicit       = sum(
        1 for note in notes
        for w in adj[note.name].values()
        if w == 1.0
    ) // 2
    jaccard_only   = edge_count - explicit

    today = dt.date.today().isoformat()
    print(f"\n今日复习优先列表  {today}  公式: (1-R) × activation × importance")
    print("─" * 80)
    if not top:
        print("  （没有符合条件的笔记）")
    for rank, (score, act, imp, wk, note) in enumerate(top, 1):
        subj = f"[{note.subject}] " if note.subject else ""
        print(
            f" #{rank:<2} {subj}{note.name:<22} {score:.3f}"
            f"  激活 {act:.2f}  中心 {imp:.2f}  薄弱 {wk:.2f}"
            f"  {fmt_snap(note.record)}"
        )
    print("─" * 80)
    print(
        f"图: {len(notes)} 节点  "
        f"{edge_count} 条边（{jaccard_only} Jaccard + {explicit} 显式链接）\n"
    )


if __name__ == "__main__":
    main()
