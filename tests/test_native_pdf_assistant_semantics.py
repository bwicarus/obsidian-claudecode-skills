import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


DEPLOY = Path(__file__).resolve().parents[1] / "_server_deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

import assistant  # noqa: E402


LOCAL_FILE = "localbook:localbook-" + "a" * 64


def native_state(**overrides):
    value = {
        "contract": "reader-native-pdf-assistant-state/1",
        "file": LOCAL_FILE,
        "revisions": {
            "highlights": 3,
            "notes": 4,
            "ink": 5,
            "user_pages": 6,
        },
        "highlights": [{
            "id": "h_old", "page": 2, "rects": [[1, 2, 3, 4]],
            "text": "App highlight", "color": "#fff59d",
        }],
        "notes": [{
            "id": "nold", "anchor": {"kind": "pdf", "page": 2},
            "text": "App note", "color": "#ffffff", "strokes": [],
        }],
        "ink": {"2": [{"pts": [[0.1, 0.2], [0.3, 0.4]]}]},
        "user_pages": [{
            "id": "u_abcd", "page": 3, "title": "Local sheet",
            "blocks": [{"kind": "text", "text": "device-only text"}],
        }],
    }
    value.update(overrides)
    return value


def native_context(**overrides):
    value = {
        "file_rel": "books/demo.pdf",
        "page": 2,
        "pages": [2],
        "native_local_state": native_state(),
    }
    value.update(overrides)
    return value


def native_epub_context(**overrides):
    value = {
        "file_rel": "books/demo.epub",
        "page": 3,
        "native_local_state": {
            "contract": "reader-native-epub-assistant-state/1",
            "file": LOCAL_FILE,
            "revisions": {"highlights": 7, "notes": 8, "ink": 9},
            "highlights": [{
                "id": "e123", "anchor": {"section": 2, "start": 1, "end": 8},
                "text": "App EPUB highlight", "color": "#ffd54a",
            }],
            "notes": [{
                "id": "c_1234567890abcdef", "anchor": {"kind": "epub", "section": 2},
                "text": "App EPUB note", "color": "#ffffff", "strokes": [],
            }],
            "ink": {"2": [{"pts": [[0.1, 0.2], [0.3, 0.4]]}]},
        },
    }
    value.update(overrides)
    return value


class NativePDFAssistantSemanticsTests(unittest.TestCase):
    def test_generic_endpoints_route_epub_tools_to_the_app_snapshot(self):
        ctx = native_epub_context()
        state = assistant._native_reader_state(ctx)
        self.assertEqual(state["notes"][0]["text"], "App EPUB note")
        self.assertIsNone(assistant._native_pdf_state(ctx))

        handler, args, routed_ctx = assistant._native_epub_tool_call(
            "notes_query", {}, ctx
        )
        result = handler(args, routed_ctx)
        self.assertEqual(result["notes"][0]["text"], "App EPUB note")

        handler, args, routed_ctx = assistant._native_epub_tool_call(
            "read_page", {"page": 3}, ctx
        )
        self.assertEqual(handler.__name__, "_t_read_section")
        self.assertEqual(args["idx"], 2)
        self.assertEqual(routed_ctx["current_section_idx"], 2)

        handler, args, _routed_ctx = assistant._native_epub_tool_call(
            "read_page", {"pages": [2, 4]}, ctx
        )
        self.assertEqual(handler.__name__, "_t_read_section")
        self.assertEqual(args["sections"], [1, 3])
        self.assertEqual(args["idx"], 1)

        handler, args, routed_ctx = assistant._native_epub_tool_call(
            "highlight", {"text": "exact EPUB text", "page": 3}, ctx
        )
        result = handler(args, routed_ctx)
        self.assertEqual(result["client_action"]["fn"], "epubHighlight")
        self.assertEqual(result["client_action"]["args"][0]["section"], 2)
        self.assertEqual(
            result["client_action"]["args"][0]["texts"], ["exact EPUB text"]
        )

        created = assistant._run_tool(
            "notes_create", {"text": "generic EPUB note"}, ctx,
            surface=assistant.SURFACE_VOICE_EXECUTE,
        )
        self.assertTrue(created["pending"])
        self.assertEqual(created["client_action"]["fn"], "nativeLocalEPUBMutation")
        self.assertEqual(
            created["client_action"]["args"][0]["file"], LOCAL_FILE,
        )

        broken = native_epub_context()
        broken["native_local_state"] = dict(broken["native_local_state"], extra=True)
        with self.assertRaisesRegex(ValueError, "EPUB 助手状态合同无效"):
            assistant._native_reader_state(broken)

    def test_native_snapshot_is_strict_and_is_the_read_authority(self):
        ctx = native_context()
        self.assertEqual(assistant._native_pdf_state(ctx)["file"], LOCAL_FILE)
        with patch.object(assistant, "_pdf", side_effect=AssertionError("Pi sidecar read")):
            self.assertEqual(assistant._vb_hls("books/demo.pdf", ctx)[0]["text"], "App highlight")
            self.assertEqual(assistant._vb_notes("books/demo.pdf", ctx)[0]["text"], "App note")
        self.assertEqual(len(assistant._native_pdf_ink_for_page(ctx, 2)), 1)
        self.assertIn("device-only text", assistant._upage_read_text("books/demo.pdf", 3, ctx))

        broken = native_context()
        broken["native_local_state"] = dict(broken["native_local_state"], extra=True)
        with self.assertRaisesRegex(ValueError, "合同无效"):
            assistant._native_pdf_state(broken)

    def test_native_highlight_returns_geometry_action_without_pi_sidecar_write(self):
        class Rect:
            x0, y0, x1, y1 = 10.0, 20.0, 80.0, 42.0

        class Page:
            rect = types.SimpleNamespace(width=600.0, height=800.0)

            @staticmethod
            def search_for(_text):
                return [Rect()]

        class Document:
            page_count = 5

            @staticmethod
            def close():
                return None

            @staticmethod
            def __getitem__(_page):
                return Page()

        fake_fitz = types.SimpleNamespace(open=lambda _path: Document())
        forbidden_pdf = types.SimpleNamespace(
            _hl_edit=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("Pi highlight sidecar write")
            )
        )
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(sys.modules, {"fitz": fake_fitz}), \
                patch.object(assistant, "VAULT_ROOT", Path(directory)), \
                patch.object(assistant, "_vb_localize", side_effect=lambda f, p: (f, p)), \
                patch.object(assistant, "_pdf", return_value=forbidden_pdf):
            result = assistant._t_highlight(
                {"text": "exact local text", "page": 2}, native_context()
            )

        self.assertEqual(result["highlighted"], 1)
        action = result["client_action"]
        self.assertEqual(action["fn"], "_assistEdit")
        data = action["args"][0]
        self.assertRegex(data["native_operation_id"], r"^npdf_[0-9a-f]{24}$")
        self.assertEqual(data["items"][0]["pdf_page"], 2)
        self.assertEqual(data["items"][0]["rects"], [[10.0, 20.0, 80.0, 42.0]])

    def test_native_notes_and_undo_are_client_commits_not_pi_mutations(self):
        forbidden_pdf = types.SimpleNamespace(
            _notes_edit=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("Pi note sidecar write")
            )
        )
        ctx = native_context()
        with patch.object(assistant, "_pdf", return_value=forbidden_pdf):
            created = assistant._t_notes_create({"text": "new local note"}, ctx)
            edited = assistant._t_notes_edit(
                {"id": "nold", "text": "edited locally"}, ctx
            )
        for result in (created, edited):
            action = result["client_action"]
            self.assertEqual(action["fn"], "_assistEdit")
            self.assertRegex(
                action["args"][0]["native_operation_id"],
                r"^npdf_[0-9a-f]{24}$",
            )
        self.assertEqual(edited["client_action"]["args"][0]["items"][0]["old"]["text"], "App note")
        self.assertEqual(edited["client_action"]["args"][0]["items"][0]["new"]["text"], "edited locally")

        undone = assistant._t_undo_last({}, ctx)
        self.assertEqual(undone["client_action"]["fn"], "_nativePDFUndoLast")
        self.assertRegex(undone["client_action"]["args"][0], r"^npdf_[0-9a-f]{24}$")


if __name__ == "__main__":
    unittest.main()
