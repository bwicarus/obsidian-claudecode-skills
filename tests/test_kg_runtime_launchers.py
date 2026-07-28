from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from scripts import reader_deploy_manifest
from scripts import reader_kg_release


ROOT = Path(__file__).resolve().parents[1]


class ImmutableKgLauncherContractTest(unittest.TestCase):
    maxDiff = None

    def _publish_runtime(
        self,
        base: Path,
        *,
        corrupt_lifecycle_semantics: bool = False,
    ) -> Path:
        stage = base / "stage"
        for source_rel in reader_deploy_manifest.KG_RUNTIME_SOURCE_FILES:
            source = ROOT / source_rel
            target = stage / source_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if corrupt_lifecycle_semantics:
            target = stage / "scripts" / "kg" / "promote_concepts.py"
            content = target.read_text("utf-8")
            old = 'status = "shadow"'
            self.assertIn(old, content)
            target.write_text(
                content.replace(old, 'status = "auto"', 1),
                "utf-8",
            )
        marker = reader_kg_release.write_manifest(stage, "0.2.52")
        runtime_root = base / "runtime"
        reader_kg_release.publish(stage, runtime_root)
        return runtime_root / "releases" / marker["deployId"]

    @staticmethod
    def _mutable_project(base: Path) -> Path:
        mutable = base / "mutable-project"
        (mutable / "tests").mkdir(parents=True)
        (mutable / "scripts").mkdir()
        (mutable / "vault").mkdir()
        (mutable / "tests" / "test_concept_graph_lifecycle.py").write_text(
            "raise RuntimeError('MUTABLE_CHECKOUT_TEST_EXECUTED')\n",
            "utf-8",
        )
        (mutable / "scripts" / "attention_profile.py").write_text(
            "raise RuntimeError('MUTABLE_ATTENTION_PROFILE_EXECUTED')\n",
            "utf-8",
        )
        return mutable

    @staticmethod
    def _environment(mutable: Path) -> dict[str, str]:
        return {
            **os.environ,
            "CLAUDE_PROJECT": str(mutable),
            "OBSIDIAN_VAULT": str(mutable / "vault"),
        }

    def test_launchers_name_only_release_local_executable_code(self):
        quick = (ROOT / "scripts" / "quick_sync.py").read_text("utf-8")
        daily = (
            ROOT / "scripts" / "concept_graph_daily.py"
        ).read_text("utf-8")
        self.assertIn(
            'str(CODE_ROOT / "scripts" / "attention_profile.py")',
            quick,
        )
        self.assertNotIn(
            'str(PROJECT_DIR / "scripts" / "attention_profile.py")',
            quick,
        )
        self.assertIn(
            'CODE_ROOT / "scripts" / "kg_lifecycle_gate.py"',
            daily,
        )
        self.assertNotIn("tests.test_concept_graph_lifecycle", daily)
        self.assertIn(
            "scripts/kg_lifecycle_gate.py",
            reader_deploy_manifest.KG_RUNTIME_SOURCE_FILES,
        )

    def test_broken_mutable_checkout_cannot_change_release_gate_or_quick_path(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            release = self._publish_runtime(base / "correct")
            mutable = self._mutable_project(base)
            env = self._environment(mutable)

            gate = subprocess.run(
                [
                    sys.executable,
                    str(release / "scripts" / "concept_graph_daily.py"),
                    "--gate-only",
                ],
                cwd=mutable,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(
                gate.returncode,
                0,
                gate.stdout + gate.stderr,
            )
            self.assertIn(
                "KG lifecycle release gate: PASS",
                gate.stdout,
            )
            self.assertNotIn(
                "MUTABLE_CHECKOUT_TEST_EXECUTED",
                gate.stdout + gate.stderr,
            )

            probe = textwrap.dedent(
                f"""
                import importlib.util
                import json
                from pathlib import Path

                script = Path({str(release / "scripts" / "quick_sync.py")!r})
                spec = importlib.util.spec_from_file_location(
                    "_release_quick_sync",
                    script,
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                calls = []
                module.run = (
                    lambda name, command:
                    calls.append((name, list(command))) or 0
                )
                module.prune_kg_notes = lambda: 0
                result = module.main()
                print(json.dumps({{
                    "result": result,
                    "calls": calls,
                }}))
                """
            )
            quick = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=mutable,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(
                quick.returncode,
                0,
                quick.stdout + quick.stderr,
            )
            payload = json.loads(quick.stdout.splitlines()[-1])
            attention = [
                command
                for name, command in payload["calls"]
                if name == "attention_profile"
            ]
            self.assertEqual(len(attention), 1)
            self.assertEqual(
                Path(attention[0][1]).resolve(),
                (
                    release / "scripts" / "attention_profile.py"
                ).resolve(),
            )

    def test_semantically_broken_release_fails_closed_even_with_mutable_data(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            release = self._publish_runtime(
                base / "broken",
                corrupt_lifecycle_semantics=True,
            )
            mutable = self._mutable_project(base)
            gate = subprocess.run(
                [
                    sys.executable,
                    str(release / "scripts" / "concept_graph_daily.py"),
                    "--gate-only",
                ],
                cwd=mutable,
                env=self._environment(mutable),
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertNotEqual(
                gate.returncode,
                0,
                "损坏 release 不得通过 gate",
            )
            self.assertIn(
                "KG lifecycle release gate: FAIL",
                gate.stdout + gate.stderr,
            )
            self.assertIn(
                "中止(不动概念网)",
                gate.stdout + gate.stderr,
            )


if __name__ == "__main__":
    unittest.main()
