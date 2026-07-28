"""Account-isolation contracts for browser material, cookies, and HTML marks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "_server_deploy"), str(ROOT / "scripts")]

import attention_profile  # noqa: E402
import build_search_index  # noqa: E402
import html_reader  # noqa: E402
from reader_sidecar_store import ReaderStorageIdentity, SidecarStore  # noqa: E402
from web_cache_store import (  # noqa: E402
    iter_account_web_cache,
    legacy_account_cache_path,
    legacy_shared_cache_path,
    read_web_cache,
    write_web_cache,
)
from web_cookie_store import (  # noqa: E402
    cookie_values_for_url,
    load_cookie_store,
    playwright_cookies_for_user,
    put_cookie_header,
)


class ExactHostCookieStoreTest(unittest.TestCase):
    def test_legacy_cookie_is_exact_host_https_only_and_secure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "7.json").write_text(
                json.dumps({"example.com": {"sid": "legacy-secret"}}),
                "utf-8",
            )
            self.assertEqual(
                cookie_values_for_url(root, 7, "https://example.com/account"),
                {"sid": "legacy-secret"},
            )
            self.assertEqual(
                cookie_values_for_url(root, 7, "https://www.example.com/account"),
                {},
            )
            self.assertEqual(
                cookie_values_for_url(root, 7, "http://example.com/account"),
                {},
            )
            browser = playwright_cookies_for_user(
                root,
                7,
                "https://example.com/account",
            )
            self.assertEqual(browser[0]["url"], "https://example.com/")
            self.assertTrue(browser[0]["secure"])
            self.assertNotIn("domain", browser[0])

    def test_new_cookie_header_uses_v2_without_parent_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                put_cookie_header(root, 9, ".app.example.com", "a=1; b=2"),
                2,
            )
            stored = json.loads((root / "9.json").read_text("utf-8"))
            self.assertEqual(stored["version"], 2)
            self.assertEqual(set(stored["hosts"]), {"app.example.com"})
            self.assertEqual(
                cookie_values_for_url(root, 9, "https://app.example.com/"),
                {"a": "1", "b": "2"},
            )
            self.assertEqual(
                cookie_values_for_url(root, 9, "https://cdn.example.com/"),
                {},
            )
            self.assertEqual(load_cookie_store(root, 9), stored)

    def test_ipv6_playwright_cookie_url_uses_brackets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put_cookie_header(root, 10, "2001:db8::1", "sid=v6")
            browser = playwright_cookies_for_user(
                root,
                10,
                "https://[2001:db8::1]:8443/account",
            )
            self.assertEqual(browser[0]["url"], "https://[2001:db8::1]:8443/")


class WebCacheIsolationTest(unittest.TestCase):
    def test_reads_and_enumeration_require_one_explicit_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = "https://private.example/article"
            a = write_web_cache(root, 7, url, title="A", text="A private")
            b = write_web_cache(root, 8, url, title="B", text="B private")
            self.assertNotEqual(a, b)
            self.assertEqual(read_web_cache(root, 7, url)[0]["text"], "A private")
            self.assertEqual(read_web_cache(root, 8, url)[0]["text"], "B private")
            self.assertEqual(
                [(uid, data["text"]) for uid, _path, data in iter_account_web_cache(
                    root,
                    user_id=7,
                )],
                [("7", "A private")],
            )
            with self.assertRaises(TypeError):
                list(iter_account_web_cache(root))  # type: ignore[call-arg]
            with self.assertRaises(ValueError):
                list(iter_account_web_cache(root, user_id=""))

    def test_legacy_records_are_read_only_and_never_cross_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = "https://legacy.example/article"
            account_legacy = legacy_account_cache_path(root, 7, url)
            account_legacy.write_text(
                json.dumps({"url": url, "title": "old A", "text": "A only"}),
                "utf-8",
            )
            self.assertEqual(read_web_cache(root, 7, url)[0]["text"], "A only")
            self.assertIsNone(read_web_cache(root, 8, url)[0])

            account_legacy.unlink()
            shared = legacy_shared_cache_path(root, url)
            shared.write_text(
                json.dumps({"url": url, "title": "shared", "text": "assigned"}),
                "utf-8",
            )
            with patch.dict(
                os.environ,
                {"READER_LEGACY_WEB_CACHE_OWNER_UID": "7"},
                clear=False,
            ):
                self.assertEqual(read_web_cache(root, 7, url)[0]["text"], "assigned")
                self.assertIsNone(read_web_cache(root, 8, url)[0])
            self.assertTrue(shared.exists())


class HtmlHighlightIsolationTest(unittest.TestCase):
    def test_sidecars_are_partitioned_and_legacy_needs_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with patch.object(html_reader, "_HTML_HL_DIR", state / "legacy"), patch.object(
                html_reader,
                "_HTML_HL_USER_DIR",
                state / "by-user",
            ), patch.object(
                html_reader,
                "_HTML_HL_ACCOUNT_PATH",
                None,
            ):
                html_reader._html_hl_save(
                    "web:https://private.example/a",
                    [{"id": "a", "text": "A"}],
                    user_id=7,
                )
                html_reader._html_hl_save(
                    "web:https://private.example/a",
                    [{"id": "b", "text": "B"}],
                    user_id=8,
                )
                self.assertEqual(
                    html_reader._html_hl_load(
                        "web:https://private.example/a",
                        user_id=7,
                    )[0]["text"],
                    "A",
                )
                self.assertEqual(
                    html_reader._html_hl_load(
                        "web:https://private.example/a",
                        user_id=8,
                    )[0]["text"],
                    "B",
                )

                legacy_rel = "资源/web/legacy.html"
                legacy_path = html_reader._html_hl_path(legacy_rel, legacy=True)
                assert legacy_path is not None
                legacy_path.parent.mkdir(parents=True)
                legacy_path.write_text(
                    json.dumps([{"id": "old", "text": "assigned legacy"}]),
                    "utf-8",
                )
                self.assertEqual(
                    html_reader._html_hl_load(legacy_rel, user_id=7),
                    [],
                )
                with patch.dict(
                    os.environ,
                    {"READER_LEGACY_HTML_HIGHLIGHT_OWNER_UID": "7"},
                    clear=False,
                ):
                    self.assertEqual(
                        html_reader._html_hl_load(legacy_rel, user_id=7)[0]["text"],
                        "assigned legacy",
                    )
                    self.assertEqual(
                        html_reader._html_hl_load(legacy_rel, user_id=8),
                        [],
                    )

    def test_production_path_uses_claimed_account_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            sidecars = root / "reader-sidecars"
            rel = "资源/web/claimed.html"
            name = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16] + ".json"
            source = legacy / "html-highlights" / name
            source.parent.mkdir(parents=True)
            source_payload = json.dumps(
                [{"id": "old", "text": "claimed"}],
                ensure_ascii=False,
            )
            source.write_text(source_payload, "utf-8")
            identity = ReaderStorageIdentity(
                user_id=7,
                storage_namespace="acct-v1-" + ("a" * 64),
            )
            store = SidecarStore(sidecars, legacy, lambda owner: owner == identity)

            def account_path(*parts: str) -> Path:
                return store.account_path(identity, *parts)

            with patch.object(
                html_reader,
                "_HTML_HL_ACCOUNT_PATH",
                account_path,
            ):
                self.assertEqual(
                    html_reader._html_hl_load(rel)[0]["text"],
                    "claimed",
                )
                expected = (
                    sidecars
                    / "by-user"
                    / "7"
                    / "html-highlights"
                    / name
                )
                self.assertEqual(html_reader._html_hl_path(rel), expected)
                html_reader._html_hl_save(
                    rel,
                    [{"id": "new", "text": "account only"}],
                )
                self.assertEqual(
                    json.loads(expected.read_text("utf-8"))[0]["text"],
                    "account only",
                )
                with self.assertRaises(PermissionError):
                    html_reader._html_hl_path(rel, user_id=7)

            # Legacy input is the immutable migration source, not a live store.
            self.assertEqual(source.read_text("utf-8"), source_payload)

    def test_production_identity_failure_never_falls_back_to_shared_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "html-highlights"
            rel = "资源/web/private.html"
            name = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16] + ".json"
            source = legacy / name
            source.parent.mkdir(parents=True)
            source.write_text(
                json.dumps([{"id": "shared", "text": "must not leak"}]),
                "utf-8",
            )

            def reject(*_parts: str) -> Path:
                raise PermissionError("missing verified account")

            with patch.object(html_reader, "_HTML_HL_DIR", legacy), patch.object(
                html_reader,
                "_HTML_HL_ACCOUNT_PATH",
                reject,
            ), patch.dict(
                os.environ,
                {"READER_LEGACY_HTML_HIGHLIGHT_OWNER_UID": "7"},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "missing verified account",
                ):
                    html_reader._html_hl_load(rel)
                with self.assertRaisesRegex(
                    PermissionError,
                    "missing verified account",
                ):
                    html_reader._html_hl_save(
                        rel,
                        [{"id": "new", "text": "must not write"}],
                    )


class PrivateCacheConsumerTest(unittest.TestCase):
    def test_attention_follows_claimed_sidecar_instead_of_frozen_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            state = project / "state"
            vault = base / "vault"
            sidecars = base / "reader-sidecars"
            rel = "claimed.pdf"
            vault.mkdir(parents=True)
            (vault / rel).write_bytes(b"placeholder")
            name = hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".json"
            legacy_path = state / "pdf-highlights" / name
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(
                json.dumps({
                    "pdf_rel": rel,
                    "highlights": [{
                        "id": "legacy",
                        "text": "旧数据冻结内容",
                        "time": 1,
                    }],
                }),
                "utf-8",
            )
            identity = ReaderStorageIdentity(
                7,
                "acct-v1-" + ("7" * 64),
            )
            store = SidecarStore(
                sidecars,
                state,
                lambda owner: owner == identity,
            )
            account_path = store.account_path(
                identity,
                "pdf-highlights",
                name,
            )
            account_path.write_text(
                json.dumps({
                    "pdf_rel": rel,
                    "highlights": [{
                        "id": "current",
                        "text": "向量空间定理证明",
                        "time": 2,
                    }],
                }),
                "utf-8",
            )
            attention_dir = base / "attention"
            with patch.object(
                attention_profile,
                "PROJECT_DIR",
                project,
            ), patch.object(
                attention_profile,
                "STATE",
                state,
            ), patch.object(
                attention_profile,
                "VAULT_ROOT",
                vault,
            ), patch.object(
                attention_profile,
                "ATT_DIR",
                attention_dir,
            ), patch.object(
                attention_profile,
                "DB",
                attention_dir / "events.db",
            ), patch.dict(
                os.environ,
                {
                    "READER_SIDECAR_ROOT": str(sidecars),
                    "READER_ATTENTION_OWNER_UID": "",
                },
                clear=False,
            ):
                db = attention_profile._db()
                try:
                    attention_profile.import_highlights(db)
                    rows = db.execute(
                        "SELECT text, uid FROM events "
                        "WHERE channel='highlight'"
                    ).fetchall()
                finally:
                    db.close()
            self.assertEqual(rows, [("向量空间定理证明", "7")])
            legacy_payload = json.loads(legacy_path.read_text("utf-8"))
            self.assertEqual(
                legacy_payload["highlights"][0]["text"],
                "旧数据冻结内容",
            )

    def test_shared_search_selects_one_owner_and_never_merges_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            cache = project / "state" / "web-cache"
            url_a = "https://a.example/private"
            url_b = "https://b.example/private"
            write_web_cache(cache, 7, url_a, title="A", text="A " * 100)
            with patch.object(build_search_index, "CLAUDE_DIR", project), patch.dict(
                os.environ,
                {"READER_SEARCH_OWNER_UID": ""},
                clear=False,
            ):
                self.assertEqual(
                    [item["rel"] for item in build_search_index._list_webs()],
                    ["web:" + url_a],
                )
                write_web_cache(cache, 8, url_b, title="B", text="B " * 100)
                self.assertEqual(build_search_index._list_webs(), [])
            with patch.object(build_search_index, "CLAUDE_DIR", project), patch.dict(
                os.environ,
                {"READER_SEARCH_OWNER_UID": "8"},
                clear=False,
            ):
                self.assertEqual(
                    [item["rel"] for item in build_search_index._list_webs()],
                    ["web:" + url_b],
                )

    def test_attention_page_reader_uses_selected_uid_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cache = state / "web-cache"
            url = "https://private.example/page"
            write_web_cache(cache, 7, url, title="A", text="A account text")
            write_web_cache(cache, 8, url, title="B", text="B account text")
            with patch.object(attention_profile, "STATE", state), patch.dict(
                os.environ,
                {"READER_ATTENTION_OWNER_UID": ""},
                clear=False,
            ):
                self.assertEqual(
                    attention_profile._page_text("web:" + url, 1),
                    "",
                )
            with patch.object(attention_profile, "STATE", state), patch.dict(
                os.environ,
                {"READER_ATTENTION_OWNER_UID": "7"},
                clear=False,
            ):
                self.assertEqual(
                    attention_profile._page_text("web:" + url, 1),
                    "A account text",
                )
                self.assertEqual(
                    attention_profile._page_text(
                        "web:" + url,
                        1,
                        user_id=8,
                    ),
                    "B account text",
                )

    def test_attention_material_detail_requires_and_passes_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cache = state / "web-cache"
            url = "https://private.example/material"
            write_web_cache(cache, 7, url, title="A title", text="A material body")
            write_web_cache(cache, 8, url, title="B title", text="B material body")
            with patch.object(attention_profile, "STATE", state), patch.object(
                html_reader,
                "WEB_CACHE_DIR",
                cache,
            ), patch.dict(
                os.environ,
                {"READER_ATTENTION_OWNER_UID": ""},
                clear=False,
            ):
                denied = attention_profile.read_material("web:" + url)
                self.assertIn("未选择账户", denied["error"])
            with patch.object(attention_profile, "STATE", state), patch.object(
                html_reader,
                "WEB_CACHE_DIR",
                cache,
            ), patch.dict(
                os.environ,
                {"READER_ATTENTION_OWNER_UID": "7"},
                clear=False,
            ):
                material = attention_profile.read_material("web:" + url)
                self.assertTrue(material["ok"])
                self.assertEqual(material["content"], "A material body")
                self.assertNotIn("B material body", material["content"])


if __name__ == "__main__":
    unittest.main()
