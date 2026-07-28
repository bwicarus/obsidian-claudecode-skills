from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

from scripts import reader_kg_release as release


class ReaderKgReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.stage = self.base / "stage"
        (self.stage / "scripts" / "kg").mkdir(parents=True)
        (self.stage / "scripts" / "kg" / "service.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        self.runtime = self.base / "runtime"

    def tearDown(self):
        self.temp.cleanup()

    def _prepare_publish(self, version="0.2.52"):
        marker = release.write_manifest(self.stage, version)
        published = release.publish(self.stage, self.runtime)
        self.assertEqual(published, marker)
        return marker

    def test_manifest_and_release_id_are_reproducible(self):
        first = release.make_manifest(self.stage, "0.2.52")
        second = release.make_manifest(self.stage, "0.2.52")
        self.assertEqual(first, second)
        self.assertRegex(
            first["deployId"],
            r"^kg-0\.2\.52-[0-9a-f]{20}$",
        )
        self.assertEqual(first["dataSchemaMin"], "kg-node-history/1")
        self.assertEqual(first["dataSchemaMax"], "kg-node-history/1")

    def test_publish_seals_exact_tree_and_is_idempotent(self):
        marker = self._prepare_publish()
        target = self.runtime / "releases" / marker["deployId"]
        release.verify_release(target, require_sealed=True)
        self.assertFalse(target.stat().st_mode & stat.S_IWUSR)
        self.assertEqual(release.publish(self.stage, self.runtime), marker)

    def test_publish_rejects_symlink_or_tamper(self):
        outside = self.base / "outside.py"
        outside.write_text("secret\n", encoding="utf-8")
        (self.stage / "scripts" / "kg" / "link.py").symlink_to(outside)
        with self.assertRaisesRegex(release.ReleaseError, "symlink"):
            release.write_manifest(self.stage, "0.2.52")
        (self.stage / "scripts" / "kg" / "link.py").unlink()
        marker = self._prepare_publish()
        target = self.runtime / "releases" / marker["deployId"]
        source = target / "scripts" / "kg" / "service.py"
        source.chmod(0o644)
        source.write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(
            release.ReleaseError,
            "(writable|digest)",
        ):
            release.verify_release(target, require_sealed=True)

    def test_existing_release_is_never_overwritten(self):
        marker = release.write_manifest(self.stage, "0.2.52")
        target = self.runtime / "releases" / marker["deployId"]
        target.mkdir(parents=True)
        (target / "runtime-manifest.json").write_text(
            json.dumps(marker),
            encoding="utf-8",
        )
        with self.assertRaises(release.ReleaseError):
            release.publish(self.stage, self.runtime)

    def test_current_switch_and_bootstrap_rollback_are_cas_guarded(self):
        marker = self._prepare_publish()
        release.switch_current(
            self.runtime,
            marker["deployId"],
            expected=None,
        )
        self.assertEqual(release.current_id(self.runtime), marker["deployId"])
        with self.assertRaisesRegex(release.ReleaseError, "concurrently"):
            release.switch_current(
                self.runtime,
                None,
                expected=None,
            )
        release.switch_current(
            self.runtime,
            None,
            expected=marker["deployId"],
        )
        self.assertIsNone(release.current_id(self.runtime))

    def test_current_rejects_absolute_nested_or_non_symlink_targets(self):
        marker = self._prepare_publish()
        current = self.runtime / "current"
        current.symlink_to(
            (self.runtime / "releases" / marker["deployId"]).resolve()
        )
        with self.assertRaisesRegex(release.ReleaseError, "relative"):
            release.current_id(self.runtime)
        current.unlink()
        current.mkdir()
        with self.assertRaisesRegex(release.ReleaseError, "not a symlink"):
            release.current_id(self.runtime)

    def test_current_rejects_noncanonical_or_redirected_parent_paths(self):
        marker = self._prepare_publish()
        current = self.runtime / "current"
        for raw in (
            f"releases//{marker['deployId']}",
            f"releases/./{marker['deployId']}",
        ):
            with self.subTest(raw=raw):
                current.symlink_to(raw)
                with self.assertRaisesRegex(
                    release.ReleaseError,
                    "direct relative",
                ):
                    release.current_id(self.runtime)
                current.unlink()

        current.symlink_to(f"releases/{marker['deployId']}")
        alias = self.base / "runtime-alias"
        alias.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaisesRegex(release.ReleaseError, "real directory"):
            release.current_id(alias)

        current.unlink()
        real_releases = self.runtime / "real-releases"
        (self.runtime / "releases").rename(real_releases)
        (self.runtime / "releases").symlink_to(
            real_releases,
            target_is_directory=True,
        )
        current.symlink_to(f"releases/{marker['deployId']}")
        with self.assertRaisesRegex(release.ReleaseError, "real directory"):
            release.current_id(self.runtime)

    def test_manifest_rejects_path_traversal_and_changed_bytes(self):
        marker = release.write_manifest(self.stage, "0.2.52")
        marker_path = self.stage / "runtime-manifest.json"
        value = json.loads(marker_path.read_text("utf-8"))
        value["files"]["../escape.py"] = "0" * 64
        value["manifestDigest"] = release.hashlib.sha256(
            release._canonical_json(
                {
                    key: item
                    for key, item in value.items()
                    if key != "manifestDigest"
                }
            )
        ).hexdigest()
        marker_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(release.ReleaseError):
            release.publish(self.stage, self.runtime)
        self.assertIn("deployId", marker)

    def test_manifest_cannot_add_fields_or_reuse_old_content_id(self):
        release.write_manifest(self.stage, "0.2.52")
        marker_path = self.stage / "runtime-manifest.json"
        value = json.loads(marker_path.read_text("utf-8"))
        value["extra"] = True
        value["manifestDigest"] = release.hashlib.sha256(
            release._canonical_json(
                {
                    key: item
                    for key, item in value.items()
                    if key != "manifestDigest"
                }
            )
        ).hexdigest()
        marker_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseError, "field set"):
            release.publish(self.stage, self.runtime)

        value.pop("extra")
        target = self.stage / "scripts" / "kg" / "service.py"
        target.write_text("VALUE = 9\n", encoding="utf-8")
        value["files"]["scripts/kg/service.py"] = release.hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
        value["manifestDigest"] = release.hashlib.sha256(
            release._canonical_json(
                {
                    key: item
                    for key, item in value.items()
                    if key != "manifestDigest"
                }
            )
        ).hexdigest()
        marker_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            release.ReleaseError,
            "content-addressed",
        ):
            release.publish(self.stage, self.runtime)


if __name__ == "__main__":
    unittest.main()
