"""插删页时词锚（page-chars）跟着迁 —— 服务端 _pam_notes 的行为测试。

**行为测试，不是字面量断言**：把 `_pam_notes` 里的 `mut()` 函数体从
`pdf_reader.py` 原样抠出来 exec 后实跑。这样测的是文件里那段代码本身，
改名/搬走/语义悄悄变了都会立刻炸。直接 import pdf_reader 不现实（重依赖），
但也不能因此改成 match 字面量 —— 教训见 references/silent-failure-lessons.md 第六节。

背景：这条 2026-08-20 首次修时只覆盖了 card 槽，而 AI 直绑的词锚落在
html.bind（rc-stickynote.js::persistBoundCard）。漏的表现不是报错，
而是「卡片去了新页、描边和序号留在旧页」，看着像标记随机丢失。

App 表面的同一件事由 tests/reader_contract/page-migration-word-bind.behavior.test.mjs
覆盖（插删页在 App 内本地执行，两个表面各有一份实现）。
"""

import io
import json
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = io.open(_ROOT / "_server_deploy" / "pdf_reader.py", encoding="utf-8").read()


def _mut_body():
    i = _SRC.index("def _pam_notes(ctx):")
    j = _SRC.index("    plan, warn = _up_json_plan(", i)
    inner = _SRC[i:j].split("\n", 1)[1]
    # 去掉一层缩进，把内层 mut 提到模块级
    return "\n".join(l[4:] if l.startswith("    ") else l for l in inner.split("\n"))


_BODY = _mut_body()


def run(notes, mv):
    """用给定的页号映射跑一遍真实的 mut()，返回 (迁移后的 notes, changed)。"""
    d = json.loads(json.dumps(notes))
    ns = {"ctx": {"mv": mv}}
    exec(_BODY, ns)  # noqa: S102 - 就是要跑文件里那段真代码
    return d, ns["mut"](d)


def _insert_at_3(p):
    return p + 1 if p >= 3 else p


def _delete_3(p):
    if p == 3:
        return None
    return p - 1 if p > 3 else p


def _note(slot, page, anchor_page=5):
    return [{
        "id": "n1",
        "anchor": {"kind": "pdf", "page": anchor_page},
        slot: {"content": "x", "bind": {"kind": "page-chars", "page": page, "from": 10, "to": 12}},
    }]


class PageMigrationWordBindTests(unittest.TestCase):
    def test_card_slot_follows_insert(self):
        d, changed = run(_note("card", 4), _insert_at_3)
        self.assertEqual(d[0]["card"]["bind"]["page"], 5)
        self.assertEqual(d[0]["anchor"]["page"], 6, "anchor 那一路不能被带坏")
        self.assertTrue(changed)

    def test_html_slot_follows_insert(self):
        """AI 直绑的词锚落在 html 槽 —— 这一槽此前完全没被迁过。"""
        d, changed = run(_note("html", 4), _insert_at_3)
        self.assertEqual(d[0]["html"]["bind"]["page"], 5)
        self.assertTrue(changed)

    def test_anchored_page_deleted_clears_bind_but_keeps_note(self):
        """跟 anchor 的处置不同：anchor 没了便签无处安放（整条丢弃），
        词锚没了它还能作为普通便签继续存在。"""
        d, _ = run(_note("html", 3, anchor_page=9), _delete_3)
        self.assertEqual(len(d), 1, "便签不该被丢掉")
        self.assertIsNone(d[0]["html"]["bind"])
        self.assertEqual(d[0]["html"]["content"], "x", "卡片内容必须还在")

    def test_anchor_page_deleted_still_drops_the_note(self):
        """原有语义，不能被词锚这次改动带坏。"""
        d, _ = run(_note("card", 9, anchor_page=3), _delete_3)
        self.assertEqual(len(d), 0)

    def test_both_slots_migrate_independently(self):
        d, _ = run([{
            "id": "n2",
            "anchor": {"kind": "pdf", "page": 1},
            "card": {"bind": {"kind": "page-chars", "page": 4}},
            "html": {"bind": {"kind": "page-chars", "page": 6}},
        }], _insert_at_3)
        self.assertEqual(d[0]["card"]["bind"]["page"], 5)
        self.assertEqual(d[0]["html"]["bind"]["page"], 7)

    def test_untouched_shapes_stay_untouched(self):
        d, changed = run([
            {"id": "a", "anchor": {"kind": "pdf", "page": 1},
             "card": {"bind": {"kind": "epub-cfi", "page": 4}}},
            {"id": "b", "anchor": {"kind": "pdf", "page": 1}, "card": {"bind": None}},
            {"id": "c", "anchor": {"kind": "pdf", "page": 1}, "html": "不是对象"},
            {"id": "d", "anchor": {"kind": "pdf", "page": 1}, "text": "普通便签"},
            {"id": "e", "anchor": {"kind": "epub", "page": 4}},
        ], _insert_at_3)
        self.assertEqual(len(d), 5, "一条都不该被丢掉")
        self.assertEqual(d[0]["card"]["bind"]["page"], 4, "非 page-chars 不归这条管")
        self.assertIsNone(d[1]["card"]["bind"])
        self.assertEqual(d[2]["html"], "不是对象", "槽不是 dict 时要跳过，不能崩")
        self.assertEqual(d[3]["text"], "普通便签")
        self.assertEqual(d[4]["anchor"]["page"], 4, "EPUB 锚不参与 PDF 页号迁移")
        self.assertFalse(changed, "什么都没动时不该报 changed")

    def test_bool_page_is_skipped(self):
        """Python 里 True 是 int 的子类 —— 不排除的话会被当页号 1 迁走。

        ⚠ 这里必须用一个**会移动第 1 页**的映射。原先用的 _insert_at_3 对 True
        恰好返回 True（True >= 3 为假），于是有没有 bool 守卫结果都一样 ——
        变异验证时这条测试不会变红，等于没测。
        """
        insert_at_1 = lambda p: p + 1 if p >= 1 else p  # noqa: E731
        # 先确认这个映射对 True 确实有区分力，否则这条测试又白写
        self.assertEqual(insert_at_1(True), 2)
        d, changed = run([{
            "id": "f", "anchor": {"kind": "epub", "page": 9},
            "card": {"bind": {"kind": "page-chars", "page": True}},
        }], insert_at_1)
        self.assertIs(d[0]["card"]["bind"]["page"], True, "bool 页号被当成整数迁走了")
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
