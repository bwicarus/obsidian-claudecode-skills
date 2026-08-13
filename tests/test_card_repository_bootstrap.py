"""Contracts for the account-scoped legacy Reader card bootstrap route."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

if sys.platform == "win32" and "fcntl" not in sys.modules:
    fcntl_stub = types.ModuleType("fcntl")
    fcntl_stub.LOCK_EX = 1
    fcntl_stub.LOCK_SH = 2
    fcntl_stub.LOCK_NB = 4
    fcntl_stub.LOCK_UN = 8
    fcntl_stub.flock = lambda *_args, **_kwargs: None
    sys.modules["fcntl"] = fcntl_stub

import pdf_reader  # noqa: E402
from reader_sidecar_store import (  # noqa: E402
    ReaderStorageIdentity,
    SidecarStore,
    atomic_write_json,
)


NS_A = "acct-v1-" + "a" * 64
NS_B = "acct-v1-" + "b" * 64


def card(front: str, *, source_ref="", requirement="", meta=None):
    value = {
        "kind": "cards",
        "url": "https://asset-transport.invalid/private",
        "local": "private-cache.bin",
        "content_type": "application/octet-stream",
        "data": [{"type": "basic", "front": front, "back": "A"}],
        "states": {"0": {"_st": "learn", "_next": "2030-01-02T03:04:05Z"}},
        "source_ref": source_ref,
        "req": requirement,
        "ts": 123,
    }
    value.update(meta or {})
    return value


class CardRepositoryBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.identity_a = ReaderStorageIdentity(11, NS_A)
        self.identity_b = ReaderStorageIdentity(22, NS_B)
        self.store = SidecarStore(
            base / "private",
            base / "legacy",
            lambda _identity: False,
        )
        self.previous_store = pdf_reader._READER_SIDECAR_STORE
        self.previous_root = pdf_reader._READER_SIDECAR_ROOT
        pdf_reader._READER_SIDECAR_STORE = self.store
        pdf_reader._READER_SIDECAR_ROOT = base / "private"

        self.identity = self.identity_a.as_dict()
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.app.extensions["reader_storage_identity_resolver"] = (
            lambda: self.identity
        )
        self.app.register_blueprint(pdf_reader.bp)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        pdf_reader._READER_SIDECAR_STORE = self.previous_store
        pdf_reader._READER_SIDECAR_ROOT = self.previous_root
        self.temp.cleanup()

    def registry_path(self, identity):
        return self.store.account_path(identity, "assets", "registry.json")

    def write_registry(self, identity, value):
        atomic_write_json(self.registry_path(identity), value, indent=1)

    def test_account_isolation_and_non_card_entries_are_excluded(self) -> None:
        self.write_registry(self.identity_a, {
            "card_aaaa": card("A-only"),
            "img_abcdef": {"kind": "img", "url": "https://example.invalid/a"},
        })
        self.write_registry(self.identity_b, {
            "card_bbbb": card("B-only"),
        })

        response_a = self.client.get("/pdf/api/card-repository/bootstrap")
        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response_a.get_json()["items"]],
            ["card_aaaa"],
        )

        self.identity = self.identity_b.as_dict()
        response_b = self.client.get("/pdf/api/card-repository/bootstrap")
        self.assertEqual(response_b.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response_b.get_json()["items"]],
            ["card_bbbb"],
        )

        self.identity = None
        unauthorized = self.client.get("/pdf/api/card-repository/bootstrap")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.get_json()["code"], "authentication_required")

    def test_stable_sorted_pagination_and_full_snapshot_digest(self) -> None:
        registry = {
            "card_cccc": card("C"),
            "card_aaaa": card("A"),
            "card_bbbb": card("B"),
        }
        self.write_registry(self.identity_a, registry)
        first = self.client.get(
            "/pdf/api/card-repository/bootstrap",
            query_string={"limit": "1"},
        ).get_json()
        second = self.client.get(
            "/pdf/api/card-repository/bootstrap",
            query_string={"limit": "1", "cursor": first["nextCursor"]},
        ).get_json()
        third = self.client.get(
            "/pdf/api/card-repository/bootstrap",
            query_string={"limit": "1", "cursor": second["nextCursor"]},
        ).get_json()

        self.assertEqual(
            [first["items"][0]["id"], second["items"][0]["id"], third["items"][0]["id"]],
            ["card_aaaa", "card_bbbb", "card_cccc"],
        )
        self.assertEqual(first["snapshotDigest"], second["snapshotDigest"])
        self.assertEqual(second["snapshotDigest"], third["snapshotDigest"])
        self.assertRegex(first["snapshotDigest"], r"^sha256:[a-f0-9]{64}$")
        self.assertFalse(first["complete"])
        self.assertFalse(second["complete"])
        self.assertTrue(third["complete"])
        self.assertIsNone(third["nextCursor"])

        reordered = {key: registry[key] for key in reversed(tuple(registry))}
        self.write_registry(self.identity_a, reordered)
        repeated = self.client.get(
            "/pdf/api/card-repository/bootstrap",
            query_string={"limit": "1"},
        ).get_json()
        self.assertEqual(repeated["snapshotDigest"], first["snapshotDigest"])
        self.assertEqual(repeated["nextCursor"], first["nextCursor"])

    def test_cursor_rejects_a_changed_snapshot_instead_of_mixing_pages(self) -> None:
        registry = {
            "card_aaaa": card("A"),
            "card_bbbb": card("B"),
        }
        self.write_registry(self.identity_a, registry)
        first = self.client.get(
            "/pdf/api/card-repository/bootstrap",
            query_string={"limit": "1"},
        ).get_json()
        registry["card_cccc"] = card("C")
        self.write_registry(self.identity_a, registry)

        stale = self.client.get(
            "/pdf/api/card-repository/bootstrap",
            query_string={"limit": "1", "cursor": first["nextCursor"]},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["code"], "snapshot_changed")

    def test_card_fields_are_lossless_and_asset_transport_meta_is_removed(self) -> None:
        entry = card(
            "问题",
            source_ref="book:books/a.pdf#p2",
            requirement="只问一个事实",
            meta={
                "src": "legacy-source",
                "custom": {
                    "tags": ["physics", "jp"],
                    "nested": {"keep": True, "url": "https://drop.invalid"},
                },
                "token": "must-not-leak",
            },
        )
        self.write_registry(self.identity_a, {"card_abcdef": entry})
        path = self.registry_path(self.identity_a)
        before = path.read_bytes()

        response = self.client.get("/pdf/api/card-repository/bootstrap")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(response.data),
            pdf_reader._CARD_BOOTSTRAP_MAX_RESPONSE_BYTES,
        )
        item = response.get_json()["items"][0]
        self.assertEqual(item["cards"], entry["data"])
        self.assertEqual(item["states"], entry["states"])
        self.assertEqual(item["source_ref"], entry["source_ref"])
        self.assertEqual(item["req"], entry["req"])
        self.assertEqual(item["meta"]["src"], "legacy-source")
        self.assertEqual(item["meta"]["ts"], 123)
        self.assertEqual(item["meta"]["custom"]["tags"], ["physics", "jp"])
        self.assertEqual(item["meta"]["custom"]["nested"], {"keep": True})
        for forbidden in ("url", "local", "content_type", "token"):
            self.assertNotIn(forbidden, item["meta"])
        self.assertEqual(path.read_bytes(), before, "bootstrap must stay read-only")

    def test_malicious_or_ambiguous_query_parameters_fail_closed(self) -> None:
        self.write_registry(self.identity_a, {"card_aaaa": card("A")})
        cases = [
            [("limit", "0")],
            [("limit", "-1")],
            [("limit", "101")],
            [("limit", "01")],
            [("limit", "nope")],
            [("cursor", "not+a+cursor")],
            [("unexpected", "1")],
            [("limit", "1"), ("limit", "2")],
        ]
        for query in cases:
            with self.subTest(query=query):
                response = self.client.get(
                    "/pdf/api/card-repository/bootstrap",
                    query_string=query,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["code"], "bad_request")

    def test_one_oversized_record_is_rejected_without_truncation(self) -> None:
        oversized = card("x" * (pdf_reader._CARD_BOOTSTRAP_MAX_RESPONSE_BYTES + 1))
        self.write_registry(self.identity_a, {"card_aaaa": oversized})
        response = self.client.get("/pdf/api/card-repository/bootstrap")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["code"], "card_record_too_large")


if __name__ == "__main__":
    unittest.main()
