from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from reader_book_library import (  # noqa: E402
    BookLibrary,
    CatalogCorruptError,
    InvalidBookContentError,
    UploadTooLargeError,
    UnsafeLibraryPathError,
    UnsupportedBookError,
)
import reader_book_library as library_module  # noqa: E402


PDF_A = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
PDF_B = b"%PDF-1.4\n2 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


def epub_bytes(
    title: str = "Test",
    *,
    mimetype_payload: bytes = b"application/epub+zip",
    mimetype_compression: int = zipfile.ZIP_STORED,
    extra_members: tuple[tuple[str, bytes, int], ...] = (),
) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "mimetype",
            mimetype_payload,
            compress_type=mimetype_compression,
        )
        archive.writestr(
            "META-INF/container.xml",
            "<container><rootfiles/></container>",
        )
        archive.writestr("book.xhtml", f"<html><title>{title}</title></html>")
        for name, payload, compression in extra_members:
            archive.writestr(name, payload, compress_type=compression)
    return output.getvalue()


def encrypted_epub_bytes() -> bytes:
    payload = bytearray(epub_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = payload.find(signature)
        if position < 0:
            raise AssertionError("zip header missing")
        flags = int.from_bytes(payload[position + offset:position + offset + 2], "little") | 1
        payload[position + offset:position + offset + 2] = flags.to_bytes(2, "little")
    return bytes(payload)


def sanitize(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in " -_")[:120] or "untitled"


class BookLibraryCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.vault = base / "vault"
        self.state = base / "state"
        self.vault.mkdir()
        self.library = BookLibrary(self.vault, self.state)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_catalog_hashes_once_and_never_exposes_absolute_path(self) -> None:
        path = self.vault / "Books" / "A.pdf"
        path.parent.mkdir()
        path.write_bytes(PDF_A)

        first = self.library.catalog()
        self.assertEqual(len(first), 1)
        entry = first[0]
        expected = hashlib.sha256(PDF_A).hexdigest()
        self.assertEqual(entry["contentSha256"], expected)
        self.assertEqual(entry["version"], expected)
        self.assertEqual(entry["rel"], "Books/A.pdf")
        self.assertTrue(entry["downloadUrl"].startswith("/pdf/api/library/download/book_"))
        self.assertNotIn(str(self.vault), json.dumps(entry))

        with patch.object(
            library_module,
            "_stable_sha256",
            wraps=library_module._stable_sha256,
        ) as hasher:
            second = self.library.catalog()
        self.assertEqual(second, first)
        hasher.assert_not_called()

    def test_rename_reclaims_book_id_by_content_digest(self) -> None:
        old = self.vault / "Old.pdf"
        old.write_bytes(PDF_A)
        before = self.library.catalog()[0]
        target_dir = self.vault / "Renamed"
        target_dir.mkdir()
        new = target_dir / "New.pdf"
        old.rename(new)

        after = self.library.catalog()[0]
        self.assertEqual(after["bookId"], before["bookId"])
        self.assertEqual(after["contentSha256"], before["contentSha256"])
        self.assertEqual(after["rel"], "Renamed/New.pdf")

    def test_changed_bytes_keep_identity_and_change_version(self) -> None:
        path = self.vault / "Mutable.pdf"
        path.write_bytes(PDF_A)
        before = self.library.catalog()[0]
        path.write_bytes(PDF_B + b"changed")

        after = self.library.catalog()[0]
        self.assertEqual(after["bookId"], before["bookId"])
        self.assertNotEqual(after["version"], before["version"])

    def test_inode_and_ctime_detect_replacement_with_same_size_and_mtime(self) -> None:
        path = self.vault / "Replaced.pdf"
        path.write_bytes(PDF_A)
        old_stat = path.stat()
        before = self.library.catalog()[0]
        replacement = self.vault / "replacement.tmp"
        replacement.write_bytes(PDF_B)
        os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        os.replace(replacement, path)

        after = self.library.catalog()[0]
        self.assertEqual(after["bookId"], before["bookId"])
        self.assertEqual(after["contentSha256"], hashlib.sha256(PDF_B).hexdigest())
        self.assertNotEqual(after["version"], before["version"])

    def test_resolve_rechecks_fingerprint_after_catalog_pass(self) -> None:
        path = self.vault / "Resolve.pdf"
        path.write_bytes(PDF_A)
        old_stat = path.stat()
        initial = self.library.catalog()[0]
        original_safe = library_module._safe_existing_file
        replaced = False

        def replace_before_resolve(root, rel):
            nonlocal replaced
            resolved = original_safe(root, rel)
            if not replaced:
                replacement = self.vault / "resolve-replacement.tmp"
                replacement.write_bytes(PDF_B)
                os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
                os.replace(replacement, path)
                replaced = True
            return resolved

        with patch.object(library_module, "_safe_existing_file", side_effect=replace_before_resolve):
            entry, resolved = self.library.resolve(initial["bookId"])
        self.assertEqual(resolved, path)
        self.assertEqual(entry["contentSha256"], hashlib.sha256(PDF_B).hexdigest())

    def test_excludes_internal_books_and_accepts_epub(self) -> None:
        (self.vault / "资源" / "uploads" / ".sandbox").mkdir(parents=True)
        (self.vault / "资源" / "uploads" / ".sandbox" / "hidden.pdf").write_bytes(PDF_A)
        (self.vault / "backup.orig.pdf").write_bytes(PDF_A)
        (self.vault / "backup.compressed.pdf").write_bytes(PDF_A)
        (self.vault / "book.epub").write_bytes(epub_bytes())

        books = self.library.catalog()
        self.assertEqual([(item["name"], item["kind"]) for item in books], [("book.epub", "epub")])

    def test_control_characters_are_invalid_catalog_paths(self) -> None:
        for value in (
            "Books/bad\nname.pdf",
            "\nBooks/name.pdf",
            "Books/./name.pdf",
            "Books//name.pdf",
        ):
            with self.subTest(value=value), self.assertRaises(UnsafeLibraryPathError):
                library_module._valid_relative_parts(value)

    def test_upload_limit_defaults_to_two_gib_and_accepts_env_override(self) -> None:
        self.assertEqual(
            self.library.max_upload_bytes,
            library_module.DEFAULT_UPLOAD_MAX_BYTES,
        )
        with patch.dict(
            os.environ,
            {library_module.UPLOAD_MAX_BYTES_ENV: "12345"},
        ):
            configured = BookLibrary(self.vault, self.state / "configured")
        self.assertEqual(configured.max_upload_bytes, 12345)

    def test_corrupt_catalog_fails_closed(self) -> None:
        self.state.mkdir()
        (self.state / "catalog.json").write_text('{"schema":"wrong","records":{}}', "utf-8")
        with self.assertRaises(CatalogCorruptError):
            self.library.catalog()


class BookLibraryUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.vault = base / "vault"
        self.state = base / "state"
        self.vault.mkdir()
        self.library = BookLibrary(self.vault, self.state)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_upload_is_atomic_and_same_content_is_deduplicated(self) -> None:
        first, duplicate = self.library.ingest(
            BytesIO(PDF_A),
            "My Book.PDF",
            target_dir="资源/uploads",
            sanitize_filename=sanitize,
        )
        self.assertFalse(duplicate)
        self.assertEqual(first["kind"], "pdf")
        self.assertEqual(first["contentSha256"], hashlib.sha256(PDF_A).hexdigest())
        self.assertTrue((self.vault / first["rel"]).is_file())
        self.assertEqual(list((self.vault / "资源" / "uploads").glob("*.tmp")), [])

        second, duplicate = self.library.ingest(
            BytesIO(PDF_A),
            "Another.pdf",
            target_dir="资源/uploads",
            sanitize_filename=sanitize,
        )
        self.assertTrue(duplicate)
        self.assertEqual(second["bookId"], first["bookId"])
        self.assertEqual(len(self.library.catalog()), 1)

    def test_name_collision_never_overwrites_existing_book(self) -> None:
        first, _ = self.library.ingest(
            BytesIO(PDF_A), "Same.pdf", target_dir="资源/uploads", sanitize_filename=sanitize
        )
        second, _ = self.library.ingest(
            BytesIO(PDF_B), "Same.pdf", target_dir="资源/uploads", sanitize_filename=sanitize
        )
        self.assertEqual(first["name"], "Same.pdf")
        self.assertEqual(second["name"], "Same-1.pdf")
        self.assertEqual((self.vault / first["rel"]).read_bytes(), PDF_A)
        self.assertEqual((self.vault / second["rel"]).read_bytes(), PDF_B)

    def test_epub_upload_returns_catalog_entry(self) -> None:
        payload = epub_bytes("Localized")
        entry, duplicate = self.library.ingest(
            BytesIO(payload),
            "Novel.epub",
            target_dir="资源/uploads",
            sanitize_filename=sanitize,
        )
        self.assertFalse(duplicate)
        self.assertEqual(entry["kind"], "epub")
        self.assertEqual(entry["contentSha256"], hashlib.sha256(payload).hexdigest())

    def test_invalid_paths_formats_and_content_fail_closed(self) -> None:
        for target in (
            "../outside",
            "/absolute",
            "C:/absolute",
            "Books",
            "Books/../../outside",
        ):
            with self.subTest(target=target), self.assertRaises(UnsafeLibraryPathError):
                self.library.ingest(
                    BytesIO(PDF_A),
                    "Book.pdf",
                    target_dir=target,
                    sanitize_filename=sanitize,
                )
        with self.assertRaises(UnsupportedBookError):
            self.library.ingest(
                BytesIO(b"text"),
                "Book.txt",
                target_dir="资源/uploads",
                sanitize_filename=sanitize,
            )
        with self.assertRaises(InvalidBookContentError):
            self.library.ingest(
                BytesIO(b"not a pdf"),
                "Book.pdf",
                target_dir="资源/uploads",
                sanitize_filename=sanitize,
            )
        self.assertEqual(list(self.vault.rglob(".bw-library-upload-*.tmp")), [])

    def test_streaming_upload_limit_is_enforced_and_temp_is_removed(self) -> None:
        limited = BookLibrary(self.vault, self.state, max_upload_bytes=len(PDF_A) - 1)
        with self.assertRaises(UploadTooLargeError) as raised:
            limited.ingest(
                BytesIO(PDF_A),
                "Large.pdf",
                target_dir="资源/uploads",
                sanitize_filename=sanitize,
            )
        self.assertEqual(raised.exception.max_bytes, len(PDF_A) - 1)
        self.assertEqual(list(self.vault.rglob(".bw-library-upload-*.tmp")), [])

    def test_epub_safety_limits_reject_unsafe_archives(self) -> None:
        cases = [
            ("compressed mimetype", epub_bytes(mimetype_compression=zipfile.ZIP_DEFLATED), {}),
            ("wrong mimetype", epub_bytes(mimetype_payload=b"application/epub+zop"), {}),
            (
                "duplicate mimetype",
                epub_bytes(
                    extra_members=(("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),)
                ),
                {},
            ),
            ("encrypted", encrypted_epub_bytes(), {}),
            (
                "traversal",
                epub_bytes(extra_members=(("../escape.xhtml", b"x", zipfile.ZIP_STORED),)),
                {},
            ),
            (
                "dot member",
                epub_bytes(extra_members=(("OPS/./book.xhtml", b"x", zipfile.ZIP_STORED),)),
                {},
            ),
            (
                "double separator",
                epub_bytes(extra_members=(("OPS//book.xhtml", b"x", zipfile.ZIP_STORED),)),
                {},
            ),
            ("too many", epub_bytes(), {"MAX_EPUB_MEMBERS": 1}),
            (
                "large member",
                epub_bytes(extra_members=(("large.bin", b"x" * 40, zipfile.ZIP_STORED),)),
                {"MAX_EPUB_MEMBER_BYTES": 32},
            ),
            ("large total", epub_bytes(), {"MAX_EPUB_TOTAL_BYTES": 25}),
            (
                "ratio",
                epub_bytes(extra_members=(("bomb.txt", b"0" * 2_000, zipfile.ZIP_DEFLATED),)),
                {"MAX_EPUB_COMPRESSION_RATIO": 2},
            ),
        ]
        for label, payload, limits in cases:
            patches = [patch.object(library_module, key, value) for key, value in limits.items()]
            with self.subTest(label=label):
                for active in patches:
                    active.start()
                try:
                    with self.assertRaises(InvalidBookContentError):
                        self.library.ingest(
                            BytesIO(payload),
                            "Unsafe.epub",
                            target_dir="资源/uploads",
                            sanitize_filename=sanitize,
                        )
                finally:
                    for active in reversed(patches):
                        active.stop()


if __name__ == "__main__":
    unittest.main()
