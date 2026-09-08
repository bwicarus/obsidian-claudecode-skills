# -*- coding: utf-8 -*-
"""review_deck 的测试：语音复习要念的那一组卡。

用户 2026-09-09：

> 我希望能提供一种可选的语音复习功能……ai 语音描述卡片的正面内容我来回答背面
> 内容，根据我回答的结果 ai 来判断掌握程度后录入……只是为 ai 提供一个读取最新
> 需要复习的一组 anki 卡片内容并代替我选择掌握度的一个接口

守三条：

① **念出来的必须是能听的**。卡面是 HTML，含振假名 —— 只剥标签会把
   `<ruby>教<rt>きょう</rt></ruby>` 变成「教きょう」，念出来是汉字加读音连读一遍。
② **评不了分的卡照样列出来并标明**。藏起来会让 AI 以为总数不对，
   而 2026-09-09 正好有 10 张卡因为没进 Anki 评不了分。
③ **顺序只按数据排**，不替 AI 做"他最可能忘哪张"的判断。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

DESKTOP = Path(__file__).resolve().parent.parent
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import review_deck  # noqa: E402


class SpeakableTests(unittest.TestCase):
    def test_ruby_reading_is_dropped_not_flattened(self):
        # ① 只剥标签会念成「教きょう」。
        self.assertEqual(
            review_deck.speakable("イスラム<ruby>教<rt>きょう</rt></ruby>です"),
            "イスラム教です")

    def test_rp_parentheses_are_dropped_too(self):
        self.assertEqual(
            review_deck.speakable("<ruby>教<rp>(</rp><rt>きょう</rt><rp>)</rp></ruby>"),
            "教")

    def test_breaks_become_pauses_not_run_together(self):
        # 背面常是分条的；<br> 剥成空会把两条连成一句念出去。
        self.assertEqual(
            review_deck.speakable("1. 甲<br>2. 乙"), "1. 甲\n2. 乙")
        self.assertEqual(review_deck.speakable("<p>甲</p><p>乙</p>"), "甲\n乙")

    def test_entities_are_decoded(self):
        self.assertEqual(review_deck.speakable("A &amp; B &nbsp;C"), "A & B  C")

    def test_empty_and_none_are_safe(self):
        self.assertEqual(review_deck.speakable(""), "")
        self.assertEqual(review_deck.speakable(None), "")


class DeckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.book = self.root / "replication-data" / "b1"
        self.book.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, cards, created=1000):
        (self.book / "document-notes.json").write_text(json.dumps({
            "items": {"i1": {"created": created,
                             "card": {"gid": "card_" + "a" * 12,
                                      "cid": "c1", "cards": cards}}},
        }), encoding="utf-8")

    def test_due_before_new_and_most_overdue_first(self):
        now = int(time.time() * 1000)
        self.write([
            {"front": "新", "back": "b", "_st": "learn"},
            {"front": "刚到期", "back": "b", "_next": now - 60_000},
            {"front": "很久没做", "back": "b", "_next": now - 9 * 86_400_000},
        ])
        fronts = [one["frontText"] for one in review_deck.collect(self.root)]
        self.assertEqual(fronts, ["很久没做", "刚到期", "新"])

    def test_not_yet_due_is_excluded(self):
        now = int(time.time() * 1000)
        self.write([{"front": "明天", "back": "b", "_next": now + 86_400_000}])
        self.assertEqual(review_deck.collect(self.root), [])

    def test_removed_cards_are_excluded(self):
        self.write([{"front": "删了", "back": "b", "_st": "learn",
                     "_removed": True}])
        self.assertEqual(review_deck.collect(self.root), [])

    def test_blocked_cards_are_listed_and_marked(self):
        # ② 藏起来只会让人更晚发现。
        self.write([{"front": "没进 Anki", "back": "b", "_st": "learn",
                     "_ratingUnavailable": True,
                     "_ratingUnavailableReason": "not-exported"}])
        deck = review_deck.collect(self.root)
        self.assertEqual(len(deck), 1)
        self.assertFalse(deck[0]["gradable"])
        self.assertEqual(deck[0]["blockedReason"], "not-exported")
        payload = review_deck.take(self.root)
        self.assertEqual(payload["blocked"], 1)
        # 渲出来要说清楚，且给出怎么修
        text = review_deck.render(payload)
        self.assertIn("评不了分", text)
        self.assertIn("Anki", text)

    def test_identity_carries_what_grading_needs(self):
        self.write([{"front": "f", "back": "b", "_st": "learn", "_nid": 12345}])
        one = review_deck.collect(self.root)[0]
        self.assertEqual(one["noteId"], 12345)
        self.assertEqual(one["index"], 0)
        self.assertTrue(one["gid"])

    def test_next_accepts_seconds_or_milliseconds(self):
        # 与 count_due_cards 同口径：>1e12 视为毫秒。
        now_s = int(time.time()) - 600
        self.write([{"front": "秒", "back": "b", "_next": now_s}])
        self.assertEqual(len(review_deck.collect(self.root)), 1)

    def test_limit_is_bounded_and_totals_stay_honest(self):
        self.write([{"front": "f%d" % i, "back": "b", "_st": "learn"}
                    for i in range(30)])
        payload = review_deck.take(self.root, limit=3)
        self.assertEqual(len(payload["cards"]), 3)
        # 总数报的是全部，不是这一页 —— 否则 AI 会以为只剩 3 张。
        self.assertEqual(payload["total"], 30)
        self.assertEqual(payload["new"], 30)

    def test_missing_data_directory_is_empty_not_an_error(self):
        payload = review_deck.take(Path(tempfile.mkdtemp(prefix="none-")))
        self.assertEqual(payload["cards"], [])
        self.assertEqual(payload["total"], 0)
        self.assertIn("没有该复习的卡", review_deck.render(payload))

    def test_broken_book_file_does_not_kill_the_deck(self):
        (self.book / "document-notes.json").write_text("{ 坏的", encoding="utf-8")
        other = self.root / "replication-data" / "b2"
        other.mkdir()
        (other / "document-notes.json").write_text(json.dumps({
            "items": {"i": {"card": {"gid": "g", "cards": [
                {"front": "好的", "back": "b", "_st": "learn"}]}}}}),
            encoding="utf-8")
        deck = review_deck.collect(self.root)
        self.assertEqual([one["frontText"] for one in deck], ["好的"])


if __name__ == "__main__":
    unittest.main()
