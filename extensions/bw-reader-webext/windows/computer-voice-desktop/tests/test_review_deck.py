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


class ScopeTests(unittest.TestCase):
    """指定范围的复习（用户 2026-09-21：出门戴耳机，手上没有任何书页）。

    守三条：
    ① **范围永远连同"有哪些范围"一起返回** —— AI 只有一次开口机会，
       用户说"换一本"时它得当场报得出书名和张数。
    ② **筛空了要能和"复习完了"分开** —— 后者让人放心，前者其实是书名说岔了。
    ③ **同一个书名对应多个 repbookId 是常态**（同一本书从不同设备配对过），
       按书名选是并集，不是歧义。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "replication-data").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def book(self, repid, title, cards):
        d = self.root / "replication-data" / repid
        d.mkdir(exist_ok=True)
        items = {}
        for n, (front, page, one) in enumerate(cards):
            items["i%d" % n] = {
                "created": 1000 + n,
                "anchor": {"kind": "pdf", "page": page} if page else None,
                "card": {"gid": "g%d" % n, "cid": "c%d" % n,
                         "cards": [dict(one, front=front, back="b")]},
            }
        (d / "document-notes.json").write_text(
            json.dumps({"items": items}), encoding="utf-8")
        if title:
            path = self.root / "replication-book-links.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except OSError:
                value = {"contract": "replication-book-links/1", "links": []}
            value["links"].append(
                {"replicationBookId": repid, "displayName": title})
            path.write_text(json.dumps(value), encoding="utf-8")

    def test_title_comes_from_the_link_table(self):
        self.book("repbook-a", "料理师part2 · PDF 阅读器",
                  [("f", 5, {"_st": "learn"})])
        one = review_deck.collect(self.root)[0]
        self.assertIn("料理师part2", one["bookTitle"])
        self.assertEqual(one["page"], 5)

    def test_unpaired_book_has_no_title_rather_than_a_fake_one(self):
        # 没配对过的书没有书名。拿 repbookId 冒充书名会让 AI 念一串十六进制。
        self.book("repbook-z", None, [("f", 3, {"_st": "learn"})])
        self.assertEqual(review_deck.collect(self.root)[0]["bookTitle"], "")

    def test_same_title_across_copies_is_a_union_not_a_conflict(self):
        # ③ 实测「料理师part1」有三条 repbook 记录。
        self.book("repbook-a", "料理师part1", [("甲", 1, {"_st": "learn"})])
        self.book("repbook-b", "料理师part1", [("乙", 2, {"_st": "learn"})])
        self.book("repbook-c", "别的书", [("丙", 3, {"_st": "learn"})])
        payload = review_deck.take(self.root, book="料理师part1")
        self.assertEqual(
            sorted(one["frontText"] for one in payload["cards"]), ["乙", "甲"])
        self.assertEqual(len(payload["matchedBooks"]), 2)

    def test_page_range_excludes_cards_without_a_page(self):
        # 没有页码的卡（EPUB / 未锚定）不能被猜成第 1 页。
        self.book("repbook-a", "书", [("有页", 12, {"_st": "learn"}),
                                      ("没页", None, {"_st": "learn"})])
        payload = review_deck.take(self.root, pages=(10, 20))
        self.assertEqual([one["frontText"] for one in payload["cards"]], ["有页"])

    def test_kind_filter_separates_due_from_new(self):
        now = int(time.time() * 1000)
        self.book("repbook-a", "书", [("到期", 1, {"_next": now - 60_000}),
                                      ("新", 2, {"_st": "learn"})])
        self.assertEqual(
            [one["frontText"] for one in
             review_deck.take(self.root, kind="due")["cards"]], ["到期"])
        self.assertEqual(
            [one["frontText"] for one in
             review_deck.take(self.root, kind="new")["cards"]], ["新"])

    def test_scopes_cover_every_book_not_just_the_selected_one(self):
        # ① 他说"换一本"时，AI 得看得见没被选中的那本。
        self.book("repbook-a", "甲书", [("f", 1, {"_st": "learn"})])
        self.book("repbook-b", "乙书", [("g", 2, {"_st": "learn"})])
        payload = review_deck.take(self.root, book="甲书")
        self.assertEqual(len(payload["cards"]), 1)
        self.assertEqual(
            sorted(row["title"] for row in payload["scopes"]), ["乙书", "甲书"])

    def test_narrowed_to_nothing_is_not_the_same_as_nothing_to_review(self):
        # ② 两种"0 张"必须分得开。
        self.book("repbook-a", "甲书", [("f", 1, {"_st": "learn"})])
        missed = review_deck.take(self.root, book="并不存在的书")
        self.assertTrue(missed["narrowedToNothing"])
        self.assertIn("这个范围里没有卡", review_deck.render(missed))
        done = review_deck.take(Path(tempfile.mkdtemp(prefix="empty-")))
        self.assertFalse(done["narrowedToNothing"])
        self.assertIn("没有该复习的卡", review_deck.render(done))

    def test_payload_states_that_no_reader_is_needed(self):
        # 这句话要跟着数据走到 AI 面前 —— 它上一次就是因为看不到书而放弃了。
        self.book("repbook-a", "甲书", [("f", 1, {"_st": "learn"})])
        self.assertIs(review_deck.take(self.root)["needsReaderOpen"], False)

    def test_page_spec_parsing(self):
        self.assertEqual(review_deck.parse_pages("10-30"), (10, 30))
        self.assertEqual(review_deck.parse_pages("12"), (12, 12))
        self.assertEqual(review_deck.parse_pages("30-10"), (10, 30))
        for bad in ("", "abc", "0-5", "-3", "1-x"):
            self.assertIsNone(review_deck.parse_pages(bad), bad)


if __name__ == "__main__":
    unittest.main()
