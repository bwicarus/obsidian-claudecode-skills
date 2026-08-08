"""Contracts for the one-time account claim of legacy reader sidecars."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import reader_sidecar_store as sidecar_module  # noqa: E402
from reader_sidecar_store import (  # noqa: E402
    ClaimConflictError,
    IdentityMismatchError,
    LegacySnapshotError,
    ReaderStorageIdentity,
    SidecarStore,
    UnsafePathError,
    atomic_write_json,
    default_sidecar_root,
    inventory_legacy,
)


NS_A = "acct-v1-" + "a" * 64
NS_B = "acct-v1-" + "b" * 64


def _seed_legacy(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "reader-positions.json").write_text(
        json.dumps({"book.pdf": {"page": 7}}, ensure_ascii=False),
        "utf-8",
    )
    (root / "pdf-phrases.json").write_text(
        json.dumps([{"text": "private phrase"}]),
        "utf-8",
    )
    (root / "pdf-phrase-mark.json").write_text(
        json.dumps({"private phrase": "known"}),
        "utf-8",
    )
    for directory, filename, payload in (
        ("epub-highlights", "epub-a.json", [{"id": "epub"}]),
        ("reader-notes", "note-a.json", [{"text": "private note"}]),
        ("pdf-highlights", "pdf-a.json", [{"id": "pdf"}]),
        ("html-highlights", "html-a.json", [{"id": "html"}]),
        ("assets", "registry.json", {"asset-a": {"name": "private image"}}),
        ("pdf-ink", "pdf-a.json", {"pages": {"3": [{"t": "pen"}]}}),
        ("epub-ink", "epub-a.json", {"sections": {"2": [{"t": "pen"}]}}),
        ("reader-userpages", "book-a.json", [{"id": "u_1234abcd"}]),
    ):
        target = root / directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    binary = root / "assets" / "files" / "asset-a.bin"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x00private-asset\xff")


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


class ReaderSidecarIdentityTest(unittest.TestCase):
    def test_identity_is_strict_and_immutable(self) -> None:
        identity = ReaderStorageIdentity(7, NS_A)
        with self.assertRaises((AttributeError, TypeError)):
            identity.user_id = 8  # type: ignore[misc]
        for uid in (0, -1, True, "7"):
            with self.assertRaises(ValueError):
                ReaderStorageIdentity(uid, NS_A)  # type: ignore[arg-type]
        for namespace in ("", "acct-v1-short", "acct-v1-" + "A" * 64):
            with self.assertRaises(ValueError):
                ReaderStorageIdentity(7, namespace)


class ReaderSidecarClaimTest(unittest.TestCase):
    def test_copy_only_claim_preserves_source_and_records_verified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            _seed_legacy(legacy)
            source_before = _tree_bytes(legacy)
            inventory_before = inventory_legacy(legacy)
            owner = ReaderStorageIdentity(7, NS_A)
            store = SidecarStore(
                base / "private",
                legacy,
                lambda identity: identity == owner,
            )

            account_file = store.account_path(owner, "reader-positions.json")

            self.assertEqual(account_file.read_bytes(), source_before["reader-positions.json"])
            self.assertEqual(_tree_bytes(legacy), source_before)
            self.assertEqual(inventory_legacy(legacy), inventory_before)
            claim = store.read_claim()
            assert claim is not None
            self.assertEqual(claim["owner"], owner.as_dict())
            self.assertEqual(claim["source"]["inventory"], inventory_before)
            self.assertEqual(claim["source"]["digest"], claim["backup"]["digest"])
            self.assertEqual(claim["source"]["digest"], claim["account"]["digest"])
            backup = store.root / claim["backup"]["relative_path"]
            self.assertTrue((backup / "snapshot.json").is_file())
            self.assertEqual(inventory_legacy(backup / "data"), inventory_before)
            self.assertFalse(os.access(backup / "snapshot.json", os.W_OK))

    def test_only_authorized_owner_claims_and_second_account_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            _seed_legacy(legacy)
            owner = ReaderStorageIdentity(7, NS_A)
            other = ReaderStorageIdentity(8, NS_B)
            store = SidecarStore(
                base / "private",
                legacy,
                lambda identity: identity == owner,
            )

            other_old = store.account_path(other, "pdf-phrases.json")
            self.assertFalse(other_old.exists())
            self.assertIsNone(store.read_claim())

            owner_old = store.account_path(owner, "pdf-phrases.json")
            self.assertIn("private phrase", owner_old.read_text("utf-8"))
            self.assertFalse(other_old.exists())
            self.assertNotEqual(owner_old.parent, other_old.parent)

            owner_new = store.account_path(owner, "reader-notes", "new.json")
            other_new = store.account_path(other, "reader-notes", "new.json")
            atomic_write_json(owner_new, {"owner": "A"})
            atomic_write_json(other_new, {"owner": "B"})
            self.assertEqual(json.loads(owner_new.read_text("utf-8"))["owner"], "A")
            self.assertEqual(json.loads(other_new.read_text("utf-8"))["owner"], "B")
            self.assertEqual(_tree_bytes(legacy)["reader-notes/note-a.json"], b'[{"text": "private note"}]')

            with self.assertRaises(IdentityMismatchError):
                store.account_path(
                    ReaderStorageIdentity(owner.user_id, NS_B),
                    "reader-positions.json",
                )

    def test_existing_owner_claim_extends_new_fixed_datasets_copy_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            _seed_legacy(legacy)
            owner = ReaderStorageIdentity(7, NS_A)
            other = ReaderStorageIdentity(8, NS_B)
            new_names = {"pdf-ink", "epub-ink", "reader-userpages"}
            old_registry = tuple(
                item
                for item in sidecar_module.LEGACY_DATASETS
                if item[0] not in new_names
            )

            with patch.object(sidecar_module, "LEGACY_DATASETS", old_registry):
                old_store = SidecarStore(
                    base / "private",
                    legacy,
                    lambda identity: identity == owner,
                )
                old_store.account_path(owner, "reader-positions.json")
                old_claim = old_store.read_claim()
                assert old_claim is not None
                self.assertEqual(old_claim.get("extensions"), [])

            source_before = _tree_bytes(legacy)
            store = SidecarStore(
                base / "private",
                legacy,
                lambda identity: identity == owner,
            )
            # A different verified account cannot trigger or observe the old
            # owner's newly declared legacy state.
            self.assertFalse(
                store.account_path(other, "pdf-ink", "pdf-a.json").exists()
            )
            self.assertEqual(store.read_claim().get("extensions"), [])

            # Simulate a crash after one exact directory activation. Recovery
            # may continue only because the bytes match the legacy snapshot.
            owner_root = store.by_user_root / str(owner.user_id)
            shutil.copytree(legacy / "pdf-ink", owner_root / "pdf-ink")
            claimed = store.account_path(owner, "pdf-ink", "pdf-a.json")
            self.assertEqual(claimed.read_bytes(), source_before["pdf-ink/pdf-a.json"])
            self.assertEqual(
                store.account_path(owner, "epub-ink", "epub-a.json").read_bytes(),
                source_before["epub-ink/epub-a.json"],
            )
            self.assertEqual(
                store.account_path(
                    owner,
                    "reader-userpages",
                    "book-a.json",
                ).read_bytes(),
                source_before["reader-userpages/book-a.json"],
            )
            self.assertEqual(_tree_bytes(legacy), source_before)
            manifest = store.read_claim()
            assert manifest is not None
            self.assertEqual(len(manifest["extensions"]), 1)
            self.assertEqual(
                manifest["extensions"][0]["datasets"],
                ["pdf-ink", "epub-ink", "reader-userpages"],
            )
            backup = store.root / manifest["extensions"][0]["backup"]["relative_path"]
            self.assertEqual(
                inventory_legacy(backup / "data"),
                manifest["extensions"][0]["source"]["inventory"],
            )
            self.assertFalse(os.access(backup / "snapshot.json", os.W_OK))

    def test_existing_claim_extension_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            _seed_legacy(legacy)
            owner = ReaderStorageIdentity(7, NS_A)
            new_names = {"pdf-ink", "epub-ink", "reader-userpages"}
            old_registry = tuple(
                item
                for item in sidecar_module.LEGACY_DATASETS
                if item[0] not in new_names
            )
            with patch.object(sidecar_module, "LEGACY_DATASETS", old_registry):
                old_store = SidecarStore(
                    base / "private",
                    legacy,
                    lambda identity: identity == owner,
                )
                owner_root = old_store.account_path(owner)

            # This target cannot be proven to be the same legacy dataset, so
            # the extension must neither replace it nor publish a claim marker.
            atomic_write_json(
                owner_root / "pdf-ink" / "pdf-a.json",
                {"pages": {"9": [{"t": "different"}]}},
            )
            store = SidecarStore(
                base / "private",
                legacy,
                lambda identity: identity == owner,
            )
            with self.assertRaisesRegex(ClaimConflictError, "different pdf-ink"):
                store.account_path(owner, "pdf-ink", "pdf-a.json")
            manifest = store.read_claim()
            assert manifest is not None
            self.assertEqual(manifest.get("extensions"), [])
            self.assertEqual(
                json.loads((legacy / "pdf-ink" / "pdf-a.json").read_text("utf-8")),
                {"pages": {"3": [{"t": "pen"}]}},
            )

    def test_missing_manifest_recovers_activated_copy_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            _seed_legacy(legacy)
            owner = ReaderStorageIdentity(7, NS_A)
            authorize_calls: list[ReaderStorageIdentity] = []

            def authorize(identity: ReaderStorageIdentity) -> bool:
                authorize_calls.append(identity)
                return identity == owner

            store = SidecarStore(base / "private", legacy, authorize)
            first_path = store.account_path(owner, "pdf-highlights", "pdf-a.json")
            first_payload = first_path.read_bytes()
            first_claim = store.read_claim()
            assert first_claim is not None
            store.claim_path.unlink()  # Simulate crash after activation, before marker.

            recovered = SidecarStore(base / "private", legacy, authorize)
            second_path = recovered.account_path(
                owner,
                "pdf-highlights",
                "pdf-a.json",
            )
            second_claim = recovered.read_claim()
            assert second_claim is not None
            self.assertEqual(second_path.read_bytes(), first_payload)
            self.assertEqual(second_claim["claim_id"], first_claim["claim_id"])
            self.assertEqual(
                [path.name for path in recovered.backups_root.iterdir()],
                [first_claim["claim_id"]],
            )
            self.assertEqual(authorize_calls, [owner, owner])

    def test_recovery_mismatch_fails_closed_without_new_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            _seed_legacy(legacy)
            source_before = _tree_bytes(legacy)
            owner = ReaderStorageIdentity(7, NS_A)
            store = SidecarStore(base / "private", legacy, lambda _identity: True)
            account_file = store.account_path(owner, "reader-positions.json")
            store.claim_path.unlink()
            atomic_write_json(account_file, {"tampered": True})

            recovered = SidecarStore(
                base / "private",
                legacy,
                lambda _identity: True,
            )
            with self.assertRaises(ClaimConflictError):
                recovered.account_path(owner, "reader-positions.json")
            self.assertFalse(recovered.claim_path.exists())
            self.assertEqual(_tree_bytes(legacy), source_before)

    def test_activated_copy_is_not_exposed_when_recovery_is_unauthorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            _seed_legacy(legacy)
            owner = ReaderStorageIdentity(7, NS_A)
            store = SidecarStore(base / "private", legacy, lambda _identity: True)
            claimed = store.account_path(owner, "pdf-phrases.json")
            self.assertTrue(claimed.exists())
            store.claim_path.unlink()

            denied = SidecarStore(
                base / "private",
                legacy,
                lambda _identity: False,
            )
            with self.assertRaises(ClaimConflictError):
                denied.account_path(owner, "pdf-phrases.json")
            self.assertFalse(denied.claim_path.exists())

    def test_rejects_legacy_symlinks_and_account_path_traversal(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            legacy.mkdir()
            outside = base / "secret"
            outside.write_text("do not copy", "utf-8")
            assets = legacy / "assets"
            assets.mkdir()
            try:
                (assets / "link").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            owner = ReaderStorageIdentity(7, NS_A)
            store = SidecarStore(base / "private", legacy, lambda _identity: True)
            with self.assertRaises(LegacySnapshotError):
                store.account_path(owner, "assets", "registry.json")
            self.assertIsNone(store.read_claim())
            self.assertEqual(outside.read_text("utf-8"), "do not copy")

            clean_store = SidecarStore(
                base / "clean-private",
                base / "empty-legacy",
                lambda _identity: False,
            )
            for unsafe in ("../escape", "/absolute", r"windows\\escape"):
                with self.assertRaises(UnsafePathError):
                    clean_store.account_path(owner, unsafe)


class ReaderSidecarLockTest(unittest.TestCase):
    def test_dataset_lock_serializes_thread_read_modify_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            owner = ReaderStorageIdentity(7, NS_A)
            store = SidecarStore(
                base / "private",
                base / "legacy",
                lambda _identity: False,
            )
            value_path = store.account_path(owner, "counter.json")
            atomic_write_json(value_path, {"value": 0})

            def increment() -> None:
                for _index in range(15):
                    with store.lock(owner, "counter", "global"):
                        value = json.loads(value_path.read_text("utf-8"))["value"]
                        time.sleep(0.001)
                        atomic_write_json(value_path, {"value": value + 1})

            workers = [threading.Thread(target=increment) for _index in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(json.loads(value_path.read_text("utf-8"))["value"], 60)


class ReaderSidecarRootTest(unittest.TestCase):
    def test_default_root_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    default_sidecar_root(base),
                    (base / "state" / "reader-sidecars").resolve(),
                )
            with patch.dict(
                os.environ,
                {"WEBAPP_DATA": str(base / "web-data")},
                clear=True,
            ):
                self.assertEqual(
                    default_sidecar_root(base),
                    (base / "web-data" / "reader-sidecars").resolve(),
                )
            with patch.dict(
                os.environ,
                {
                    "WEBAPP_DATA": str(base / "web-data"),
                    "READER_SIDECAR_ROOT": str(base / "explicit"),
                },
                clear=True,
            ):
                self.assertEqual(
                    default_sidecar_root(base),
                    (base / "explicit").resolve(),
                )


if __name__ == "__main__":
    unittest.main()
