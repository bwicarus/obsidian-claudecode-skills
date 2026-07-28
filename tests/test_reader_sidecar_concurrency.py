"""Concurrency contracts for account-scoped reader sidecar editors."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import pdf_reader  # noqa: E402
from reader_sidecar_store import ReaderStorageIdentity, SidecarStore  # noqa: E402


IDENTITY = ReaderStorageIdentity(
    41,
    "acct-v1-" + ("4" * 64),
)


class ReaderSidecarEditorConcurrencyTest(unittest.TestCase):
    def test_document_editors_do_not_lose_concurrent_writes(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="bw-reader-sidecar-editor-test-"
        ) as temp:
            base = Path(temp)
            store = SidecarStore(
                base / "sidecars",
                base / "legacy",
                lambda _identity: False,
            )
            old_root = pdf_reader._READER_SIDECAR_ROOT
            old_store = pdf_reader._READER_SIDECAR_STORE
            pdf_reader._READER_SIDECAR_ROOT = store.root
            pdf_reader._READER_SIDECAR_STORE = store
            try:
                self._exercise_list_editor(
                    pdf_reader._notes_edit,
                    pdf_reader._notes_load,
                    "notes.pdf",
                )
                self._exercise_list_editor(
                    pdf_reader._epub_hl_edit,
                    pdf_reader._epub_hl_load,
                    "highlights.epub",
                )
                self._exercise_pdf_highlight_editor()
            finally:
                pdf_reader._READER_SIDECAR_ROOT = old_root
                pdf_reader._READER_SIDECAR_STORE = old_store

    def test_corrupt_private_json_is_not_replaced_with_empty_data(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="bw-reader-sidecar-corrupt-test-"
        ) as temp:
            base = Path(temp)
            store = SidecarStore(
                base / "sidecars",
                base / "legacy",
                lambda _identity: False,
            )
            old_root = pdf_reader._READER_SIDECAR_ROOT
            old_store = pdf_reader._READER_SIDECAR_STORE
            pdf_reader._READER_SIDECAR_ROOT = store.root
            pdf_reader._READER_SIDECAR_STORE = store
            try:
                pdf_reader._reader_storage_identity_bind_for_thread(
                    IDENTITY.as_dict()
                )
                path = pdf_reader._notes_path("corrupt.pdf", IDENTITY)
                path.parent.mkdir(parents=True, exist_ok=True)
                original = b'{"unfinished":'
                path.write_bytes(original)
                with self.assertRaises(json.JSONDecodeError):
                    with pdf_reader._notes_edit("corrupt.pdf") as items:
                        items.append({"id": "must-not-be-written"})
                self.assertEqual(path.read_bytes(), original)
            finally:
                pdf_reader._READER_SIDECAR_ROOT = old_root
                pdf_reader._READER_SIDECAR_STORE = old_store

    def _run_workers(self, mutate) -> None:
        count = 20
        barrier = threading.Barrier(count)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                pdf_reader._reader_storage_identity_bind_for_thread(
                    IDENTITY.as_dict()
                )
                barrier.wait(timeout=5)
                mutate(index)
            except BaseException as exc:  # surfaced in the parent assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(
            [thread.name for thread in threads if thread.is_alive()],
            "sidecar writers did not finish",
        )
        self.assertEqual(errors, [])

    def _exercise_list_editor(self, editor, loader, rel: str) -> None:
        def mutate(index: int) -> None:
            with editor(rel) as items:
                items.append({"id": f"item-{index}"})

        self._run_workers(mutate)
        items = loader(rel, IDENTITY)
        self.assertEqual(
            {item["id"] for item in items},
            {f"item-{index}" for index in range(20)},
        )

    def _exercise_pdf_highlight_editor(self) -> None:
        rel = "highlights.pdf"

        def mutate(index: int) -> None:
            with pdf_reader._hl_edit(rel) as document:
                document["highlights"].append({"id": f"highlight-{index}"})

        self._run_workers(mutate)
        document = pdf_reader._hl_load(rel, IDENTITY)
        self.assertEqual(
            {item["id"] for item in document["highlights"]},
            {f"highlight-{index}" for index in range(20)},
        )


if __name__ == "__main__":
    unittest.main()
