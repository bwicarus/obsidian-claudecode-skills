from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
import kg_runtime  # noqa: E402


class KgRuntimeReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "kg"
        self.release = self._make_release("test-release", value=7)
        self.module_path = (
            self.release / "scripts" / "kg" / "concept_node_service.py"
        )
        (self.root / "current").symlink_to(
            Path("releases") / self.release.name
        )
        self.env = mock.patch.dict(
            os.environ,
            {"BW_READER_KG_RUNTIME_ROOT": str(self.root)},
        )
        self.env.start()
        self.old_sys_path = list(sys.path)
        self.old_activated = set(kg_runtime._ACTIVATED_IMPORT_PATHS)
        self.saved_modules = {
            "concept_node_service": sys.modules.get("concept_node_service"),
            "other_core": sys.modules.get("other_core"),
        }
        sys.modules.pop("concept_node_service", None)
        sys.modules.pop("other_core", None)

    def tearDown(self):
        for name, module in self.saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.path[:] = self.old_sys_path
        kg_runtime._ACTIVATED_IMPORT_PATHS = self.old_activated
        self.env.stop()
        self.temp.cleanup()

    def _make_release(
        self,
        name: str,
        *,
        value: int,
        reader_version: str = "0.2.52",
    ) -> Path:
        release = self.root / "releases" / f".stage-{name}"
        for relative in (
            "scripts/kg",
            "scripts/lib",
            "_client/core",
            "_server_deploy",
        ):
            (release / relative).mkdir(parents=True, exist_ok=True)
        (release / "scripts" / "kg" / "concept_node_service.py").write_text(
            f"VALUE = {value}\n",
            "utf-8",
        )
        (release / "scripts" / "kg" / "other_core.py").write_text(
            "VALUE = 'other'\n",
            "utf-8",
        )
        files = self._manifest_files(release)
        identity = {
            "contract": kg_runtime.RUNTIME_CONTRACT,
            "readerVersion": reader_version,
            "dataSchemaMin": kg_runtime.SUPPORTED_DATA_SCHEMA,
            "dataSchemaMax": kg_runtime.SUPPORTED_DATA_SCHEMA,
            "files": files,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        final = (
            self.root
            / "releases"
            / f"kg-{reader_version}-{digest[:20]}"
        )
        release.rename(final)
        release = final
        self._write_marker(release, reader_version=reader_version)
        return release

    @staticmethod
    def _manifest_files(release: Path) -> dict[str, str]:
        files = {}
        for path in sorted((release / "scripts" / "kg").glob("*.py")):
            relative = path.relative_to(release).as_posix()
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    def _write_marker(
        self,
        release: Path | None = None,
        *,
        reader_version: str = "0.2.52",
        mutate=None,
    ) -> dict:
        release = release or self.release
        marker = {
            "contract": kg_runtime.RUNTIME_CONTRACT,
            "deployId": release.name,
            "readerVersion": reader_version,
            "dataSchemaMin": kg_runtime.SUPPORTED_DATA_SCHEMA,
            "dataSchemaMax": kg_runtime.SUPPORTED_DATA_SCHEMA,
            "files": self._manifest_files(release),
        }
        if mutate:
            mutate(marker)
        marker["manifestDigest"] = hashlib.sha256(
            json.dumps(
                marker,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        (release / "runtime-manifest.json").write_text(
            json.dumps(marker, ensure_ascii=False),
            "utf-8",
        )
        return marker

    def _replace_current(self, release: Path) -> None:
        replacement = self.root / ".current-next"
        replacement.symlink_to(Path("releases") / release.name)
        os.replace(replacement, self.root / "current")

    def test_current_release_and_import_are_manifest_bound(self):
        pinned = kg_runtime.pin_release()
        self.assertEqual(pinned.release, self.release.resolve())
        self.assertEqual(pinned.deploy_id, self.release.name)
        self.assertEqual(pinned.reader_version, "0.2.52")
        self.assertEqual(kg_runtime.current_release(), self.release.resolve())
        self.assertEqual(
            kg_runtime.runtime_file(
                "scripts/kg/concept_node_service.py"
            ),
            self.module_path.resolve(),
        )
        module = kg_runtime.import_module(
            "concept_node_service",
            pinned=pinned,
        )
        self.assertEqual(module.VALUE, 7)
        self.assertEqual(Path(module.__file__).resolve(), self.module_path)

    def test_activate_imports_covers_complete_release_layout(self):
        pinned = kg_runtime.pin_release()
        self.assertEqual(
            pinned.activate_imports(),
            self.release / "scripts" / "kg",
        )
        expected = [
            self.release / "scripts" / "kg",
            self.release / "scripts",
            self.release / "scripts" / "lib",
            self.release / "_client" / "core",
            self.release / "_server_deploy",
        ]
        self.assertEqual(
            [Path(value) for value in sys.path[:5]],
            expected,
        )

    def test_pinned_release_survives_atomic_current_switch(self):
        pinned = kg_runtime.pin_release()
        second = self._make_release("second-release", value=9)
        self._replace_current(second)

        self.assertEqual(
            pinned.runtime_file(
                "scripts/kg/concept_node_service.py"
            ),
            self.module_path,
        )
        old_module = pinned.import_module("concept_node_service")
        self.assertEqual(old_module.VALUE, 7)

        sys.modules.pop("concept_node_service", None)
        new_pinned = kg_runtime.pin_release()
        self.assertEqual(new_pinned.release, second)
        self.assertEqual(
            new_pinned.import_module("concept_node_service").VALUE,
            9,
        )

    def test_pinned_release_rechecks_file_digest(self):
        pinned = kg_runtime.pin_release()
        self.module_path.write_text("VALUE = 8\n", "utf-8")
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "摘要不一致",
        ):
            pinned.runtime_file("scripts/kg/concept_node_service.py")

    def test_tampered_runtime_file_fails_closed(self):
        self.module_path.write_text("VALUE = 8\n", "utf-8")
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "摘要不一致",
        ):
            kg_runtime.current_release()

    def test_tampered_manifest_digest_fails_closed(self):
        marker_path = self.release / "runtime-manifest.json"
        marker = json.loads(marker_path.read_text("utf-8"))
        marker["manifestDigest"] = "0" * 64
        marker_path.write_text(json.dumps(marker), "utf-8")
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "身份或摘要无效",
        ):
            kg_runtime.current_release()

    def test_rewritten_files_and_manifest_cannot_reuse_release_name(self):
        self.module_path.write_text("VALUE = 88\n", "utf-8")
        self._write_marker()
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "内容寻址身份",
        ):
            kg_runtime.current_release()

    def test_current_must_be_direct_relative_managed_symlink(self):
        cases = (
            str(self.release),
            "releases/nested/test-release",
            "../kg/releases/test-release",
            "test-release",
        )
        for target in cases:
            with self.subTest(target=target):
                (self.root / "current").unlink()
                (self.root / "current").symlink_to(target)
                with self.assertRaisesRegex(
                    kg_runtime.KgRuntimeError,
                    "直属相对符号链接",
                ):
                    kg_runtime.current_release()

    def test_current_must_be_symlink_not_directory(self):
        (self.root / "current").unlink()
        (self.root / "current").mkdir()
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "符号链接",
        ):
            kg_runtime.current_release()

    def test_manifest_and_requested_paths_cannot_escape(self):
        with self.assertRaises(kg_runtime.KgRuntimeError):
            kg_runtime.runtime_file("../outside.py")
        self._write_marker(
            mutate=lambda marker: marker["files"].update(
                {"../outside.py": "0" * 64}
            )
        )
        with self.assertRaises(kg_runtime.KgRuntimeError):
            kg_runtime.current_release()

    def test_manifest_file_must_be_regular_not_symlink(self):
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("VALUE = 7\n", "utf-8")
        self.module_path.unlink()
        self.module_path.symlink_to(outside)
        self._write_marker()
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "禁止符号链接",
        ):
            kg_runtime.current_release()

    def test_manifest_itself_must_not_be_symlink(self):
        marker_path = self.release / "runtime-manifest.json"
        outside = Path(self.temp.name) / "outside-manifest.json"
        outside.write_bytes(marker_path.read_bytes())
        marker_path.unlink()
        marker_path.symlink_to(outside)
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "禁止符号链接",
        ):
            kg_runtime.current_release()

    def test_manifest_parent_directory_must_not_be_symlink(self):
        outside = Path(self.temp.name) / "outside-kg"
        outside.mkdir()
        (outside / "concept_node_service.py").write_text(
            "VALUE = 7\n",
            "utf-8",
        )
        (outside / "other_core.py").write_text(
            "VALUE = 'other'\n",
            "utf-8",
        )
        kg_dir = self.release / "scripts" / "kg"
        for path in kg_dir.iterdir():
            path.unlink()
        kg_dir.rmdir()
        kg_dir.symlink_to(outside, target_is_directory=True)
        self._write_marker()
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "禁止符号链接",
        ):
            kg_runtime.current_release()

    def test_manifest_identity_schema_and_reader_version_are_strict(self):
        invalid_mutations = (
            lambda marker: marker.pop("readerVersion"),
            lambda marker: marker.update({"readerVersion": "0.02.52"}),
            lambda marker: marker.update({"readerVersion": "1.2.3.4.5"}),
            lambda marker: marker.update({"readerVersion": "1.65536"}),
            lambda marker: marker.update({"readerVersion": "1." + "9" * 5000}),
            lambda marker: marker.update({"readerVersion": "０.2.52"}),
            lambda marker: marker.update({"readerVersion": "0.0"}),
            lambda marker: marker.update({"dataSchemaMin": ""}),
            lambda marker: marker.update({"dataSchemaMax": "../history"}),
            lambda marker: marker.update({"dataSchemaMin": "kg-old/1"}),
            lambda marker: marker.update({"extra": True}),
        )
        for index, mutate in enumerate(invalid_mutations):
            with self.subTest(index=index):
                self._write_marker(mutate=mutate)
                with self.assertRaises(kg_runtime.KgRuntimeError):
                    kg_runtime.current_release()
        self._write_marker()
        self.assertEqual(kg_runtime.pin_release().reader_version, "0.2.52")

    def test_import_root_directory_must_not_be_symlink(self):
        client_core = self.release / "_client" / "core"
        client_core.rmdir()
        outside = Path(self.temp.name) / "outside-client-core"
        outside.mkdir()
        client_core.symlink_to(outside, target_is_directory=True)
        pinned = kg_runtime.pin_release()
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "禁止符号链接",
        ):
            pinned.activate_imports()

    def test_manifest_rejects_duplicate_json_keys(self):
        marker_path = self.release / "runtime-manifest.json"
        marker = marker_path.read_text("utf-8")
        marker_path.write_text(
            marker[:-1] + ',"contract":"bw-reader-kg-runtime/1"}',
            "utf-8",
        )
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "重复字段",
        ):
            kg_runtime.current_release()

    def test_preloaded_worktree_module_is_rejected(self):
        fake = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(
                "concept_node_service",
                loader=None,
            )
        )
        fake.__file__ = str(
            ROOT / "scripts" / "kg" / "concept_node_service.py"
        )
        sys.modules["concept_node_service"] = fake
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "非 pinned release",
        ):
            kg_runtime.import_module("concept_node_service")

    def test_cached_module_from_previous_release_is_rejected(self):
        old_module = kg_runtime.import_module("concept_node_service")
        self.assertEqual(old_module.VALUE, 7)
        second = self._make_release("second-release", value=9)
        self._replace_current(second)
        with self.assertRaisesRegex(
            kg_runtime.KgRuntimeError,
            "非 pinned release",
        ):
            kg_runtime.import_module("concept_node_service")

    def test_unlisted_or_unsafe_module_is_rejected(self):
        pinned = kg_runtime.pin_release()
        for name in ("../concept_node_service", "json", ["other_core"]):
            with self.subTest(name=name):
                with self.assertRaises(kg_runtime.KgRuntimeError):
                    pinned.import_module(name)


if __name__ == "__main__":
    unittest.main()
