"""Service-level contracts for the one-time reader-sidecar account claim.

The pure ``SidecarStore`` unit tests cover crash recovery and filesystem edge
cases.  This test deliberately imports the real Flask application in a fresh
process and exercises its authenticated HTTP routes, so an endpoint that
accidentally bypasses the account store (or leaks a request owner) is visible.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReaderSidecarServiceIntegrationTest(unittest.TestCase):
    def test_claim_is_copy_only_and_http_routes_are_account_isolated(self) -> None:
        script = textwrap.dedent(
            """
            import hashlib
            import json
            import os
            from pathlib import Path
            import sys

            root = Path(os.environ["REPO_ROOT"])
            project = Path(os.environ["CLAUDE_PROJECT"])
            legacy = project / "state"
            vault = Path(os.environ["OBSIDIAN_VAULT"])
            sidecars = Path(os.environ["READER_SIDECAR_ROOT"])
            rel = "book.pdf"

            sys.path[:0] = [
                str(root / "_server_deploy"),
                str(root / "scripts"),
            ]

            def write_json(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    "utf-8",
                )

            def tree_bytes(path):
                return {
                    item.relative_to(path).as_posix(): item.read_bytes()
                    for item in sorted(path.rglob("*"))
                    if item.is_file() and not item.is_symlink()
                }

            legacy.mkdir(parents=True, exist_ok=True)
            vault.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_bytes(b"service-integration-placeholder-pdf")

            note_name = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16] + ".json"
            html_highlight_name = note_name
            highlight_name = hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".json"
            write_json(
                legacy / "reader-positions.json",
                {rel: {"kind": "pdf", "pos": 7, "ts": 1}},
            )
            write_json(
                legacy / "pdf-phrases.json",
                {"phrases": ["legacyprivatephrase"]},
            )
            write_json(
                legacy / "pdf-phrase-mark.json",
                {"marks": {"legacyprivatephrase": True}},
            )
            write_json(
                legacy / "reader-notes" / note_name,
                [{
                    "id": "nlegacyowner",
                    "anchor": {"kind": "pdf", "page": 1, "x": 0.2, "y": 0.3},
                    "text": "legacy owner note",
                }],
            )
            write_json(
                legacy / "pdf-highlights" / highlight_name,
                {
                    "pdf_rel": rel,
                    "highlights": [{
                        "id": "h_legacy_owner",
                        "page": 1,
                        "rects": [[1, 2, 3, 4]],
                        "color": "#fff59d",
                        "text": "legacy owner highlight",
                        "time": 1,
                    }],
                },
            )
            write_json(
                legacy / "html-highlights" / html_highlight_name,
                [{
                    "id": "hlegacyhtml",
                    "start": 1,
                    "end": 8,
                    "text": "legacy owner html highlight",
                    "color": "#fff59d",
                    "time": 1,
                }],
            )
            write_json(
                legacy / "assets" / "registry.json",
                {
                    "img_abcdef": {
                        "kind": "img",
                        "url": "https://example.invalid/private.png",
                        "local": "",
                        "concept": "legacy owner asset",
                    },
                },
            )
            asset_bytes = b"\\x00legacy-private-asset\\xff"
            asset_file = legacy / "assets" / "files" / "img_abcdef.bin"
            asset_file.parent.mkdir(parents=True, exist_ok=True)
            asset_file.write_bytes(asset_bytes)

            from reader_sidecar_store import inventory_digest, inventory_legacy

            source_before = tree_bytes(legacy)
            inventory_before = inventory_legacy(legacy)
            digest_before = inventory_digest(inventory_before)

            import app as module

            with module.app.app_context():
                db = module.get_db()
                owner_row = db.execute(
                    "SELECT id, username, storage_namespace FROM users"
                ).fetchone()
                assert owner_row is not None
                owner_id = int(owner_row["id"])
                owner_name = str(owner_row["username"])
                owner_namespace = str(owner_row["storage_namespace"])
                assert db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1

            client = module.app.test_client()

            def login(user_id, username):
                with client.session_transaction() as browser_session:
                    browser_session.clear()
                    browser_session["user_id"] = int(user_id)
                    browser_session["username"] = str(username)

            def get_json(path, expected_status=200):
                response = client.get(path)
                assert response.status_code == expected_status, (
                    path,
                    response.status_code,
                    response.get_data(as_text=True),
                )
                return response.get_json()

            def post_json(path, payload, expected_status=200):
                response = client.post(path, json=payload)
                assert response.status_code == expected_status, (
                    path,
                    response.status_code,
                    response.get_data(as_text=True),
                )
                return response.get_json()

            login(owner_id, owner_name)

            # The first real account-scoped route is the cut-over trigger.
            owner_positions = get_json("/pdf/api/reading-pos")
            assert owner_positions["positions"][rel]["pos"] == 7

            # Claiming is copy-only: source, verified backup, and activated
            # account snapshot all have the exact pre-claim inventory/digest.
            assert tree_bytes(legacy) == source_before
            claim_path = sidecars / "legacy-claim.json"
            assert claim_path.is_file()
            claim = json.loads(claim_path.read_text("utf-8"))
            assert claim["owner"] == {
                "user_id": owner_id,
                "storage_namespace": owner_namespace,
            }
            assert claim["source"]["root"] == str(legacy.resolve())
            for section in ("source", "backup", "account"):
                assert claim[section]["inventory"] == inventory_before
                assert claim[section]["digest"] == digest_before

            backup_root = sidecars / claim["backup"]["relative_path"]
            account_root = sidecars / claim["account"]["relative_path"]
            assert (backup_root / "snapshot.json").is_file()
            assert inventory_legacy(backup_root / "data") == inventory_before
            assert inventory_legacy(account_root) == inventory_before
            for relative, original in source_before.items():
                assert (backup_root / "data" / relative).read_bytes() == original
                assert (account_root / relative).read_bytes() == original

            # Legacy data must be visible through each migrated service route.
            assert get_json("/pdf/api/phrases")["phrases"] == [
                "legacyprivatephrase"
            ]
            assert get_json(
                "/pdf/api/notes?file=book.pdf"
            )["notes"][0]["text"] == "legacy owner note"
            assert get_json(
                "/pdf/api/highlights?file=book.pdf"
            )["highlights"][0]["text"] == "legacy owner highlight"
            assert get_json(
                "/pdf/api/html-highlights?file=book.pdf"
            )["highlights"][0]["text"] == "legacy owner html highlight"
            assert get_json(
                "/pdf/api/entity/img_abcdef"
            )["id"] == "img_abcdef"

            # A second account is created only after the immutable owner claim.
            with module.app.app_context():
                db = module.get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("second-sidecar-user", "x", "user"),
                )
                db.commit()
                other_id = int(db.execute(
                    "SELECT id FROM users WHERE username=?",
                    ("second-sidecar-user",),
                ).fetchone()["id"])
                other_namespace = module._reader_storage_namespace(other_id)

            login(other_id, "second-sidecar-user")

            # Exercise A -> B on the same Flask worker thread.  A ContextVar
            # left bound after A's request would leak every dataset here.
            assert get_json("/pdf/api/reading-pos")["positions"] == {}
            assert get_json("/pdf/api/phrases")["phrases"] == []
            assert get_json("/pdf/api/notes?file=book.pdf")["notes"] == []
            assert get_json(
                "/pdf/api/highlights?file=book.pdf"
            )["highlights"] == []
            assert get_json(
                "/pdf/api/html-highlights?file=book.pdf"
            )["highlights"] == []
            assert get_json(
                "/pdf/api/entity/img_abcdef",
                expected_status=404,
            )["ok"] is False

            post_json(
                "/pdf/api/reading-pos",
                {"file": rel, "kind": "pdf", "pos": 13},
            )
            post_json(
                "/pdf/api/phrases",
                {"text": "secondprivatephrase"},
            )
            other_note = post_json(
                "/pdf/api/notes",
                {
                    "file": rel,
                    "id": "c_22222222",
                    "anchor": {
                        "kind": "pdf",
                        "page": 1,
                        "x": 0.4,
                        "y": 0.5,
                    },
                    "text": "second owner note",
                },
            )
            assert other_note["id"] == "c_22222222"
            other_highlight = post_json(
                "/pdf/api/highlights",
                {
                    "file": rel,
                    "id": "c_33333333",
                    "page": 1,
                    "rects": [[5, 6, 7, 8]],
                    "text": "second owner highlight",
                },
            )
            assert other_highlight["id"] == "c_33333333"
            other_html_highlight = post_json(
                "/pdf/api/html-highlights",
                {
                    "file": rel,
                    "start": 10,
                    "end": 20,
                    "text": "second owner html highlight",
                },
            )
            assert other_html_highlight["highlight"]["text"] == (
                "second owner html highlight"
            )

            assert get_json(
                "/pdf/api/reading-pos"
            )["positions"][rel]["pos"] == 13
            assert get_json("/pdf/api/phrases")["phrases"] == [
                "secondprivatephrase"
            ]
            assert [
                item["text"] for item in get_json(
                    "/pdf/api/notes?file=book.pdf"
                )["notes"]
            ] == ["second owner note"]
            assert [
                item["text"] for item in get_json(
                    "/pdf/api/highlights?file=book.pdf"
                )["highlights"]
            ] == ["second owner highlight"]
            assert [
                item["text"] for item in get_json(
                    "/pdf/api/html-highlights?file=book.pdf"
                )["highlights"]
            ] == ["second owner html highlight"]

            # Returning to A must reveal only the immutable claimed data.
            login(owner_id, owner_name)
            assert get_json(
                "/pdf/api/reading-pos"
            )["positions"][rel]["pos"] == 7
            assert get_json("/pdf/api/phrases")["phrases"] == [
                "legacyprivatephrase"
            ]
            assert [
                item["text"] for item in get_json(
                    "/pdf/api/notes?file=book.pdf"
                )["notes"]
            ] == ["legacy owner note"]
            assert [
                item["text"] for item in get_json(
                    "/pdf/api/highlights?file=book.pdf"
                )["highlights"]
            ] == ["legacy owner highlight"]
            assert [
                item["text"] for item in get_json(
                    "/pdf/api/html-highlights?file=book.pdf"
                )["highlights"]
            ] == ["legacy owner html highlight"]
            assert get_json(
                "/pdf/api/entity/img_abcdef"
            )["id"] == "img_abcdef"

            # The second account has a distinct, explicitly identified root.
            other_root = sidecars / "by-user" / str(other_id)
            other_metadata = json.loads(
                (other_root / ".reader-account.json").read_text("utf-8")
            )
            assert other_metadata["identity"] == {
                "user_id": other_id,
                "storage_namespace": other_namespace,
            }
            assert other_metadata["legacy_claim"] is None
            assert other_root != account_root
            assert json.loads(
                (other_root / "reader-positions.json").read_text("utf-8")
            )[rel]["pos"] == 13
            assert "secondprivatephrase" in (
                other_root / "pdf-phrases.json"
            ).read_text("utf-8")
            assert "secondprivatephrase" not in (
                account_root / "pdf-phrases.json"
            ).read_text("utf-8")

            # Writes after the cut-over never mutate or delete the legacy tree.
            assert tree_bytes(legacy) == source_before
            assert json.loads(claim_path.read_text("utf-8")) == claim
            """
        )
        with tempfile.TemporaryDirectory(
            prefix="bw-reader-sidecar-service-test-"
        ) as temp:
            base = Path(temp)
            env = os.environ.copy()
            env.update(
                REPO_ROOT=str(ROOT),
                SECRET_KEY="reader-sidecar-service-secret",
                WEBAPP_DATA=str(base / "webapp-data"),
                CLAUDE_PROJECT=str(base / "project"),
                OBSIDIAN_VAULT=str(base / "vault"),
                READER_SIDECAR_ROOT=str(base / "reader-sidecars"),
                PASSWORD_HASH="development-test-bootstrap-hash",
                SESSION_COOKIE_SECURE="0",
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=(result.stdout + "\n" + result.stderr).strip(),
        )


if __name__ == "__main__":
    unittest.main()
