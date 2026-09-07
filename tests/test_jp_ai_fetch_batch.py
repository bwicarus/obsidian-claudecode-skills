"""jp_ai_fetch_batch:一问多词、逐词归一落缓存;没回来的词不在返回里(2026-09-07 即时刷新存量旧条目)。"""
from pathlib import Path
import json
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "scripts" / "vocab", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import dict_sources as ds  # noqa: E402
import ai_client  # noqa: E402


class JpAiFetchBatchTests(unittest.TestCase):
    def test_batch_parses_object_normalizes_and_saves_each_word(self):
        payload = {
            "サングリア": {"reading": "サングリア", "romaji": "sanguria", "pos": "名詞", "zh": "桑格利亚酒",
                          "source_word": "sangría", "source_lang": "es", "examples": []},
            "エスカルゴ": {"reading": "エスカルゴ", "romaji": "esukarugo", "pos": "名詞", "zh": "法式焗蜗牛",
                          "source_word": "escargot", "source_lang": "fr", "source_kind": "loan", "examples": []},
        }
        saved = {}
        prompts = []

        def fake_ask(prompt, **kw):
            prompts.append(prompt)
            return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"

        with mock.patch.object(ai_client, "ask", side_effect=fake_ask), \
                mock.patch.object(ds, "_cache_save", side_effect=lambda src, w, d: saved.__setitem__(w, d)):
            out = ds.jp_ai_fetch_batch(["サングリア", "エスカルゴ", "サングリア", "  "], "haiku", None)
        self.assertEqual(sorted(out), ["エスカルゴ", "サングリア"])
        self.assertEqual(saved["サングリア"]["source_kind"], "loan", "有源词却没给 kind → 归一成 loan")
        self.assertEqual(saved["サングリア"]["pv"], ds._JP_PROMPT_VER)
        self.assertEqual(saved["サングリア"]["source"], "jp_ai")
        self.assertTrue(ds._jp_entry_fresh("エスカルゴ", saved["エスカルゴ"]))
        self.assertEqual(len(prompts), 1, "两个词一次提问")
        self.assertIn(ds._JP_RULES_TEXT[:20], prompts[0], "批量提示词与单词提示词共用同一段规则")
        self.assertIn('"サングリア", "エスカルゴ"', prompts[0])

    def test_missing_or_empty_entries_are_not_saved(self):
        payload = {"ゴミ": {"reading": "ゴミ", "zh": ""}, "サイト": {"reading": "サイト", "zh": "网站", "source_word": "site"}}
        saved = {}
        with mock.patch.object(ai_client, "ask", return_value=json.dumps(payload, ensure_ascii=False)), \
                mock.patch.object(ds, "_cache_save", side_effect=lambda src, w, d: saved.__setitem__(w, d)):
            out = ds.jp_ai_fetch_batch(["ゴミ", "サイト", "ウイルス"], "haiku", None)
        self.assertEqual(list(out), ["サイト"])
        self.assertEqual(list(saved), ["サイト"])

    def test_backend_failure_returns_empty(self):
        with mock.patch.object(ai_client, "ask", return_value=""), \
                mock.patch.object(ds, "_cache_save", side_effect=AssertionError("must not save")):
            self.assertEqual(ds.jp_ai_fetch_batch(["ゴミ"], "haiku", None), {})
        with mock.patch.object(ai_client, "ask", return_value="not json at all"), \
                mock.patch.object(ds, "_cache_save", side_effect=AssertionError("must not save")):
            self.assertEqual(ds.jp_ai_fetch_batch(["ゴミ"], "haiku", None), {})


if __name__ == "__main__":
    unittest.main()
