"""
聚合学习数据，生成供 Web 仪表板使用的 dashboard.json。

读取来源：
  - anki/records/*.json  (status_snapshot + priority_snapshot)
  - index/*.md           (科目信息、关键词)
  - vault 笔记文件       ([[links]] → 图谱边)

输出到：
  dashboard/dashboard.json  （由每日任务 SCP 到服务器）
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).resolve().parents[1]
RECORDS_DIR = PROJECT_DIR / "anki" / "records"
INDEX_DIR   = PROJECT_DIR / "index"
VAULT_ROOT  = Path(os.environ.get("OBSIDIAN_VAULT", r"C:\obsidian"))
OUTPUT_PATH = PROJECT_DIR / "dashboard" / "dashboard.json"

# 拼音搜索：pypinyin 装在项目本地 vendor/（不污染系统 Python）。
# 装失败则降级——拼音留空，前端仍可中文模糊匹配。
sys.path.insert(0, str(PROJECT_DIR / "vendor"))
try:
    from pypinyin import lazy_pinyin, Style as _PyStyle
    def _py(s: str) -> str:
        return "".join(lazy_pinyin(s)).lower()
    def _init(s: str) -> str:
        return "".join(lazy_pinyin(s, style=_PyStyle.FIRST_LETTER)).lower()
except Exception:
    def _py(s: str) -> str: return ""
    def _init(s: str) -> str: return ""

_ENTRY_RE = re.compile(r"-\s+\[\[([^\]]+)\]\]\s+`([^`]*)`\s+—\s+(.+)")
_LINK_RE  = re.compile(r"\[\[([^\]#|]+)")
VAULT_NAME = "Obsidian Vault"


def parse_index(index_dir: Path) -> dict[str, tuple[frozenset[str], str]]:
    """返回 {note_name: (keywords, subject)}"""
    result: dict[str, tuple[frozenset[str], str]] = {}
    for f in sorted(index_dir.glob("*.md")):
        if f.name == "knowledge-index.md":
            continue
        subject = f.stem
        try:
            for m in _ENTRY_RE.finditer(f.read_text(encoding="utf-8")):
                name = m.group(1).strip()
                kws  = frozenset(k.strip() for k in m.group(2).split(",") if k.strip())
                result[name] = (kws, subject)
        except OSError:
            pass
    return result


def obsidian_url(note_name: str) -> str:
    return f"obsidian://open?vault={VAULT_NAME}&file={quote(note_name)}"


def resolve_path(record: dict) -> Path | None:
    sn = record.get("source_note", "")
    if sn:
        p = (VAULT_ROOT / sn).resolve()
        if p.exists():
            return p
    return None


def parse_links(note_path: Path) -> frozenset[str]:
    try:
        return frozenset(m.strip() for m in _LINK_RE.findall(
            note_path.read_text(encoding="utf-8")
        ))
    except OSError:
        return frozenset()


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_graph(
    names: list[str],
    index_data: dict[str, tuple[frozenset[str], str]],
    note_paths: dict[str, Path | None],
) -> tuple[list[dict], list[dict]]:
    name_set = set(names)

    # 边权重（跟 review_priority.build_adjacency 必须一致）：
    #   w = log2(1 + eff_kw) × (0.6 + 0.4 × jaccard)
    #   eff_kw = 共享关键词数；无共享但有显式链接时兜底 = 1
    # 旧公式靠显式链接数，但 /connect 去重 + 反向补全后 n_links 几乎
    # 恒为 2，w 退化成常数 ~3.10 毫无区分度。改由「共享多少关键词」
    # 主导强度，显式链接只保证「被判定相关」的边不消失。
    # link_count / kw_shared / kind 仍记，供前端 hover + 样式区分。
    kw_shared: dict[tuple[str, str], int] = {}
    jac_w:     dict[tuple[str, str], float] = {}
    fwd:       dict[tuple[str, str], int] = {}
    rev:       dict[tuple[str, str], int] = {}

    def key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a < b else (b, a)

    for i, a in enumerate(names):
        kws_a = index_data.get(a, (frozenset(), ""))[0]
        for b in names[i + 1:]:
            kws_b = index_data.get(b, (frozenset(), ""))[0]
            inter = len(kws_a & kws_b)
            if inter == 0:
                continue
            w = jaccard(kws_a, kws_b)
            if w > 0:
                k = key(a, b)
                jac_w[k] = round(w, 3)
                kw_shared[k] = inter

    for name, path in note_paths.items():
        if path is None:
            continue
        for target in parse_links(path):
            if target in name_set and target != name:
                k = key(name, target)
                if (name, target) == k:
                    fwd[k] = fwd.get(k, 0) + 1
                else:
                    rev[k] = rev.get(k, 0) + 1

    import math
    edge_list = []
    for k in set(jac_w) | set(fwd) | set(rev):
        s, t = k
        nf, nr = fwd.get(k, 0), rev.get(k, 0)
        n_links = nf + nr
        inter = kw_shared.get(k, 0)
        jac   = jac_w.get(k, 0.0)
        eff_kw = inter if inter > 0 else (1 if n_links > 0 else 0)
        w = math.log2(1 + eff_kw) * (0.6 + 0.4 * jac)
        kind = ("both" if (n_links and k in jac_w)
                else "explicit" if n_links else "kw")
        edge_list.append({
            "source": s, "target": t,
            "weight": round(w, 3),
            "link_count": n_links,
            "kw_shared": inter,
            "kind": kind,
        })

    # weighted degree：每个节点相连边 weight 之和 + 连接数。
    # 前端节点面积 ∝ wdeg（r = base + k·√wdeg，面积视觉线性）。
    wdeg: dict[str, float] = {n: 0.0 for n in names}
    deg:  dict[str, int] = {n: 0 for n in names}
    for e in edge_list:
        wdeg[e["source"]] = wdeg.get(e["source"], 0.0) + e["weight"]
        wdeg[e["target"]] = wdeg.get(e["target"], 0.0) + e["weight"]
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1

    nodes = [
        {"id": n, "wdeg": round(wdeg.get(n, 0.0), 3), "degree": deg.get(n, 0)}
        for n in names
    ]
    return nodes, edge_list


def build_keyword_cloud(
    notes_out: list[dict],
    index_data: dict[str, tuple[frozenset[str], str]],
) -> list[list]:
    """复习优先度词云。

    数据源：索引文件中手工录入的关键词（不是 Anki tags）。

    权重公式：
        weight(K) = (Σ priority over notes containing K) × (1 + IDF_normalized)

    其中 IDF_normalized = log(N_total / N_containing) / log(N_total)，
    范围 [0, 1]，作为温和的"罕见关键词加成"。罕见但出现在重要笔记中的
    关键词得到适度提升，而高频通用词不会淹没整张图。
    """
    kw_to_notes: dict[str, list[str]] = {}
    for note in notes_out:
        kws = index_data.get(note["name"], (frozenset(), ""))[0]
        for kw in kws:
            kw_to_notes.setdefault(kw, []).append(note["name"])

    if not kw_to_notes:
        return []

    note_priority = {n["name"]: n["priority"] for n in notes_out}
    n_total = len(notes_out)
    max_idf = math.log(n_total) if n_total > 1 else 1.0

    result: list[list] = []
    for kw, names in kw_to_notes.items():
        priority_sum = sum(note_priority.get(n, 0.0) for n in names)
        if priority_sum <= 0:
            continue
        n_containing = len(names)
        if n_containing >= n_total or max_idf <= 0:
            idf_norm = 0.0
        else:
            idf_norm = math.log(n_total / n_containing) / max_idf
        weight = priority_sum * (1.0 + idf_norm)
        result.append([kw, round(weight, 4), _py(kw), _init(kw)])

    return sorted(result, key=lambda x: -x[1])


def main() -> None:
    index_data = parse_index(INDEX_DIR)

    # 加载所有 records
    records: dict[str, dict] = {}
    for rf in sorted(RECORDS_DIR.glob("*.json")):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
            sn  = rec.get("source_note", "")
            if sn:
                records[Path(sn).stem] = rec
        except (json.JSONDecodeError, OSError):
            pass

    # 有卡笔记 → 主列表
    notes_out = []
    for name, rec in records.items():
        if rec.get("status") == "no_cards" or not rec.get("cards"):
            continue
        snap     = rec.get("status_snapshot", {})
        priority = rec.get("priority_snapshot", {})
        kws, subj = index_data.get(name, (frozenset(), ""))
        notes_out.append({
            "name":            name,
            "subject":         subj,
            "keywords":        sorted(kws),
            "obsidian_url":    rec.get("source_url") or obsidian_url(name),
            "anki_total":      snap.get("total",      0),
            "anki_new":        snap.get("new",         0),
            "anki_learning":   snap.get("learning",    0),
            "anki_review":     snap.get("review",      0),
            "anki_relearning": snap.get("relearning",  0),
            "retention":       round(snap.get("retention_avg", 0.0), 4),
            "mastery":         round(snap.get("mastery_avg",   0.0), 4),
            "reps_total":      snap.get("reps_total",   0),
            "lapses_total":    snap.get("lapses_total", 0),
            "priority":   round(priority.get("score",      0), 4),
            "activation": round(priority.get("activation", 0), 4),
            "importance": round(priority.get("importance", 0), 4),
            "weakness":   round(priority.get("weakness",   0), 4),
        })
    notes_out.sort(key=lambda n: n["priority"], reverse=True)

    # 图谱：包含所有有索引条目的笔记（含 no_cards）
    graph_names = list(index_data.keys())
    note_paths  = {
        name: resolve_path(records[name]) if name in records else None
        for name in graph_names
    }
    graph_nodes, graph_edges = build_graph(graph_names, index_data, note_paths)

    # 为图谱节点补充属性
    notes_by_name = {n["name"]: n for n in notes_out}
    for node in graph_nodes:
        n = notes_by_name.get(node["id"], {})
        node["subject"]      = index_data.get(node["id"], (frozenset(), ""))[1]
        node["priority"]     = n.get("priority", 0)
        node["weakness"]     = n.get("weakness", 0)
        node["retention"]    = n.get("retention", 0)
        node["mastery"]      = n.get("mastery", 0)
        node["importance"]   = n.get("importance", 0)
        node["obsidian_url"] = n.get("obsidian_url") or obsidian_url(node["id"])
        node["has_cards"]    = node["id"] in notes_by_name

    keywords = build_keyword_cloud(notes_out, index_data)

    # 读取上次自动任务的运行结果（来自 daily_anki_status.ps1）
    last_run = None
    last_run_file = PROJECT_DIR / "state" / "last_run.json"
    if last_run_file.exists():
        try:
            last_run = json.loads(last_run_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            last_run = None

    output = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes":    notes_out,
        "graph":    {"nodes": graph_nodes, "edges": graph_edges},
        "keywords": keywords,
        "last_run": last_run,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"dashboard.json 已生成 → {len(notes_out)} 条笔记 / "
        f"{len(graph_nodes)} 图节点 / {len(graph_edges)} 图边 / "
        f"{len(keywords)} 关键词"
    )


if __name__ == "__main__":
    main()
