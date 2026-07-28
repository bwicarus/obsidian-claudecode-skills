import importlib.util
from collections import Counter
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_reader_network",
    ROOT / "scripts/audit_reader_network.py",
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


POLICIES = [
    {
        "id": "vocabulary.mastery.set",
        "matches": [{"path": "/pdf/api/vocab-mark", "methods": ["POST"]}],
        "ui": "local-immediate",
    },
    {
        "id": "dictionary.quick.read",
        "matches": [{"path": "/pdf/api/dict-quick", "methods": ["GET"]}],
        "ui": "cache-first",
    },
]


class ReaderNetworkAuditTests(unittest.TestCase):
    def _call_file(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.js"
            path.write_text(source, encoding="utf-8")
            return audit.find_calls(path, "fixture.js")

    def test_parser_ignores_comments_and_strings_and_reads_multiline_method(self):
        calls = self._call_file(
            """
            // fetch('/comment-only')
            const example = "fetch('/string-only')";
            fetch(
              '/pdf/api/vocab-mark',
              { headers: {}, method: 'POST' }
            );
            """
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url, "/pdf/api/vocab-mark")
        self.assertEqual(calls[0].method, "POST")

    def test_cache_first_and_local_immediate_calls_require_annotation(self):
        cached = self._call_file("fetch('/pdf/api/dict-quick?word=be');")
        self.assertEqual(
            [item.code for item in audit.audit_calls(cached, POLICIES)],
            ["cache-first-unannotated"],
        )
        cached_annotated = self._call_file(
            """
            // @interaction dictionary.quick.read
            fetch('/pdf/api/dict-quick?word=be');
            """
        )
        self.assertEqual(audit.audit_calls(cached_annotated, POLICIES), [])

        local = self._call_file(
            "fetch('/pdf/api/vocab-mark', {method:'POST'});"
        )
        issues = audit.audit_calls(local, POLICIES)
        self.assertEqual([item.code for item in issues], [
            "local-immediate-unannotated"
        ])

        annotated = self._call_file(
            """
            // @interaction vocabulary.mastery.set
            fetch('/pdf/api/vocab-mark', {method:'POST'});
            """
        )
        self.assertEqual(audit.audit_calls(annotated, POLICIES), [])

    def test_dynamic_and_unknown_literal_calls_are_debt(self):
        dynamic = self._call_file("fetch(makeEndpoint(), requestOptions);")
        self.assertEqual(
            audit.audit_calls(dynamic, POLICIES)[0].code,
            "dynamic-unannotated",
        )
        unknown = self._call_file("fetch('/new/unclassified');")
        self.assertEqual(
            audit.audit_calls(unknown, POLICIES)[0].code,
            "unclassified-literal",
        )

    def test_dynamic_call_can_be_bound_to_a_known_action(self):
        calls = self._call_file(
            """
            // @interaction vocabulary.mastery.set
            fetch(endpointForWord(word), requestOptions);
            """
        )
        self.assertEqual(audit.audit_calls(calls, POLICIES), [])

    def test_fetch_identifier_boundary_and_window_qualified_form(self):
        calls = self._call_file(
            """
            prefetch('/not-a-network-callee');
            window.fetch('/pdf/api/dict-quick');
            """
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].callee, "window.fetch")
        self.assertEqual(calls[0].url, "/pdf/api/dict-quick")

    def test_concatenated_url_is_dynamic_not_partial_literal(self):
        calls = self._call_file(
            "fetch('/pdf/api/entity/' + id, {method:'PATCH'});"
        )
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].url)
        self.assertEqual(
            audit.audit_calls(calls, POLICIES)[0].code,
            "dynamic-unannotated",
        )

    def test_one_annotation_cannot_cover_two_calls(self):
        calls = self._call_file(
            """
            // @interaction vocabulary.mastery.set
            fetch('/pdf/api/vocab-mark', {method:'POST'});
            fetch('/pdf/api/vocab-mark', {method:'POST'});
            """
        )
        self.assertEqual(
            [call.annotation for call in calls],
            ["vocabulary.mastery.set", None],
        )
        self.assertEqual(
            [item.code for item in audit.audit_calls(calls, POLICIES)],
            ["local-immediate-unannotated"],
        )

    def test_wrong_or_unknown_annotation_fails(self):
        mismatch = self._call_file(
            """
            // @interaction dictionary.quick.read
            fetch('/pdf/api/vocab-mark', {method:'POST'});
            """
        )
        self.assertEqual(
            audit.audit_calls(mismatch, POLICIES)[0].code,
            "annotation-mismatch",
        )
        unknown = self._call_file(
            """
            // @interaction made.up.action
            fetch(buildUrl());
            """
        )
        self.assertEqual(
            audit.audit_calls(unknown, POLICIES)[0].code,
            "annotation-unknown",
        )

    def test_baseline_allows_existing_count_but_rejects_one_more_copy(self):
        existing_calls = self._call_file("fetch(buildUrl());")
        existing_issues = audit.audit_calls(existing_calls, POLICIES)
        baseline = audit.issue_counts(existing_issues)
        new_issues, retired = audit.compare_baseline(existing_issues, baseline)
        self.assertEqual(new_issues, [])
        self.assertEqual(retired, 0)

        duplicated = existing_issues + existing_issues
        new_issues, retired = audit.compare_baseline(duplicated, baseline)
        self.assertEqual(len(new_issues), 1)
        self.assertEqual(retired, 0)

        new_issues, retired = audit.compare_baseline([], baseline)
        self.assertEqual(new_issues, [])
        self.assertEqual(retired, 1)

    def test_check_fails_when_baseline_has_retired_allowance(self):
        with (
            mock.patch.object(audit, "load_policies", return_value=[]),
            mock.patch.object(audit, "audit_paths", return_value=([], [], [])),
            mock.patch.object(
                audit,
                "read_baseline",
                return_value=Counter({"retired-fingerprint": 1}),
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(audit.main(["--check"]), 1)

    def test_default_discovery_excludes_generated_and_vendor_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "_server_deploy/static/pdf"
            source.mkdir(parents=True)
            (source / "canonical.js").write_text("fetch('/one')", encoding="utf-8")
            (source / "reader.js").write_text("fetch('/built')", encoding="utf-8")
            (source / "bundle.min.js").write_text("fetch('/min')", encoding="utf-8")
            vendor = source / "vendor"
            vendor.mkdir()
            (vendor / "copy.js").write_text("fetch('/copy')", encoding="utf-8")
            found = audit.discover_files(
                root, ("_server_deploy/static/pdf",)
            )
            self.assertEqual([path.name for path in found], ["canonical.js"])

    def test_default_targets_do_not_scan_unrelated_templates(self):
        self.assertNotIn("_server_deploy/templates", audit.DEFAULT_SCAN_TARGETS)
        self.assertIn(
            "_server_deploy/templates/pdf_reader.html",
            audit.DEFAULT_SCAN_TARGETS,
        )
        self.assertNotIn(
            "_server_deploy/templates/fitness/log.html",
            audit.DEFAULT_SCAN_TARGETS,
        )

    def test_repository_baseline_rejects_no_current_call(self):
        policies = audit.load_policies(audit.DEFAULT_POLICY)
        _, issues, _ = audit.audit_paths(ROOT, policies)
        new_issues, _ = audit.compare_baseline(
            issues,
            audit.read_baseline(audit.DEFAULT_BASELINE),
        )
        self.assertEqual(
            new_issues,
            [],
            "\n".join(
                f"{item.path}:{item.line} {item.code} {item.detail}"
                for item in new_issues[:20]
            ),
        )


if __name__ == "__main__":
    unittest.main()
