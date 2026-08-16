# 纸张块归一化单测(2026-08-16 语音直连制卷闭环)。
# 归一化从 assistant._norm_block 下沉到 paper.normalize_block(单源);这里钉住的每条
# 行为都对应一次真实故障:内容放错字段渲染不出=用户实测"page_add 失败"、choice 空壳、
# 语音链路一个坏块毁掉整张纸。
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_server_deploy"))

import paper  # noqa: E402


class NormalizeBlockTest(unittest.TestCase):
    def test_text_content_moved_from_label(self):
        b = paper.normalize_block({"kind": "text", "label": "题干在错误字段"}, 0)
        self.assertEqual(b["text"], "题干在错误字段")
        self.assertNotIn("label", b)

    def test_blank_content_moved_from_text(self):
        b = paper.normalize_block({"kind": "blank", "text": "1. 假名是?"}, 0)
        self.assertEqual(b["label"], "1. 假名是?")
        self.assertNotIn("text", b)

    def test_correct_fields_untouched(self):
        b = paper.normalize_block({"kind": "blank", "label": "2.", "answer": "ばら"}, 1)
        self.assertEqual(b["label"], "2.")
        self.assertEqual(b["answer"], "ばら")

    def test_choice_string_options_split_and_prefix_stripped(self):
        b = paper.normalize_block(
            {"kind": "choice", "text": "题干", "options": "A. 甲/B. 乙/C. 丙", "answer": "c"}, 0)
        self.assertEqual(b["options"], ["甲", "乙", "丙"])
        self.assertEqual(b["answer"], "C")

    def test_choice_without_options_degrades_to_text(self):
        b = paper.normalize_block({"kind": "choice", "text": "题干"}, 0)
        self.assertEqual(b["kind"], "text")
        self.assertNotIn("options", b)

    def test_options_capped_at_six(self):
        b = paper.normalize_block(
            {"kind": "choice", "text": "q", "options": [str(i) for i in range(9)]}, 0)
        self.assertEqual(len(b["options"]), 6)

    def test_unknown_keys_dropped_id_defaulted(self):
        b = paper.normalize_block({"kind": "text", "text": "t", "onclick": "evil()"}, 3)
        self.assertNotIn("onclick", b)
        self.assertEqual(b["id"], "b3")


class NormalizeBlocksTest(unittest.TestCase):
    def test_invalid_entries_dropped_not_fatal(self):
        out = paper.normalize_blocks(
            [{"kind": "text", "text": "ok"}, "字符串", {"kind": "video"}, None,
             {"kind": "blank", "label": "1."}])
        self.assertEqual([b["kind"] for b in out], ["text", "blank"])

    def test_non_list_input_yields_empty(self):
        self.assertEqual(paper.normalize_blocks({"kind": "text"}), [])
        self.assertEqual(paper.normalize_blocks(None), [])

    def test_capped_at_max_blocks(self):
        out = paper.normalize_blocks([{"kind": "text", "text": str(i)} for i in range(60)])
        self.assertEqual(len(out), paper.MAX_BLOCKS)

    def test_ids_are_positional_after_drops(self):
        out = paper.normalize_blocks([{"kind": "bad"}, {"kind": "text", "text": "t"}])
        self.assertEqual(out[0]["id"], "b0")


class EnsureCheckButtonTest(unittest.TestCase):
    def test_added_when_blank_and_no_button(self):
        out = paper.ensure_check_button([{"kind": "blank", "label": "1.", "id": "b0"}])
        self.assertEqual(out[-1]["kind"], "button")
        self.assertEqual(out[-1]["event"], "check")

    def test_idempotent(self):
        out = paper.ensure_check_button([{"kind": "blank", "label": "1.", "id": "b0"}])
        n = len(out)
        self.assertEqual(len(paper.ensure_check_button(out)), n)

    def test_not_added_without_blank(self):
        out = paper.ensure_check_button([{"kind": "text", "text": "只读说明", "id": "b0"}])
        self.assertEqual(len(out), 1)

    def test_existing_button_respected(self):
        blocks = [{"kind": "blank", "label": "1.", "id": "b0"},
                  {"kind": "button", "label": "念给我听", "event": "say:好", "id": "b1"}]
        self.assertEqual(len(paper.ensure_check_button(blocks)), 2)


if __name__ == "__main__":
    unittest.main()
