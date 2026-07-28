from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

from flask import Flask, request


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
sys.path.insert(0, str(SERVER))

from shared_note import CONTRACT, INITIAL_CONTENT, register_shared_note  # noqa: E402


NS_A = "acct-v1-" + "a" * 64
NS_B = "acct-v1-" + "b" * 64


class SharedNoteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events: list[dict] = []
        self.app = Flask(
            __name__,
            template_folder=str(SERVER / "templates"),
        )
        self.app.secret_key = "shared-note-test"

        def resolver():
            account = request.headers.get("X-Test-Account", "")
            if account == "a":
                return {"user_id": 1, "storage_namespace": NS_A}
            if account == "b":
                return {"user_id": 2, "storage_namespace": NS_B}
            return None

        def publish(kind, file, uid, extra=None):
            self.events.append({
                "kind": kind,
                "file": file,
                "uid": uid,
                **dict(extra or {}),
            })
            return 2

        self.app.extensions["reader_storage_identity_resolver"] = resolver
        register_shared_note(self.app, root=self.root, publish_fn=publish)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _headers(account="a"):
        return {"X-Test-Account": account}

    def _get(self, account="a"):
        return self.client.get(
            "/pdf/api/shared-note",
            headers=self._headers(account),
        )

    def _post(self, body, account="a"):
        return self.client.post(
            "/pdf/api/shared-note",
            headers=self._headers(account),
            json=body,
        )

    @staticmethod
    def _replace(revision, content, update_id="test:update:1"):
        return {
            "contract": CONTRACT,
            "operation": "replace",
            "baseRevision": revision,
            "source": "codex-test",
            "updateId": update_id,
            "content": content,
        }

    def test_routes_fail_closed_without_verified_identity(self):
        page = self.client.get("/pdf/shared-note")
        api_get = self.client.get("/pdf/api/shared-note")
        api_post = self.client.post(
            "/pdf/api/shared-note",
            json=self._replace(1, "x"),
        )
        self.assertEqual(page.status_code, 401)
        self.assertEqual(api_get.status_code, 401)
        self.assertEqual(api_post.status_code, 401)
        self.assertEqual(api_get.json["error"]["code"], "BW_SHARED_NOTE_AUTH")

    def test_page_and_seeded_context_draft_are_available(self):
        page = self.client.get(
            "/pdf/shared-note",
            headers=self._headers(),
        )
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("实时共享便签", html)
        self.assertIn("new EventSource('/pdf/api/reader-events')", html)
        self.assertIn("你的未保存内容没有被覆盖", html)
        self.assertIn("keepLocalButton", html)
        self.assertIn("loadRemoteButton", html)
        self.assertIn("queuedFetch = true", html)
        self.assertIn("if (queuedFetch)", html)
        self.assertEqual(page.headers["Cache-Control"], "no-store")

        response = self._get()
        self.assertEqual(response.status_code, 200)
        note = response.json["note"]
        self.assertEqual(note["revision"], 1)
        self.assertEqual(note["content"], INITIAL_CONTENT)
        self.assertEqual(note["source"], "system-context-draft")
        self.assertNotIn("receipts", note)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        states = list(self.root.glob("by-account/*/shared-note.json"))
        self.assertEqual(len(states), 1)
        self.assertNotIn(NS_A, str(states[0]))

    def test_browser_style_full_save_is_authoritative_and_publishes_invalidation(self):
        initial = self._get().json["note"]
        response = self._post(
            self._replace(initial["revision"], "# 新正文\n", "browser:save-1")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["result"], "updated")
        self.assertEqual(response.json["revision"], 2)
        self.assertTrue(response.json["liveNotified"])
        self.assertEqual(response.json["liveSubscriberCount"], 2)
        self.assertEqual(self._get().json["note"]["content"], "# 新正文\n")

        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["kind"], "shared-note")
        self.assertIsNone(event["uid"])
        self.assertNotIn("revision", event)
        self.assertNotIn("content", event)
        self.assertNotIn("source", event)
        self.assertNotIn("updateId", event)

    def test_post_reaches_existing_reader_events_bus_without_private_payload(self):
        import reader_events

        event_queue = queue.Queue(maxsize=2)
        with reader_events._LOCK:
            reader_events._SUBS.add(event_queue)
        self.app.extensions["reader_shared_note_publish"] = reader_events.publish
        try:
            revision = self._get().json["note"]["revision"]
            response = self._post(
                self._replace(revision, "live", "live:reader-events")
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["liveNotified"])
            event = event_queue.get(timeout=1)
            self.assertEqual(
                event,
                {
                    "kind": "shared-note",
                    "file": "",
                    "uid": None,
                    "t": event["t"],
                },
            )
        finally:
            with reader_events._LOCK:
                reader_events._SUBS.discard(event_queue)
            self.app.extensions["reader_shared_note_publish"] = (
                lambda kind, file, uid, extra=None: 0
            )

    def test_append_and_unique_exact_replace_text(self):
        revision = self._get().json["note"]["revision"]
        first = self._post({
            "contract": CONTRACT,
            "operation": "replace",
            "baseRevision": revision,
            "source": "test",
            "updateId": "ops:replace",
            "content": "Alpha\nBeta\n",
        })
        self.assertEqual(first.status_code, 200)
        second = self._post({
            "contract": CONTRACT,
            "operation": "append",
            "baseRevision": first.json["revision"],
            "source": "claude",
            "updateId": "ops:append",
            "text": "Gamma\n",
        })
        self.assertEqual(second.status_code, 200)
        third = self._post({
            "contract": CONTRACT,
            "operation": "replace-text",
            "baseRevision": second.json["revision"],
            "source": "codex",
            "updateId": "ops:replace-text",
            "oldText": "Beta",
            "newText": "β",
        })
        self.assertEqual(third.status_code, 200)
        self.assertEqual(self._get().json["note"]["content"], "Alpha\nβ\nGamma\n")

    def test_replace_text_never_guesses_missing_or_non_unique_target(self):
        revision = self._get().json["note"]["revision"]
        seeded = self._post(
            self._replace(revision, "same + same", "target:seed")
        )
        duplicate = self._post({
            "contract": CONTRACT,
            "operation": "replace-text",
            "baseRevision": seeded.json["revision"],
            "source": "codex",
            "updateId": "target:duplicate",
            "oldText": "same",
            "newText": "changed",
        })
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.json["error"]["code"],
            "BW_SHARED_NOTE_TARGET_NOT_UNIQUE",
        )
        self.assertEqual(duplicate.json["note"]["content"], "same + same")

        missing = self._post({
            "contract": CONTRACT,
            "operation": "replace-text",
            "baseRevision": seeded.json["revision"],
            "source": "codex",
            "updateId": "target:missing",
            "oldText": "absent",
            "newText": "changed",
        })
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(
            missing.json["error"]["code"],
            "BW_SHARED_NOTE_TARGET_MISSING",
        )
        self.assertEqual(self._get().json["note"]["content"], "same + same")

    def test_duplicate_update_id_replays_but_reuse_with_other_payload_fails(self):
        revision = self._get().json["note"]["revision"]
        body = self._replace(revision, "once", "stable:id-1")
        first = self._post(body)
        replay = self._post(body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["result"], "idempotent-replay")
        self.assertEqual(replay.json["revision"], first.json["revision"])
        self.assertEqual(len(self.events), 1)

        reused = self._post({
            **body,
            "content": "different",
        })
        self.assertEqual(reused.status_code, 409)
        self.assertEqual(
            reused.json["error"]["code"],
            "BW_SHARED_NOTE_UPDATE_ID_REUSE",
        )
        self.assertEqual(self._get().json["note"]["content"], "once")

    def test_stale_revision_conflict_returns_current_authoritative_note(self):
        revision = self._get().json["note"]["revision"]
        won = self._post(self._replace(revision, "winner", "race:winner"))
        lost = self._post(self._replace(revision, "loser", "race:loser"))
        self.assertEqual(won.status_code, 200)
        self.assertEqual(lost.status_code, 409)
        self.assertEqual(
            lost.json["error"]["code"],
            "BW_SHARED_NOTE_REVISION_CONFLICT",
        )
        self.assertEqual(lost.json["note"]["revision"], won.json["revision"])
        self.assertEqual(lost.json["note"]["content"], "winner")
        self.assertEqual(len(self.events), 1)

    def test_two_simultaneous_writers_have_one_winner_and_one_explicit_conflict(self):
        revision = self._get().json["note"]["revision"]
        barrier = threading.Barrier(2)

        def write(label):
            with self.app.test_client() as client:
                barrier.wait(timeout=2)
                return client.post(
                    "/pdf/api/shared-note",
                    headers=self._headers(),
                    json=self._replace(revision, label, "concurrent:" + label),
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(write, ("alpha", "beta")))
        statuses = sorted(response.status_code for response in responses)
        self.assertEqual(statuses, [200, 409])
        conflict_response = next(
            response for response in responses if response.status_code == 409
        )
        self.assertEqual(
            conflict_response.json["error"]["code"],
            "BW_SHARED_NOTE_REVISION_CONFLICT",
        )
        authoritative = self._get().json["note"]
        self.assertIn(authoritative["content"], {"alpha", "beta"})
        self.assertEqual(authoritative["revision"], revision + 1)
        self.assertEqual(
            conflict_response.json["note"]["content"],
            authoritative["content"],
        )

    def test_accounts_have_isolated_authoritative_notes(self):
        a = self._get("a").json["note"]
        b = self._get("b").json["note"]
        self.assertEqual(a["content"], INITIAL_CONTENT)
        self.assertEqual(b["content"], INITIAL_CONTENT)
        changed = self._post(
            self._replace(a["revision"], "account-a", "isolation:a"),
            "a",
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self._get("a").json["note"]["content"], "account-a")
        self.assertEqual(self._get("b").json["note"]["content"], INITIAL_CONTENT)
        states = list(self.root.glob("by-account/*/shared-note.json"))
        self.assertEqual(len(states), 2)

    def test_invalid_contract_unknown_fields_and_oversized_content_are_rejected(self):
        revision = self._get().json["note"]["revision"]
        wrong_contract = self._replace(revision, "x", "invalid:contract")
        wrong_contract["contract"] = "other/1"
        response = self._post(wrong_contract)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json["error"]["code"],
            "BW_SHARED_NOTE_CONTRACT",
        )

        unknown = self._replace(revision, "x", "invalid:unknown")
        unknown["mystery"] = True
        response = self._post(unknown)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json["error"]["code"],
            "BW_SHARED_NOTE_INVALID",
        )

        too_large = self._replace(
            revision,
            "界" * (512 * 1024),
            "invalid:large",
        )
        response = self._post(too_large)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.json["error"]["code"],
            "BW_SHARED_NOTE_TOO_LARGE",
        )

    def test_app_registration_and_existing_navigation_entry_are_present(self):
        app_source = (SERVER / "app.py").read_text("utf-8")
        nav_source = (SERVER / "static" / "nav.js").read_text("utf-8")
        manifest_source = (
            ROOT / "scripts" / "reader_deploy_manifest.py"
        ).read_text("utf-8")
        self.assertIn(
            "from shared_note import register_shared_note",
            app_source,
        )
        self.assertIn("register_shared_note(app)", app_source)
        self.assertIn('href="/pdf/shared-note">共享便签</a>', nav_source)
        self.assertIn('"shared_note.py"', manifest_source)
        self.assertIn('"shared_note.html"', manifest_source)
        self.assertIn('"nav.js"', manifest_source)

    def test_state_file_is_valid_json_and_receipts_stay_private(self):
        revision = self._get().json["note"]["revision"]
        saved = self._post(self._replace(revision, "private-ledger", "ledger:1"))
        self.assertEqual(saved.status_code, 200)
        state_file = next(self.root.glob("by-account/*/shared-note.json"))
        persisted = json.loads(state_file.read_text("utf-8"))
        self.assertEqual(persisted["receipts"][0]["updateId"], "ledger:1")
        self.assertNotIn("receipts", self._get().json["note"])


class SharedNoteProductionAuthIntegrationTest(unittest.TestCase):
    def test_real_app_accepts_session_and_bearer_but_rejects_bad_token(self):
        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path

            root = Path({str(ROOT)!r})
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module

            with module.app.app_context():
                db = module.get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("shared-note-owner", "x", "user"),
                )
                db.commit()
                uid = int(db.execute(
                    "SELECT id FROM users WHERE username=?",
                    ("shared-note-owner",),
                ).fetchone()["id"])
                db.execute(
                    "INSERT INTO api_tokens(user_id,token,label) VALUES(?,?,?)",
                    (uid, "shared-note-bearer", "shared-note-test"),
                )
                db.commit()

            client = module.app.test_client()
            bad = client.get(
                "/pdf/api/shared-note",
                headers={{"Authorization": "Bearer wrong"}},
            )
            assert bad.status_code == 401, bad.get_data(as_text=True)

            bearer = client.get(
                "/pdf/api/shared-note",
                headers={{"Authorization": "Bearer shared-note-bearer"}},
            )
            assert bearer.status_code == 200, bearer.get_data(as_text=True)
            note = bearer.get_json()["note"]
            saved = client.post(
                "/pdf/api/shared-note",
                headers={{"Authorization": "Bearer shared-note-bearer"}},
                json={{
                    "contract": {CONTRACT!r},
                    "operation": "replace",
                    "baseRevision": note["revision"],
                    "source": "codex-integration",
                    "updateId": "integration:bearer:1",
                    "content": "Bearer write",
                }},
            )
            assert saved.status_code == 200, saved.get_data(as_text=True)
            assert saved.get_json()["result"] == "updated"

            browser = module.app.test_client()
            with browser.session_transaction() as session:
                session["user_id"] = uid
                session["username"] = "shared-note-owner"
            page = browser.get("/pdf/shared-note")
            assert page.status_code == 200, page.get_data(as_text=True)
            current = browser.get("/pdf/api/shared-note")
            assert current.status_code == 200, current.get_data(as_text=True)
            assert current.get_json()["note"]["content"] == "Bearer write"
            """
        )
        with tempfile.TemporaryDirectory(
            prefix="bw-shared-note-app-test-"
        ) as data:
            env = os.environ.copy()
            env.update(
                SECRET_KEY="shared-note-app-integration-secret",
                WEBAPP_DATA=data,
                CLAUDE_PROJECT=str(ROOT),
                PASSWORD_HASH="",
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
