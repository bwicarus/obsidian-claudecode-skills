"""Reader Vault namespace 必须持久，扩展必须出示当前用户页面拿到的服务器证明。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReaderProviderIdentityTest(unittest.TestCase):
    def test_persistent_namespace_and_ticket_verification(self) -> None:
        script = textwrap.dedent(
            f"""
            import sys
            import time
            from pathlib import Path
            root = Path({str(ROOT)!r})
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module

            with module.app.app_context():
                db = module.get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("reader-test", "x", "user"),
                )
                db.commit()
                uid = db.execute(
                    "SELECT id FROM users WHERE username=?",
                    ("reader-test",),
                ).fetchone()["id"]
                first = module._reader_storage_namespace(uid)
                module.app.secret_key = "rotated-session-secret"
                second = module._reader_storage_namespace(uid)
                assert first == second
                expires_at = int(time.time()) + 600
                ticket = module._reader_provider_ticket(
                    first,
                    expires_at=expires_at,
                    nonce="a" * 32,
                )
                assert ticket.startswith(f"pvt-v2-{{expires_at}}-{{'a' * 32}}-")
                assert module._verify_reader_provider_ticket(
                    first, ticket, now=expires_at - 1
                ) == expires_at
                assert module._verify_reader_provider_ticket(
                    first, ticket, now=expires_at
                ) is None
                too_long = module._reader_provider_ticket(
                    first,
                    expires_at=expires_at + 3601,
                    nonce="d" * 32,
                )
                assert module._verify_reader_provider_ticket(
                    first, too_long, now=expires_at
                ) is None

            client = module.app.test_client()
            refresh_unauthenticated = client.post(
                "/api/reader/provider-ticket",
                json={{"page": "/pdf/view"}},
                headers={{"X-BW-Reader-Provider": "1"}},
            )
            assert refresh_unauthenticated.status_code == 401

            allowed = client.post(
                "/api/reader/provider-authorize",
                json={{"namespace": first, "ticket": ticket}},
            )
            assert allowed.status_code == 200
            assert allowed.get_json()["storage_namespace"] == first
            assert allowed.get_json()["expires_at"] == expires_at
            assert 0 < allowed.get_json()["expires_in"] <= 600

            bad_signature = ticket[:-1] + ("0" if ticket[-1] != "0" else "1")
            denied = client.post(
                "/api/reader/provider-authorize",
                json={{"namespace": first, "ticket": bad_signature}},
            )
            assert denied.status_code == 403

            expired = module._reader_provider_ticket(
                first,
                expires_at=int(time.time()) - 1,
                nonce="b" * 32,
            )
            denied_expired = client.post(
                "/api/reader/provider-authorize",
                json={{"namespace": first, "ticket": expired}},
            )
            assert denied_expired.status_code == 403

            orphan_namespace = "acct-v1-" + "f" * 64
            orphan_ticket = module._reader_provider_ticket(
                orphan_namespace,
                expires_at=int(time.time()) + 600,
                nonce="c" * 32,
            )
            denied_orphan = client.post(
                "/api/reader/provider-authorize",
                json={{"namespace": orphan_namespace, "ticket": orphan_ticket}},
            )
            assert denied_orphan.status_code == 403

            with client.session_transaction() as browser_session:
                browser_session["user_id"] = uid
            refresh_without_header = client.post(
                "/api/reader/provider-ticket",
                json={{"page": "/pdf/view"}},
            )
            assert refresh_without_header.status_code == 403
            refresh_wrong_page = client.post(
                "/api/reader/provider-ticket",
                json={{"page": "/dashboard"}},
                headers={{"X-BW-Reader-Provider": "1"}},
            )
            assert refresh_wrong_page.status_code == 400
            refreshed = client.post(
                "/api/reader/provider-ticket",
                json={{"page": "/pdf/view"}},
                headers={{"X-BW-Reader-Provider": "1"}},
            )
            assert refreshed.status_code == 200
            refresh_data = refreshed.get_json()
            assert refresh_data["storage_namespace"] == first
            assert refresh_data["ticket"].startswith("pvt-v2-")
            assert refresh_data["expires_at"] > int(time.time())
            assert 0 < refresh_data["expires_in"] <= 900
            assert "no-store" in refreshed.headers["Cache-Control"]

            for path in module.READER_PROVIDER_ENTRY_PATHS:
                with module.app.test_request_context(path):
                    module.session["user_id"] = uid
                    response = module.Response(
                        "<!doctype html><html><head></head><body>reader</body></html>",
                        mimetype="text/html",
                    )
                    processed = module.inject_nav(response)
                    body = processed.get_data(as_text=True)
                    assert '"storage_namespace"' in body, path
                    assert '"storage_provider_ticket"' in body, path
                    assert "pvt-v2-" in body, path
                    assert "/static/reader-runtime/pwa-cache-identity.js" in body, path
                    assert processed.headers["X-BW-Reader-Cache-Namespace"] == first
                    assert "Cookie" in processed.headers["Vary"]

            for path in ("/dashboard", "/pdf", "/pdf/api/ping", "/profile"):
                with module.app.test_request_context(path):
                    module.session["user_id"] = uid
                    response = module.Response(
                        "<!doctype html><html><head></head><body>other</body></html>",
                        mimetype="text/html",
                    )
                    body = module.inject_nav(response).get_data(as_text=True)
                    assert '"storage_provider_ticket"' not in body, path
                    assert "pvt-v2-" not in body, path

            with module.app.test_request_context("/pdf/api/page-image"):
                module.session["user_id"] = uid
                api_response = module.inject_nav(module.Response(
                    b"page", mimetype="image/webp"
                ))
                assert api_response.headers["X-BW-Reader-Cache-Namespace"] == first

            for path in (
                "/pdf/web/proxy",
                "/pdf/web/rbi",
                "/pdf/web/res",
                "/pdf/web/p/example",
                "/pdf/web/r/example",
            ):
                with module.app.test_request_context(path):
                    module.session["user_id"] = uid
                    proxy_response = module.inject_nav(module.Response(
                        b"proxy", mimetype="text/html"
                    ))
                    assert "X-BW-Reader-Cache-Namespace" not in proxy_response.headers, path

            identity_without_marker = client.get("/pdf/api/cache-identity")
            assert identity_without_marker.status_code == 403
            identity = client.get(
                "/pdf/api/cache-identity",
                headers={{"X-BW-Reader-Cache-Identity": "1"}},
            )
            assert identity.status_code == 200
            assert identity.get_json() == {{"ok": True}}
            assert "storage_namespace" not in identity.get_data(as_text=True)
            assert identity.headers["X-BW-Reader-Cache-Namespace"] == first
            assert "no-store" in identity.headers["Cache-Control"]

            anonymous = module.app.test_client()
            anonymous_identity = anonymous.get(
                "/pdf/api/cache-identity",
                headers={{"X-BW-Reader-Cache-Identity": "1"}},
            )
            assert anonymous_identity.status_code == 302
            assert "/login" in (anonymous_identity.headers.get("Location") or "")
            assert "X-BW-Reader-Cache-Namespace" not in anonymous_identity.headers
            """
        )
        with tempfile.TemporaryDirectory(prefix="bw-reader-provider-test-") as data:
            env = os.environ.copy()
            env.update(
                SECRET_KEY="reader-test-secret-one",
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
