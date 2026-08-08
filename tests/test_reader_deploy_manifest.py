from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from scripts import reader_deploy_manifest as manifest


class ReaderDeployManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = manifest.manifest_entries()

    def test_manifest_targets_are_unique_safe_and_source_backed(self):
        target_keys = [
            (entry.target_group, entry.target_rel)
            for entry in self.entries
        ]
        self.assertEqual(len(target_keys), len(set(target_keys)))
        self.assertEqual(
            {entry.target_group for entry in self.entries},
            {"webapp", "static", "kg_runtime", "systemd"},
        )
        self.assertTrue(
            all(entry.source_path().is_file() for entry in self.entries)
        )

    def test_reader_modules_and_legacy_recovery_assets_are_explicit(self):
        webapp_targets = {
            entry.target_rel
            for entry in self.entries
            if entry.target_group == "webapp"
        }
        self.assertTrue(
            {
                "assistant.py",
                "tool_registry.py",
                "voice.py",
                "voice_realtime_relay.py",
                "task_runtime.py",
                "kg_runtime.py",
                "control.py",
                "skilltree.py",
                "card_improvement_runtime.py",
                "card_improvement_service.py",
                "card_candidate_service.py",
                "reader_book_library.py",
                "favorites_reader.py",
                "reader_sidecar_store.py",
                "reader_sync_relay.py",
                "reader_events.py",
                "book_toc.py",
                "grammar_reader.py",
                "epub_assistant.py",
                "vbook_route_policy.py",
                "rbi_access.py",
                "rbi_server.py",
                "web_translate_protocol.py",
                "templates/rbi_live.html",
                "templates/web_live.html",
            }.issubset(webapp_targets)
        )
        self.assertNotIn("mcp_server.py", webapp_targets)
        self.assertFalse(
            any(
                entry.source_rel.endswith("/mcp_server.py")
                for entry in self.entries
            )
        )

    def test_external_shared_modules_have_one_source_and_one_exact_alias(self):
        translate_matches = [
            entry
            for entry in self.entries
            if entry.target_group == "webapp"
            and entry.target_rel == "web_translate_protocol.py"
        ]
        self.assertEqual(len(translate_matches), 1)
        translate = translate_matches[0]
        self.assertEqual(translate.source_rel, "scripts/vocab/translate.py")
        self.assertEqual(translate.policy, manifest.POLICY_EXACT)
        self.assertTrue(translate.source_path().is_file())
        self.assertFalse(
            (manifest.ROOT / "_server_deploy" / "web_translate_protocol.py")
            .exists()
        )
        service_matches = [
            entry
            for entry in self.entries
            if entry.target_group == "webapp"
            and entry.target_rel == "card_improvement_service.py"
        ]
        self.assertEqual(len(service_matches), 1)
        service = service_matches[0]
        self.assertEqual(
            service.source_rel,
            "_client/core/card_improvement_service.py",
        )
        self.assertEqual(service.policy, manifest.POLICY_EXACT)
        self.assertTrue(service.source_path().is_file())
        self.assertFalse(
            (manifest.ROOT / "_server_deploy" / "card_improvement_service.py")
            .exists()
        )
        self.assertEqual(
            set(manifest.EXTERNAL_DEPLOY_ENTRIES),
            {translate, service},
        )

    def test_kg_runtime_inventory_is_explicit_complete_and_exact(self):
        declared_kg = set(manifest.KG_RUNTIME_KG_SOURCES)
        actual_kg = {
            path.relative_to(manifest.ROOT).as_posix()
            for path in (manifest.ROOT / "scripts" / "kg").glob("*.py")
            if path.is_file()
        }
        self.assertEqual(declared_kg, actual_kg)

        runtime_entries = tuple(
            entry
            for entry in self.entries
            if entry.target_group == "kg_runtime"
        )
        self.assertEqual(
            {entry.source_rel for entry in runtime_entries},
            set(manifest.KG_RUNTIME_SOURCE_FILES),
        )
        self.assertEqual(
            {entry.target_rel for entry in runtime_entries},
            set(manifest.KG_RUNTIME_SOURCE_FILES),
        )
        self.assertTrue(
            all(
                entry.source_rel == entry.target_rel
                and entry.policy == manifest.POLICY_EXACT
                for entry in runtime_entries
            )
        )
        self.assertEqual(
            manifest.TARGET_ROOTS["kg_runtime"],
            Path("/home/bwicarus/reader-runtime/kg/current"),
        )

        # Freeze the executable closure used by the KG jobs.  These are not
        # mutable data paths and cannot silently fall back to the checkout.
        self.assertTrue(
            {
                "scripts/config.py",
                "scripts/attention_profile.py",
                "scripts/lib/__init__.py",
                "scripts/lib/book_groups.py",
                "scripts/lib/claude_quota.py",
                "_client/core/ai_backends.py",
                "_server_deploy/reader_sidecar_store.py",
                "_server_deploy/skilltree.py",
                "_server_deploy/web_cache_store.py",
                "scripts/quick_sync.py",
                "scripts/concept_graph_daily.py",
            }.issubset(set(manifest.KG_RUNTIME_SOURCE_FILES))
        )

    def test_runtime_and_systemd_required_entries_cannot_be_omitted(self):
        required_entries = (
            manifest.KG_RUNTIME_DEPLOY_ENTRIES[0],
            manifest.SYSTEMD_DEPLOY_ENTRIES[0],
            next(
                entry
                for entry in self.entries
                if entry.target_group == "webapp"
                and entry.target_rel == "kg_runtime.py"
            ),
            next(
                entry
                for entry in self.entries
                if entry.target_group == "webapp"
                and entry.target_rel == "card_candidate_service.py"
            ),
        )
        for removed in required_entries:
            with self.subTest(removed=removed):
                changed = tuple(
                    entry for entry in self.entries if entry != removed
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "missing required exact entries",
                ):
                    manifest.validate_entries(
                        changed,
                        require_sources=False,
                    )

    def test_review_candidate_lazy_route_dependency_is_atomic(self):
        pdf_reader = (
            manifest.ROOT / "_server_deploy" / "pdf_reader.py"
        ).read_text("utf-8")
        self.assertIn(
            "from card_candidate_service import (",
            pdf_reader,
        )
        matches = [
            entry
            for entry in self.entries
            if entry.target_group == "webapp"
            and entry.target_rel == "card_candidate_service.py"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            matches[0].source_rel,
            "_server_deploy/card_candidate_service.py",
        )
        self.assertEqual(matches[0].policy, manifest.POLICY_EXACT)

    def test_runtime_and_systemd_entries_reject_non_exact_or_retargeting(self):
        cases = (
            replace(
                manifest.KG_RUNTIME_DEPLOY_ENTRIES[0],
                policy=manifest.POLICY_READER_GIT_STAMP,
            ),
            replace(
                manifest.KG_RUNTIME_DEPLOY_ENTRIES[0],
                target_rel="scripts/kg/other.py",
            ),
            replace(
                manifest.SYSTEMD_DEPLOY_ENTRIES[0],
                target_rel="other.service",
            ),
            replace(
                manifest.SYSTEMD_DEPLOY_ENTRIES[0],
                target_group="webapp",
            ),
        )
        originals = (
            manifest.KG_RUNTIME_DEPLOY_ENTRIES[0],
            manifest.KG_RUNTIME_DEPLOY_ENTRIES[0],
            manifest.SYSTEMD_DEPLOY_ENTRIES[0],
            manifest.SYSTEMD_DEPLOY_ENTRIES[0],
        )
        for original, changed_entry in zip(originals, cases):
            with self.subTest(changed=changed_entry):
                changed = tuple(
                    changed_entry if entry == original else entry
                    for entry in self.entries
                )
                with self.assertRaises(ValueError):
                    manifest.validate_entries(
                        changed,
                        require_sources=False,
                    )

    def test_service_units_are_exact_manifest_rows_and_use_stable_copies(self):
        unit_entries = {
            entry.target_rel: entry
            for entry in self.entries
            if entry.target_group == "systemd"
        }
        self.assertEqual(
            set(unit_entries),
            {
                "voice-rt.service",
                "bwicarus-quick-sync.service",
                "bwicarus-quick-sync.timer",
                "bwicarus-daily.service",
                "bwicarus-daily.timer",
                "concept-graph.service",
                "concept-graph.timer",
            },
        )
        self.assertTrue(
            all(
                entry.policy == manifest.POLICY_EXACT
                and entry.target_path()
                == Path("/etc/systemd/system") / entry.target_rel
                for entry in unit_entries.values()
            )
        )

        voice = unit_entries["voice-rt.service"].source_path().read_text(
            "utf-8"
        )
        quick = unit_entries[
            "bwicarus-quick-sync.service"
        ].source_path().read_text("utf-8")
        concept = unit_entries[
            "concept-graph.service"
        ].source_path().read_text("utf-8")
        quick_timer = unit_entries[
            "bwicarus-quick-sync.timer"
        ].source_path().read_text("utf-8")
        daily_timer = unit_entries[
            "bwicarus-daily.timer"
        ].source_path().read_text("utf-8")
        self.assertIn(
            "/home/bwicarus/webapp/voice_realtime_relay.py",
            voice,
        )
        self.assertNotIn(
            "/home/bwicarus/claude/_server_deploy/"
            "voice_realtime_relay.py",
            voice,
        )
        self.assertIn(
            "/home/bwicarus/reader-runtime/kg/current/"
            "scripts/quick_sync.py",
            quick,
        )
        self.assertIn(
            "/home/bwicarus/reader-runtime/kg/current/"
            "scripts/concept_graph_daily.py",
            concept,
        )
        self.assertNotIn("Requires=bwicarus-quick-sync.service", quick_timer)
        self.assertNotIn("Requires=bwicarus-daily.service", daily_timer)
        self.assertNotIn(
            "/home/bwicarus/claude/scripts/quick_sync.py",
            quick,
        )
        self.assertNotIn(
            "/home/bwicarus/claude/scripts/concept_graph_daily.py",
            concept,
        )

    def test_external_source_exceptions_are_exact(self):
        for entry in manifest.EXTERNAL_DEPLOY_ENTRIES:
            cases = (
                replace(entry, source_rel="scripts/vocab/other.py"),
                replace(entry, target_rel="other.py"),
                replace(entry, target_group="static"),
                replace(entry, policy=manifest.POLICY_READER_GIT_STAMP),
            )
            for changed_entry in cases:
                with self.subTest(original=entry, changed=changed_entry):
                    changed = tuple(
                        changed_entry if item == entry else item
                        for item in self.entries
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "outside _server_deploy",
                    ):
                        manifest.validate_entries(
                            changed,
                            require_sources=False,
                        )

    def test_duplicate_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate production target"):
            manifest.validate_entries(
                (*self.entries, self.entries[0]),
                require_sources=False,
            )

    def test_unsafe_or_out_of_scope_paths_are_rejected(self):
        cases = (
            (
                replace(self.entries[0], source_rel="../outside.py"),
                "unsafe source path",
            ),
            (
                replace(self.entries[0], source_rel="scripts/other.py"),
                "outside _server_deploy",
            ),
            (
                replace(self.entries[0], target_rel="../outside.py"),
                "unsafe target path",
            ),
            (
                replace(self.entries[0], target_rel=r"nested\\outside.py"),
                "unsafe target path",
            ),
            (
                replace(self.entries[0], target_group="templates"),
                "unknown target group",
            ),
        )
        for bad_entry, message in cases:
            with self.subTest(bad_entry=bad_entry):
                changed = (bad_entry, *self.entries[1:])
                with self.assertRaisesRegex(ValueError, message):
                    manifest.validate_entries(
                        changed,
                        require_sources=False,
                    )

    def test_stamp_policy_is_only_allowed_for_reader_bundle(self):
        exact_entry = next(
            entry
            for entry in self.entries
            if entry.policy == manifest.POLICY_EXACT
        )
        changed = (
            replace(
                exact_entry,
                policy=manifest.POLICY_READER_GIT_STAMP,
            ),
            *(
                entry
                for entry in self.entries
                if entry is not exact_entry
            ),
        )
        with self.assertRaisesRegex(
            ValueError,
            "reader_git_stamp policy is only valid",
        ):
            manifest.validate_entries(changed, require_sources=False)

    def test_reader_bundle_requires_exactly_one_valid_stamp(self):
        entry = next(
            item
            for item in self.entries
            if item.policy == manifest.POLICY_READER_GIT_STAMP
        )
        source = b"reader source\n"
        valid = (
            source
            + b"\n;window.__READER_GIT="
            + b"'a4f85ef+dirty\xc2\xb70725-0254';\n"
        )
        self.assertTrue(entry.deployed_content_matches(source, valid))

        invalid_targets = (
            source,
            source + b"\n;window.__READER_GIT='anything';\n",
            source + b"\n;window.__READER_GIT='A4F85EF+dirty\xc2\xb70725-0254';\n",
            source + b"\n;window.__READER_GIT='a4f85ef+dirty\xc2\xb70725-2460';\n",
            valid + b"\n;window.__READER_GIT='a4f85ef+dirty\xc2\xb70725-0254';\n",
            valid + b"trailing",
        )
        for target in invalid_targets:
            with self.subTest(target=target[-80:]):
                self.assertFalse(
                    entry.deployed_content_matches(source, target)
                )

    def test_exact_policy_requires_byte_equality(self):
        entry = next(
            item
            for item in self.entries
            if item.policy == manifest.POLICY_EXACT
        )
        self.assertTrue(entry.deployed_content_matches(b"same", b"same"))
        self.assertFalse(
            entry.deployed_content_matches(b"same", b"same\n")
        )

if __name__ == "__main__":
    unittest.main()
