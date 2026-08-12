from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGER = ROOT / "ios" / "BWReader" / "package_local_reader.py"


def load_packager():
    spec = importlib.util.spec_from_file_location("bw_package_local_reader", PACKAGER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load local Reader packager")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LocalReaderPackagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.packager = load_packager()

    def test_third_party_sources_are_pinned_and_licensed(self) -> None:
        packages = {
            package.name: package for package in self.packager.EXTERNAL_PACKAGES
        }
        self.assertEqual(packages["pdfjs-dist"].version, "4.7.76")
        self.assertEqual(packages["marked"].version, "9.1.6")
        self.assertEqual(packages["mathjax-full"].version, "3.2.2")
        self.assertEqual(packages["html2canvas"].version, "1.4.1")
        self.assertEqual(packages["jszip"].version, "3.10.1")
        self.assertEqual(packages["dompurify"].version, "3.4.7")
        for package in packages.values():
            self.assertRegex(package.sha256, r"^[0-9a-f]{64}$")
            self.assertTrue(package.url.startswith("https://registry.npmjs.org/"))
            self.assertIn(package.name, self.packager.EXTERNAL_LICENSES)
        self.assertEqual(len(self.packager.EXPECTED_MATHJAX_FONTS), 23)
        self.assertEqual(self.packager.EXPECTED_PDFJS_CMAP_COUNT, 169)
        self.assertEqual(self.packager.EXPECTED_PDFJS_STANDARD_FONT_COUNT, 16)
        self.assertEqual(
            self.packager.DICTIONARY_SOURCE_RELEASE,
            "3.6.2+20260810124713",
        )
        self.assertRegex(self.packager.DICTIONARY_SOURCE_SHA256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            self.packager.EXTERNAL_LICENSES["JMdict"],
            "static/pdf/dictionary-data/LICENSE-JMdict.txt",
        )

    def test_generated_dictionary_source_is_complete_and_exact_lookup_works(self) -> None:
        manifest = self.packager.validate_dictionary_data(
            self.packager.DICTIONARY_SOURCE
        )
        self.assertEqual(manifest["contract"], "bw-jmdict-manifest/1")
        self.assertFalse(manifest["zhOverlay"]["complete"])
        self.assertFalse(manifest["zhOverlay"]["authoritative"])
        key = self.packager._dictionary_shard_key("取り寄せる")
        shard = json.loads(
            (
                self.packager.DICTIONARY_SOURCE
                / manifest["shards"][key]["path"]
            ).read_text(encoding="utf-8")
        )
        candidates = [
            shard["entries"][index]
            for index in shard["exact"]["取り寄せる"]
        ]
        self.assertTrue(any(item["lemma"] == "取り寄せる" for item in candidates))

    def test_pdf_shell_contract(self) -> None:
        shell = self.packager.build_pdf_shell()
        self.assertTrue(shell.lstrip().lower().startswith("<!doctype html>"))
        for placeholder in self.packager.PDF_PLACEHOLDERS:
            self.assertIn(placeholder, shell)
        self.assertIn("window.__BW_NATIVE_LOCAL_READER__=true", shell)
        self.assertIn(
            "if (window.__BW_NATIVE_LOCAL_READER__ === true) return;",
            shell,
        )
        self.assertIn(self.packager.CSP_NONCE_PLACEHOLDER, shell)
        self.assertTrue(all(
            f'nonce="{self.packager.CSP_NONCE_PLACEHOLDER}"' in tag
            for tag in re.findall(r"<script\b[^>]*>", shell)
        ))
        self.assertLess(
            shell.index("/static/pdf/vendor/purify.min.js"),
            shell.index("/static/pdf/native-local-runtime.js"),
        )
        self.assertLess(
            shell.index("/static/pdf/native-local-runtime.js"),
            shell.index("/static/qa/marked.js"),
        )
        self.assertNotIn("pwa-extension-bridge.js", shell)
        self.assertNotIn("pwa-service-bridge.js", shell)
        self.assertNotIn("pwa-runtime.js", shell)
        self.assertLess(
            shell.index("/static/reader-runtime/sync-runtime.js"),
            shell.index("/static/reader-runtime/native-sync-bootstrap.js"),
        )
        self.assertNotIn('rel="manifest"', shell)

    def test_epub_shell_contract_and_jszip_order(self) -> None:
        shell = self.packager.build_epub_shell()
        self.assertTrue(shell.lstrip().lower().startswith("<!doctype html>"))
        for placeholder in self.packager.EPUB_PLACEHOLDERS:
            self.assertIn(placeholder, shell)
        flag = shell.index("window.__BW_NATIVE_LOCAL_READER__=true")
        jszip = shell.index("/static/pdf/vendor/jszip.min.js")
        purifier = shell.index("/static/pdf/vendor/purify.min.js")
        runtime = shell.index("/static/pdf/native-local-runtime.js")
        marked = shell.index("/static/qa/marked.js")
        self.assertLess(flag, jszip)
        self.assertLess(jszip, purifier)
        self.assertLess(purifier, runtime)
        self.assertLess(runtime, marked)
        self.assertTrue(all(
            f'nonce="{self.packager.CSP_NONCE_PLACEHOLDER}"' in tag
            for tag in re.findall(r"<script\b[^>]*>", shell)
        ))
        self.assertNotIn("pwa-extension-bridge.js", shell)
        self.assertNotIn("pwa-service-bridge.js", shell)
        self.assertNotIn("pwa-runtime.js", shell)
        self.assertLess(
            shell.index("/static/reader-runtime/sync-runtime.js"),
            shell.index("/static/reader-runtime/native-sync-bootstrap.js"),
        )

    def test_manifest_hashes_exact_file_set_and_detects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "asset.txt").write_text("one\n", encoding="utf-8")
            manifest = self.packager.write_manifest(root)
            self.assertEqual(manifest["contract"], "bw-local-reader-bundle/1")
            self.assertEqual(set(manifest["files"]), {"asset.txt"})
            sources = {item["name"]: item for item in manifest["externalSources"]}
            self.assertEqual(sources["pdfjs-dist"]["version"], "4.7.76")
            self.assertEqual(
                sources["html2canvas"]["licensePath"],
                "licenses/html2canvas-LICENSE",
            )
            self.assertEqual(
                sources["dompurify"]["licensePath"],
                "licenses/dompurify-LICENSE",
            )
            self.packager.validate_manifest(root)
            (root / "asset.txt").write_text("two\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "resource digest mismatch"):
                self.packager.validate_manifest(root)

    def test_manifest_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / self.packager.MANIFEST_NAME).write_text(
                json.dumps(
                    {
                        "contract": self.packager.BUNDLE_CONTRACT,
                        "files": {"../outside": "0" * 64},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "unsafe entries"):
                self.packager.validate_manifest(root)

    def test_xcodegen_and_ci_require_generated_bundle(self) -> None:
        project = (ROOT / "ios" / "BWReader" / "project.yml").read_text(
            encoding="utf-8"
        )
        workflow = (
            ROOT / ".github" / "workflows" / "safari-extension-ios.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("url: https://github.com/swhitty/FlyingFox.git", project)
        self.assertIn("exactVersion: 0.27.1", project)
        self.assertIn("- path: Generated/ReaderBundle", project)
        self.assertIn("- path: Extension/Resources/dictionary-data", project)
        self.assertIn("product: FlyingFox", project)
        self.assertIn("product: FlyingSocks", project)
        build_at = workflow.index("Build deterministic native ReaderBundle")
        xcodegen_at = workflow.index("Generate the Xcode project")
        self.assertLess(build_at, xcodegen_at)
        self.assertIn('--verify "$READER_BUNDLE"', workflow)
        self.assertIn('READER_BUNDLE="$APP_PATH/ReaderBundle"', workflow)


if __name__ == "__main__":
    unittest.main()
