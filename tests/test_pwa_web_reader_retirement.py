"""Contracts for retiring PWA webpage parsing/proxy/RBI without deleting data."""

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

import html_reader  # noqa: E402


class LegacyRedirectValidationTest(unittest.TestCase):
    def test_only_plain_http_urls_are_handed_back_to_the_browser(self) -> None:
        valid = "https://example.com/article?q=reader#part"
        self.assertEqual(html_reader._retired_web_redirect_target(valid), valid)
        self.assertEqual(
            html_reader._retired_web_redirect_target("http://localhost:8080/a"),
            "http://localhost:8080/a",
        )
        for invalid in (
            "",
            "//example.com/a",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://user:secret@example.com/",
            "https://example.com\\@evil.test/",
            "https://example.com/\nLocation:https://evil.test/",
            "https:///missing-host",
            "https://example.com:99999/",
        ):
            self.assertEqual(
                html_reader._retired_web_redirect_target(invalid),
                "",
                invalid,
            )

    def test_bookshelf_has_no_pwa_web_reader_entry(self) -> None:
        source = (
            ROOT / "_server_deploy" / "templates" / "pdf_index.html"
        ).read_text("utf-8")
        self.assertNotIn('href="/pdf/web"', source)
        self.assertNotIn("/pdf/api/web-fetch", source)
        self.assertNotIn("function openWebPage", source)

    def test_retired_translation_cache_has_no_runtime_reader_or_writer(self) -> None:
        source = (ROOT / "_server_deploy" / "html_reader.py").read_text("utf-8")
        for retired_name in (
            "WEBTR_DIR",
            "_WEBTR_TTL",
            "_WEBTR_MAX_ITEMS",
            "def _webtr_path",
            "def _webtr_get",
            "def _webtr_put",
        ):
            self.assertNotIn(retired_name, source)


class PwaWebReaderRetirementIntegrationTest(unittest.TestCase):
    def test_routes_are_gone_while_book_and_shared_services_remain(self) -> None:
        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            from unittest.mock import patch
            from urllib.parse import quote

            root = Path({str(ROOT)!r})
            project = Path(__import__("os").environ["CLAUDE_PROJECT"])
            vault = Path(__import__("os").environ["OBSIDIAN_VAULT"])
            (vault / "books").mkdir(parents=True, exist_ok=True)
            (vault / "books" / "local.md").write_text(
                "# Local book\\n\\nThis remains a real local book.",
                "utf-8",
            )
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module
            import html_reader as reader
            from web_cache_store import write_web_cache
            from web_proxy_cap import issue_web_proxy_cap

            with module.app.app_context():
                db = module.get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    ("retired-web-user", "x", "user"),
                )
                db.commit()
                uid = int(db.execute(
                    "SELECT id FROM users WHERE username='retired-web-user'"
                ).fetchone()["id"])

            client = module.app.test_client()
            with client.session_transaction() as browser_session:
                browser_session["user_id"] = uid
                browser_session["username"] = "retired-web-user"

            def retired(response):
                assert response.status_code == 410, (
                    response.status_code,
                    response.headers.get("Location"),
                    response.get_data(as_text=True)[:200],
                )
                payload = response.get_json()
                assert payload["error"] == "pwa_web_reader_retired"
                assert payload["replacement"] == "browser_extension"
                assert "no-store" in response.headers.get("Cache-Control", "")

            # Old portal/bookmark paths do not render a webpage reader.
            for path in ("/pdf/web", "/pdf/html/view?file=__web__"):
                response = client.get(path)
                assert response.status_code == 302
                assert response.headers["Location"].endswith("/pdf/")

            handoff = client.get(
                "/pdf/web/live?url="
                + quote("https://example.com/article?q=one#part", safe="")
            )
            assert handoff.status_code == 302
            assert handoff.headers["Location"] == "https://example.com/article?q=one#part"
            assert "no-referrer" == handoff.headers.get("Referrer-Policy")
            for bad in (
                "javascript:alert(1)",
                "file:///etc/passwd",
                "https://user:secret@example.com/",
            ):
                assert client.get(
                    "/pdf/web/live?url=" + quote(bad, safe="")
                ).status_code == 400

            capability = issue_web_proxy_cap(
                module._reader_web_proxy_secret(),
                uid,
                scope_url="https://example.com",
            )
            cap = quote(capability, safe="")
            legacy_trcache = project / "state" / "web-trcache" / "legacy.json"
            legacy_trcache.parent.mkdir(parents=True, exist_ok=True)
            legacy_trcache.write_text('{{"sentinel":"unchanged"}}', "utf-8")
            with patch.object(
                reader, "_fetch_web_page", side_effect=AssertionError("web fetch ran")
            ) as web_fetch, patch.object(
                reader, "_proxy_page", side_effect=AssertionError("proxy ran")
            ) as proxy_page, patch.object(
                reader, "_px_open", side_effect=AssertionError("upstream opened")
            ) as upstream:
                for path in (
                    "/pdf/web/proxy?url=https%3A%2F%2Fexample.com",
                    "/pdf/web/frame?url=https%3A%2F%2Fexample.com",
                    "/pdf/web/rbi?url=https%3A%2F%2Fexample.com",
                    "/pdf/web/rbi-live?url=https%3A%2F%2Fexample.com",
                    "/pdf/web/p/https/example.com/article?__bwcap=" + cap,
                    "/pdf/web/r/https/example.com/app.js?__bwcap=" + cap,
                    "/pdf/web/res?url=https%3A%2F%2Fexample.com%2Fa.js&__bwcap=" + cap,
                ):
                    retired(client.get(path))
                retired(client.post(
                    "/pdf/api/web-fetch",
                    json={{"url": "https://example.com"}},
                ))
                retired(client.get("/pdf/api/web-cookie"))
                retired(client.post(
                    "/pdf/api/web-cookie",
                    json={{"domain": "example.com", "cookie": "secret=value"}},
                ))
                retired(client.post(
                    "/pdf/api/rbi-ticket",
                    headers={{"X-BW-RBI": "1"}},
                ))
                retired(client.get("/pdf/api/web-trcache"))
                assert not web_fetch.called
                assert not proxy_page.called
                assert not upstream.called
                assert legacy_trcache.read_text("utf-8") == '{{"sentinel":"unchanged"}}'

            # The real local-book reader and shared extension network services remain.
            local_book = client.get("/pdf/html/view?file=books%2Flocal.md")
            assert local_book.status_code == 200
            assert "Local book" in local_book.get_data(as_text=True)
            assert client.get(
                "/pdf/api/html-highlights?file=books%2Flocal.md"
            ).status_code == 200
            assert client.post(
                "/pdf/api/web-translate", json={{"texts": []}}
            ).status_code == 200
            assert client.post(
                "/pdf/api/web-vocab", json={{"texts": []}}
            ).status_code == 200
            # web:<URL> remains a read-only legacy-cache identity. Cache misses
            # neither touch the network nor create a new record.
            cached_url = "https://cached.example/article"
            write_web_cache(
                reader.WEB_CACHE_DIR,
                uid,
                cached_url,
                title="Legacy cached title",
                text="Legacy cached body",
                timestamp=1,
            )
            cached = reader.web_material("web:" + cached_url, user_id=uid)
            assert cached["text"] == "Legacy cached body"
            missing_url = "https://missing.example/article"
            missing_path = reader._web_cache_path(missing_url, user_id=uid)
            assert not missing_path.exists()
            with patch.object(
                reader, "_public_http_text", side_effect=AssertionError("network fallback ran")
            ) as network:
                missing = reader.web_material("web:" + missing_url, user_id=uid)
            assert missing["url"] == missing_url
            assert missing["text"] == ""
            assert "浏览器扩展" in missing["error"]
            assert not network.called
            assert not missing_path.exists()

            assert not (project / "state" / "web-cookies").exists()
            assert not (project / "state" / "rbi-profiles").exists()
            """
        )
        with tempfile.TemporaryDirectory(prefix="bw-pwa-web-retired-") as data:
            root = Path(data)
            project = root / "project"
            vault = root / "vault"
            project.mkdir()
            vault.mkdir()
            env = os.environ.copy()
            env.update(
                SECRET_KEY="pwa-web-retirement-test-secret",
                READER_WEB_PROXY_SECRET="retired-transport-test-secret",
                WEBAPP_DATA=str(root / "app-data"),
                CLAUDE_PROJECT=str(project),
                OBSIDIAN_VAULT=str(vault),
                SESSION_COOKIE_SECURE="0",
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=(result.stdout + "\n" + result.stderr).strip(),
        )


if __name__ == "__main__":
    unittest.main()
