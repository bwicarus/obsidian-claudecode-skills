"""Focused contracts for account-scoped book user-state packages."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from reader_book_user_state import (  # noqa: E402
    BaselineHeader,
    CONTRACT,
    DOMAIN_NAMES,
    DomainHeader,
    ReaderBookUserStateService,
    UserStatePackageError,
    UserStatePackageTooLarge,
    attachment_descriptor,
    build_package,
    decode_package,
    encode_package,
    package_headers,
    plan_import,
)
from reader_sidecar_store import (  # noqa: E402
    ReaderStorageIdentity,
    SidecarStore,
)


BOOK_ID = "book_" + "1" * 32
CONTENT_SHA = "2" * 64
NS_A = "acct-v1-" + "a" * 64
NS_B = "acct-v1-" + "b" * 64


def sample_domains() -> dict:
    return {
        "reading-position": {"kind": "pdf", "pos": 12, "ts": 1234},
        "highlights": {
            "pdf": [{"id": "h_1", "page": 12, "text": "重点"}],
            "epub": [],
        },
        "ink": {
            "pdf": {"12": [{"c": "#f00", "w": 2, "pts": [[0.1, 0.2]]}]},
            "epub": {},
        },
        "closed-regions": {
            "pdf": {"12": [{
                "id": "region_1",
                "t": "region",
                "pts": [[0.1, 0.2], [0.3, 0.4]],
            }]},
            "epub": {},
        },
        "notes": [{"id": "n_1", "text": "便签", "anchor": {"page": 12}}],
        "user-pages": [{"id": "u_1", "after": 12, "md": "# 解题"}],
        "card-placements": [
            {"placementId": "p_1", "entityId": "card_1", "page": 12}
        ],
        "entity-references": [{"entityId": "card_1", "kind": "card"}],
    }


def empty_domains() -> dict:
    return {
        "reading-position": None,
        "highlights": {"pdf": [], "epub": []},
        "ink": {"pdf": {}, "epub": {}},
        "closed-regions": {"pdf": {}, "epub": {}},
        "notes": [],
        "user-pages": [],
        "card-placements": [],
        "entity-references": [],
    }


def package(domains: dict | None = None, revisions: dict | None = None) -> dict:
    return build_package(
        book_id=BOOK_ID,
        content_sha256=CONTENT_SHA,
        revision=4,
        updated_at="2026-08-08T01:02:03Z",
        domains=domains or sample_domains(),
        domain_revisions=revisions or {name: 3 for name in DOMAIN_NAMES},
    )


class ReaderBookUserStateCodecTests(unittest.TestCase):
    def test_wire_bytes_are_deterministic_and_self_verifying(self):
        first = encode_package(package())
        second_domains = sample_domains()
        second_domains["reading-position"] = {
            "ts": 1234,
            "pos": 12,
            "kind": "pdf",
        }
        second = encode_package(package(second_domains))

        self.assertEqual(first, second)
        decoded = decode_package(first)
        self.assertEqual(decoded["contract"], CONTRACT)
        self.assertEqual(
            [domain["name"] for domain in decoded["domains"]],
            list(DOMAIN_NAMES),
        )
        self.assertNotIn("user_id", first.decode("utf-8"))
        self.assertNotIn("storage_namespace", first.decode("utf-8"))

    def test_tampered_payload_and_noncanonical_payload_are_rejected(self):
        value = package()
        value["domains"][0]["payloadJson"] = '{"pos":99}'
        value["domains"][0]["byteCount"] = len(
            value["domains"][0]["payloadJson"].encode()
        )
        with self.assertRaisesRegex(UserStatePackageError, "digest mismatch"):
            decode_package(json.dumps(value).encode())

        value = package()
        record = value["domains"][0]
        record["payloadJson"] = '{"pos": 12, "kind":"pdf", "ts":1234}'
        import hashlib
        raw = record["payloadJson"].encode()
        record["byteCount"] = len(raw)
        record["digest"] = hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(UserStatePackageError, "not canonical"):
            decode_package(json.dumps(value).encode())

    def test_credentials_and_absolute_local_paths_are_rejected(self):
        domains = sample_domains()
        domains["notes"] = [{"ownerToken": "do-not-export"}]
        with self.assertRaisesRegex(UserStatePackageError, "sensitive field"):
            package(domains)

        domains = sample_domains()
        domains["notes"] = [{"localPath": "C:\\Users\\private\\note.png"}]
        with self.assertRaisesRegex(UserStatePackageError, "absolute local paths"):
            package(domains)

        domains = sample_domains()
        domains["notes"] = [{"file": "file:///Users/private/note.png"}]
        with self.assertRaisesRegex(UserStatePackageError, "absolute local paths"):
            package(domains)

    def test_domain_size_is_bounded(self):
        domains = sample_domains()
        domains["reading-position"] = {"note": "x" * (65 * 1024)}
        with self.assertRaises(UserStatePackageTooLarge):
            package(domains)

    def test_wrapped_empty_domains_are_marked_empty_without_recursive_guessing(self):
        value = package(empty_domains())
        flags = {
            record["name"]: record["empty"] for record in value["domains"]
        }
        self.assertTrue(flags["highlights"])
        self.assertTrue(flags["ink"])
        self.assertTrue(flags["closed-regions"])
        self.assertTrue(all(flags.values()))

        domains = empty_domains()
        # A non-empty object inside a note is still a note record; the domain
        # is not recursively collapsed merely because some fields are empty.
        domains["notes"] = [{"text": "", "strokes": []}]
        value = package(domains)
        notes = next(x for x in value["domains"] if x["name"] == "notes")
        self.assertFalse(notes["empty"])

    def test_domain_host_shapes_are_explicit(self):
        domains = sample_domains()
        domains["highlights"] = []
        with self.assertRaisesRegex(UserStatePackageError, "pdf and epub arrays"):
            package(domains)

        domains = sample_domains()
        domains["ink"] = {"pdf": {"../../escape": []}, "epub": {}}
        with self.assertRaisesRegex(UserStatePackageError, "surface map"):
            package(domains)

        domains = sample_domains()
        domains["ink"] = {
            "pdf": {},
            "epub": {
                "pdf|资源/uploads/附录.pdf|12": [
                    {"t": "pen", "p": [[0.1, 0.2], [0.3, 0.4]]}
                ]
            },
        }
        package(domains)

        for unsafe_surface in (
            "pdf|C:/Users/private/book.pdf|1",
            "pdf|../private/book.pdf|1",
            "pdf|资源\\uploads\\附录.pdf|1",
            "pdf|资源/uploads/附录.pdf|0",
        ):
            domains = sample_domains()
            domains["ink"] = {
                "pdf": {},
                "epub": {unsafe_surface: []},
            }
            with self.assertRaisesRegex(UserStatePackageError, "surface map"):
                package(domains)

        domains = sample_domains()
        domains["closed-regions"]["pdf"]["12"][0].pop("t")
        with self.assertRaisesRegex(UserStatePackageError, "surface map"):
            package(domains)

        domains = sample_domains()
        domains["ink"]["pdf"]["12"][0]["t"] = "region"
        with self.assertRaisesRegex(UserStatePackageError, "surface map"):
            package(domains)

    def test_attachment_descriptor_has_no_server_path_or_owner(self):
        payload = encode_package(package())
        descriptor = attachment_descriptor(
            payload,
            download_url=(
                f"/pdf/api/library/user-state/{BOOK_ID}"
                f"?contentSha256={CONTENT_SHA}"
            ),
        )
        self.assertEqual(descriptor["contract"], CONTRACT)
        self.assertEqual(descriptor["mergePolicy"], "per-domain-explicit")
        self.assertNotIn("path", descriptor)
        self.assertNotIn("owner", descriptor)

        for unsafe_url in (
            f"/pdf/api/library/user-state/{BOOK_ID}?ownerToken=secret",
            f"/pdf/api/library/user-state/{BOOK_ID}?contentSha256={'f' * 64}",
            f"https://example.invalid/pdf/api/library/user-state/{BOOK_ID}"
            f"?contentSha256={CONTENT_SHA}",
        ):
            with self.assertRaisesRegex(UserStatePackageError, "downloadUrl"):
                attachment_descriptor(payload, download_url=unsafe_url)


class ReaderBookUserStatePlannerTests(unittest.TestCase):
    def test_new_empty_local_book_imports_every_nonempty_pi_domain(self):
        remote = package()
        plan = plan_import(
            package=remote,
            local_headers={},
            baseline_headers={},
            local_is_new_or_empty=True,
        )
        self.assertFalse(plan["hasConflicts"])
        self.assertTrue(all(
            decision["classification"] == "pi-newer"
            and decision["action"] == "import"
            for decision in plan["decisions"]
        ))

    def test_subsequent_sync_never_imports_both_changed_domain(self):
        remote = package()
        remote_headers = package_headers(remote)
        local = dict(remote_headers)
        baseline = {
            name: BaselineHeader(header.digest, header.revision - 1)
            for name, header in remote_headers.items()
        }
        changed = remote_headers["notes"]
        local["notes"] = DomainHeader("f" * 64, changed.revision + 1, False)
        baseline["notes"] = BaselineHeader("e" * 64, changed.revision - 1)

        plan = plan_import(
            package=remote,
            local_headers=local,
            baseline_headers=baseline,
        )
        notes = next(
            item for item in plan["decisions"] if item["name"] == "notes"
        )
        self.assertEqual(notes["classification"], "conflict")
        self.assertEqual(notes["action"], "keep")
        self.assertTrue(plan["hasConflicts"])

    def test_one_sided_changes_classify_local_and_pi_newer(self):
        remote = package()
        headers = package_headers(remote)
        local = dict(headers)
        baseline = {
            name: BaselineHeader(header.digest, header.revision)
            for name, header in headers.items()
        }
        local["ink"] = DomainHeader("d" * 64, 8, False)

        # Only local ink changed; Pi remained at the common baseline.
        first = plan_import(
            package=remote,
            local_headers=local,
            baseline_headers=baseline,
        )
        ink = next(x for x in first["decisions"] if x["name"] == "ink")
        self.assertEqual((ink["classification"], ink["action"]), (
            "local-newer", "keep"
        ))

        # Only Pi highlights changed relative to the baseline.
        baseline["highlights"] = BaselineHeader("c" * 64, 2)
        local["highlights"] = DomainHeader("c" * 64, 2, False)
        second = plan_import(
            package=remote,
            local_headers=local,
            baseline_headers=baseline,
        )
        highlights = next(
            x for x in second["decisions"] if x["name"] == "highlights"
        )
        self.assertEqual(
            (highlights["classification"], highlights["action"]),
            ("pi-newer", "import"),
        )

    def test_pi_revision_rollback_or_in_place_digest_change_never_imports(self):
        remote = package()
        headers = package_headers(remote)
        local = dict(headers)

        baseline = {
            name: BaselineHeader(header.digest, header.revision + 1)
            for name, header in headers.items()
        }
        rolled_back = plan_import(
            package=remote,
            local_headers=local,
            baseline_headers=baseline,
        )
        self.assertTrue(all(
            decision["classification"] == "conflict"
            and decision["action"] == "keep"
            for decision in rolled_back["decisions"]
        ))

        baseline = {
            name: BaselineHeader("f" * 64, header.revision)
            for name, header in headers.items()
        }
        changed_in_place = plan_import(
            package=remote,
            local_headers=local,
            baseline_headers=baseline,
        )
        self.assertTrue(all(
            decision["classification"] == "conflict"
            and decision["action"] == "keep"
            for decision in changed_in_place["decisions"]
        ))


class ReaderBookUserStateServiceTests(unittest.TestCase):
    def test_export_is_account_scoped_and_revisions_only_change_with_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            legacy.mkdir()
            store = SidecarStore(
                root / "private",
                legacy,
                authorize_claim=lambda _identity: False,
            )
            identity_a = ReaderStorageIdentity(1, NS_A)
            identity_b = ReaderStorageIdentity(2, NS_B)
            state = {
                1: sample_domains(),
                2: empty_domains(),
            }
            times = iter([
                "2026-08-08T01:00:00Z",
                "2026-08-08T01:01:00Z",
                "2026-08-08T01:02:00Z",
            ])

            service = ReaderBookUserStateService(
                store,
                lambda identity, book_reference, domain: (
                    state[identity.user_id][domain]
                    if book_reference == "private-book-ref"
                    else self.fail("unexpected private reference")
                ),
                clock=lambda: next(times),
            )
            first = service.export_bytes(
                identity=identity_a,
                book_id=BOOK_ID,
                content_sha256=CONTENT_SHA,
                book_reference="private-book-ref",
            )
            repeated = service.export_bytes(
                identity=identity_a,
                book_id=BOOK_ID,
                content_sha256=CONTENT_SHA,
                book_reference="private-book-ref",
            )
            other = service.export_bytes(
                identity=identity_b,
                book_id=BOOK_ID,
                content_sha256=CONTENT_SHA,
                book_reference="private-book-ref",
            )

            self.assertEqual(first, repeated)
            self.assertNotEqual(first, other)
            self.assertEqual(decode_package(first)["revision"], 1)
            self.assertEqual(decode_package(other)["revision"], 1)
            self.assertTrue((
                root / "private" / "by-user" / "1" / "reader-book-user-state"
            ).is_dir())
            self.assertTrue((
                root / "private" / "by-user" / "2" / "reader-book-user-state"
            ).is_dir())

            state[1]["notes"] = [{"id": "n_2", "text": "changed"}]
            changed = decode_package(service.export_bytes(
                identity=identity_a,
                book_id=BOOK_ID,
                content_sha256=CONTENT_SHA,
                book_reference="private-book-ref",
            ))
            self.assertEqual(changed["revision"], 2)
            domain_revisions = {
                item["name"]: item["revision"] for item in changed["domains"]
            }
            self.assertEqual(domain_revisions["notes"], 2)
            self.assertEqual(domain_revisions["ink"], 1)

    def test_verified_identity_is_mandatory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            legacy.mkdir()
            service = ReaderBookUserStateService(
                SidecarStore(root / "private", legacy, lambda _identity: False),
                lambda _identity, _book, _domain: None,
            )
            with self.assertRaisesRegex(UserStatePackageError, "verified"):
                service.export_package(
                    identity=None,  # type: ignore[arg-type]
                    book_id=BOOK_ID,
                    content_sha256=CONTENT_SHA,
                    book_reference="private-book-ref",
                )


if __name__ == "__main__":
    unittest.main()
