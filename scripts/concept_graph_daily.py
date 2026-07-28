#!/usr/bin/env python3
"""concept_graph_daily.py — 概念网夜间流水线(独立于 bwicarus-daily,不碰 Anki)。

步骤:release 内生命周期 gate → 概念笔记生长(科目门自守)→ 存量扫描拼边 →
边审计(≤20/晚)→ 统一图重建。
影响开关另有 note-codes.json 的 gating_enabled(默认 false=shadow:边只展示不参与解锁)。
由 concept-graph.timer 驱动;手动跑:python3 scripts/concept_graph_daily.py
"""
import argparse
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import config

CODE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = Path(config.PROJECT_DIR)
KG_SCRIPT_DIR = CODE_ROOT / "scripts" / "kg"
LIFECYCLE_GATE = CODE_ROOT / "scripts" / "kg_lifecycle_gate.py"
PY = sys.executable or "/usr/bin/python3"


def run(name, args):
    print("\n=== %s ===" % name, flush=True)
    r = subprocess.run([PY] + args, cwd=str(PROJECT_ROOT))
    print("=== %s → rc=%d ===" % (name, r.returncode), flush=True)
    return r.returncode


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gate-only",
        action="store_true",
        help="只运行 release 内零数据生命周期 gate，不执行夜间写入步骤",
    )
    args = parser.parse_args(argv)

    # Gate code and every module it imports are shipped in this same immutable
    # release.  Never execute PROJECT_ROOT/tests: that tree is mutable
    # production data/configuration and may be ahead of or behind the selected
    # runtime.
    rc = run("lifecycle release gate", [str(LIFECYCLE_GATE)])
    if rc != 0:
        print("✗ 回归失败,中止(不动概念网)")
        return rc
    if args.gate_only:
        return 0
    stages = [
        ("概念笔记生长", [str(KG_SCRIPT_DIR / "propose_concept_notes.py"), "--run"]),
        ("存量扫描拼边", [str(KG_SCRIPT_DIR / "promote_concepts.py"), "--edges", "--write"]),
        ("边审计", [str(KG_SCRIPT_DIR / "audit_edges.py"), "--run"]),
        ("统一图重建", [str(KG_SCRIPT_DIR / "build_unified_graph.py"), "--write"]),
    ]
    first_failure = 0
    for name, args in stages:
        stage_rc = run(name, args)
        if stage_rc != 0 and first_failure == 0:
            first_failure = stage_rc
    return first_failure


if __name__ == "__main__":
    sys.exit(main())
