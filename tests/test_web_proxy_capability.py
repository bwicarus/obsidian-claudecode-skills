"""Sandboxed web proxy capabilities must stay scoped and transport-only."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from web_proxy_cap import (  # noqa: E402
    MAX_RESOURCE_BYTES_PER_RESPONSE,
    MAX_TTL_SECONDS,
    WebProxyCapBudgetRegistry,
    issue_web_proxy_cap,
    normalize_web_proxy_origin,
    verify_web_proxy_cap,
    verify_web_proxy_cap_details,
    web_proxy_cap_matches_url,
)


class WebProxyCapTokenTest(unittest.TestCase):
    def test_round_trip_preserves_user_expiry_and_canonical_scope(self) -> None:
        self.assertEqual(MAX_TTL_SECONDS, 300)
        token = issue_web_proxy_cap(
            "secret",
            17,
            scope_url="HTTPS://BÜCHER.Example./article",
            now=1_000,
            ttl_seconds=90,
        )
        details = verify_web_proxy_cap_details("secret", token, now=1_001)
        self.assertIsNotNone(details)
        assert details is not None
        self.assertEqual(details.user_id, 17)
        self.assertEqual(details.expires_at, 1_090)
        self.assertEqual(details.scope_scheme, "https")
        self.assertEqual(details.scope_host, "xn--bcher-kva.example")
        self.assertEqual(details.scope_port, 443)
        self.assertEqual(
            details.scope_origin,
            "https://xn--bcher-kva.example:443",
        )
        self.assertTrue(
            web_proxy_cap_matches_url(
                details,
                "https://xn--bcher-kva.example:443/other",
            )
        )
        self.assertFalse(
            web_proxy_cap_matches_url(
                details,
                "http://xn--bcher-kva.example/other",
            )
        )
        self.assertEqual(verify_web_proxy_cap("secret", token, now=1_001), 17)

    def test_tamper_expiry_and_future_window_are_rejected(self) -> None:
        token = issue_web_proxy_cap(
            "secret",
            7,
            scope_url="https://reader.example",
            now=2_000,
            ttl_seconds=60,
        )
        parts = token.split(".")
        tampered_scope = parts.copy()
        tampered_scope[3] = tampered_scope[3][:-1] + (
            "A" if tampered_scope[3][-1] != "A" else "B"
        )
        self.assertIsNone(
            verify_web_proxy_cap_details("secret", ".".join(tampered_scope), now=2_001)
        )
        tampered_signature = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertIsNone(
            verify_web_proxy_cap_details("secret", tampered_signature, now=2_001)
        )
        # Expiry is exclusive: a token is dead at the exact expiry second.
        self.assertIsNone(verify_web_proxy_cap_details("secret", token, now=2_060))
        # A valid signature must not make an excessively future-dated token valid.
        future = issue_web_proxy_cap(
            "secret",
            7,
            scope_url="https://reader.example",
            now=2_000,
            ttl_seconds=MAX_TTL_SECONDS,
        )
        self.assertIsNone(verify_web_proxy_cap_details("secret", future, now=1_999))

    def test_invalid_user_and_scope_are_rejected_at_issue_time(self) -> None:
        with self.assertRaises(ValueError):
            issue_web_proxy_cap("secret", 0, scope_url="https://reader.example")
        with self.assertRaises(ValueError):
            issue_web_proxy_cap("secret", 1, scope_url="https://bad host.example")

    def test_default_and_nondefault_ports_are_distinct(self) -> None:
        self.assertEqual(
            normalize_web_proxy_origin("https://Reader.Example/path"),
            "https://reader.example:443",
        )
        self.assertEqual(
            normalize_web_proxy_origin("https://reader.example:443/path"),
            "https://reader.example:443",
        )
        self.assertEqual(
            normalize_web_proxy_origin("https://reader.example:8443/path"),
            "https://reader.example:8443",
        )
        token = issue_web_proxy_cap(
            "secret",
            9,
            scope_url="https://reader.example:8443/a",
            now=3_000,
        )
        details = verify_web_proxy_cap_details("secret", token, now=3_001)
        assert details is not None
        self.assertTrue(
            web_proxy_cap_matches_url(details, "https://reader.example:8443/b")
        )
        self.assertFalse(
            web_proxy_cap_matches_url(details, "https://reader.example/b")
        )

    def test_per_cap_request_response_and_total_byte_budgets(self) -> None:
        details = verify_web_proxy_cap_details(
            "secret",
            issue_web_proxy_cap(
                "secret",
                5,
                scope_url="https://reader.example",
                now=4_000,
            ),
            now=4_001,
        )
        assert details is not None
        registry = WebProxyCapBudgetRegistry(max_requests=2, max_bytes=10)
        first = registry.begin("cap-a", details, now=4_001)
        assert first is not None
        self.assertFalse(first.consume(MAX_RESOURCE_BYTES_PER_RESPONSE + 1))
        self.assertTrue(first.consume(6))
        second = registry.begin("cap-a", details, now=4_001)
        assert second is not None
        self.assertTrue(second.consume(4))
        self.assertFalse(second.consume(1))
        self.assertIsNone(registry.begin("cap-a", details, now=4_001))


class WebProxyCapabilityIntegrationTest(unittest.TestCase):
    def test_proxy_never_reconstructs_authority_from_referer(self) -> None:
        source = (ROOT / "_server_deploy" / "html_reader.py").read_text("utf-8")
        self.assertNotIn("def _leak_rescue", source)
        self.assertIn('resp.headers["Referrer-Policy"] = "no-referrer"', source)

    @unittest.skip(
        "historical proxy integration retained for recovery; HTTP transport retired 2026-07-25"
    )
    def test_route_gate_scope_cookie_cache_and_identity_injection(self) -> None:
        script = textwrap.dedent(
            f"""
            import json
            import sys
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import patch
            from urllib.parse import parse_qs, quote, urlparse

            root = Path({str(ROOT)!r})
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module
            import html_reader as reader
            import rbi_render
            from web_proxy_cap import issue_web_proxy_cap, verify_web_proxy_cap_details

            secret = module._reader_web_proxy_secret()
            with module.app.app_context():
                db = module.get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("proxy-user", "x", "user"),
                )
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("other-user", "x", "user"),
                )
                db.commit()
                uid = db.execute(
                    "SELECT id FROM users WHERE username='proxy-user'"
                ).fetchone()["id"]
                other_uid = db.execute(
                    "SELECT id FROM users WHERE username='other-user'"
                ).fetchone()["id"]

            def cap(user_id=uid, scope="reader.example", **kw):
                scope_url = scope if "://" in scope else "https://" + scope
                return issue_web_proxy_cap(
                    secret,
                    user_id,
                    scope_url=scope_url,
                    **kw,
                )

            def login_redirect(response):
                return (
                    response.status_code == 302
                    and "/login" in (response.headers.get("Location") or "")
                )

            anon = module.app.test_client()
            valid = cap()

            # No session: a valid active-user capability reaches /frame.
            frame = anon.get(
                "/pdf/web/frame?url=https%3A%2F%2Freader.example%2Farticle"
                "&__bwcap=" + quote(valid, safe="")
            )
            assert frame.status_code == 302
            frame_location = frame.headers["Location"]
            assert frame_location.startswith("/pdf/web/p/https/reader.example/article")
            forwarded = parse_qs(urlparse(frame_location).query)["__bwcap"][0]
            assert forwarded == valid
            forwarded_claims = verify_web_proxy_cap_details(secret, forwarded)
            assert forwarded_claims.scope_host == "reader.example"
            assert forwarded_claims.scope_scheme == "https"
            assert forwarded_claims.scope_port == 443

            # Tampered, expired, and unknown/deleted-user capabilities never enter frame.
            tampered = valid[:-1] + ("0" if valid[-1] != "0" else "1")
            assert login_redirect(anon.get(
                "/pdf/web/frame?url=https%3A%2F%2Freader.example&__bwcap="
                + quote(tampered, safe="")
            ))
            expired = cap(now=1, ttl_seconds=60)
            assert login_redirect(anon.get(
                "/pdf/web/frame?url=https%3A%2F%2Freader.example&__bwcap="
                + quote(expired, safe="")
            ))
            unknown = cap(user_id=999999)
            assert login_redirect(anon.get(
                "/pdf/web/frame?url=https%3A%2F%2Freader.example&__bwcap="
                + quote(unknown, safe="")
            ))
            doomed = cap(user_id=other_uid)
            with module.app.app_context():
                db = module.get_db()
                db.execute("DELETE FROM users WHERE id=?", (other_uid,))
                db.commit()
            assert login_redirect(anon.get(
                "/pdf/web/frame?url=https%3A%2F%2Freader.example&__bwcap="
                + quote(doomed, safe="")
            ))

            # Every transport endpoint accepts the valid cap and rejects ambient session alone.
            def fake_proxy(url, capability):
                return module.Response("proxy-ok", content_type="text/html"), ""

            with patch.object(reader, "_proxy_page", side_effect=fake_proxy):
                page = anon.get(
                    "/pdf/web/p/https/reader.example/article?__bwcap="
                    + quote(valid, safe="")
                )
                assert page.status_code == 200 and page.get_data(as_text=True) == "proxy-ok"

            with patch.object(reader, "_url_safe", return_value=""), patch.object(
                reader, "_rescache_get", return_value=(b"ok", "image/png")
            ):
                resource = anon.get(
                    "/pdf/web/res?url=https%3A%2F%2Fcdn.reader.example%2Fa.png"
                    "&__bwcap=" + quote(valid, safe="")
                )
                mirrored = anon.get(
                    "/pdf/web/r/https/cdn.reader.example/a.png?__bwcap="
                    + quote(valid, safe="")
                )
                assert resource.status_code == 200, (
                    resource.status_code,
                    resource.get_data(as_text=True),
                )
                assert mirrored.status_code == 200, (
                    mirrored.status_code,
                    mirrored.get_data(as_text=True),
                )

            logged = module.app.test_client()
            with logged.session_transaction() as sess:
                sess["user_id"] = uid
                sess["username"] = "proxy-user"
            assert logged.get(
                "/pdf/web/p/https/reader.example/article"
            ).status_code == 403
            assert logged.get(
                "/pdf/web/res?url=https%3A%2F%2Freader.example%2Fa.js"
            ).status_code == 403
            assert logged.get(
                "/pdf/web/r/https/reader.example/a.js"
            ).status_code == 403

            # A real outer-shell nav ticket can create a new scope; session alone cannot.
            nav_ticket = "navigation-ticket-" + "n" * 32
            with logged.session_transaction() as sess:
                sess[reader._WEB_NAV_TICKET_SESSION_KEY] = nav_ticket
            denied_launch = logged.get(
                "/pdf/web/frame?url=https%3A%2F%2Fnew.example%2Farticle"
            )
            assert denied_launch.status_code == 403
            allowed_launch = logged.get(
                "/pdf/web/frame?url=https%3A%2F%2Fnew.example%2Farticle"
                "&__bwnav=" + quote(nav_ticket, safe="")
            )
            assert allowed_launch.status_code == 302
            launch_cap = parse_qs(
                urlparse(allowed_launch.headers["Location"]).query
            )["__bwcap"][0]
            assert verify_web_proxy_cap_details(
                secret, launch_cap
            ).scope_host == "new.example"
            legacy_launch = logged.get(
                "/pdf/web/proxy?url=https%3A%2F%2Flegacy.example%2Farticle"
                "&__bwnav=" + quote(nav_ticket, safe="")
            )
            assert legacy_launch.status_code == 302
            assert "/pdf/web/p/https/legacy.example/article" in legacy_launch.headers["Location"]
            assert "__bwnav" not in legacy_launch.headers["Location"]
            assert "__bwcap" in legacy_launch.headers["Location"]

            # Even if a sandbox navigation carries the App session cookie, its old
            # cap cannot renew or pivot without the outer-only navigation ticket.
            old_scope = cap(scope="reader.example")
            pivot = logged.get(
                "/pdf/web/frame?url=https%3A%2F%2Fother.example%2Farticle"
                "&__bwcap=" + quote(old_scope, safe="")
            )
            assert pivot.status_code == 403
            trusted_pivot = logged.get(
                "/pdf/web/frame?url=https%3A%2F%2Fother.example%2Farticle"
                "&__bwcap=" + quote(old_scope, safe="")
                + "&__bwnav=" + quote(nav_ticket, safe="")
            )
            assert trusted_pivot.status_code == 302
            pivot_cap = parse_qs(
                urlparse(trusted_pivot.headers["Location"]).query
            )["__bwcap"][0]
            assert pivot_cap != old_scope
            assert verify_web_proxy_cap_details(
                secret, pivot_cap
            ).scope_host == "other.example"

            # A top-level /p document is also pinned to the cap scope.
            with patch.object(reader, "_proxy_page", side_effect=fake_proxy):
                assert anon.get(
                    "/pdf/web/p/https/other.example/article?__bwcap="
                    + quote(old_scope, safe="")
                ).status_code == 403
                assert anon.get(
                    "/pdf/web/p/http/reader.example/article?__bwcap="
                    + quote(old_scope, safe="")
                ).status_code == 403
                assert anon.get(
                    "/pdf/web/p/https/reader.example:8443/article?__bwcap="
                    + quote(old_scope, safe="")
                ).status_code == 403

            # A capability for another account cannot override the active session.
            other_session_cap = cap(user_id=uid + 1000)
            assert logged.get(
                "/pdf/web/frame?url=https%3A%2F%2Freader.example"
                "&__bwcap=" + quote(other_session_cap, safe="")
            ).status_code in (403, 302)

            # Capability authorization is transport-only. Legacy proxy/RBI and
            # ordinary PWA/API routes still require the real login session.
            for path in (
                "/pdf/web/proxy?url=https%3A%2F%2Freader.example",
                "/pdf/web/rbi?url=https%3A%2F%2Freader.example",
                "/pdf/web/live?url=https%3A%2F%2Freader.example",
            ):
                sep = "&" if "?" in path else "?"
                response = anon.get(path + sep + "__bwcap=" + quote(valid, safe=""))
                assert login_redirect(response), (path, response.status_code)

            # Scope is parsed even with a session. Imported cookies are available
            # only when scope and target share the same saved cookie domain.
            reader.WEBCOOKIE_DIR.mkdir(parents=True, exist_ok=True)
            (reader.WEBCOOKIE_DIR / f"{{uid}}.json").write_text(json.dumps({{
                "app.example.com": {{"account": "reader-secret"}},
                "other.test": {{"account": "other-secret"}},
            }}), "utf-8")
            scoped = cap(scope="app.example.com")
            request_path = (
                "/pdf/web/res?url=https%3A%2F%2Fcdn.example.com%2Fa.js"
                "&__bwcap=" + quote(scoped, safe="")
            )
            with module.app.test_request_context(request_path):
                module.session["user_id"] = uid
                assert module.require_login_global() is None
                assert module.g.web_proxy_scope_host == "app.example.com"
                assert reader._cookies_for("https://app.example.com/account") == {{
                    "account": "reader-secret"
                }}
                assert reader._cookies_for("https://cdn.example.com/a.js") == {{}}
                assert reader._cookies_for("http://app.example.com/a.js") == {{}}
                assert reader._cookies_for("https://other.test/private") == {{}}
                assert "__bwcap" not in reader._px_headers(
                    "https://cdn.example.com/a.js"
                ).get("Referer", "")
                cookie_file = reader.WEBCOOKIE_DIR / f"{{uid}}.json"
                before = json.loads(cookie_file.read_text("utf-8"))
                redirected_response = SimpleNamespace(
                    url="https://unrelated.test/final",
                    cookies={{"poison": "must-not-land-on-first-hop"}},
                )
                reader._save_resp_cookies(
                    redirected_response.url,
                    redirected_response,
                )
                after = json.loads(cookie_file.read_text("utf-8"))
                assert after == before

            # Cache objects are separated by user and scope, not merely URL.
            cache_url = "https://cdn.example.com/avatar.js"
            a = reader._rescache_path(
                cache_url,
                user_id=str(uid),
                scope_origin="https://app.example.com",
            )
            b = reader._rescache_path(
                cache_url,
                user_id=str(uid + 1),
                scope_origin="https://app.example.com",
            )
            c = reader._rescache_path(
                cache_url,
                user_id=str(uid),
                scope_origin="https://evil.test",
            )
            assert len({{a, b, c}}) == 3
            origin_443 = reader._rescache_path(
                cache_url,
                user_id=str(uid),
                scope_origin="https://app.example.com",
            )
            origin_8443 = reader._rescache_path(
                cache_url,
                user_id=str(uid),
                scope_origin="https://app.example.com:8443",
            )
            origin_http = reader._rescache_path(
                cache_url,
                user_id=str(uid),
                scope_origin="http://app.example.com",
            )
            assert len({{origin_443, origin_8443, origin_http}}) == 3

            # Historical raw page material remains account-partitioned. The
            # retired server translation-map cache is deliberately not read.
            private_url = "https://account.example/private"
            private_html = (
                "<html><head><title>Private A</title></head><body>"
                + ("A_ONLY_PRIVATE_TEXT " * 30)
                + "</body></html>"
            )
            with module.app.test_request_context("/pdf/api/test-cache-a"):
                module.session["user_id"] = uid
                reader._web_cache_put(private_url, private_html)
                material_a = reader.web_material("web:" + private_url)
                assert "A_ONLY_PRIVATE_TEXT" in material_a["text"]
            with module.app.test_request_context("/pdf/api/test-cache-b"):
                module.session["user_id"] = uid + 1
                with patch.object(
                    reader,
                    "_public_http_text",
                    side_effect=RuntimeError("offline test"),
                ):
                    material_b = reader.web_material("web:" + private_url)
                assert "A_ONLY_PRIVATE_TEXT" not in (material_b.get("text") or "")
            assert reader._web_cache_path(
                private_url, user_id=uid
            ) != reader._web_cache_path(
                private_url, user_id=uid + 1
            )
            assert reader._web_last_path(
                user_id=uid
            ) != reader._web_last_path(
                user_id=uid + 1
            )

            # Redirects are manual and every hop is SSRF-checked. A public open
            # redirect must never trigger a second request to loopback/private IP.
            class FakeResponse:
                def __init__(self, status, url, location=""):
                    self.status_code = status
                    self.url = url
                    self.headers = {{"Location": location}} if location else {{}}
                    self.closed = False
                def close(self):
                    self.closed = True

            class FakeSession:
                def __init__(self, responses):
                    self.responses = list(responses)
                    self.calls = []
                    self.closed = False
                def get(self, url, **kwargs):
                    self.calls.append((url, kwargs))
                    return self.responses.pop(0)
                def close(self):
                    self.closed = True

            private_redirect = FakeSession([
                FakeResponse(
                    302,
                    "https://public.example/start",
                    "http://127.0.0.1:8080/admin",
                ),
            ])
            def ssrf_check(url):
                return "blocked private" if "127.0.0.1" in url else ""
            with module.app.test_request_context("/pdf/web/res"):
                module.g.web_proxy_user_id = uid
                module.g.web_proxy_scope_host = "public.example"
                with patch(
                    "curl_cffi.requests.Session",
                    return_value=private_redirect,
                ), patch.object(reader, "_url_safe", side_effect=ssrf_check):
                    try:
                        reader._px_open("https://public.example/start", {{}})
                        raise AssertionError("private redirect was followed")
                    except ValueError as error:
                        assert "blocked proxy redirect" in str(error)
            assert len(private_redirect.calls) == 1
            assert private_redirect.closed

            anonymous_redirect = FakeSession([
                FakeResponse(
                    302,
                    "https://public.example/start",
                    "http://169.254.169.254/latest/meta-data/",
                ),
            ])
            def metadata_check(url):
                return "blocked metadata" if "169.254.169.254" in url else ""
            with patch(
                "requests.Session",
                return_value=anonymous_redirect,
            ), patch.object(reader, "_url_safe", side_effect=metadata_check):
                try:
                    reader._public_http_open("https://public.example/start")
                    raise AssertionError("anonymous private redirect was followed")
                except ValueError as error:
                    assert "blocked redirect" in str(error)
            assert len(anonymous_redirect.calls) == 1
            assert anonymous_redirect.closed

            with module.app.test_request_context("/pdf/api/private-material"):
                module.session["user_id"] = uid
                private_material = reader.web_material(
                    "web:http://127.0.0.1:8080/admin"
                )
            assert not private_material.get("text")
            assert "blocked" in private_material.get("error", "")
            assert not reader._fetch_web_page(
                "http://169.254.169.254/latest/meta-data/"
            ).get("ok")

            # Top-level documents cannot redirect into another cap scope, while a
            # public CDN redirect remains possible for subresources.
            cross_document = FakeSession([
                FakeResponse(
                    302,
                    "https://reader.example/start",
                    "https://other.example/article",
                ),
            ])
            with module.app.test_request_context("/pdf/web/p/test"):
                module.g.web_proxy_user_id = uid
                module.g.web_proxy_scope_host = "reader.example"
                with patch(
                    "curl_cffi.requests.Session",
                    return_value=cross_document,
                ), patch.object(reader, "_url_safe", return_value=""):
                    try:
                        reader._px_open(
                            "https://reader.example/start",
                            {{}},
                            redirect_scope_origin="https://reader.example",
                        )
                        raise AssertionError("cross-scope document redirect was followed")
                    except ValueError as error:
                        assert "cross-scope" in str(error)
            assert len(cross_document.calls) == 1

            cdn_resource = FakeSession([
                FakeResponse(
                    302,
                    "https://reader.example/a.js",
                    "https://cdn.example/a.js",
                ),
                FakeResponse(200, "https://cdn.example/a.js"),
            ])
            with module.app.test_request_context("/pdf/web/res"):
                module.g.web_proxy_user_id = uid
                module.g.web_proxy_scope_host = "reader.example"
                with patch(
                    "curl_cffi.requests.Session",
                    return_value=cdn_resource,
                ), patch.object(reader, "_url_safe", return_value=""):
                    opened, final_response = reader._px_open(
                        "https://reader.example/a.js",
                        {{}},
                    )
            assert opened is cdn_resource
            assert final_response.status_code == 200
            assert len(cdn_resource.calls) == 2
            opened.close()

            # RBI demo uses per-account browser profiles and the shared public
            # network guard for main-frame redirects and every subrequest.
            profile_a = rbi_render.profile_path(uid)
            profile_b = rbi_render.profile_path(uid + 1)
            assert profile_a != profile_b
            assert profile_a.parent == profile_b.parent == rbi_render.PROFILE_ROOT.resolve()
            try:
                rbi_render.profile_path(0)
                raise AssertionError("RBI accepted invalid account")
            except ValueError:
                pass
            assert rbi_render.rbi_request_url_error(
                "http://127.0.0.1/private",
                "8.8.8.8",
            )
            assert rbi_render.rbi_request_url_error(
                "https://1.1.1.1/private",
                "8.8.8.8",
                top_navigation=True,
            )
            assert not rbi_render.rbi_request_url_error(
                "https://1.1.1.1/cdn.js",
                "8.8.8.8",
                top_navigation=False,
            )

            # RBI remains session+outer-ticket only and must not receive __USER__
            # or provider identity through the global HTML injector.
            fake_rbi = SimpleNamespace(stdout=json.dumps({{
                "ok": True,
                "html": "<html><head></head><body>rbi-safe</body></html>",
                "final": "https://reader.example/article",
            }}))
            no_ticket = logged.get(
                "/pdf/web/rbi?url=https%3A%2F%2Freader.example%2Farticle"
            )
            assert no_ticket.status_code == 403, (
                no_ticket.status_code,
                no_ticket.headers.get("Location"),
                no_ticket.get_data(as_text=True)[:300],
            )
            with patch("subprocess.run", return_value=fake_rbi) as rbi_run, patch.object(
                reader, "_url_safe", return_value=""
            ):
                rbi = logged.get(
                    "/pdf/web/rbi?url=https%3A%2F%2Freader.example%2Farticle"
                    "&__bwnav=" + quote(nav_ticket, safe=""),
                    follow_redirects=True,
                )
            assert rbi.status_code == 200
            assert len(rbi.history) == 1
            render_location = rbi.history[0].headers["Location"]
            assert "__bwnav" not in render_location
            assert "__bwcap" in render_location
            command = rbi_run.call_args.args[0]
            assert command[-2:] == [str(uid), "reader.example"]
            body = rbi.get_data(as_text=True)
            assert "rbi-safe" in body
            assert "window.__USER__" not in body
            assert "storage_namespace" not in body
            """
        )
        with tempfile.TemporaryDirectory(prefix="bw-web-cap-test-") as data:
            env = os.environ.copy()
            env.update(
                SECRET_KEY="web-cap-integration-secret",
                READER_WEB_PROXY_SECRET="web-cap-transport-secret",
                WEBAPP_DATA=data,
                CLAUDE_PROJECT=data,
                OBSIDIAN_VAULT=data,
                SESSION_COOKIE_SECURE="0",
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=90,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=(result.stdout + "\n" + result.stderr).strip(),
        )


if __name__ == "__main__":
    unittest.main()
