"""Windows reader-result/1 → Reader 直接命令的隔离契约测试。

只 mock subprocess/call；不得访问 SSH、Pi、生产状态或真实阅读器。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import io
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_client():
    spec = importlib.util.spec_from_file_location(
        "bridge_client_result_test", ROOT / "scripts" / "bridge_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TransportContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _load_client()

    def test_remote_reads_stdin_without_nonexistent_stdin_flag_and_uses_utf8(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok":true}', stderr="")
        with mock.patch.object(
                self.client.subprocess, "run", return_value=completed) as run:
            out = self.client._run_once({"text": "物理学"})
        self.assertTrue(out["ok"])
        args, kwargs = run.call_args
        self.assertEqual(args[0][-2:], ["python3", self.client.REMOTE])
        self.assertNotIn("--stdin", args[0])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "strict")
        self.assertEqual(json.loads(kwargs["input"]), {"text": "物理学"})

    def test_reconnect_reuses_the_exact_same_request_id(self):
        seen = []

        def flaky(env):
            seen.append(json.loads(json.dumps(env)))
            if len(seen) == 1:
                raise self.client.BridgeError("dead master")
            return {"ok": True}

        with mock.patch.object(self.client, "_run_once", side_effect=flaky), \
                mock.patch.object(self.client, "_drop_master") as drop:
            out = self.client.call(
                "assistant_turn", {"text": ""}, request_id="same-id")
        self.assertTrue(out["ok"])
        self.assertTrue(out["_reconnected"])
        self.assertEqual([item["request_id"] for item in seen],
                         ["same-id", "same-id"])
        self.assertEqual(seen[0], seen[1])
        drop.assert_called_once_with()


class ResultMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _load_client()

    @staticmethod
    def base(kind, payload):
        return {
            "envelope": "reader-result/1",
            "correlation": "voice.abc-1:card",
            "kind": kind,
            "payload": payload,
            "anchor": {"file": "Physics/费恩曼.pdf", "page": 24},
        }

    def publish(self, env):
        with mock.patch.object(
                self.client, "call", return_value={"ok": True}) as call:
            result = self.client.publish_result(env)
        self.assertTrue(result["ok"])
        return call.call_args

    @staticmethod
    def command_parts(args):
        command = args[1]
        return command, command["params"]["parts"]

    def test_weather_maps_to_result_present_direct_command(self):
        env = self.base("weather", {
            "lo": 18, "hi": 27, "cond": "晴", "loc": "东京"})
        env.update({
            "title": "今日天气",
            "brief": "适合散步",
            "sources": [{"url": "https://example.test/w", "title": "天气源"}],
        })
        args, kwargs = self.publish(env)
        command, parts = self.command_parts(args)
        self.assertEqual(args[0], "direct_command")
        self.assertEqual(command["contract"], "reader-direct-command/1")
        self.assertEqual(command["correlation"], "voice.abc-1:card")
        self.assertEqual(command["mode"], "independent")
        self.assertEqual(command["idempotency"], "voice.abc-1:card")
        self.assertEqual(command["action"], "result.present")
        self.assertEqual(command["anchor"], {
            "file": "Physics/费恩曼.pdf", "page": 24,
        })
        self.assertEqual(command["params"]["turnId"], "voice.abc-1:card")
        self.assertEqual(parts, [{
            "kind": "card",
            "card": {
                "kind": "weather",
                "data": {"lo": 18, "hi": 27, "cond": "晴", "loc": "东京"},
                "title": "今日天气",
                "brief": "适合散步",
                "sources": [{
                    "url": "https://example.test/w", "title": "天气源"}],
            },
        }])
        self.assertEqual(kwargs, {"request_id": "voice.abc-1:card"})

    def test_ordinary_chat_stays_assistant_turn_without_card_inference(self):
        with mock.patch.object(
                self.client, "call", return_value={"ok": True}) as call:
            result = self.client.say("东京今天晴，最高 27 度")
        self.assertTrue(result["ok"])
        call.assert_called_once_with(
            "assistant_turn",
            {"text": "东京今天晴，最高 27 度"},
        )

    def test_all_tool_payload_shapes_map_without_network(self):
        samples = {
            "news": {"items": [{"t": "标题", "s": "摘要", "src": "来源"}]},
            "images": {"items": [{"url": "https://x/img.jpg", "title": "图"}]},
            "videos": {"items": [{"title": "视频", "url": "https://x/v"}]},
            "fact": {"answer": "42", "detail": "计算"},
            "general": {"text": "综合答案"},
        }
        for kind, payload in samples.items():
            with self.subTest(kind=kind):
                args, _ = self.publish(self.base(kind, payload))
                _, parts = self.command_parts(args)
                self.assertEqual(parts[0]["kind"], "card")
                self.assertEqual(parts[0]["card"]["kind"], kind)
                self.assertEqual(parts[0]["card"]["data"], payload)

    def test_reader_relative_page_image_is_allowed_only_for_image_surfaces(self):
        page_image = (
            "/pdf/api/page-image?"
            "file=Physics%2Fbook.pdf&page=24&w=1200&v=7"
        )
        args, _ = self.publish(self.base("images", {
            "items": [{"url": page_image, "title": "当前页"}],
        }))
        _, parts = self.command_parts(args)
        self.assertEqual(
            parts[0]["card"]["data"]["items"][0]["url"],
            page_image,
        )
        args, _ = self.publish(self.base("videos", {
            "items": [{
                "title": "讲解",
                "thumb": page_image,
                "url": "https://example.test/video",
            }],
        }))
        _, parts = self.command_parts(args)
        self.assertEqual(
            parts[0]["card"]["data"]["items"][0]["thumb"],
            page_image,
        )

    def test_cards_map_to_flashcards_and_infer_cloze_type(self):
        args, kwargs = self.publish(self.base("cards", {
            "cards": [
                {"front": "Q", "back": "A"},
                {"text": "光速是 {{c1::c}}"},
            ],
            "draft": True,
        }))
        command, parts = self.command_parts(args)
        self.assertEqual(args[0], "direct_command")
        self.assertEqual(parts, [{
            "kind": "cards",
            "cards": [
                {"type": "basic", "front": "Q", "back": "A"},
                {"type": "cloze", "text": "光速是 {{c1::c}}"},
            ],
            "draft": True,
        }])
        self.assertEqual(command["action"], "result.present")
        self.assertEqual(kwargs["request_id"], "voice.abc-1:card")

    def test_cli_publish_result_reads_stdin_and_uses_mapper(self):
        envelope = self.base("fact", {"answer": "42"})
        with mock.patch.object(
                self.client.sys, "argv",
                ["bridge_client.py", "--publish-result"]), \
                mock.patch.object(
                    self.client.sys, "stdin",
                    io.StringIO(
                        "\ufeff" + json.dumps(envelope, ensure_ascii=False)
                    )), \
                mock.patch.object(
                    self.client, "publish_result",
                    return_value={"ok": True, "written": True}) as publish, \
                mock.patch("builtins.print"):
            exit_code = self.client.main()
        self.assertEqual(exit_code, 0)
        publish.assert_called_once_with(envelope)

    def test_cli_validate_result_is_utf8_local_only(self):
        envelope = self.base("fact", {
            "answer": "费恩曼路径积分",
            "detail": "本地验证不触网",
        })
        printed = []
        with mock.patch.object(
                self.client.sys, "argv",
                ["bridge_client.py", "--validate-result"]), \
                mock.patch.object(
                    self.client.sys, "stdin",
                    io.StringIO(json.dumps(envelope, ensure_ascii=False))), \
                mock.patch.object(
                    self.client, "call",
                    side_effect=AssertionError("validate must not call SSH")), \
                mock.patch.object(
                    self.client, "_ssh_base",
                    side_effect=AssertionError("validate must not build SSH")), \
                mock.patch("builtins.print",
                           side_effect=lambda value, **_: printed.append(value)):
            exit_code = self.client.main()
        self.assertEqual(exit_code, 0)
        output = json.loads(printed[0])
        self.assertEqual(output["contract"], "reader-result-validation/1")
        self.assertFalse(output["networkAttempted"])
        self.assertEqual(output["action"], "direct_command")
        self.assertEqual(output["payload"]["action"], "result.present")
        self.assertEqual(
            output["payload"]["params"]["parts"][0]["card"]["data"]["answer"],
            "费恩曼路径积分",
        )


class ResultRejectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _load_client()

    @staticmethod
    def valid():
        return {
            "envelope": "reader-result/1",
            "correlation": "result-1",
            "kind": "fact",
            "payload": {"answer": "42"},
            "anchor": {"file": "book.pdf", "page": 1},
        }

    def assert_rejected_before_call(self, mutate, message):
        env = self.valid()
        mutate(env)
        with mock.patch.object(self.client, "call") as call, \
                self.assertRaisesRegex(self.client.ResultEnvelopeError, message):
            self.client.publish_result(env)
        call.assert_not_called()

    def test_rejects_wrong_contract_unknown_top_field_and_bad_correlation(self):
        self.assert_rejected_before_call(
            lambda e: e.update(envelope="reader-result/2"), "envelope 必须")
        self.assert_rejected_before_call(
            lambda e: e.update(evil=True), "未知/多余字段")
        self.assert_rejected_before_call(
            lambda e: e.update(correlation="空 格"), "correlation 必须")
        self.assert_rejected_before_call(
            lambda e: e.update(correlation="a" * 41), "correlation 必须")

    def test_rejects_bad_anchor_and_unrepresentable_selection(self):
        self.assert_rejected_before_call(
            lambda e: e.update(anchor={"file": "../secret", "page": 1}),
            "vault 相对路径")
        self.assert_rejected_before_call(
            lambda e: e.update(anchor={"file": "book.pdf", "page": 0}),
            "正整数")
        self.assert_rejected_before_call(
            lambda e: e.update(anchor={
                "file": "book.pdf", "page": 1, "selection": "x"}),
            "selection 当前没有确定性落点")

    def test_rejects_unknown_or_malformed_kind_payload_and_item_fields(self):
        self.assert_rejected_before_call(
            lambda e: e.update(kind="html"), "kind 不支持")
        self.assert_rejected_before_call(
            lambda e: e.update(payload={"detail": "missing answer"}),
            "缺字段")
        self.assert_rejected_before_call(
            lambda e: e.update(kind="images", payload={
                "items": [{"url": "https://x", "onclick": "evil"}]}),
            "未知/多余字段")

    def test_rejects_unsafe_result_urls_before_call(self):
        unsafe = [
            "http://example.test/image.jpg",
            "javascript:alert(1)",
            "data:image/png;base64,AA==",
            "file:///C:/secret",
            "//example.test/image.jpg",
            "https://user:pass@example.test/image.jpg",
            "https://example.test/\nhidden",
            "https://example.test/\u202eevil",
            "/other/path/image.jpg",
            "/pdf/api/page-image?file=../secret&page=1",
            "/pdf/api/page-image?file=book.pdf&page=0",
            "/pdf/api/page-image?file=book.pdf&page=1&next=https://evil.test",
        ]
        for url in unsafe:
            with self.subTest(url=url):
                self.assert_rejected_before_call(
                    lambda e, value=url: e.update(
                        kind="images",
                        payload={"items": [{"url": value}]},
                    ),
                    "URL|页图",
                )
        self.assert_rejected_before_call(
            lambda e: e.update(
                sources=[{
                    "url": "http://example.test/source",
                    "title": "不安全来源",
                }],
            ),
            "HTTPS",
        )
        self.assert_rejected_before_call(
            lambda e: e.update(
                kind="videos",
                payload={"items": [{
                    "title": "视频",
                    "url": "/pdf/api/page-image?file=book.pdf&page=1",
                }]},
            ),
            "HTTPS",
        )

    def test_cli_rejects_duplicate_keys_and_nonfinite_numbers(self):
        samples = [
            (
                '{"envelope":"reader-result/1","correlation":"a",'
                '"kind":"fact","payload":{"answer":"42","answer":"43"},'
                '"anchor":{"file":"book.pdf","page":1}}',
                "重复字段",
            ),
            (
                '{"envelope":"reader-result/1","correlation":"a",'
                '"kind":"fact","payload":{"answer":NaN},'
                '"anchor":{"file":"book.pdf","page":1}}',
                "不允许 NaN",
            ),
        ]
        for raw, message in samples:
            with self.subTest(message=message), \
                    mock.patch.object(
                        self.client.sys,
                        "argv",
                        ["bridge_client.py", "--validate-result"],
                    ), \
                    mock.patch.object(
                        self.client.sys, "stdin", io.StringIO(raw)
                    ), \
                    mock.patch.object(
                        self.client.sys, "stderr", io.StringIO()
                    ) as stderr, \
                    mock.patch.object(self.client, "call") as call:
                self.assertEqual(self.client.main(), 2)
                self.assertIn(message, stderr.getvalue())
                call.assert_not_called()

    def test_rejects_unrepresentable_cards_fields_and_non_draft(self):
        self.assert_rejected_before_call(
            lambda e: e.update(
                kind="cards",
                payload={"cards": [{"front": "Q", "back": "A", "tags": []}]}),
            "未知/多余字段")
        self.assert_rejected_before_call(
            lambda e: e.update(
                kind="cards",
                payload={"cards": [{"front": "Q"}], "draft": False}),
            "只能省略或为 true")
        self.assert_rejected_before_call(
            lambda e: e.update(
                kind="cards", payload={"cards": [{"back": "orphan"}]}),
            "缺 front/cloze/text")


if __name__ == "__main__":
    unittest.main()
