"""Server fences for account-owned command-outbox/2 batches."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CommandOutboxServerTest(unittest.TestCase):
    def test_owner_gate_allowlist_bearer_forwarding_and_token_owner(self) -> None:
        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            root = Path({str(ROOT)!r})
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module
            import pdf_reader as reader
            from flask import jsonify, request, session

            with module.app.app_context():
                db = module.get_db()
                for username, token in (("owner-a", "token-a"), ("owner-b", "token-b")):
                    db.execute(
                        "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                        (username, "x", "user"),
                    )
                    uid = db.execute(
                        "SELECT id FROM users WHERE username=?",
                        (username,),
                    ).fetchone()["id"]
                    db.execute(
                        "INSERT INTO api_tokens(user_id,token,label) VALUES(?,?,?)",
                        (uid, token, "outbox-test"),
                    )
                db.commit()
                row_a = db.execute(
                    "SELECT id FROM users WHERE username='owner-a'"
                ).fetchone()
                row_b = db.execute(
                    "SELECT id FROM users WHERE username='owner-b'"
                ).fetchone()
                uid_a, uid_b = row_a["id"], row_b["id"]
                owner_a = module._reader_storage_namespace(uid_a)
                owner_b = module._reader_storage_namespace(uid_b)

            seen = []
            def probe_reading_pos():
                seen.append({{
                    "authorization": request.headers.get("Authorization", ""),
                    "cookie": request.headers.get("Cookie", ""),
                    "mutation": request.headers.get("X-BW-Mutation-Id", ""),
                    "contract": request.headers.get("X-BW-Command-Outbox", ""),
                    "session_uid": session.get("user_id"),
                }})
                return jsonify({{"ok": True}})
            module.app.view_functions[
                "pdf_reader.pdf_api_reading_pos"
            ] = probe_reading_pos

            def envelope(owner, url="/pdf/api/reading-pos", method="POST",
                         mutation="mut-v2-" + "1" * 32):
                return {{
                    "contract": "command-outbox/2",
                    "ownerNamespace": owner,
                    "generation": 1,
                    "ops": [{{
                        "mutationId": mutation,
                        "url": url,
                        "method": method,
                        "body": {{"file": "book.pdf", "kind": "pdf", "pos": 2}},
                    }}],
                }}

            # token-owner never accepts an ambient browser session as a substitute.
            session_only = module.app.test_client()
            with session_only.session_transaction() as browser_session:
                browser_session["user_id"] = uid_a
            denied_session = session_only.post("/api/reader/token-owner")
            assert denied_session.status_code == 401
            denied_bad_token = session_only.post(
                "/api/reader/token-owner",
                headers={{"Authorization": "Bearer invalid"}},
            )
            assert denied_bad_token.status_code == 401

            token_client = module.app.test_client()
            token_owner = token_client.post(
                "/api/reader/token-owner",
                headers={{"Authorization": "Bearer token-a"}},
            )
            assert token_owner.status_code == 200
            assert token_owner.get_json() == {{
                "ok": True,
                "storage_namespace": owner_a,
            }}
            token_owner_text = token_owner.get_data(as_text=True)
            for forbidden in ("uid", "username", "token-a"):
                assert forbidden not in token_owner_text
            assert "no-store" in token_owner.headers["Cache-Control"]

            # owner is mandatory and must equal the current authenticated session.
            browser = module.app.test_client()
            with browser.session_transaction() as browser_session:
                browser_session["user_id"] = uid_a
            missing_owner = browser.post(
                "/pdf/api/sync-batch",
                json={{"contract": "command-outbox/2", "ops": []}},
            )
            assert missing_owner.status_code == 400
            legacy_batch = browser.post(
                "/pdf/api/sync-batch",
                json={{"ops": envelope(owner_a)["ops"]}},
            )
            assert legacy_batch.status_code == 400
            mismatch = browser.post(
                "/pdf/api/sync-batch",
                json=envelope(owner_b),
            )
            assert mismatch.status_code == 403
            assert seen == []

            allowed = browser.post(
                "/pdf/api/sync-batch",
                json=envelope(owner_a),
            )
            assert allowed.status_code == 200
            allowed_data = allowed.get_json()
            assert allowed_data["contract"] == "command-outbox/2"
            assert allowed_data["ownerNamespace"] == owner_a
            assert allowed_data["results"] == [{{"status": 200}}]
            assert seen[-1]["cookie"]
            assert seen[-1]["authorization"] == ""
            assert seen[-1]["mutation"] == "mut-v2-" + "1" * 32
            assert seen[-1]["contract"] == "command-outbox/2"
            assert int(seen[-1]["session_uid"]) == int(uid_a)

            # If Cookie and Bearer are both present, both reach the child request.
            both = browser.post(
                "/pdf/api/sync-batch",
                json=envelope(owner_a, mutation="mut-v2-" + "2" * 32),
                headers={{"Authorization": "Bearer token-a"}},
            )
            assert both.status_code == 200
            assert seen[-1]["cookie"]
            assert seen[-1]["authorization"] == "Bearer token-a"
            assert seen[-1]["mutation"] == "mut-v2-" + "2" * 32

            # An explicit conflicting/invalid Bearer cannot fall back to Cookie.
            conflict = browser.post(
                "/pdf/api/sync-batch",
                json=envelope(owner_a),
                headers={{"Authorization": "Bearer token-b"}},
            )
            assert conflict.status_code == 401
            invalid_with_cookie = browser.post(
                "/pdf/api/sync-batch",
                json=envelope(owner_a),
                headers={{"Authorization": "Bearer invalid"}},
            )
            assert invalid_with_cookie.status_code == 401

            # Bearer-only batches resolve their own owner and forward Authorization.
            bearer = module.app.test_client()
            bearer_allowed = bearer.post(
                "/pdf/api/sync-batch",
                json=envelope(owner_b, mutation="mut-v2-" + "3" * 32),
                headers={{"Authorization": "Bearer token-b"}},
            )
            assert bearer_allowed.status_code == 200
            assert seen[-1]["authorization"] == "Bearer token-b"
            assert int(seen[-1]["session_uid"]) == int(uid_b)

            # Forbidden endpoint/method/mutation are per-op 400 and never dispatched.
            before = len(seen)
            for bad in (
                envelope(owner_a, url="/pdf/api/translate"),
                envelope(owner_a, url="/api/assistant/chat"),
                envelope(owner_a, method="PUT"),
                envelope(owner_a, mutation="not-stable"),
            ):
                rejected = browser.post("/pdf/api/sync-batch", json=bad)
                assert rejected.status_code == 200
                assert rejected.get_json()["results"] == [{{"status": 400}}]
            assert len(seen) == before

            # The fixed server list covers every currently queued business family.
            for url, method in (
                ("/pdf/api/lookup-event", "POST"),
                ("/pdf/api/vocab-mark", "POST"),
                ("/pdf/api/jp-vocab-mark", "POST"),
                ("/pdf/api/phrases", "POST"),
                ("/pdf/api/phrases", "DELETE"),
                ("/pdf/api/phrase-mark", "POST"),
                ("/pdf/api/highlights", "POST"),
                ("/pdf/api/highlights?id=h1", "PATCH"),
                ("/pdf/api/highlights?id=h1", "DELETE"),
                ("/pdf/api/notes", "POST"),
                ("/pdf/api/notes?id=n1", "PATCH"),
                ("/pdf/api/notes?id=n1", "DELETE"),
                ("/pdf/api/anki-add-cards", "POST"),
                ("/pdf/api/entity/card_abc", "PATCH"),
                ("/pdf/api/review-answer", "POST"),
                ("/pdf/api/reading-pos", "POST"),
            ):
                assert reader._sync_batch_target(url, method) == url
            assert reader._sync_batch_target("/pdf/api/translate", "POST") is None
            assert reader._sync_batch_target("https://evil.example/pdf/api/notes", "POST") is None
            """
        )
        with tempfile.TemporaryDirectory(prefix="bw-command-outbox-test-") as data:
            env = os.environ.copy()
            env.update(
                SECRET_KEY="command-outbox-test-secret",
                WEBAPP_DATA=data,
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
