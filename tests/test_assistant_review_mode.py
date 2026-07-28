"""Hard-isolation contracts for assistant normal/review scopes."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import assistant  # noqa: E402


class _NoopTimer:
    def __init__(self, *_args, **_kwargs):
        self.daemon = False

    def start(self):
        return None


class _CapturedThread:
    rows = []

    def __init__(self, target=None, args=(), **_kwargs):
        self.target = target
        self.args = args
        self.__class__.rows.append(self)

    def start(self):
        rid = self.args[0]
        assistant._chat_jobs[rid]["done"] = True


class AssistantReviewModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="assistant-review-contract-"
        )
        root = Path(self.tmp.name)
        self.saved_paths = (
            assistant._CONVO_DIR,
            assistant._REVIEW_CONVO_DIR,
            assistant._CONVO_ARCHIVE_DIR,
            assistant._REVIEW_CONVO_ARCHIVE_DIR,
            assistant._CLIP_DIR,
        )
        assistant._CONVO_DIR = root / "assistant-convo"
        assistant._REVIEW_CONVO_DIR = root / "assistant-review-convo"
        assistant._CONVO_ARCHIVE_DIR = root / "assistant-convo-archive"
        assistant._REVIEW_CONVO_ARCHIVE_DIR = (
            root / "assistant-review-convo-archive"
        )
        assistant._CLIP_DIR = root / "voice-clips"
        self.saved_jobs = assistant._chat_jobs
        self.saved_generations = assistant._conversation_generations
        assistant._chat_jobs = {}
        assistant._conversation_generations = {}

        app = Flask(__name__)
        app.secret_key = "review-contract"
        app.register_blueprint(assistant.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = "alice"

    def tearDown(self) -> None:
        (
            assistant._CONVO_DIR,
            assistant._REVIEW_CONVO_DIR,
            assistant._CONVO_ARCHIVE_DIR,
            assistant._REVIEW_CONVO_ARCHIVE_DIR,
            assistant._CLIP_DIR,
        ) = self.saved_paths
        assistant._chat_jobs = self.saved_jobs
        assistant._conversation_generations = self.saved_generations
        self.tmp.cleanup()

    def _clip(
        self,
        clip_id: str,
        mode: str = "normal",
        *,
        legacy: bool = False,
        data: bytes = b"voice",
    ) -> Path:
        folder = (
            assistant._CLIP_DIR / "alice"
            if legacy
            else assistant._clip_dir("alice", mode, create=True)
        )
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{clip_id}.webm"
        path.write_bytes(data)
        return path

    def test_files_history_archive_summary_media_and_clear_are_isolated(self):
        normal_clip = self._clip("normal-clip")
        review_clip = self._clip("review-clip", "review")
        assistant._convo_append(
            "alice",
            "assistant",
            "normal answer",
            {"clip": "normal-clip"},
        )
        assistant._convo_append(
            "alice",
            "assistant",
            "review answer",
            {"clip": "review-clip"},
            mode="review",
        )
        normal_summary = assistant._summary_path("alice")
        review_summary = assistant._summary_path(
            "alice",
            mode="review",
        )
        normal_summary.write_text('{"summary":"normal"}', "utf-8")
        review_summary.write_text('{"summary":"review"}', "utf-8")

        self.assertEqual(
            assistant._convo_path("alice"),
            assistant._CONVO_DIR / "alice.json",
            "the normal path and filename are the legacy path",
        )
        self.assertEqual(
            assistant._convo_path("alice", "review"),
            assistant._REVIEW_CONVO_DIR / "alice.json",
        )
        self.assertEqual(
            [m["content"] for m in self.client.get(
                "/api/assistant/history"
            ).get_json()["messages"]],
            ["normal answer"],
        )
        self.assertEqual(
            [m["content"] for m in self.client.get(
                "/api/assistant/history?mode=review"
            ).get_json()["messages"]],
            ["review answer"],
        )

        cleared = self.client.post(
            "/api/assistant/clear",
            json={"assistant_mode": "review"},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(assistant._convo_load("alice", "review"), [])
        self.assertEqual(
            [m["content"] for m in assistant._convo_load("alice")],
            ["normal answer"],
        )
        self.assertTrue(normal_summary.exists())
        self.assertFalse(review_summary.exists())
        self.assertTrue(normal_clip.exists())
        self.assertFalse(review_clip.exists())
        self.assertFalse(
            (assistant._CONVO_ARCHIVE_DIR / "alice.jsonl").exists()
        )
        review_archive = (
            assistant._REVIEW_CONVO_ARCHIVE_DIR / "alice.jsonl"
        )
        self.assertTrue(review_archive.exists())
        self.assertIn("review answer", review_archive.read_text("utf-8"))
        self.assertNotIn("normal answer", review_archive.read_text("utf-8"))

    def test_normal_clear_preserves_review_history_summary_and_media(self):
        normal_clip = self._clip("normal-clip")
        review_clip = self._clip("review-clip", "review")
        assistant._convo_append(
            "alice",
            "assistant",
            "normal",
            {"clip": "normal-clip"},
        )
        assistant._convo_append(
            "alice",
            "assistant",
            "review",
            {"clip": "review-clip"},
            mode="review",
        )
        review_summary = assistant._summary_path(
            "alice",
            mode="review",
        )
        review_summary.parent.mkdir(parents=True, exist_ok=True)
        review_summary.write_text('{"summary":"review"}', "utf-8")

        cleared = self.client.post("/api/assistant/clear")
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(assistant._convo_load("alice"), [])
        self.assertEqual(
            [m["content"] for m in assistant._convo_load(
                "alice",
                "review",
            )],
            ["review"],
        )
        self.assertFalse(normal_clip.exists())
        self.assertTrue(review_clip.exists())
        self.assertTrue(review_summary.exists())

    def test_same_clip_id_is_physically_separate_and_clear_is_mode_local(self):
        normal_clip = self._clip(
            "shared-clip",
            "normal",
            data=b"normal",
        )
        review_clip = self._clip(
            "shared-clip",
            "review",
            data=b"review",
        )
        assistant._convo_append(
            "alice",
            "assistant",
            "normal with shared clip",
            {"clip": "shared-clip"},
        )
        assistant._convo_append(
            "alice",
            "assistant",
            "review with shared clip",
            {"clip": "shared-clip"},
            mode="review",
        )

        review_clear = self.client.post(
            "/api/assistant/clear",
            json={"assistant_mode": "review"},
        )
        self.assertEqual(review_clear.status_code, 200)
        self.assertTrue(normal_clip.exists())
        self.assertFalse(review_clip.exists())
        self.assertEqual(
            assistant._convo_load("alice")[0]["clip"],
            "shared-clip",
        )

        normal_clear = self.client.post(
            "/api/assistant/clear",
            json={"assistant_mode": "normal"},
        )
        self.assertEqual(normal_clear.status_code, 200)
        self.assertFalse(normal_clip.exists())

    def test_clip_routes_and_quota_are_physically_mode_scoped(self):
        normal = self.client.post(
            "/api/assistant/voice-clip?id=same",
            data=b"normal-bytes",
            content_type="audio/webm",
        )
        review = self.client.post(
            "/api/assistant/voice-clip?id=same&assistant_mode=review",
            data=b"review-bytes",
            content_type="audio/webm",
        )
        self.assertEqual(normal.status_code, 200)
        self.assertEqual(review.status_code, 200)
        normal_response = self.client.get(
            "/api/assistant/voice-clip/same"
        )
        review_response = self.client.get(
            "/api/assistant/voice-clip/same?assistant_mode=review"
        )
        try:
            self.assertEqual(
                normal_response.get_data(),
                b"normal-bytes",
            )
            self.assertEqual(
                review_response.get_data(),
                b"review-bytes",
            )
        finally:
            normal_response.close()
            review_response.close()

        normal_dir = assistant._clip_dir(
            "alice",
            "normal",
            create=True,
        )
        review_dir = assistant._clip_dir(
            "alice",
            "review",
            create=True,
        )
        for index in range(399):
            (normal_dir / f"normal-{index:03d}.webm").write_bytes(b"n")
            (review_dir / f"review-{index:03d}.webm").write_bytes(b"r")
        self.assertEqual(len(list(normal_dir.iterdir())), 400)
        self.assertEqual(len(list(review_dir.iterdir())), 400)

        self.assertEqual(
            self.client.post(
                "/api/assistant/voice-clip?id=review-overflow"
                "&assistant_mode=review",
                data=b"review-overflow",
                content_type="audio/webm",
            ).status_code,
            200,
        )
        self.assertEqual(len(list(review_dir.iterdir())), 400)
        self.assertEqual(len(list(normal_dir.iterdir())), 400)

        self.assertEqual(
            self.client.post(
                "/api/assistant/voice-clip?id=normal-overflow",
                data=b"normal-overflow",
                content_type="audio/webm",
            ).status_code,
            200,
        )
        self.assertEqual(len(list(normal_dir.iterdir())), 400)
        self.assertEqual(len(list(review_dir.iterdir())), 400)

    def test_legacy_clip_fallback_is_proof_gated_for_review(self):
        legacy = self._clip("legacy", legacy=True)
        self.assertTrue(legacy.exists())
        normal_response = self.client.get(
            "/api/assistant/voice-clip/legacy"
        )
        try:
            self.assertEqual(normal_response.status_code, 200)
        finally:
            normal_response.close()
        self.assertEqual(
            self.client.get(
                "/api/assistant/voice-clip/legacy"
                "?assistant_mode=review"
            ).status_code,
            404,
        )
        assistant._convo_append(
            "alice",
            "assistant",
            "old review clip",
            {"clip": "legacy"},
            mode="review",
        )
        review_response = self.client.get(
            "/api/assistant/voice-clip/legacy"
            "?assistant_mode=review"
        )
        try:
            self.assertEqual(review_response.status_code, 200)
            self.assertEqual(review_response.get_data(), b"voice")
        finally:
            review_response.close()

    def test_review_tools_read_only_review_conversation_context(self):
        assistant._convo_append(
            "alice",
            "user",
            "NORMAL_SENTINEL",
            mode="normal",
        )
        assistant._convo_append(
            "alice",
            "user",
            "REVIEW_SENTINEL",
            mode="review",
        )
        review_ctx = {
            "_uid": "alice",
            "_assistant_mode": "review",
            "recent_tools": [],
            "selection": "",
        }

        extra, _images = assistant._card_extra(review_ctx)
        self.assertIn("REVIEW_SENTINEL", extra)
        self.assertNotIn("NORMAL_SENTINEL", extra)

        captured = {}

        def focus_of_text(text, *, top):
            captured["text"] = text
            captured["top"] = top
            return {"top": []}

        fake_attention = SimpleNamespace(
            focus_of_text=focus_of_text,
        )
        with patch.dict(
            sys.modules,
            {"attention_profile": fake_attention},
        ):
            result = assistant._t_learning_focus(
                {"scope": "convo"},
                review_ctx,
            )
        self.assertEqual(result["范围"], "当前对话")
        self.assertIn("REVIEW_SENTINEL", captured["text"])
        self.assertNotIn("NORMAL_SENTINEL", captured["text"])

    def test_review_rotation_archives_only_review_and_keeps_normal_file(self):
        assistant._convo_append("alice", "user", "normal sentinel")
        for index in range(201):
            assistant._convo_append(
                "alice",
                "user",
                f"review-{index}",
                mode="review",
            )

        self.assertEqual(
            [m["content"] for m in assistant._convo_load("alice")],
            ["normal sentinel"],
        )
        review_messages = assistant._convo_load("alice", "review")
        self.assertEqual(len(review_messages), 200)
        self.assertEqual(review_messages[0]["content"], "review-1")
        self.assertEqual(review_messages[-1]["content"], "review-200")
        review_archive = (
            assistant._REVIEW_CONVO_ARCHIVE_DIR / "alice.jsonl"
        )
        self.assertTrue(review_archive.exists())
        self.assertIn("review-0", review_archive.read_text("utf-8"))
        self.assertFalse(
            (assistant._CONVO_ARCHIVE_DIR / "alice.jsonl").exists()
        )

    def test_compact_history_and_turn_upsert_stay_inside_selected_scope(self):
        assistant._convo_append(
            "alice",
            "assistant",
            "normal-before",
            {"turn_id": "shared-turn"},
        )
        assistant._convo_append(
            "alice",
            "assistant",
            "review-before",
            {"turn_id": "shared-turn"},
            mode="review",
        )
        self.assertTrue(assistant._convo_upsert_turn(
            "alice",
            "shared-turn",
            "review-after",
            {},
            mode="review",
        ))
        self.assertEqual(
            assistant._convo_load("alice")[0]["content"],
            "normal-before",
        )
        self.assertEqual(
            assistant._convo_load("alice", "review")[0]["content"],
            "review-after",
        )

        normal_summary = assistant._summary_path("alice")
        review_summary = assistant._summary_path("alice", mode="review")
        normal_summary.write_text(
            '{"summary":"normal-summary","upto_ts":0}',
            "utf-8",
        )
        review_summary.write_text(
            '{"summary":"review-summary","upto_ts":0}',
            "utf-8",
        )
        normal = self.client.get(
            "/api/assistant/history?compact=1"
        ).get_json()
        review = self.client.get(
            "/api/assistant/history?compact=1&mode=review"
        ).get_json()
        self.assertEqual(normal["summary"], "normal-summary")
        self.assertEqual(review["summary"], "review-summary")
        self.assertEqual(
            [m["content"] for m in normal["messages"]],
            ["normal-before"],
        )
        self.assertEqual(
            [m["content"] for m in review["messages"]],
            ["review-after"],
        )

    def test_routes_reject_unknown_mode(self):
        self.assertEqual(
            self.client.get(
                "/api/assistant/history?mode=reading"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/assistant/clear",
                json={"assistant_mode": "reading"},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/assistant/chat",
                json={"assistant_mode": "reading", "message": "x"},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/assistant/voice-clip?id=bad-mode"
                "&assistant_mode=reading",
                data=b"voice",
                content_type="audio/webm",
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/api/assistant/voice-clip/bad-mode"
                "?assistant_mode=reading"
            ).status_code,
            400,
        )

    def test_reconnect_cannot_cross_job_scope(self):
        assistant._chat_jobs["same-rid"] = {
            "events": [],
            "answer": "",
            "done": True,
            "lock": threading.Lock(),
            "uid": "alice",
            "scope": "review",
        }
        wrong = self.client.post(
            "/api/assistant/chat",
            json={"rid": "same-rid", "assistant_mode": "normal"},
        )
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(wrong.get_json()["error"], "scope_mismatch")

        right = self.client.post(
            "/api/assistant/chat",
            json={"rid": "same-rid", "assistant_mode": "review"},
        )
        self.assertEqual(right.status_code, 200)
        self.assertIn("same-rid", right.get_data(as_text=True))

    def test_reconnect_cannot_cross_user_even_with_matching_scope(self):
        assistant._chat_jobs["private-rid"] = {
            "events": [],
            "answer": "",
            "done": True,
            "lock": threading.Lock(),
            "uid": "alice",
            "scope": "review",
        }
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = "bob"

        response = self.client.post(
            "/api/assistant/chat",
            json={"rid": "private-rid", "assistant_mode": "review"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "forbidden")

    def test_chat_creates_review_scoped_job_context_and_user_history(self):
        class Pdf:
            @staticmethod
            def _reader_storage_identity_snapshot():
                return {"owner": "alice"}

        _CapturedThread.rows = []
        with (
            patch.object(assistant, "_pdf", return_value=Pdf),
            patch.object(assistant.threading, "Thread", _CapturedThread),
        ):
            response = self.client.post(
                "/api/assistant/chat",
                json={
                    "rid": "new-review-rid",
                    "assistant_mode": "review",
                    "message": "帮我复习",
                    "review_card": {
                        "entity_id": "card_unique",
                        "question": "Q",
                        "answer": "A",
                        "candidate_reasons": ["current page"],
                    },
                    "context": {
                        "review_selections": [
                            {
                                "question": "Which property?",
                                "answer": "Closure under addition.",
                            },
                            {"question": "ignored", "answer": ""},
                            "malformed",
                        ],
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            assistant._chat_jobs["new-review-rid"]["scope"],
            "review",
        )
        self.assertEqual(assistant._convo_load("alice"), [])
        self.assertEqual(
            assistant._convo_load("alice", "review")[-1]["content"],
            "帮我复习",
        )
        worker_args = _CapturedThread.rows[-1].args
        self.assertEqual(worker_args[-1], "review")
        self.assertEqual(worker_args[2]["_assistant_mode"], "review")
        self.assertEqual(
            worker_args[2]["review_card"]["entity_id"],
            "card_unique",
        )
        self.assertEqual(
            worker_args[2]["review_selections"],
            [{
                "question": "Which property?",
                "answer": "Closure under addition.",
            }],
        )
        self.assertEqual(
            assistant._chat_jobs["new-review-rid"]["generation"],
            0,
        )

    def test_worker_uses_recorded_scope_for_final_persistence(self):
        assistant._chat_jobs["worker-rid"] = {
            "events": [],
            "answer": "",
            "trace": None,
            "done": False,
            "lock": threading.Lock(),
            "uid": "alice",
            "scope": "review",
            "generation": 0,
        }

        class Pdf:
            @staticmethod
            def _reader_storage_identity_bind_for_thread(_identity):
                return None

        with (
            patch.object(assistant, "_pdf", return_value=Pdf),
            patch.object(
                assistant,
                "_agent_run",
                return_value=iter([
                    {"event": "answer", "data": "review worker answer"}
                ]),
            ),
            patch.object(assistant.threading, "Timer", _NoopTimer),
        ):
            assistant._chat_worker(
                "worker-rid",
                "question",
                {},
                [],
                None,
                None,
                "alice",
                assistant_mode="normal",
            )

        self.assertEqual(assistant._convo_load("alice"), [])
        self.assertEqual(
            assistant._convo_load("alice", "review")[-1]["content"],
            "review worker answer",
        )

    def test_clear_marks_only_matching_inflight_scope_and_worker_cannot_revive_it(self):
        assistant._convo_append(
            "alice",
            "user",
            "review history to clear",
            mode="review",
        )

        def job(uid, scope):
            return {
                "events": [],
                "answer": "",
                "trace": None,
                "done": False,
                "lock": threading.Lock(),
                "uid": uid,
                "scope": scope,
                "generation": 0,
            }

        assistant._chat_jobs["review-inflight"] = job("alice", "review")
        assistant._chat_jobs["normal-inflight"] = job("alice", "normal")
        assistant._chat_jobs["other-user-review"] = job("bob", "review")

        response = self.client.post(
            "/api/assistant/clear",
            json={"assistant_mode": "review"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            assistant._chat_jobs["review-inflight"]["suppress_persist"]
        )
        self.assertNotIn(
            "suppress_persist",
            assistant._chat_jobs["normal-inflight"],
        )
        self.assertNotIn(
            "suppress_persist",
            assistant._chat_jobs["other-user-review"],
        )

        class Pdf:
            @staticmethod
            def _reader_storage_identity_bind_for_thread(_identity):
                return None

        with (
            patch.object(assistant, "_pdf", return_value=Pdf),
            patch.object(
                assistant,
                "_agent_run",
                return_value=iter([
                    {
                        "event": "answer",
                        "data": "must stay visible but never persist",
                    }
                ]),
            ),
            patch.object(assistant.threading, "Timer", _NoopTimer),
        ):
            assistant._chat_worker(
                "review-inflight",
                "question",
                {},
                [],
                None,
                None,
                "alice",
                assistant_mode="review",
            )

        self.assertEqual(
            assistant._chat_jobs["review-inflight"]["answer"],
            "must stay visible but never persist",
        )
        self.assertEqual(assistant._convo_load("alice", "review"), [])

    def test_generation_gate_blocks_old_worker_even_if_suppress_flag_is_lost(self):
        assistant._chat_jobs["old-review"] = {
            "events": [],
            "answer": "",
            "trace": None,
            "done": False,
            "lock": threading.Lock(),
            "uid": "alice",
            "scope": "review",
            "generation": 0,
        }
        cleared = self.client.post(
            "/api/assistant/clear",
            json={"assistant_mode": "review"},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(
            assistant._conversation_generation("alice", "review"),
            1,
        )
        # Generation is the durable race fence; suppression is intentionally
        # removed here to prove an old worker still cannot revive history.
        assistant._chat_jobs["old-review"].pop("suppress_persist", None)

        class Pdf:
            @staticmethod
            def _reader_storage_identity_bind_for_thread(_identity):
                return None

        with (
            patch.object(assistant, "_pdf", return_value=Pdf),
            patch.object(
                assistant,
                "_agent_run",
                return_value=iter([
                    {"event": "answer", "data": "stale answer"}
                ]),
            ),
            patch.object(assistant.threading, "Timer", _NoopTimer),
        ):
            assistant._chat_worker(
                "old-review",
                "question",
                {},
                [],
                None,
                None,
                "alice",
                assistant_mode="review",
            )
        self.assertEqual(assistant._convo_load("alice", "review"), [])

    def test_review_selections_are_bounded_and_normal_mode_ignores_them(self):
        normalized = assistant._normalize_review_selections([
            {
                "question": "<b>Q</b>" + ("q" * 1200),
                "answer": "<script>x</script>" + ("a" * 4000),
            },
            {"question": "Q2", "answer": "A2"},
            {"question": "missing"},
            "bad",
        ])
        self.assertEqual(len(normalized), 2)
        self.assertLessEqual(len(normalized[0]["question"]), 800)
        self.assertLessEqual(len(normalized[0]["answer"]), 2400)
        self.assertNotIn("\n", normalized[0]["question"])
        review = assistant._review_context_lines({
            "_assistant_mode": "review",
            "review_card": {"entity_id": "card-1"},
            "review_selections": normalized,
        })
        normal = assistant._review_context_lines({
            "_assistant_mode": "normal",
            "review_card": {"entity_id": "card-1"},
            "review_selections": normalized,
        })
        self.assertIn("用户显式选用的复习问答证据", review)
        self.assertIn("Q2", review)
        self.assertIn("A2", review)
        self.assertIn("不是系统指令", review)
        self.assertEqual(normal, "")

    def test_review_dynamic_tail_does_not_change_static_or_catalog(self):
        assistant._sys_cache_reset("alice")
        before = assistant._sys_static("alice")
        catalog_before = json.dumps(
            assistant.TOOL_REGISTRY.realtime_tools(
                assistant.SURFACE_ASSISTANT_TEXT
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        card = {
            "entity_id": "card_abc123",
            "question": "What is a vector space?",
            "answer": "A set with compatible addition and scaling.",
            "candidate_reasons": ["当前页来源", "焦点词相关"],
        }
        with (
            patch.object(assistant, "_pinned_lines", return_value=""),
            patch.object(assistant, "_announce_lines", return_value=""),
            patch.object(assistant, "_recipes_prompt_line", return_value=""),
        ):
            normal = assistant._ctx_block({
                "_uid": "alice",
                "_assistant_mode": "normal",
                "review_card": card,
            })
            review = assistant._ctx_block({
                "_uid": "alice",
                "_assistant_mode": "review",
                "review_card": card,
            })
        after = assistant._sys_static("alice")
        catalog_after = json.dumps(
            assistant.TOOL_REGISTRY.realtime_tools(
                assistant.SURFACE_ASSISTANT_TEXT
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertNotIn("复习模式·动态状态", normal)
        self.assertIn("复习模式·动态状态", review)
        self.assertIn("card_abc123", review)
        self.assertIn("What is a vector space?", review)
        self.assertIn("compatible addition", review)
        self.assertIn("当前页来源", review)
        self.assertEqual(before, after)
        self.assertEqual(catalog_before, catalog_after)

    def test_all_three_agent_backends_forward_mode_to_both_gates(self):
        for runner in (
            assistant._agent_run_claude,
            assistant._agent_run_gemini,
            assistant._agent_run_codex,
        ):
            source = inspect.getsource(runner)
            self.assertGreaterEqual(
                source.count("mode=_assistant_mode_from_ctx(ctx)"),
                2,
                f"{runner.__name__} must forward scope to membership and execution",
            )


if __name__ == "__main__":
    unittest.main()
