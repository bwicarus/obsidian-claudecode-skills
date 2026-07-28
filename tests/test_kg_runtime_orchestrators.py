from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import kg_runtime_client
from scripts import reader_kg_release


ROOT = Path(__file__).resolve().parents[1]


class KgRuntimeOrchestratorContractTest(unittest.TestCase):
    def test_daily_and_register_pin_once_and_never_build_worktree_kg_paths(self):
        daily = (ROOT / "scripts" / "daily_anki_status.py").read_text("utf-8")
        register = (ROOT / "scripts" / "register_notes.py").read_text("utf-8")
        self.assertIn(
            "pinned = kg_runtime_client.pin(project_root=PROJECT_DIR)",
            daily,
        )
        self.assertIn(
            "kg_runtime = kg_runtime_client.pin("
            "project_root=config.PROJECT_DIR)",
            register,
        )
        self.assertNotIn('run_py("kg/', daily)
        self.assertNotIn(
            'config.PROJECT_DIR / "scripts" / "kg"',
            register,
        )

    def test_linux_client_uses_explicit_resolver_and_pins_current(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            stage = base / "stage"
            (stage / "scripts" / "kg").mkdir(parents=True)
            (stage / "scripts" / "kg" / "concept_node_service.py").write_text(
                "VALUE = 1\n",
                "utf-8",
            )
            # Resolver requires all import roots even when this test only asks
            # for a file path.
            for relative in (
                "scripts/lib",
                "_client/core",
                "_server_deploy",
            ):
                (stage / relative).mkdir(parents=True)
            marker = reader_kg_release.write_manifest(stage, "0.2.52")
            runtime = base / "runtime"
            reader_kg_release.publish(stage, runtime)
            reader_kg_release.switch_current(
                runtime,
                marker["deployId"],
                expected=None,
            )
            resolver = ROOT / "_server_deploy" / "kg_runtime.py"
            with mock.patch.dict(
                os.environ,
                {
                    "BW_READER_KG_RESOLVER": str(resolver),
                    "BW_READER_KG_RUNTIME_ROOT": str(runtime),
                },
            ):
                pinned = kg_runtime_client.pin(project_root=base / "data")
            self.assertEqual(pinned.deploy_id, marker["deployId"])
            self.assertEqual(
                pinned.runtime_file(
                    "scripts/kg/concept_node_service.py"
                ).read_text("utf-8"),
                "VALUE = 1\n",
            )

    def test_missing_or_symlinked_production_resolver_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            outside = base / "outside.py"
            outside.write_text("def pin_release(): return None\n", "utf-8")
            linked = base / "resolver.py"
            linked.symlink_to(outside)
            for resolver in (base / "missing.py", linked):
                with self.subTest(resolver=resolver), mock.patch.dict(
                    os.environ,
                    {"BW_READER_KG_RESOLVER": str(resolver)},
                ):
                    with self.assertRaises(
                        kg_runtime_client.KgRuntimeClientError
                    ):
                        kg_runtime_client.pin(project_root=base)


if __name__ == "__main__":
    unittest.main()
