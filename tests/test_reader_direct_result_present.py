"""result.present 的隔离合同：直接命令展示，不调 AI、不触网。"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import assistant  # noqa: E402


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "reader_bridge_direct_result_test",
        ROOT / "scripts" / "reader_bridge.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DirectResultBottomTest(unittest.TestCase):
    def test_durably_upserts_history_and_publishes_existing_event(self) -> None:
        parts = [{
            "kind": "card",
            "card": {"kind": "fact", "data": {"answer": "42"}},
        }]
        events = types.SimpleNamespace(
            publish=mock.Mock(return_value=2))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            convo = root / "7.json"
            with mock.patch.object(
                    assistant, "_sanitize_ext_parts", return_value=parts), \
                    mock.patch.object(
                        assistant, "_convo_dir", return_value=root), \
                    mock.patch.object(
                        assistant, "_convo_path", return_value=convo), \
                    mock.patch.dict(sys.modules, {"reader_events": events}):
                result = assistant.reader_direct_present_result(
                    7,
                    text="",
                    parts=parts,
                    file="Physics/book.pdf",
                    page=24,
                    turn_id="voice.abc-1:card",
                )
                replay = assistant.reader_direct_present_result(
                    7,
                    text="",
                    parts=parts,
                    file="Physics/book.pdf",
                    page=24,
                    turn_id="voice.abc-1:card",
                )
            history = assistant.json.loads(convo.read_text("utf-8"))

        self.assertTrue(result["written"])
        self.assertTrue(result["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(result["delivery"]["subscribers"], 2)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "[卡片]")
        self.assertEqual(history[0]["parts"], parts)
        self.assertEqual(history[0]["file_rel"], "Physics/book.pdf")
        self.assertEqual(history[0]["page"], 24)
        self.assertEqual(history[0]["turn_id"], "voice.abc-1:card")
        self.assertEqual(history[0]["via"], "bridge")
        self.assertEqual(events.publish.call_count, 2)
        self.assertEqual(
            events.publish.call_args_list,
            [
                mock.call(
                    "assistant-history",
                    "Physics/book.pdf",
                    7,
                    {"turn_id": "voice.abc-1:card", "n": 1},
                ),
                mock.call(
                    "assistant-history",
                    "Physics/book.pdf",
                    7,
                    {"turn_id": "voice.abc-1:card", "n": 1},
                ),
            ],
        )

    def test_bad_turn_id_fails_before_history_write(self) -> None:
        with mock.patch.object(
                assistant, "_convo_put_direct_result") as put:
            with self.assertRaisesRegex(ValueError, "turn_id"):
                assistant.reader_direct_present_result(
                    7,
                    text="",
                    parts=[{"kind": "text", "text": "x"}],
                    file="book.pdf",
                    page=1,
                    turn_id="has space",
                )
        put.assert_not_called()

    def test_unsafe_card_url_fails_before_history_write(self) -> None:
        parts = [{
            "kind": "card",
            "card": {
                "kind": "images",
                "data": {
                    "items": [{
                        "url": "javascript:alert(1)",
                        "title": "bad",
                    }],
                },
            },
        }]
        with mock.patch.object(
                assistant, "_sanitize_ext_parts", return_value=parts), \
                mock.patch.object(
                    assistant, "_convo_put_direct_result") as put:
            with self.assertRaisesRegex(ValueError, "URL"):
                assistant.reader_direct_present_result(
                    7,
                    text="",
                    parts=parts,
                    file="book.pdf",
                    page=1,
                    turn_id="result-unsafe-url",
                )
        put.assert_not_called()

    def test_http_and_arbitrary_local_urls_fail_closed(self) -> None:
        for url in (
            "http://example.test/image.jpg",
            "/other/path/image.jpg",
        ):
            parts = [{
                "kind": "card",
                "card": {
                    "kind": "images",
                    "data": {"items": [{"url": url}]},
                },
            }]
            with self.subTest(url=url), \
                    mock.patch.object(
                        assistant, "_sanitize_ext_parts",
                        return_value=parts), \
                    mock.patch.object(
                        assistant, "_convo_put_direct_result") as put, \
                    self.assertRaisesRegex(ValueError, "URL|HTTPS"):
                assistant.reader_direct_present_result(
                    7,
                    text="",
                    parts=parts,
                    file="book.pdf",
                    page=1,
                    turn_id="result-unsafe-http",
                )
            put.assert_not_called()

    def test_frontend_event_reuses_existing_history_renderer(self) -> None:
        source = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-assistant.js"
        ).read_text("utf-8")
        self.assertIn("assistant-history", source)
        self.assertIn("function onHistoryEvent(ev)", source)
        self.assertIn("RC.turnCard.renderTurn('live' + tid, hit.parts)", source)
        self.assertIn("RC.turnCard.renderTurn(_rtid, m.parts)", source)


class CrossMachineDirectTransportTest(unittest.TestCase):
    def test_direct_command_is_only_forwarded_to_existing_endpoint(self) -> None:
        bridge = _load_bridge()
        command = {
            "contract": "reader-direct-command/1",
            "correlation": "result-1",
            "action": "result.present",
            "anchor": {"file": "book.pdf", "page": 1},
            "params": {
                "turnId": "result-1",
                "parts": [{"kind": "text", "text": "x"}],
            },
        }
        with mock.patch.object(
                bridge, "_api", return_value={"ok": True}) as api:
            result = bridge._do_direct_command({
                "request_id": "result-1",
                "payload": command,
            })
        self.assertTrue(result["ok"])
        api.assert_called_once_with("/pdf/api/direct-command", command)

    def test_direct_command_rejects_wrong_contract_before_network(self) -> None:
        bridge = _load_bridge()
        with mock.patch.object(bridge, "_api") as api:
            result = bridge._do_direct_command({
                "payload": {"contract": "reader-direct-command/2"},
            })
        self.assertFalse(result["ok"])
        api.assert_not_called()

    def test_result_command_rejects_mismatched_transport_id(self) -> None:
        bridge = _load_bridge()
        command = {
            "contract": "reader-direct-command/1",
            "correlation": "result-1",
            "action": "result.present",
        }
        with mock.patch.object(bridge, "_api") as api:
            result = bridge._do_direct_command({
                "request_id": "other-result",
                "payload": command,
            })
        self.assertFalse(result["ok"])
        self.assertIn("完全相同", result["error"])
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
