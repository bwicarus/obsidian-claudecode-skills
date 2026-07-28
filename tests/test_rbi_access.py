"""Offline security contracts for the RBI browser boundary."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import websockets


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "_server_deploy"), str(ROOT / "scripts")]

import rbi_access  # noqa: E402
import rbi_render  # noqa: E402
import rbi_server  # noqa: E402


SECRET = b"rbi-test-secret-" + b"x" * 48


def _resolver(*addresses: str):
    def resolve(host, port, **kwargs):
        del host, port, kwargs
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 0, 0, 0) if ":" in address else (address, 0),
            )
            for address in addresses
        ]

    return resolve


class RbiTicketTest(unittest.TestCase):
    def test_ticket_is_short_lived_signed_and_uid_bound(self) -> None:
        token = rbi_access.issue_rbi_ticket(
            SECRET,
            17,
            now=1_000,
            ttl_seconds=90,
        )
        claims = rbi_access.verify_rbi_ticket(SECRET, token, now=1_001)
        self.assertIsNotNone(claims)
        assert claims is not None
        self.assertEqual(claims.identity.user_id, 17)
        self.assertEqual(claims.expires_at, 1_090)
        self.assertIsNone(
            rbi_access.verify_rbi_ticket(
                SECRET,
                token,
                now=1_001,
                expected_user_id=18,
            )
        )
        self.assertIsNone(rbi_access.verify_rbi_ticket(SECRET, token, now=1_090))

    def test_forged_and_excessively_future_tickets_are_rejected(self) -> None:
        token = rbi_access.issue_rbi_ticket(
            SECRET,
            3,
            now=2_000,
            ttl_seconds=60,
        )
        forged = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertIsNone(rbi_access.verify_rbi_ticket(SECRET, forged, now=2_001))
        future = rbi_access.issue_rbi_ticket(
            SECRET,
            3,
            now=2_000,
            ttl_seconds=rbi_access.MAX_TTL_SECONDS,
        )
        self.assertIsNone(rbi_access.verify_rbi_ticket(SECRET, future, now=1_999))
        self.assertIsNone(rbi_access.verify_rbi_ticket(SECRET, "../3", now=2_001))

    def test_secret_is_atomically_shared_during_concurrent_first_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"READER_RBI_SECRET": ""},
            clear=False,
        ):
            with ThreadPoolExecutor(max_workers=8) as pool:
                values = list(
                    pool.map(
                        lambda _: rbi_access.load_rbi_ticket_secret(tmp),
                        range(16),
                    )
                )
            self.assertEqual(len(set(values)), 1)
            self.assertEqual(len(values[0]), 64)
            self.assertEqual(
                (Path(tmp) / "state" / "rbi-ticket-secret").stat().st_mode & 0o777,
                0o600,
            )

    def test_nonce_is_single_use_concurrent_bounded_and_pruned(self) -> None:
        registry = rbi_access.RbiTicketNonceRegistry(max_entries=2)
        claims = rbi_access.RbiTicketClaims(
            identity=rbi_access.RbiIdentity(17),
            expires_at=1_100,
            nonce="a" * 24,
        )
        with ThreadPoolExecutor(max_workers=16) as pool:
            consumed = list(
                pool.map(
                    lambda _: registry.consume(claims, now=1_000),
                    range(32),
                )
            )
        self.assertEqual(consumed.count(True), 1)
        self.assertEqual(consumed.count(False), 31)

        second = rbi_access.RbiTicketClaims(
            identity=rbi_access.RbiIdentity(18),
            expires_at=1_050,
            nonce="b" * 24,
        )
        third = rbi_access.RbiTicketClaims(
            identity=rbi_access.RbiIdentity(19),
            expires_at=1_200,
            nonce="c" * 24,
        )
        self.assertTrue(registry.consume(second, now=1_000))
        self.assertFalse(registry.consume(third, now=1_000))
        self.assertEqual(registry.active_count(now=1_050), 1)
        self.assertTrue(registry.consume(third, now=1_050))
        self.assertEqual(registry.active_count(now=1_100), 1)


class RbiIdentityPathTest(unittest.TestCase):
    def test_profile_and_cookie_paths_only_accept_verified_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            cookies = root / "cookies"
            profiles.mkdir()
            cookies.mkdir()
            identity = rbi_access.RbiIdentity(42)
            with patch.object(rbi_server, "PROFILES", profiles), patch.object(
                rbi_server,
                "WEBCOOKIES",
                cookies,
            ):
                self.assertEqual(rbi_server._profile_path(identity), profiles / "42")
                self.assertEqual(rbi_server._cookie_path(identity), cookies / "42.json")
                with self.assertRaises(TypeError):
                    rbi_server._profile_path("../other-user")  # type: ignore[arg-type]
                with self.assertRaises(TypeError):
                    rbi_server._cookie_path("1/../../other")  # type: ignore[arg-type]
                (profiles / "99").symlink_to(profiles / "42", target_is_directory=True)
                (cookies / "99.json").symlink_to(cookies / "42.json")
                with self.assertRaises(ValueError):
                    rbi_server._profile_path(rbi_access.RbiIdentity(99))
                with self.assertRaises(ValueError):
                    rbi_server._cookie_path(rbi_access.RbiIdentity(99))

    def test_cookie_lookup_cannot_cross_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp)
            (cookies / "7.json").write_text(
                json.dumps({"example.com": {"sid": "seven"}}),
                "utf-8",
            )
            (cookies / "8.json").write_text(
                json.dumps({"example.com": {"sid": "eight"}}),
                "utf-8",
            )
            with patch.object(rbi_server, "WEBCOOKIES", cookies):
                seven = rbi_server._cookies_for(
                    rbi_access.RbiIdentity(7),
                    "https://example.com/",
                )
                eight = rbi_server._cookies_for(
                    rbi_access.RbiIdentity(8),
                    "https://example.com/",
                )
                insecure = rbi_server._cookies_for(
                    rbi_access.RbiIdentity(7),
                    "http://example.com/",
                )
                sibling = rbi_server._cookies_for(
                    rbi_access.RbiIdentity(7),
                    "https://www.example.com/",
                )
            self.assertEqual(seven[0]["value"], "seven")
            self.assertEqual(eight[0]["value"], "eight")
            self.assertEqual(seven[0]["url"], "https://example.com/")
            self.assertTrue(seven[0]["secure"])
            self.assertNotIn("domain", seven[0])
            self.assertEqual(insecure, [])
            self.assertEqual(sibling, [])

    def test_demo_and_live_share_profile_with_read_only_uid_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            canonical = state / rbi_access.RBI_PROFILE_DIRNAME
            legacy = state / rbi_access.RBI_LEGACY_PROFILE_DIRNAME
            (legacy / "7").mkdir(parents=True)
            (legacy / "8").mkdir()
            (legacy / "7" / "session.json").write_text("seven", "utf-8")
            (legacy / "8" / "session.json").write_text("eight", "utf-8")
            (legacy / "7" / "foreign-link").symlink_to(
                legacy / "8" / "session.json"
            )

            identity = rbi_access.RbiIdentity(7)
            with patch.object(rbi_server, "PROFILES", canonical), patch.object(
                rbi_server,
                "LEGACY_PROFILES",
                legacy,
            ), patch.object(
                rbi_render,
                "PROFILE_ROOT",
                canonical,
            ), patch.object(
                rbi_render,
                "LEGACY_PROFILE_ROOT",
                legacy,
            ):
                live = rbi_server._prepare_profile_path(identity)
                demo = rbi_render.prepare_profile_path(7)

            self.assertEqual(live, canonical / "7")
            self.assertEqual(demo, canonical / "7")
            self.assertEqual(
                (canonical / "7" / "session.json").read_text("utf-8"),
                "seven",
            )
            self.assertFalse((canonical / "7" / "foreign-link").exists())
            self.assertFalse((canonical / "8").exists())
            (canonical / "7" / "session.json").write_text("changed", "utf-8")
            self.assertEqual(
                (legacy / "7" / "session.json").read_text("utf-8"),
                "seven",
            )

    def test_profile_lease_is_cross_process_and_demo_falls_back_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            canonical = Path(tmp) / rbi_access.RBI_PROFILE_DIRNAME
            identity = rbi_access.RbiIdentity(23)
            profile = rbi_access.prepare_rbi_profile(
                canonical,
                Path(tmp) / rbi_access.RBI_LEGACY_PROFILE_DIRNAME,
                identity,
            )
            (profile / "Default").mkdir()
            (profile / "Default" / "Preferences").write_text(
                "profile-data",
                "utf-8",
            )
            (profile / "DevToolsActivePort").write_text("locked", "utf-8")
            (profile / "Default" / "LOCK").write_text("locked", "utf-8")

            lease = rbi_access.acquire_rbi_profile_lease(
                canonical,
                identity,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            child = textwrap.dedent(
                f"""
                import sys
                from pathlib import Path
                sys.path.insert(0, {str(ROOT / "_server_deploy")!r})
                from rbi_access import (
                    RbiIdentity,
                    acquire_rbi_profile_lease,
                )
                lease = acquire_rbi_profile_lease(
                    Path({str(canonical)!r}),
                    RbiIdentity(23),
                )
                print("acquired" if lease is not None else "busy")
                if lease is not None:
                    lease.release()
                """
            )
            busy = subprocess.run(
                [sys.executable, "-c", child],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(busy.returncode, 0, msg=busy.stderr)
            self.assertEqual(busy.stdout.strip(), "busy")

            with rbi_access.open_rbi_demo_profile(
                canonical,
                identity,
            ) as demo:
                temporary = demo.path
                self.assertTrue(demo.temporary)
                self.assertNotEqual(temporary, profile)
                self.assertEqual(
                    (temporary / "Default" / "Preferences").read_text("utf-8"),
                    "profile-data",
                )
                self.assertFalse((temporary / "DevToolsActivePort").exists())
                self.assertFalse((temporary / "Default" / "LOCK").exists())
                (temporary / "Default" / "Preferences").write_text(
                    "demo-only",
                    "utf-8",
                )
            self.assertFalse(temporary.exists())
            self.assertEqual(
                (profile / "Default" / "Preferences").read_text("utf-8"),
                "profile-data",
            )

            lease.release()
            available = subprocess.run(
                [sys.executable, "-c", child],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(available.returncode, 0, msg=available.stderr)
            self.assertEqual(available.stdout.strip(), "acquired")
            with rbi_access.open_rbi_demo_profile(
                canonical,
                identity,
            ) as direct:
                self.assertFalse(direct.temporary)
                self.assertEqual(direct.path, profile)


class RbiPublicNetworkGuardTest(unittest.IsolatedAsyncioTestCase):
    def test_open_url_rejects_file_localhost_private_and_mixed_dns(self) -> None:
        self.assertTrue(
            rbi_access.public_network_url_error(
                "file:///etc/passwd",
                resolver=_resolver("93.184.216.34"),
            )
        )
        self.assertTrue(
            rbi_access.public_network_url_error(
                "https://127.0.0.1\\@public.example/",
                resolver=_resolver("93.184.216.34"),
            )
        )
        self.assertTrue(
            rbi_access.public_network_url_error(
                "http://localhost/admin",
                resolver=_resolver("127.0.0.1"),
            )
        )
        self.assertTrue(
            rbi_access.public_network_url_error(
                "https://private.example/",
                resolver=_resolver("10.0.0.8"),
            )
        )
        self.assertTrue(
            rbi_access.public_network_url_error(
                "https://rebinding.example/",
                resolver=_resolver("93.184.216.34", "192.168.1.2"),
            )
        )
        self.assertEqual(
            rbi_access.public_network_url_error(
                "https://public.example/",
                resolver=_resolver("93.184.216.34"),
            ),
            "",
        )

    async def test_redirect_and_subrequest_guard_abort_private_target(self) -> None:
        class Request:
            url = "http://169.254.169.254/latest/meta-data"

        class Route:
            aborted = False
            continued = False

            async def abort(self, reason):
                self.aborted = reason == "blockedbyclient"

            async def continue_(self):
                self.continued = True

        route = Route()
        with patch.object(
            rbi_server,
            "browser_request_url_error",
            side_effect=lambda url: (
                "不允许本机或内网地址" if "169.254." in url else ""
            ),
        ):
            await rbi_server._guard_browser_route(route, Request())
        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)

    def test_browser_guard_blocks_file_and_private_websocket(self) -> None:
        self.assertTrue(rbi_access.browser_request_url_error("file:///etc/passwd"))
        self.assertTrue(
            rbi_access.browser_request_url_error(
                "ws://socket.example/",
                resolver=_resolver("127.0.0.1"),
            )
        )
        self.assertEqual(
            rbi_access.browser_request_url_error(
                "wss://socket.example/",
                resolver=_resolver("93.184.216.34"),
            ),
            "",
        )


class RbiProfileLeaseLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_context_close_releases_profile_lease(self) -> None:
        class Context:
            closed = False

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / rbi_access.RBI_PROFILE_DIRNAME
            identity = rbi_access.RbiIdentity(61)
            rbi_access.prepare_rbi_profile(
                root,
                Path(tmp) / rbi_access.RBI_LEGACY_PROFILE_DIRNAME,
                identity,
            )
            lease = rbi_access.acquire_rbi_profile_lease(root, identity)
            self.assertIsNotNone(lease)
            assert lease is not None
            context = Context()
            with patch.object(rbi_server, "_CTX", {61: context}), patch.object(
                rbi_server,
                "_CTX_LEASES",
                {61: lease},
            ):
                await rbi_server._drop_context_locked(61)
                self.assertEqual(rbi_server._CTX, {})
                self.assertEqual(rbi_server._CTX_LEASES, {})
            self.assertTrue(context.closed)
            self.assertFalse(lease.active)
            reopened = rbi_access.acquire_rbi_profile_lease(root, identity)
            self.assertIsNotNone(reopened)
            assert reopened is not None
            reopened.release()


class _FakeHeaders(dict):
    pass


class _FakeWs:
    def __init__(self, first_message, *, origin="https://reader.example"):
        self.request_headers = _FakeHeaders({
            "Origin": origin,
            "Host": "reader.example",
        })
        self.first_message = first_message
        self.sent = []
        self.closed = None

    async def recv(self):
        return self.first_message

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def close(self, *, code=None, reason=None):
        self.closed = (code, reason)


class RbiWebSocketAuthTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_frame_authenticates_and_does_not_publish_uid(self) -> None:
        token = rbi_access.issue_rbi_ticket(SECRET, 55)
        ws = _FakeWs(json.dumps({"cmd": "auth", "ticket": token}))
        with patch.object(rbi_server, "_RBI_SECRET", SECRET), patch.dict(
            os.environ,
            {"READER_RBI_ALLOWED_ORIGINS": ""},
            clear=False,
        ), patch.object(
            rbi_server,
            "_TICKET_NONCES",
            rbi_access.RbiTicketNonceRegistry(),
        ):
            claims = await rbi_server._authenticate_ws(ws)
        self.assertIsNotNone(claims)
        assert claims is not None
        self.assertEqual(claims.identity, rbi_access.RbiIdentity(55))
        self.assertEqual(ws.sent[0]["t"], "ready")
        self.assertNotIn("uid", ws.sent[0])
        self.assertIsNone(ws.closed)

    async def test_forged_ticket_and_cross_site_origin_fail_closed(self) -> None:
        token = rbi_access.issue_rbi_ticket(SECRET, 55)
        forged = token[:-1] + ("0" if token[-1] != "0" else "1")
        forged_ws = _FakeWs(json.dumps({"cmd": "auth", "ticket": forged}))
        cross_site_ws = _FakeWs(
            json.dumps({"cmd": "auth", "ticket": token}),
            origin="https://attacker.example",
        )
        with patch.object(rbi_server, "_RBI_SECRET", SECRET), patch.dict(
            os.environ,
            {"READER_RBI_ALLOWED_ORIGINS": ""},
            clear=False,
        ), patch.object(
            rbi_server,
            "_TICKET_NONCES",
            rbi_access.RbiTicketNonceRegistry(),
        ):
            self.assertIsNone(await rbi_server._authenticate_ws(forged_ws))
            self.assertIsNone(await rbi_server._authenticate_ws(cross_site_ws))
        self.assertEqual(forged_ws.closed[0], 4401)
        self.assertEqual(cross_site_ws.closed[0], 4401)

    async def test_local_origin_requires_explicit_configuration(self) -> None:
        token = rbi_access.issue_rbi_ticket(SECRET, 9)
        ws = _FakeWs(
            json.dumps({"cmd": "auth", "ticket": token}),
            origin="http://127.0.0.1:5000",
        )
        ws.request_headers["Host"] = "127.0.0.1:5000"
        with patch.object(rbi_server, "_RBI_SECRET", SECRET), patch.dict(
            os.environ,
            {"READER_RBI_ALLOWED_ORIGINS": "http://127.0.0.1:5000"},
            clear=False,
        ), patch.object(
            rbi_server,
            "_TICKET_NONCES",
            rbi_access.RbiTicketNonceRegistry(),
        ):
            claims = await rbi_server._authenticate_ws(ws)
        self.assertIsNotNone(claims)
        assert claims is not None
        self.assertEqual(claims.identity, rbi_access.RbiIdentity(9))

    async def test_ticket_replay_is_rejected_under_concurrent_auth(self) -> None:
        token = rbi_access.issue_rbi_ticket(SECRET, 31)
        first = _FakeWs(json.dumps({"cmd": "auth", "ticket": token}))
        second = _FakeWs(json.dumps({"cmd": "auth", "ticket": token}))
        with patch.object(rbi_server, "_RBI_SECRET", SECRET), patch.dict(
            os.environ,
            {"READER_RBI_ALLOWED_ORIGINS": ""},
            clear=False,
        ), patch.object(
            rbi_server,
            "_TICKET_NONCES",
            rbi_access.RbiTicketNonceRegistry(),
        ):
            results = await asyncio.gather(
                rbi_server._authenticate_ws(first),
                rbi_server._authenticate_ws(second),
            )
        self.assertEqual(sum(result is not None for result in results), 1)
        rejected = first if first.closed else second
        self.assertEqual(rejected.closed[0], 4401)

    async def test_active_connection_closes_at_exclusive_expiry(self) -> None:
        ws = _FakeWs("")
        with patch.object(rbi_server.time, "time", return_value=10_000):
            await rbi_server._close_ws_at_expiry(ws, 10_000)
        self.assertEqual(ws.closed[0], 4408)

    async def test_served_connection_is_actively_closed_when_ticket_expires(self) -> None:
        token = rbi_access.issue_rbi_ticket(
            SECRET,
            44,
            ttl_seconds=1,
        )
        with patch.object(rbi_server, "_RBI_SECRET", SECRET), patch.dict(
            os.environ,
            {"READER_RBI_ALLOWED_ORIGINS": "http://reader.test"},
            clear=False,
        ), patch.object(
            rbi_server,
            "_TICKET_NONCES",
            rbi_access.RbiTicketNonceRegistry(),
        ):
            async with websockets.serve(
                rbi_server._serve,
                "127.0.0.1",
                0,
            ) as server:
                port = server.sockets[0].getsockname()[1]
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}",
                    origin="http://reader.test",
                ) as client:
                    await client.send(
                        json.dumps({"cmd": "auth", "ticket": token})
                    )
                    ready = json.loads(await client.recv())
                    self.assertEqual(ready["t"], "ready")
                    with self.assertRaises(websockets.ConnectionClosed) as closed:
                        await asyncio.wait_for(client.recv(), timeout=2)
                    self.assertEqual(closed.exception.rcvd.code, 4408)

    async def test_installed_websockets_handshake_headers_are_supported(self) -> None:
        token = rbi_access.issue_rbi_ticket(SECRET, 12)
        identities = []

        async def auth_once(ws):
            identities.append(await rbi_server._authenticate_ws(ws))

        with patch.object(rbi_server, "_RBI_SECRET", SECRET), patch.dict(
            os.environ,
            {"READER_RBI_ALLOWED_ORIGINS": "http://reader.test"},
            clear=False,
        ), patch.object(
            rbi_server,
            "_TICKET_NONCES",
            rbi_access.RbiTicketNonceRegistry(),
        ):
            async with websockets.serve(auth_once, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}",
                    origin="http://reader.test",
                ) as client:
                    await client.send(json.dumps({"cmd": "auth", "ticket": token}))
                    ready = json.loads(await client.recv())
                    self.assertEqual(ready["t"], "ready")
        self.assertEqual(len(identities), 1)
        self.assertIsNotNone(identities[0])
        self.assertEqual(
            identities[0].identity,
            rbi_access.RbiIdentity(12),
        )


class RbiBrowserLaunchContractTest(unittest.TestCase):
    def test_browser_security_flags_and_startup_order_are_shared(self) -> None:
        live_source = (
            ROOT / "_server_deploy" / "rbi_server.py"
        ).read_text("utf-8")
        demo_source = (ROOT / "scripts" / "rbi_render.py").read_text("utf-8")
        for source in (live_source, demo_source):
            self.assertNotIn("--no-sandbox", source)
            self.assertNotIn("IsolateOrigins", source)
            self.assertNotIn("site-per-process", source)

        live_context = live_source[
            live_source.index("async def _context("):
            live_source.index("_RECORD_JS =")
        ]
        self.assertLess(
            live_context.index("offline=True"),
            live_context.index('new_ctx.route("**/*"'),
        )
        self.assertLess(
            live_context.index('new_ctx.route("**/*"'),
            live_context.index("new_ctx.route_web_socket"),
        )
        self.assertLess(
            live_context.index("new_ctx.route_web_socket"),
            live_context.index("startup_page.close"),
        )
        self.assertLess(
            live_context.index("startup_page.close"),
            live_context.index("new_ctx.set_offline(False)"),
        )
        self.assertIn("acquire_rbi_profile_lease", live_context)
        self.assertIn("_CTX_LEASES[uid] = lease", live_context)

        demo_render = demo_source[
            demo_source.index("def render("):
            demo_source.index('if __name__ == "__main__":')
        ]
        self.assertLess(
            demo_render.index("offline=True"),
            demo_render.index('ctx.route("**/*"'),
        )
        self.assertLess(
            demo_render.index('ctx.route("**/*"'),
            demo_render.index("ctx.route_web_socket"),
        )
        self.assertLess(
            demo_render.index("ctx.route_web_socket"),
            demo_render.index("startup_page.close"),
        )
        self.assertLess(
            demo_render.index("startup_page.close"),
            demo_render.index("ctx.set_offline(False)"),
        )
        self.assertLess(
            demo_render.index("ctx.set_offline(False)"),
            demo_render.index("ctx.new_page()"),
        )


class RbiPageContractTest(unittest.TestCase):
    def test_page_authenticates_with_ticket_and_never_sends_uid(self) -> None:
        template = (ROOT / "_server_deploy" / "templates" / "rbi_live.html").read_text(
            "utf-8"
        )
        self.assertIn("cmd:'auth'", template)
        self.assertIn("/pdf/api/rbi-ticket", template)
        self.assertNotIn("CFG.uid", template)
        self.assertNotIn("uid:CFG", template)


class RbiFlaskTicketIntegrationTest(unittest.TestCase):
    @unittest.skip(
        "historical RBI ticket integration retained for recovery; issuance retired 2026-07-25"
    )
    def test_page_and_reconnect_ticket_are_bound_to_current_session(self) -> None:
        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            from unittest.mock import patch

            root = Path({str(ROOT)!r})
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module
            import html_reader as reader
            from rbi_access import verify_rbi_ticket

            with module.app.app_context():
                db = module.get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("rbi-user", "x", "user"),
                )
                db.commit()
                uid = db.execute(
                    "SELECT id FROM users WHERE username='rbi-user'"
                ).fetchone()["id"]

            client = module.app.test_client()
            with client.session_transaction() as browser_session:
                browser_session["user_id"] = uid

            with patch.object(reader, "_url_safe", return_value=""):
                page = client.get(
                    "/pdf/web/rbi-live?url=https%3A%2F%2Fpublic.example%2F"
                )
            assert page.status_code == 200
            assert "rbit-v1." not in page.get_data(as_text=True)

            no_header = client.post("/pdf/api/rbi-ticket")
            assert no_header.status_code == 403
            refreshed = client.post(
                "/pdf/api/rbi-ticket",
                json={{"uid": uid + 1}},
                headers={{"X-BW-RBI": "1"}},
            )
            assert refreshed.status_code == 200
            data = refreshed.get_json()
            assert verify_rbi_ticket(
                "z" * 64,
                data["ticket"],
                expected_user_id=uid,
            ) is not None
            assert verify_rbi_ticket(
                "z" * 64,
                data["ticket"],
                expected_user_id=uid + 1,
            ) is None
            assert "no-store" in refreshed.headers["Cache-Control"]
            reconnected = client.post(
                "/pdf/api/rbi-ticket",
                headers={{"X-BW-RBI": "1"}},
            )
            assert reconnected.status_code == 200
            assert reconnected.get_json()["ticket"] != data["ticket"]
            assert verify_rbi_ticket(
                "z" * 64,
                reconnected.get_json()["ticket"],
                expected_user_id=uid,
            ) is not None
            with module.app.app_context():
                db = module.get_db()
                db.execute("DELETE FROM users WHERE id=?", (uid,))
                db.commit()
            stale_session = client.post(
                "/pdf/api/rbi-ticket",
                headers={{"X-BW-RBI": "1"}},
            )
            assert stale_session.status_code == 403

            anonymous = module.app.test_client()
            denied = anonymous.post(
                "/pdf/api/rbi-ticket",
                headers={{"X-BW-RBI": "1"}},
            )
            assert denied.status_code in (302, 401, 403)
            """
        )
        with tempfile.TemporaryDirectory(prefix="bw-rbi-flask-") as data:
            env = os.environ.copy()
            env.update(
                SECRET_KEY="rbi-session-test-secret",
                READER_RBI_SECRET="z" * 64,
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
