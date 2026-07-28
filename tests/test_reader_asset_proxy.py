"""Security and account-isolation contracts for numbered image proxying."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from flask import Flask
import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "_server_deploy"), str(ROOT / "scripts")]

import pdf_reader  # noqa: E402
from reader_sidecar_store import ReaderStorageIdentity, SidecarStore  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        status=200,
        *,
        headers=None,
        chunks=(),
        url="https://public.example/image",
        stream_error=None,
    ):
        self.status_code = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.url = url
        self.stream_error = stream_error
        self.closed = False

    def iter_content(self, _size):
        for chunk in self._chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


class PublicImageTransportTest(unittest.TestCase):
    def fetch_with(self, session, guard=lambda _url: ""):
        with patch("requests.Session", return_value=session), patch(
            "rbi_access.public_network_url_error",
            side_effect=guard,
        ):
            return pdf_reader._fetch_public_image(
                "https://public.example/start",
                max_bytes=16,
                total_timeout=5,
            )

    def test_checks_every_redirect_and_returns_only_supported_image(self):
        first = FakeResponse(
            302,
            headers={"Location": "https://cdn.example/final.png"},
            url="https://public.example/start",
        )
        final = FakeResponse(
            headers={"Content-Type": "image/png", "Content-Length": "4"},
            chunks=[b"pi", b"ng"],
            url="https://cdn.example/final.png",
        )
        session = FakeSession([first, final])
        checked = []
        value = self.fetch_with(
            session,
            guard=lambda url: checked.append(url) or "",
        )
        self.assertEqual(value, (b"ping", "image/png", "https://cdn.example/final.png"))
        self.assertIn("https://public.example/start", checked)
        self.assertIn("https://cdn.example/final.png", checked)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertTrue(session.calls[0][1]["stream"])
        self.assertTrue(first.closed)
        self.assertTrue(final.closed)
        self.assertTrue(session.closed)

    def test_blocks_private_redirect_before_following_it(self):
        first = FakeResponse(
            302,
            headers={"Location": "http://127.0.0.1/private.png"},
            url="https://public.example/start",
        )
        session = FakeSession([first])

        def guard(url):
            return "不允许本机或内网地址" if "127.0.0.1" in url else ""

        with self.assertRaises(pdf_reader._PublicImageFetchError) as raised:
            self.fetch_with(session, guard=guard)
        self.assertEqual(raised.exception.http_status, 403)
        self.assertEqual(len(session.calls), 1)

    def test_rejects_declared_or_streamed_oversize_payloads(self):
        declared = FakeSession([FakeResponse(
            headers={"Content-Type": "image/png", "Content-Length": "17"},
            chunks=[b"x"],
        )])
        with self.assertRaises(pdf_reader._PublicImageFetchError) as raised:
            self.fetch_with(declared)
        self.assertEqual(raised.exception.http_status, 413)

        streamed = FakeSession([FakeResponse(
            headers={"Content-Type": "image/png"},
            chunks=[b"123456789", b"123456789"],
        )])
        with self.assertRaises(pdf_reader._PublicImageFetchError) as raised:
            self.fetch_with(streamed)
        self.assertEqual(raised.exception.http_status, 413)

    def test_rejects_non_image_and_maps_timeout(self):
        html = FakeSession([FakeResponse(
            headers={"Content-Type": "text/html"},
            chunks=[b"<html>"],
        )])
        with self.assertRaises(pdf_reader._PublicImageFetchError) as raised:
            self.fetch_with(html)
        self.assertEqual(raised.exception.http_status, 415)

        timed_out = FakeSession([requests.Timeout("slow")])
        with self.assertRaises(pdf_reader._PublicImageFetchError) as raised:
            self.fetch_with(timed_out)
        self.assertEqual(raised.exception.http_status, 504)


class AccountOwnedAssetProxyTest(unittest.TestCase):
    def test_same_asset_id_resolves_only_inside_verified_account(self):
        app = Flask(__name__)
        app.secret_key = "asset-proxy-test"
        owner = {"value": None}
        app.extensions["reader_storage_identity_resolver"] = (
            lambda: owner["value"]
        )
        app.extensions["reader_legacy_sidecar_claim_authorizer"] = (
            lambda _identity: False
        )

        identity_a = ReaderStorageIdentity(
            7,
            "acct-v1-" + ("a" * 64),
        )
        identity_b = ReaderStorageIdentity(
            8,
            "acct-v1-" + ("b" * 64),
        )
        with tempfile.TemporaryDirectory(prefix="bw-asset-proxy-") as temp:
            base = Path(temp)
            store = SidecarStore(
                base / "sidecars",
                base / "legacy",
                lambda _identity: False,
            )
            with patch.object(pdf_reader, "_READER_SIDECAR_STORE", store):
                owner["value"] = identity_a
                with app.test_request_context("/"):
                    pdf_reader._asset_save({
                        "img_abcdef": {
                            "kind": "img",
                            "url": "https://a.example/private.png",
                            "local": "",
                        }
                    })
                owner["value"] = identity_b
                with app.test_request_context("/"):
                    pdf_reader._asset_save({
                        "img_abcdef": {
                            "kind": "img",
                            "url": "https://b.example/private.png",
                            "local": "",
                        }
                    })

                fetched = []

                def download(url):
                    fetched.append(url)
                    marker = b"A" if url.startswith("https://a.") else b"B"
                    return marker, "image/png", url

                with patch.object(
                    pdf_reader,
                    "_fetch_public_image",
                    side_effect=download,
                ):
                    owner["value"] = identity_a
                    with app.test_request_context(
                        "/pdf/api/asset/img_abcdef"
                        "?proxy=1&url=http://127.0.0.1/ignored"
                    ):
                        response_a = pdf_reader.pdf_api_asset("img_abcdef")
                    owner["value"] = identity_b
                    with app.test_request_context(
                        "/pdf/api/asset/img_abcdef?proxy=1"
                    ):
                        response_b = pdf_reader.pdf_api_asset("img_abcdef")

                self.assertEqual(response_a.get_data(), b"A")
                self.assertEqual(response_b.get_data(), b"B")
                self.assertEqual(fetched, [
                    "https://a.example/private.png",
                    "https://b.example/private.png",
                ])
                for response in (response_a, response_b):
                    self.assertIn("private", response.headers["Cache-Control"])
                    self.assertIn("no-store", response.headers["Cache-Control"])
                    self.assertEqual(
                        response.headers["X-Content-Type-Options"],
                        "nosniff",
                    )
                    self.assertIn(
                        "Authorization",
                        response.headers["Vary"],
                    )
                    self.assertIn("Cookie", response.headers["Vary"])


class AssetProxyServiceIntegrationTest(unittest.TestCase):
    def test_login_and_account_switch_are_enforced_by_real_app(self):
        script = textwrap.dedent(
            """
            import os
            from pathlib import Path
            import sys
            from unittest.mock import patch

            root = Path(os.environ["REPO_ROOT"])
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module
            import pdf_reader as reader

            client = module.app.test_client()
            anonymous = client.get("/pdf/api/asset/img_abcdef?proxy=1")
            assert anonymous.status_code == 302
            assert "/login" in (anonymous.headers.get("Location") or "")

            with module.app.app_context():
                db = module.get_db()
                first = db.execute(
                    "SELECT id, username FROM users ORDER BY id LIMIT 1"
                ).fetchone()
                assert first is not None
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("asset-second-user", "x", "user"),
                )
                db.commit()
                second = db.execute(
                    "SELECT id, username FROM users WHERE username=?",
                    ("asset-second-user",),
                ).fetchone()

            def login(row):
                with client.session_transaction() as browser_session:
                    browser_session.clear()
                    browser_session["user_id"] = int(row["id"])
                    browser_session["username"] = str(row["username"])

            def save(row, url):
                with module.app.test_request_context("/pdf/api/asset"):
                    module.session["user_id"] = int(row["id"])
                    module.session["username"] = str(row["username"])
                    reader._asset_save({
                        "img_abcdef": {
                            "kind": "img",
                            "url": url,
                            "local": "",
                        }
                    })

            save(first, "https://a.example/private.png")
            save(second, "https://b.example/private.png")

            def download(url):
                marker = b"A" if url.startswith("https://a.") else b"B"
                return marker, "image/png", url

            with patch.object(reader, "_fetch_public_image", side_effect=download):
                login(first)
                first_response = client.get(
                    "/pdf/api/asset/img_abcdef?proxy=1"
                )
                assert first_response.status_code == 200
                assert first_response.data == b"A"

                login(second)
                second_response = client.get(
                    "/pdf/api/asset/img_abcdef?proxy=1"
                )
                assert second_response.status_code == 200
                assert second_response.data == b"B"

                denied = client.get(
                    "/pdf/api/asset/img_abcdef?proxy=1",
                    headers={"Authorization": "Bearer invalid"},
                )
                assert denied.status_code == 401

            for response in (first_response, second_response):
                assert "private" in response.headers["Cache-Control"]
                assert "no-store" in response.headers["Cache-Control"]
                assert "Cookie" in response.headers["Vary"]
            """
        )
        with tempfile.TemporaryDirectory(
            prefix="bw-asset-proxy-service-"
        ) as temp:
            base = Path(temp)
            env = os.environ.copy()
            env.update(
                REPO_ROOT=str(ROOT),
                SECRET_KEY="reader-asset-proxy-test",
                WEBAPP_DATA=str(base / "webapp-data"),
                CLAUDE_PROJECT=str(base / "project"),
                OBSIDIAN_VAULT=str(base / "vault"),
                READER_SIDECAR_ROOT=str(base / "sidecars"),
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
