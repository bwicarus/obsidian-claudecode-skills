import copy
import json
import sys
import unittest
from pathlib import Path


DEPLOY = Path(__file__).resolve().parents[1] / "_server_deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

import assistant  # noqa: E402


LOCAL_FILE = "localbook:localbook-" + "c" * 64


def _notes():
    return [
        {
            "id": "placement-late",
            "anchor": {"kind": "pdf", "page": 7, "x": 0.7, "y": 0.2},
            "html": {
                "cid": "tool-card-2", "label": "old label", "type": "weather",
                "content": "<script>ignore()</script><b>晴天卡完整内容</b>",
                "contextText": "AI 应读的完整正文",
                "bind": {"kind": "page-chars", "page": 7, "from": 9, "to": 10, "text": "天气"},
            },
        },
        {
            "id": "placement-early",
            "anchor": {"kind": "pdf", "page": 7, "x": 0.2, "y": 0.3},
            "card": {
                "gid": "learning-card-1", "cid": "learning-card-1", "label": "问答卡",
                "bind": {"kind": "page-chars", "page": 7, "from": 2, "to": 3, "text": "锚定词"},
                "cards": [
                    {"q": "旧式问题", "a": "旧式答案"},
                    {"question": "渲染器问题", "answer": "渲染器答案"},
                ],
            },
        },
        {
            "id": "placement-unbound",
            "anchor": {"kind": "pdf", "page": 7, "x": 0.4, "y": 0.8},
            "html": {"cid": "historic-manual", "label": "历史手动卡", "content": "也必须让 AI 看到"},
        },
        {
            "id": "placement-other-page",
            "anchor": {"kind": "pdf", "page": 7, "x": 0.1, "y": 0.1},
            "html": {
                "content": "不应返回",
                "bind": {"kind": "page-chars", "page": 8, "from": 1, "to": 2, "text": "别页"},
            },
        },
    ]


def _projection():
    # Deliberately reverse bind.from order.  This proves that renderer geometry,
    # rather than Python's compatibility fallback, owns visible numbering.
    return {
        "contract": "reader-native-page-card-projection/1",
        "revision": 11,
        "pages": {
            "7": [
                {
                    "id": "placement-late", "number": 1, "kind": "card",
                    "label": "天气", "text": "AI 应读的完整正文",
                    "bind": {"kind": "page-chars", "page": 7, "from": 9, "to": 10, "text": "天气"},
                    "unbound": False,
                },
                {
                    "id": "placement-early", "number": 2, "kind": "anki",
                    "label": "锚定词", "text": "旧式问题 / 旧式答案 渲染器问题 / 渲染器答案",
                    "bind": {"kind": "page-chars", "page": 7, "from": 2, "to": 3, "text": "锚定词"},
                    "unbound": False,
                },
                {
                    "id": "placement-unbound", "number": None, "kind": "card",
                    "label": "历史手动卡", "text": "也必须让 AI 看到",
                    "bind": None, "unbound": True,
                },
            ],
        },
    }


def native_state(*, projected=True):
    state = {
        "contract": "reader-native-pdf-assistant-state/1",
        "file": LOCAL_FILE,
        "revisions": {"highlights": 2, "notes": 11, "ink": 3, "user_pages": 4},
        "highlights": [], "notes": _notes(), "ink": {}, "user_pages": [],
    }
    if projected:
        state["page_cards"] = _projection()
    return state


def native_context(*, projected=True):
    return {
        "file_rel": LOCAL_FILE, "page": 7, "pages": [7],
        "native_local_state": native_state(projected=projected),
    }


class PageCardAssistantToolsTests(unittest.TestCase):
    def test_query_uses_renderer_numbering_and_exposes_bounded_card_summaries(self):
        result = assistant._t_page_cards_query({}, native_context())

        self.assertEqual(result["revision"], 11)
        self.assertEqual(result["number_source"], "renderer-geometry")
        self.assertEqual(
            [(card["id"], card["number"], card["unbound"]) for card in result["cards"]],
            [
                ("placement-late", 1, False),
                ("placement-early", 2, False),
                ("placement-unbound", None, True),
            ],
        )
        self.assertEqual(result["cards"][0]["label"], "天气")
        self.assertEqual(result["cards"][0]["content"], "AI 应读的完整正文")
        self.assertTrue(result["cards"][0]["content_truncated"])
        self.assertNotIn("raw_content", result["cards"][0])
        self.assertNotIn("cards", result["cards"][1])
        self.assertIn("旧式问题 / 旧式答案", result["cards"][1]["content"])
        self.assertIn("渲染器答案", result["cards"][1]["content"])
        self.assertEqual(result["cards"][2]["content"], "也必须让 AI 看到")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["returned"], 3)
        self.assertFalse(result["truncated"])

    def test_query_falls_back_to_bind_order_without_claiming_geometry(self):
        result = assistant._t_page_cards_query({}, native_context(projected=False))

        self.assertEqual(result["number_source"], "bind-order")
        self.assertEqual(
            [(card["id"], card["number"]) for card in result["cards"]],
            [
                ("placement-early", 1),
                ("placement-late", 2),
                ("placement-unbound", None),
            ],
        )
        self.assertIn("仅按 bind.from/to", result["note"])

    def test_read_supports_unbound_stable_id_without_inventing_a_number(self):
        result = assistant._t_page_card_read(
            {"id": "placement-unbound"}, native_context()
        )

        self.assertEqual(result["card"]["id"], "placement-unbound")
        self.assertIsNone(result["card"]["number"])
        self.assertTrue(result["card"]["unbound"])
        source = json.loads(result["content"])
        self.assertEqual(source["content"], "也必须让 AI 看到")

    def test_read_chunks_complete_source_and_guards_continuation_revision(self):
        state = native_state()
        state["notes"][0]["html"]["content"] = "晴" * 5000
        state["notes"][0]["html"]["contextText"] = "完整天气正文"
        ctx = native_context()
        ctx["native_local_state"] = state

        first = assistant._t_page_card_read(
            {"id": "placement-late", "limit": 257}, ctx
        )
        self.assertTrue(first["truncated"])
        self.assertEqual(first["offset"], 0)
        self.assertEqual(first["next_offset"], len(first["content"]))
        chunks = [first["content"]]
        offset = first["next_offset"]
        while offset is not None:
            part = assistant._t_page_card_read({
                "id": "placement-late", "offset": offset, "limit": 257,
                "expected_revision": first["revision"],
            }, ctx)
            chunks.append(part["content"])
            offset = part["next_offset"]
        source = json.loads("".join(chunks))
        self.assertEqual(source["content"], "晴" * 5000)
        self.assertEqual(source["contextText"], "完整天气正文")

        missing = assistant._t_page_card_read(
            {"id": "placement-late", "offset": 1}, ctx
        )
        self.assertEqual(missing["code"], "page_card_revision_required")
        stale = assistant._t_page_card_read({
            "id": "placement-late", "offset": 1, "expected_revision": 10,
        }, ctx)
        self.assertEqual(stale["code"], "page_card_revision_conflict")

    def test_index_is_bounded_and_never_silently_slices_or_inlines_full_source(self):
        state = native_state()
        base = state["notes"][0]
        state["notes"] = []
        rows = []
        for index in range(40):
            note = copy.deepcopy(base)
            note["id"] = f"placement-{index:02d}"
            note["html"]["content"] = "正文" * 2000
            note["html"]["contextText"] = f"摘要-{index}-" + "甲" * 800
            note["html"]["bind"] = {
                "kind": "page-chars", "page": 7,
                "from": index * 2, "to": index * 2 + 1, "text": f"词{index}",
            }
            state["notes"].append(note)
            rows.append({
                "id": note["id"], "number": index + 1, "kind": "card",
                "label": f"词{index}", "text": note["html"]["contextText"][:2400],
                "bind": copy.deepcopy(note["html"]["bind"]), "unbound": False,
            })
        state["page_cards"]["pages"]["7"] = rows
        ctx = native_context()
        ctx["native_local_state"] = state
        result = assistant._t_page_cards_query({}, ctx)
        encoded = assistant._tool_result_for_model("page_cards_query", result)
        self.assertLessEqual(len(encoded.encode("utf-8")), 4200)
        self.assertEqual(result["count"], 40)
        self.assertLess(result["returned"], result["count"])
        self.assertTrue(result["truncated"])
        self.assertTrue(all(card["content_truncated"] for card in result["cards"]))
        self.assertEqual(encoded, json.dumps(result, ensure_ascii=False))

    def test_edit_requires_stable_reference_and_returns_strict_learning_card_action(self):
        missing = assistant._t_page_card_edit(
            {"number": 2, "cards": [{"type": "basic", "front": "Q", "back": "A"}]},
            native_context(),
        )
        self.assertEqual(missing["code"], "page_card_stable_reference_required")

        cards = [{"type": "basic", "front": "新问题", "back": "新答案"}]
        result = assistant._t_page_card_edit(
            {
                "number": 2, "id": "placement-early",
                "expected_revision": 11, "cards": cards,
            },
            native_context(),
        )

        self.assertTrue(result["pending"])
        action = result["client_action"]
        self.assertEqual(action["fn"], "_assistEdit")
        data = action["args"][0]
        self.assertEqual(data["type"], "page-card")
        self.assertEqual(data["op"], "edit")
        self.assertEqual(data["expected_id"], "placement-early")
        self.assertEqual(data["expected_revision"], 11)
        self.assertEqual(data["number"], 2)
        self.assertRegex(data["native_operation_id"], r"^npdf_[0-9a-f]{24}$")
        self.assertEqual(data["item"]["before"]["card"]["cards"][0]["q"], "旧式问题")
        self.assertEqual(data["item"]["after"]["card"]["cards"], cards)
        self.assertEqual(data["item"]["after"]["card"]["contextText"], "新问题 / 新答案")

        invalid = assistant._t_page_card_edit(
            {
                "number": 2, "id": "placement-early",
                "expected_revision": 11,
                "cards": [{"type": "basic", "front": "Q", "back": ""}],
            },
            native_context(),
        )
        self.assertEqual(invalid["code"], "page_card_cards_invalid")

    def test_write_requires_exact_renderer_geometry(self):
        result = assistant._t_page_card_delete(
            {
                "number": 1, "id": "placement-early",
                "expected_revision": 11,
            },
            native_context(projected=False),
        )
        self.assertEqual(result["code"], "page_card_geometry_required")

    def test_html_edit_replaces_content_and_context_text(self):
        result = assistant._t_page_card_edit(
            {
                "id": "placement-late",
                "expected_revision": 11, "content": "<b>新卡</b>",
                "contextText": "新卡可读文字",
            },
            native_context(projected=False),
        )
        after = result["client_action"]["args"][0]["item"]["after"]
        self.assertEqual(after["html"]["content"], "<b>新卡</b>")
        self.assertEqual(after["html"]["contextText"], "新卡可读文字")
        self.assertEqual(after["html"]["bind"]["text"], "天气")

    def test_page_card_fields_use_one_hundred_thousand_character_bug_fence(self):
        content = "正" * 100000
        context_text = "文" * 100000
        accepted = assistant._t_page_card_edit(
            {
                "id": "placement-late", "expected_revision": 11,
                "content": content, "contextText": context_text,
            },
            native_context(projected=False),
        )
        after = accepted["client_action"]["args"][0]["item"]["after"]
        self.assertEqual(after["html"]["content"], content)
        self.assertEqual(after["html"]["contextText"], context_text)

        learning = assistant._t_page_card_edit(
            {
                "id": "placement-early", "expected_revision": 11,
                "cards": [{
                    "type": "basic", "front": "问" * 100000, "back": "答",
                }],
            },
            native_context(projected=False),
        )
        self.assertEqual(
            learning["client_action"]["args"][0]["item"]["after"]
                ["card"]["cards"][0]["front"],
            "问" * 100000,
        )

        for arguments in (
            {
                "id": "placement-late", "expected_revision": 11,
                "content": "正" * 100001,
            },
            {
                "id": "placement-late", "expected_revision": 11,
                "content": "正文", "contextText": "文" * 100001,
            },
            {
                "id": "placement-early", "expected_revision": 11,
                "cards": [{
                    "type": "basic", "front": "问" * 100001, "back": "答",
                }],
            },
        ):
            rejected = assistant._t_page_card_edit(
                arguments, native_context(projected=False)
            )
            self.assertIn(
                rejected["code"],
                {"page_card_content_limit", "page_card_cards_invalid"},
            )

    def test_delete_is_placement_only_and_uses_before_snapshot(self):
        result = assistant._t_page_card_delete(
            {
                "number": 1, "id": "placement-late",
                "expected_revision": 11,
            },
            native_context(),
        )

        self.assertEqual(result["delete_scope"], "placement-only")
        data = result["client_action"]["args"][0]
        self.assertEqual(data["op"], "delete")
        self.assertEqual(data["item"]["id"], "placement-late")
        self.assertIn("before", data["item"])
        self.assertNotIn("after", data["item"])

    def test_write_fails_closed_when_revision_or_number_identity_changed(self):
        stale = assistant._t_page_card_delete(
            {
                "number": 1, "id": "placement-late",
                "expected_revision": 10,
            },
            native_context(),
        )
        self.assertEqual(stale["code"], "page_card_revision_conflict")
        self.assertEqual(stale["current_revision"], 11)

        moved = assistant._t_page_card_delete(
            {
                "number": 1, "id": "placement-early",
                "expected_revision": 11,
            },
            native_context(),
        )
        self.assertEqual(moved["code"], "page_card_identity_conflict")
        self.assertEqual(moved["current_id"], "placement-late")

    def test_unbound_card_can_be_edited_and_deleted_by_stable_id_without_number(self):
        edited = assistant._t_page_card_edit(
            {
                "id": "placement-unbound", "expected_revision": 11,
                "content": "自由卡新正文", "contextText": "AI 应读的新正文",
            },
            native_context(projected=False),
        )
        self.assertTrue(edited["pending"])
        self.assertIsNone(edited["number"])
        self.assertEqual(edited["page"], 7)
        edit_data = edited["client_action"]["args"][0]
        self.assertIsNone(edit_data["number"])
        self.assertEqual(edit_data["expected_id"], "placement-unbound")
        self.assertNotIn("bind", edit_data["item"]["after"]["html"])
        self.assertEqual(edit_data["item"]["after"]["html"]["content"], "自由卡新正文")

        deleted = assistant._t_page_card_delete(
            {"id": "placement-unbound", "expected_revision": 11},
            native_context(projected=False),
        )
        self.assertTrue(deleted["pending"])
        self.assertIsNone(deleted["number"])
        delete_data = deleted["client_action"]["args"][0]
        self.assertEqual(delete_data["page"], 7)
        self.assertEqual(delete_data["expected_id"], "placement-unbound")
        self.assertIsNone(delete_data["number"])

    def test_number_is_only_an_anchored_shortcut(self):
        result = assistant._t_page_card_delete(
            {
                "number": 1, "id": "placement-unbound",
                "expected_revision": 11,
            },
            native_context(),
        )
        self.assertEqual(result["code"], "page_card_identity_conflict")

    def test_malformed_optional_projection_is_rejected(self):
        state = native_state()
        state["page_cards"] = copy.deepcopy(state["page_cards"])
        state["page_cards"]["pages"]["7"][0]["number"] = 2
        ctx = native_context()
        ctx["native_local_state"] = state
        with self.assertRaisesRegex(ValueError, "序号投影"):
            assistant._native_pdf_state(ctx)

    def test_page_card_tools_have_their_own_bounded_namespace(self):
        self.assertEqual(
            {spec.name for spec in assistant.TOOL_REGISTRY.tools_in("page_cards")},
            {"page_cards_query", "page_card_read", "page_card_edit", "page_card_delete"},
        )
        self.assertLessEqual(len(assistant.TOOL_REGISTRY.tools_in("page_cards")), 9)
        self.assertIn("page_cards_query", assistant._READONLY_TOOLS)
        self.assertIn("page_card_read", assistant._READONLY_TOOLS)
        self.assertNotIn("page_card_edit", assistant._READONLY_TOOLS)
        self.assertNotIn("page_card_delete", assistant._READONLY_TOOLS)


if __name__ == "__main__":
    unittest.main()
