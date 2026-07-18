"""quick_sync.py — 轻量 vault 状态同步（每 15 分钟跑）

跟 daily_anki_status.py 的重型流程互补：
- 不调 Anki / 不调 AI
- 只跑 cleanup_orphans 的索引/record 部分 + KG containing_notes prune
- 让重命名/删除场景在 15 分钟内反映到 KG + 仪表盘 + 索引

systemd timer：bwicarus-quick-sync.timer (OnCalendar=*-*-* *:00/15:00)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

PROJECT_DIR = config.PROJECT_DIR
VAULT_ROOT  = config.VAULT_ROOT
PYTHON      = sys.executable


def run(name: str, cmd: list[str]) -> int:
    print(f"\n=== {name} ===", flush=True)
    r = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    return r.returncode


def prune_kg_notes() -> int:
    """扫 knowledge_graph/*.json，删除 _note_to_covered_l2 里 vault 不存在的笔记关联，
    重建每个节点的 containing_notes。无 AI。"""
    kg_dir = PROJECT_DIR / "knowledge_graph"
    if not kg_dir.exists():
        print("  无 knowledge_graph 目录，跳过"); return 0
    total_pruned = 0
    for kg_f in sorted(kg_dir.glob("*.json")):
        if kg_f.name.endswith(".bak.json"): continue
        try:
            kg = json.loads(kg_f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        persistent = kg.get("_note_to_covered_l2") or {}
        pruned = []
        for path in list(persistent.keys()):
            if not (VAULT_ROOT / path).exists():
                del persistent[path]
                pruned.append(path)
        if not pruned:
            continue
        # 重建 containing_notes
        from collections import defaultdict as _dd
        node_to_notes = _dd(list)
        for note_rel, covered_ids in persistent.items():
            for nid in covered_ids:
                if note_rel not in node_to_notes[nid]:
                    node_to_notes[nid].append(note_rel)
        for n in kg["nodes"]:
            if n["level"] == 2:
                notes = sorted(node_to_notes.get(n["id"], []))
                n["containing_notes"] = notes
                n["note_ref"] = notes[0] if notes else ""
                n["note_ref_ai_verified"] = bool(notes)
        # L1 union
        l1_notes = _dd(set)
        for n in kg["nodes"]:
            if n["level"]==2 and n.get("containing_notes") and n.get("parent_id"):
                for nt in n["containing_notes"]:
                    l1_notes[n["parent_id"]].add(nt)
        for n in kg["nodes"]:
            if n["level"] == 1:
                nts = sorted(l1_notes.get(n["id"], set()))
                n["containing_notes"] = nts
                n["note_ref"] = nts[0] if nts else ""
                n["note_ref_ai_verified"] = bool(nts)
        kg["_note_to_covered_l2"] = persistent
        tmp = kg_f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(kg_f)
        print(f"  {kg_f.name}: prune {len(pruned)} 篇笔记关联")
        total_pruned += len(pruned)
    return total_pruned


def main() -> int:
    rc = 0
    # 1. cleanup_orphans（--no-anki 跳过 Anki suspend；重命名识别 + 索引清理 + dead link + note-states）
    rc |= run("cleanup_orphans", [
        PYTHON, str(PROJECT_DIR / "scripts" / "cleanup_orphans.py"),
        "--apply", "--no-anki",
    ])
    # 2. KG prune
    print("\n=== KG prune ===", flush=True)
    n = prune_kg_notes()
    print(f"共 prune {n} 篇笔记关联")
    # 3. 如果 KG 有改动，立刻重算 mastery（无 AI）
    if n > 0:
        kg_dir = PROJECT_DIR / "knowledge_graph"
        for kg_f in sorted(kg_dir.glob("*.json")):
            if kg_f.name.endswith(".bak.json"): continue
            rc |= run(f"link_and_mastery {kg_f.name}", [
                PYTHON, str(PROJECT_DIR / "scripts" / "kg" / "link_and_mastery.py"),
                "--kg", str(kg_f), "--in-place",
            ])
    # 4. PDF 全文搜索索引增量更新（新书/改动的书入 FTS；无变动几乎零成本，只比对 mtime）
    rc |= run("build_search_index", [
        PYTHON, str(PROJECT_DIR / "scripts" / "build_search_index.py"),
    ])
    # 5. 注意力画像（查词/高亮/问答/检查 → 事件层 → 学习焦点；幂等增量,全量重算 ~2s）
    rc |= run("attention_profile", [
        PYTHON, str(PROJECT_DIR / "scripts" / "attention_profile.py"),
    ])
    # 4. v3-D:autonote 候选检测(零 AI):评估注意力榜哪些词过门,落 autonote-candidates.json 供观察
    rc |= run("autonote 候选检测", [
        PYTHON, str(PROJECT_DIR / "scripts" / "kg" / "propose_concept_notes.py"), "--detect-only",
    ])

    return rc


if __name__ == "__main__":
    sys.exit(main())
