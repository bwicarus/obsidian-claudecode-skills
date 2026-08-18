"""书源 anki/records 的写入与消费边界（C 组 #18 的 P0）。

调研实测的两件事，这些用例各钉一件：

1. **写入挂在死分支上**。record 只在 `_run_snippets_to` 的直接入库分支写，而
   App / 扩展 / 语音**全部**走 `defer_add=true` 的 `/pdf/api/anki-add-cards`
   —— 那条路一行都不写，所以 Pi 上实测 `reader-*.json` 为 0。

2. **写下去就会崩**。`source_note` 被塞了一个书路径，而
   `review_priority` / `link_with_ai` / `cleanup_orphans` / `anki_status`
   四处都把它当 vault .md 用：前两个会对 PDF 做 `read_text('utf-8')`
   （UnicodeDecodeError），第三个判它是孤儿并**挂起真卡**。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReaderBookRecordShapeTest(unittest.TestCase):
    """字段形状：书不是笔记。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "_server_deploy" / "pdf_reader.py").read_text("utf-8")
        start = cls.source.index("def _reader_record_write(")
        end = cls.source.index("\ndef ", start + 10)
        cls.writer = cls.source[start:end]

    def test_source_note_is_always_empty_for_books(self) -> None:
        # 这一条是拆定时炸弹：四个既有消费者都只认 source_note。
        self.assertIn('"source_note": "",', self.writer)
        self.assertIn('"source_kind": "book",', self.writer)
        # 出处走新字段
        self.assertIn('"source_ref": source_ref or "",', self.writer)
        self.assertIn('"source_book": source_book or "",', self.writer)

    def test_one_record_per_book_not_per_page(self) -> None:
        # 文件名里带 #p<page> 的话，一本书读 200 页就是 200 个 record 文件。
        self.assertIn('str(source_book or "reader")', self.writer)
        self.assertNotIn("#p", self.writer.split('rec_path = ')[1].split("\n")[0])
        # 页码属于每张卡
        self.assertIn('"source_page": int(source_page) if source_page else None', self.writer)

    def test_write_is_idempotent_by_note_id(self) -> None:
        # /api/anki-add-cards 的 dedup 会重放；无条件 append 会让同一张卡堆好几份。
        self.assertIn("seen = {", self.writer)
        self.assertIn("if note_id is not None and str(note_id) in seen:", self.writer)
        self.assertIn("continue", self.writer)

    def test_failure_never_breaks_card_creation(self) -> None:
        self.assertIn("return None   # 记录失败绝不该影响制卡本身", self.writer)


class ReaderBookRecordWiringTest(unittest.TestCase):
    """写入要挂在**真正入库**的那条路上。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "_server_deploy" / "pdf_reader.py").read_text("utf-8")

    def test_deferred_add_path_also_writes(self) -> None:
        # App / 扩展 / 语音全走这条；不接它等于一条 record 都不会产生。
        self.assertIn("durable = _anki_add_commit(", self.source)
        after = self.source.split("durable = _anki_add_commit(", 1)[1][:1200]
        self.assertIn("_reader_record_write(", after)
        self.assertIn('source_ref.startswith("book:")', after)

    def test_direct_path_still_writes(self) -> None:
        self.assertIn(
            "_reader_record_write(_sf, _sp, _src_ref, _aid, cards, note_ids)",
            self.source,
        )

    def test_page_number_parsed_from_material_ref(self) -> None:
        # source_ref 已经是规范化的 book:<rel>#p<N>
        after = self.source.split("durable = _anki_add_commit(", 1)[1][:1200]
        self.assertIn('partition("#p")', after)


class ReaderBookRecordConsumerGuardTest(unittest.TestCase):
    """四个既有消费者必须显式跳过书源 —— 不能依赖 source_note 恰好为空。"""

    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text("utf-8")

    def test_review_priority_skips_books_and_speaks_on_binary(self) -> None:
        source = self._read("scripts/review_priority.py")
        self.assertIn('if str(rec.get("source_kind") or "") == "book":', source)
        # UnicodeDecodeError 以前会让整个每日流程崩在这里
        self.assertEqual(source.count("except UnicodeDecodeError:"), 2)
        # 而且必须出声：静默返回空集的表现是"这条笔记没有任何链接"——
        # 一个完全说得通却完全错误的结论。
        self.assertIn("WARN 不是文本文件，跳过链接解析", source)
        self.assertIn("WARN 不是文本文件，跳过 frontmatter", source)

    def test_cleanup_orphans_does_not_suspend_book_cards(self) -> None:
        source = self._read("scripts/cleanup_orphans.py")
        self.assertIn('if str(rec.get("source_kind") or "") == "book":', source)
        marker = source.index('if str(rec.get("source_kind") or "") == "book":')
        tail = source[marker:marker + 400]
        self.assertIn("continue", tail)

    def test_anki_status_gives_books_a_snapshot(self) -> None:
        source = self._read("scripts/anki_status.py")
        self.assertIn("def process_book_records(", source)
        # 不能走 collect_notes → record_path_for：那条按 source_note 反算文件名，
        # 对 reader-<book>.json 永远对不上（这就是 mastery 恒 0.5 的根因）。
        body = source[source.index("def process_book_records("):]
        body = body[:body.index("\ndef ")]
        self.assertIn('str(record.get("source_kind") or "") != "book"', body)
        self.assertIn("write_record_snapshot(record_path, counts, args.dry_run)", body)
        self.assertNotIn("record_path_for(", body)
        # --all 时一并处理
        self.assertIn("process_book_records(args) if args.all else 0", source)


class ReaderBookRecordRoundTripTest(unittest.TestCase):
    """真跑一遍写入：形状、幂等、页码落到卡上。"""

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT / "_server_deploy"))

    def test_round_trip(self) -> None:
        import tempfile
        import importlib.util

        # 只取那一个函数来跑，不导入整个 pdf_reader（它要 Flask 上下文）。
        source = (ROOT / "_server_deploy" / "pdf_reader.py").read_text("utf-8")
        start = source.index("_RECORD_STEM_RE = re.compile(")
        end = source.index("\ndef ", source.index("def _reader_record_write(") + 10)
        snippet = source[start:end]

        with tempfile.TemporaryDirectory() as tmp:
            namespace: dict = {
                "re": re,
                "json": json,
                "time": __import__("time"),
                "CLAUDE_DIR": Path(tmp),
            }
            exec(compile(snippet, "<writer>", "exec"), namespace)
            write = namespace["_reader_record_write"]

            cards = [{"type": "basic", "front": "Q", "back": "A"}]
            path = write("资源/books/x.pdf", 46, "book:资源/books/x.pdf#p46",
                         "card_abcd", cards, [1001])
            self.assertIsNotNone(path)
            data = json.loads(Path(path).read_text("utf-8"))
            self.assertEqual(data["source_note"], "")
            self.assertEqual(data["source_kind"], "book")
            self.assertEqual(data["source_ref"], "book:资源/books/x.pdf#p46")
            self.assertEqual(len(data["cards"]), 1)
            self.assertEqual(data["cards"][0]["source_page"], 46)

            # 重放同一张卡（dedup 会让上游重发）→ 不该堆第二份
            write("资源/books/x.pdf", 46, "book:资源/books/x.pdf#p46",
                  "card_abcd", cards, [1001])
            data = json.loads(Path(path).read_text("utf-8"))
            self.assertEqual(len(data["cards"]), 1, "重放堆了重复卡")

            # 同一本书的另一页 → 还是同一个文件，卡各自记页码
            path2 = write("资源/books/x.pdf", 77, "book:资源/books/x.pdf#p77",
                          "card_efgh", cards, [1002])
            self.assertEqual(Path(path2), Path(path), "一本书应当只有一个 record")
            data = json.loads(Path(path).read_text("utf-8"))
            self.assertEqual(len(data["cards"]), 2)
            self.assertEqual(
                sorted(c["source_page"] for c in data["cards"]), [46, 77]
            )


if __name__ == "__main__":
    unittest.main()
