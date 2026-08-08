"""Account isolation for legacy ink and user-page Reader state."""

from __future__ import annotations

import hashlib
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
from reader_sidecar_store import ReaderStorageIdentity, SidecarStore  # noqa: E402


NS_A = "acct-v1-" + "a" * 64
NS_B = "acct-v1-" + "b" * 64


class ReaderAccountScopedLegacyStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.vault = base / "vault"
        self.vault.mkdir()
        self.rel = "books/private.pdf"
        book = self.vault / self.rel
        book.parent.mkdir(parents=True)
        book.write_bytes(b"%PDF-1.4\n%%EOF\n")

        self.legacy = base / "legacy"
        digest = hashlib.sha1(self.rel.encode("utf-8")).hexdigest()
        digest16 = digest[:16]
        payloads = (
            (
                self.legacy / "pdf-ink" / f"{digest}.json",
                {
                    "pdf_rel": self.rel,
                    "pages": {
                        "1": [
                            {"t": "pen", "c": "#112233", "p": [[0.1, 0.2]]},
                            {"t": "region", "id": "r_pdf", "p": [[0.2, 0.3]]},
                        ]
                    },
                },
            ),
            (
                self.legacy / "epub-ink" / f"{digest16}.json",
                {
                    "file_rel": self.rel,
                    "sections": {
                        "2": [
                            {"t": "pen", "c": "#445566", "p": [[0.3, 0.4]]},
                            {"t": "region", "id": "r_epub", "p": [[0.4, 0.5]]},
                        ]
                    },
                },
            ),
            (
                self.legacy / "reader-userpages" / f"{digest16}.json",
                [{"id": "u_1234abcd", "after": 1, "title": "private"}],
            ),
        )
        for path, value in payloads:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        self.source_before = {
            path.relative_to(self.legacy).as_posix(): path.read_bytes()
            for path in self.legacy.rglob("*")
            if path.is_file()
        }

        self.identity_a = ReaderStorageIdentity(1, NS_A)
        self.identity_b = ReaderStorageIdentity(2, NS_B)
        self.store = SidecarStore(
            base / "private",
            self.legacy,
            lambda identity: identity == self.identity_a,
        )
        self.old_store = pdf_reader._READER_SIDECAR_STORE
        self.old_root = pdf_reader._READER_SIDECAR_ROOT
        self.old_vault = pdf_reader.OBSIDIAN_ROOT
        pdf_reader._READER_SIDECAR_STORE = self.store
        pdf_reader._READER_SIDECAR_ROOT = base / "private"
        pdf_reader.OBSIDIAN_ROOT = self.vault

        self.identity = self.identity_a.as_dict()
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.extensions["reader_storage_identity_resolver"] = (
            lambda: self.identity
        )
        self.app.register_blueprint(pdf_reader.bp)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        pdf_reader._READER_SIDECAR_STORE = self.old_store
        pdf_reader._READER_SIDECAR_ROOT = self.old_root
        pdf_reader.OBSIDIAN_ROOT = self.old_vault
        self.temp.cleanup()

    def test_three_apis_use_verified_owner_and_never_write_legacy_paths(self) -> None:
        self.assertEqual(
            set(self.client.get(
                "/pdf/api/ink", query_string={"file": self.rel}
            ).get_json()["pages"]),
            {"1"},
        )
        self.assertEqual(
            set(self.client.get(
                "/pdf/api/epub-ink", query_string={"file": self.rel}
            ).get_json()["sections"]),
            {"2"},
        )
        self.assertEqual(
            self.client.get(
                "/pdf/api/userpages", query_string={"file": self.rel}
            ).get_json()["pages"][0]["title"],
            "private",
        )

        self.assertEqual(self.client.post("/pdf/api/ink", json={
            "file": self.rel,
            "page": 3,
            "strokes": [{"t": "pen", "p": [[0.5, 0.6]]}],
        }).status_code, 200)
        self.assertEqual(self.client.post("/pdf/api/epub-ink", json={
            "file": self.rel,
            "idx": 4,
            "strokes": [{"t": "pen", "p": [[0.6, 0.7]]}],
        }).status_code, 200)
        self.assertEqual(self.client.post("/pdf/api/userpages", json={
            "file": self.rel,
            "after": 2,
            "title": "owner-only",
        }).status_code, 200)

        self.identity = self.identity_b.as_dict()
        self.assertEqual(self.client.get(
            "/pdf/api/ink", query_string={"file": self.rel}
        ).get_json()["pages"], {})
        self.assertEqual(self.client.get(
            "/pdf/api/epub-ink", query_string={"file": self.rel}
        ).get_json()["sections"], {})
        self.assertEqual(self.client.get(
            "/pdf/api/userpages", query_string={"file": self.rel}
        ).get_json()["pages"], [])

        self.identity = None
        for path in ("ink", "epub-ink", "userpages"):
            response = self.client.get(
                f"/pdf/api/{path}",
                query_string={"file": self.rel},
            )
            self.assertEqual(response.status_code, 401)

        self.assertEqual(
            {
                path.relative_to(self.legacy).as_posix(): path.read_bytes()
                for path in self.legacy.rglob("*")
                if path.is_file()
            },
            self.source_before,
        )

    def test_exporter_splits_ink_regions_and_includes_user_pages(self) -> None:
        ink = pdf_reader._reader_book_user_state_domain(
            self.identity_a,
            self.rel,
            "ink",
        )
        regions = pdf_reader._reader_book_user_state_domain(
            self.identity_a,
            self.rel,
            "closed-regions",
        )
        pages = pdf_reader._reader_book_user_state_domain(
            self.identity_a,
            self.rel,
            "user-pages",
        )
        self.assertEqual(ink["pdf"]["1"][0]["t"], "pen")
        self.assertEqual(ink["epub"]["2"][0]["t"], "pen")
        self.assertEqual(regions["pdf"]["1"][0]["id"], "r_pdf")
        self.assertEqual(regions["epub"]["2"][0]["id"], "r_epub")
        self.assertEqual(pages[0]["id"], "u_1234abcd")

        self.assertEqual(
            pdf_reader._reader_book_user_state_domain(
                self.identity_b,
                self.rel,
                "ink",
            ),
            {"pdf": {}, "epub": {}},
        )
        self.assertEqual(
            pdf_reader._reader_book_user_state_domain(
                self.identity_b,
                self.rel,
                "user-pages",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
