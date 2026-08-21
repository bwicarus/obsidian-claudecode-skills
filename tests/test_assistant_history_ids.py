"""Stable conversation history identity and scope-isolation contracts."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import assistant  # noqa: E402
import epub_assistant  # noqa: E402


def _write(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")


class AssistantHistoryIdTest(unittest.TestCase):
    def test_pdf_legacy_ids_are_persisted_unique_and_mode_isolated(self):
        with tempfile.TemporaryDirectory(prefix="assistant-history-id-") as td:
            root = Path(td)
            normal_dir = root / "normal"
            review_dir = root / "review"
            normal_path = normal_dir / "alice.json"
            review_path = review_dir / "alice.json"
            normal_rows = [
                {"role": "user", "content": "normal-1", "history_id": "shared"},
                {"role": "assistant", "content": "normal-2", "history_id": "shared"},
                {"role": "assistant", "content": "normal-3"},
            ]
            review_rows = [{"role": "assistant", "content": "review"}]
            _write(normal_path, normal_rows)
            _write(review_path, review_rows)

            generated = iter(("shared", "normal-new", "normal-third"))
            with mock.patch.object(assistant, "_CONVO_DIR", normal_dir), \
                    mock.patch.object(assistant, "_REVIEW_CONVO_DIR", review_dir), \
                    mock.patch.object(
                        assistant, "_new_history_id", side_effect=lambda: next(generated)
                    ):
                migrated = assistant._convo_load_for_history("alice", "normal")

            self.assertEqual(
                [row["history_id"] for row in migrated],
                ["shared", "normal-new", "normal-third"],
            )
            self.assertEqual(json.loads(normal_path.read_text("utf-8")), migrated)
            self.assertNotIn(
                "history_id",
                json.loads(review_path.read_text("utf-8"))[0],
                "normal migration must not write the review namespace",
            )

            with mock.patch.object(assistant, "_CONVO_DIR", normal_dir), \
                    mock.patch.object(assistant, "_REVIEW_CONVO_DIR", review_dir), \
                    mock.patch.object(
                        assistant, "_new_history_id", return_value="review-new"
                    ):
                review = assistant._convo_load_for_history("alice", "review")
                stable_again = assistant._convo_load_for_history("alice", "normal")

            self.assertEqual(review[0]["history_id"], "review-new")
            self.assertEqual(stable_again, migrated)
            self.assertEqual(
                len({row["history_id"] for row in migrated}),
                len(migrated),
            )

    def test_pdf_ids_survive_public_window_slide_and_duplicate_content(self):
        with tempfile.TemporaryDirectory(prefix="assistant-history-window-") as td:
            normal_dir = Path(td) / "normal"
            path = normal_dir / "alice.json"
            rows = [
                {"role": "assistant", "content": f"row-{index}"}
                for index in range(102)
            ]
            rows[-2]["content"] = "duplicate legacy content"
            rows[-1]["content"] = "duplicate legacy content"
            _write(path, rows)
            generated = iter(f"h_pdf_{index:04d}" for index in range(1000))

            with mock.patch.object(assistant, "_CONVO_DIR", normal_dir), \
                    mock.patch.object(
                        assistant, "_new_history_id", side_effect=lambda: next(generated)
                    ):
                first = assistant._convo_load_for_history("alice", "normal")
                first_window = first[-100:]
                assistant._convo_append("alice", "assistant", "new tail")
                second = assistant._convo_load_for_history("alice", "normal")
                second_window = second[-100:]

            first_row_three = next(
                row for row in first_window if row["content"] == "row-3"
            )
            second_row_three = next(
                row for row in second_window if row["content"] == "row-3"
            )
            self.assertEqual(
                first_row_three["history_id"],
                second_row_three["history_id"],
                "a surviving row keeps its id when its index inside [-100:] changes",
            )
            duplicate_ids = {
                row["history_id"]
                for row in first
                if row["content"] == "duplicate legacy content"
            }
            self.assertEqual(
                len(duplicate_ids),
                2,
                "identical legacy content still receives distinct persistent identities",
            )
            self.assertEqual(len(first_window), 100)
            self.assertEqual(len(second_window), 100)
            self.assertEqual(second_window[-1]["content"], "new tail")

    def test_epub_legacy_ids_are_persisted_per_book_and_window_stable(self):
        with tempfile.TemporaryDirectory(prefix="epub-history-id-") as td:
            convo_dir = Path(td) / "epub"
            with mock.patch.object(epub_assistant, "_ECONVO_DIR", convo_dir):
                first_path = epub_assistant._econvo_path("alice", "Books/one.epub")
                second_path = epub_assistant._econvo_path("alice", "Books/two.epub")
                first_rows = [
                    {"role": "assistant", "content": f"one-{index}"}
                    for index in range(102)
                ]
                first_rows[-2]["content"] = "duplicate EPUB content"
                first_rows[-1]["content"] = "duplicate EPUB content"
                _write(first_path, first_rows)
                _write(second_path, [{"role": "assistant", "content": "two"}])

                generated = iter(f"h_epub_{index:04d}" for index in range(1000))
                with mock.patch.object(
                    epub_assistant,
                    "_new_history_id",
                    side_effect=lambda: next(generated),
                ):
                    first = epub_assistant._econvo_load_for_history(
                        "alice", "Books/one.epub"
                    )
                    first_window = first[-100:]
                    epub_assistant._econvo_append(
                        "alice", "Books/one.epub", "assistant", "one-tail"
                    )
                    first_after_append = epub_assistant._econvo_load_for_history(
                        "alice", "Books/one.epub"
                    )
                    second_window = first_after_append[-100:]

                self.assertEqual(len({row["history_id"] for row in first}), 102)
                first_row_three = next(
                    row for row in first_window if row["content"] == "one-3"
                )
                second_row_three = next(
                    row for row in second_window if row["content"] == "one-3"
                )
                self.assertEqual(
                    first_row_three["history_id"],
                    second_row_three["history_id"],
                )
                self.assertEqual(
                    len({
                        row["history_id"]
                        for row in first
                        if row["content"] == "duplicate EPUB content"
                    }),
                    2,
                )
                self.assertNotIn(
                    "history_id",
                    json.loads(second_path.read_text("utf-8"))[0],
                    "one EPUB book must not migrate another book's history",
                )

                with mock.patch.object(
                    epub_assistant, "_new_history_id", return_value="epub-two"
                ):
                    second = epub_assistant._econvo_load_for_history(
                        "alice", "Books/two.epub"
                    )
                    stable_again = epub_assistant._econvo_load_for_history(
                        "alice", "Books/one.epub"
                    )

            self.assertEqual(second[0]["history_id"], "epub-two")
            self.assertEqual(stable_again, first_after_append)
            self.assertEqual(
                len({row["history_id"] for row in first}),
                len(first),
            )
            self.assertEqual(
                json.loads(first_path.read_text("utf-8")),
                first_after_append,
            )
            self.assertEqual(json.loads(second_path.read_text("utf-8")), second)


if __name__ == "__main__":
    unittest.main()
