#!/usr/bin/env python3
"""concept_graph_daily.py — 概念网夜间流水线(独立于 bwicarus-daily,不碰 Anki)。

步骤:生命周期回归 gate → 概念笔记生长(科目门自守)→ 存量扫描拼边 → 边审计(≤20/晚)→ 统一图重建。
影响开关另有 note-codes.json 的 gating_enabled(默认 false=shadow:边只展示不参与解锁)。
由 concept-graph.timer 驱动;手动跑:python3 scripts/concept_graph_daily.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable or "/usr/bin/python3"


def run(name, args):
    print("\n=== %s ===" % name, flush=True)
    r = subprocess.run([PY] + args, cwd=str(ROOT))
    print("=== %s → rc=%d ===" % (name, r.returncode), flush=True)
    return r.returncode


def main():
    # gate:生命周期回归(隔离零 AI,~0.1s)挂了就别动图
    rc = run("lifecycle 回归 gate", ["-m", "unittest", "tests.test_concept_graph_lifecycle", "-q"])
    if rc != 0:
        print("✗ 回归失败,中止(不动概念网)")
        return rc
    run("概念笔记生长", [str(ROOT / "scripts/kg/propose_concept_notes.py"), "--run"])
    run("存量扫描拼边", [str(ROOT / "scripts/kg/promote_concepts.py"), "--edges", "--write"])
    run("边审计", [str(ROOT / "scripts/kg/audit_edges.py"), "--run"])
    run("统一图重建", [str(ROOT / "scripts/kg/build_unified_graph.py"), "--write"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
