import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import epub_assistant as E  # noqa: E402


class _AssistantStub:
    @staticmethod
    def _note_color_arg(value):
        return str(value or "").lower()

    @staticmethod
    def _note_color_label(value):
        return str(value or "#ffffff")

    @staticmethod
    def _note_color_norm(value, dflt="#ffffff"):
        value = str(value or "")
        return value if value.startswith("#") else dflt


def native_state(**overrides):
    state = {
        "contract": "reader-native-epub-assistant-state/1",
        "file": "localbook:localbook-" + "b" * 64,
        "revisions": {"highlights": 3, "notes": 4, "ink": 5},
        "highlights": [],
        "notes": [],
        "ink": {},
    }
    state.update(overrides)
    return state


class NativeEPUBAssistantSemanticsTests(unittest.TestCase):
    def test_native_state_contract_is_exact_and_drives_note_reads(self):
        note = {
            "id": "c_1111111111111111",
            "anchor": {"kind": "epub", "section": 7},
            "text": "App owned note",
            "color": "#ffffff",
            "strokes": [{"pts": [[0, 0], [1, 1]]}],
        }
        ctx = {"native_local_state": native_state(notes=[note])}
        with mock.patch.object(E, "_A", return_value=_AssistantStub()):
            result = E._t_native_notes_read({"id": note["id"]}, ctx)
        self.assertEqual(result["text"], "App owned note")
        self.assertEqual(result["stroke_count"], 1)

        corrupt = native_state(notes=[note], unexpected=True)
        self.assertIsNone(E._native_epub_state({"native_local_state": corrupt}))

    def test_local_book_identity_is_conversation_scope_not_file_path(self):
        state = native_state()
        ctx = {"native_local_state": state}
        scope, native_only = E._epub_conversation_scope(ctx, "")
        self.assertEqual(scope, state["file"])
        self.assertTrue(native_only)
        self.assertNotIn("file_rel", ctx)

        remote_scope, remote_native_only = E._epub_conversation_scope(
            ctx, "Books/example.epub"
        )
        self.assertEqual(remote_scope, "Books/example.epub")
        self.assertFalse(remote_native_only)

        malformed = native_state(file="localbook:../../vault/book.epub")
        self.assertIsNone(E._native_epub_state({"native_local_state": malformed}))
        self.assertEqual(
            E._epub_conversation_scope(
                {"native_local_state": malformed}, ""
            ),
            ("", False),
        )

    def test_native_note_create_returns_only_a_client_pending_mutation(self):
        ctx = {"native_local_state": native_state(), "current_section_idx": 2}
        with mock.patch.object(E, "_A", return_value=_AssistantStub()):
            result = E._t_native_notes_create({"text": "remember this"}, ctx)
        self.assertTrue(result["pending"])
        self.assertNotIn("action", result)
        client = result["client_action"]
        self.assertEqual(client["fn"], "nativeLocalEPUBMutation")
        request = client["args"][0]
        self.assertEqual(request["contract"], "reader-native-epub-action/1")
        self.assertEqual(request["action"]["redo"]["op"], "sticky_create")
        self.assertIn("pending 说成已经写入", result["note"])

    def test_native_commit_updates_only_conversation_metadata_with_cas(self):
        app = Flask(__name__)
        app.secret_key = "test"
        previous = {
            "id": "act_1", "kind": "notes_create", "state": "done",
            "undo": {"op": "sticky_delete", "ids": ["c_1"]},
            "redo": {"op": "sticky_create", "notes": [{"id": "c_1"}]},
        }
        updated = copy.deepcopy(previous)
        updated["state"] = "undone"
        messages = [{"role": "assistant", "content": "ok", "actions": [copy.deepcopy(previous)]}]
        saved = []
        body = {
            "op": "native_commit", "requested_op": "undo", "file": "book.epub",
            "native_contract": "reader-native-epub-action/1",
            "previous_action": previous, "action": updated,
        }
        with app.test_request_context("/pdf/api/epub-action", method="POST", json=body), \
                mock.patch.object(E, "_logged_in", return_value=True), \
                mock.patch.object(E, "_uid", return_value="u1"), \
                mock.patch.object(E, "_econvo_load", return_value=messages), \
                mock.patch.object(E, "_econvo_save_all", side_effect=lambda uid, file, rows: (saved.append(copy.deepcopy(rows)) or True)), \
                mock.patch.object(E, "_action_undo", side_effect=AssertionError("Pi sidecar must not mutate")):
            response = E._eassistant_action()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(saved[0][0]["actions"][0]["state"], "undone")

    def test_native_commit_reports_persistence_failure(self):
        app = Flask(__name__)
        app.secret_key = "test"
        previous = {
            "id": "act_2", "kind": "notes_create", "state": "done",
            "undo": {"op": "sticky_delete", "ids": ["c_2"]},
            "redo": {"op": "sticky_create", "notes": [{"id": "c_2"}]},
        }
        updated = copy.deepcopy(previous)
        updated["state"] = "undone"
        body = {
            "op": "native_commit", "requested_op": "undo", "file": "book.epub",
            "native_contract": "reader-native-epub-action/1",
            "previous_action": previous, "action": updated,
        }
        with app.test_request_context("/pdf/api/epub-action", method="POST", json=body), \
                mock.patch.object(E, "_logged_in", return_value=True), \
                mock.patch.object(E, "_uid", return_value="u1"), \
                mock.patch.object(E, "_econvo_load", return_value=[{
                    "role": "assistant", "content": "ok", "actions": [copy.deepcopy(previous)]
                }]), \
                mock.patch.object(E, "_econvo_save_all", return_value=False), \
                mock.patch.object(E, "_action_undo", side_effect=AssertionError("Pi sidecar must not mutate")):
            response, status = E._eassistant_action()
        self.assertEqual(status, 503)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(response.get_json()["error"], "native_action_persist_failed")


if __name__ == "__main__":
    unittest.main()
