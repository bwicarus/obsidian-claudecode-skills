"""Strict Pi AnkiConnect gateway and at-most-once mutation contracts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
if sys.platform == "win32" and "fcntl" not in sys.modules:
    fcntl_stub = types.ModuleType("fcntl")
    fcntl_stub.LOCK_EX = 1
    fcntl_stub.LOCK_SH = 2
    fcntl_stub.LOCK_NB = 4
    fcntl_stub.LOCK_UN = 8
    fcntl_stub.flock = lambda *_args, **_kwargs: None
    sys.modules["fcntl"] = fcntl_stub

import pdf_reader  # noqa: E402


class _UrlResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class AnkiCardOperationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        state = Path(self.temp.name)
        self.patches = [
            patch.object(
                pdf_reader,
                "_ANKI_CARD_OPERATION_RECEIPTS",
                state / "anki-card-operation-receipts.json",
            ),
            patch.object(
                pdf_reader,
                "_ANKI_CARD_OPERATION_LOCK_FILE",
                state / "anki-card-operation-idempotency.lock",
            ),
            patch.object(
                pdf_reader,
                "_ANKI_CARD_OPERATION_MUTATION_LOCK_DIR",
                state / "anki-card-operation-mutation-locks",
            ),
        ]
        for item in self.patches:
            item.start()
        pdf_reader._ANKI_CARD_OPERATION_COMPLETED_MEMORY.clear()
        self.app = Flask(__name__)
        self.app.config["TESTING"] = True

    def tearDown(self):
        pdf_reader._ANKI_CARD_OPERATION_COMPLETED_MEMORY.clear()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _invoke(self, body):
        with self.app.test_request_context(
            "/pdf/api/anki-card-operation",
            method="POST",
            json=body,
        ):
            result = pdf_reader.pdf_api_anki_card_operation()
            if isinstance(result, tuple):
                response, status = result
            else:
                response = result
                status = response.status_code
            return status, response.get_json()

    @staticmethod
    def _success(actions, *, delay=0):
        lock = threading.Lock()

        def call(action, params=None, **_kwargs):
            with lock:
                actions.append((action, params or {}))
            if delay and action in (
                "updateNoteFields", "deleteNotes", "answerCards", "sync"
            ):
                time.sleep(delay)
            if action == "notesInfo":
                return [
                    {"noteId": value, "fields": {"Front": {"value": "Q"}}}
                    for value in (params or {}).get("notes", [])
                ]
            if action == "cardsInfo":
                return [
                    {"cardId": value, "note": value + 1000, "due": 1}
                    for value in (params or {}).get("cards", [])
                ]
            if action == "answerCards":
                return [True] * len((params or {}).get("answers", []))
            if action in ("updateNoteFields", "deleteNotes", "sync"):
                return None
            raise AssertionError(action)

        return call

    def test_reads_exact_notes_without_mutation_id(self):
        actions = []
        with patch.object(
            pdf_reader,
            "_anki_card_operation_connect",
            side_effect=self._success(actions),
        ):
            status, data = self._invoke({
                "operation": "read-notes",
                "noteIds": [11, 12],
            })

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual([row["noteId"] for row in data["result"]], [11, 12])
        self.assertEqual(data["missing_ids"], [])
        self.assertEqual(actions, [("notesInfo", {"notes": [11, 12]})])
        self.assertEqual(data["reader_applied"]["status"], "not-owned")
        self.assertEqual(
            data["anki_local_applied"]["status"], "not-requested"
        )
        self.assertEqual(
            data["anki_web_sync"]["status"], "not-requested"
        )

    def test_connect_uses_fixed_ankiconnect_envelope_and_classifies_errors(self):
        captured = []

        def success(request, timeout):
            captured.append((json.loads(request.data), timeout))
            return _UrlResponse({"result": [{"noteId": 7}], "error": None})

        with patch("urllib.request.urlopen", side_effect=success):
            result = pdf_reader._anki_card_operation_connect(
                "notesInfo", {"notes": [7]}
            )
        self.assertEqual(result, [{"noteId": 7}])
        self.assertEqual(captured[0][0], {
            "action": "notesInfo",
            "version": 6,
            "params": {"notes": [7]},
        })
        self.assertEqual(captured[0][1], 15)

        with patch(
            "urllib.request.urlopen",
            return_value=_UrlResponse({"result": None, "error": "denied"}),
        ):
            with self.assertRaises(pdf_reader._AnkiCardActionError):
                pdf_reader._anki_card_operation_connect("sync")
        with patch(
            "urllib.request.urlopen",
            return_value=_UrlResponse({"unexpected": True}),
        ):
            with self.assertRaises(pdf_reader._AnkiCardProtocolError):
                pdf_reader._anki_card_operation_connect("sync")

    def test_reads_cards_and_reports_missing_ids(self):
        def call(action, params=None):
            self.assertEqual(action, "cardsInfo")
            self.assertEqual(params, {"cards": [21, 22]})
            return [{"cardId": 21}]

        with patch.object(
            pdf_reader, "_anki_card_operation_connect", side_effect=call
        ):
            status, data = self._invoke({
                "operation": "read-cards",
                "cardIds": [21, 22],
            })

        self.assertEqual(status, 200)
        self.assertEqual(data["missing_ids"], [22])

    def test_update_is_local_then_sync_and_replay_is_deduplicated(self):
        actions = []
        body = {
            "operation": "update-note-fields",
            "mutationId": "update-1",
            "noteId": 31,
            "fields": {"Front": "新问题", "Back": "新答案"},
        }
        with patch.object(
            pdf_reader,
            "_anki_card_operation_connect",
            side_effect=self._success(actions),
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(second["dedup"])
        self.assertEqual(
            [action for action, _ in actions],
            ["notesInfo", "updateNoteFields", "sync"],
        )
        self.assertEqual(first["anki_local_applied"]["status"], "succeeded")
        self.assertEqual(first["anki_web_sync"]["status"], "succeeded")
        self.assertEqual(first["updated_fields"], ["Back", "Front"])

    def test_update_stores_public_image_before_note_and_replay_is_idempotent(self):
        actions = []
        stored = {}

        def call(action, params=None, **_kwargs):
            params = params or {}
            actions.append((action, params))
            if action == "notesInfo":
                return [{"noteId": 32, "fields": {"Back": {"value": "old"}}}]
            if action == "storeMediaFile":
                stored.update(params)
                return params["filename"]
            if action in ("updateNoteFields", "sync"):
                return None
            raise AssertionError(action)

        image_url = "https://images.example.test/soup.png?size=large"
        image_bytes = b"\x89PNG\r\n\x1a\nprojection-test"
        body = {
            "operation": "update-note-fields",
            "mutationId": "update-image-1",
            "noteId": 32,
            "fields": {
                "Back": '<p>答案</p><img src="' + image_url + '">',
            },
        }
        with (
            patch.object(
                pdf_reader,
                "_anki_card_operation_connect",
                side_effect=call,
            ),
            patch.object(
                pdf_reader,
                "_fetch_public_image",
                return_value=(image_bytes, "image/png", image_url),
            ) as fetch,
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)

        filename = pdf_reader._anki_projection_media_filename(
            image_bytes, "image/png"
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(second["dedup"])
        self.assertEqual(
            [action for action, _ in actions],
            ["notesInfo", "storeMediaFile", "updateNoteFields", "sync"],
        )
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args, (image_url,))
        self.assertEqual(
            fetch.call_args.kwargs["allowed_schemes"], ("https",)
        )
        self.assertEqual(stored["filename"], filename)
        self.assertEqual(
            stored["data"],
            "iVBORw0KGgpwcm9qZWN0aW9uLXRlc3Q=",
        )
        updated = actions[2][1]["note"]["fields"]["Back"]
        self.assertIn('src="' + filename + '"', updated)
        self.assertNotIn("https://", updated)
        self.assertEqual(first["anki_local_applied"]["status"], "succeeded")

    def test_update_rejects_http_image_before_media_or_note_write(self):
        actions = []
        body = {
            "operation": "update-note-fields",
            "mutationId": "update-http-image",
            "noteId": 33,
            "fields": {
                "Back": '<img src="http://127.0.0.1/private.png">',
            },
        }
        with (
            patch.object(
                pdf_reader,
                "_anki_card_operation_connect",
                side_effect=self._success(actions),
            ),
            patch.object(pdf_reader, "_fetch_public_image") as fetch,
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)

        self.assertEqual(first_status, 400)
        self.assertEqual(second_status, 400)
        self.assertEqual(first["code"], "card_image_url_invalid")
        self.assertEqual(second["code"], "card_image_url_invalid")
        self.assertEqual(
            [action for action, _ in actions],
            ["notesInfo", "notesInfo"],
        )
        fetch.assert_not_called()

    def test_image_projection_rejects_malformed_or_autoloading_html(self):
        cases = [
            '<img src=https://images.example.test/a.png>',
            '<img src="a.png" src="b.png">',
            '<img src="a.png" srcset="https://images.example.test/a@2x.png 2x">',
            '<video poster="https://images.example.test/a.png"></video>',
            '<p style=color:red>unsafe</p>',
            '<p style="color:red">unsafe</p>',
        ]
        for index, html in enumerate(cases):
            actions = []
            with (
                self.subTest(html=html),
                patch.object(
                    pdf_reader,
                    "_anki_card_operation_connect",
                    side_effect=self._success(actions),
                ),
                patch.object(pdf_reader, "_fetch_public_image") as fetch,
            ):
                status, data = self._invoke({
                    "operation": "update-note-fields",
                    "mutationId": f"update-bad-markup-{index}",
                    "noteId": 40 + index,
                    "fields": {"Back": html},
                })
            self.assertEqual(status, 400)
            self.assertEqual(data["code"], "anki_media_url_invalid")
            self.assertEqual(
                [action for action, _params in actions], ["notesInfo"]
            )
            fetch.assert_not_called()

    def test_image_projection_checks_magic_and_operation_count_before_write(self):
        actions = []
        body = {
            "operation": "update-note-fields",
            "mutationId": "update-bad-magic",
            "noteId": 50,
            "fields": {
                "Back": '<img src="https://images.example.test/a.png">',
            },
        }
        with (
            patch.object(
                pdf_reader,
                "_anki_card_operation_connect",
                side_effect=self._success(actions),
            ),
            patch.object(
                pdf_reader,
                "_fetch_public_image",
                return_value=(b"not-a-png", "image/png", "https://images.example.test/a.png"),
            ),
        ):
            status, data = self._invoke(body)
        self.assertEqual(status, 415)
        self.assertEqual(data["code"], "anki_media_fetch_failed")
        self.assertEqual(
            [action for action, _params in actions], ["notesInfo"]
        )

        actions.clear()
        images = "".join(
            '<img src="https://images.example.test/%d.png">' % index
            for index in range(9)
        )
        with (
            patch.object(
                pdf_reader,
                "_anki_card_operation_connect",
                side_effect=self._success(actions),
            ),
            patch.object(pdf_reader, "_fetch_public_image") as fetch,
        ):
            status, data = self._invoke({
                "operation": "update-note-fields",
                "mutationId": "update-too-many-images",
                "noteId": 51,
                "fields": {"Back": images},
            })
        self.assertEqual(status, 413)
        self.assertEqual(data["code"], "anki_media_fetch_failed")
        self.assertEqual(
            [action for action, _params in actions], ["notesInfo"]
        )
        fetch.assert_not_called()

    def test_image_projection_aggregate_limit_writes_no_media(self):
        actions = []
        images = "".join(
            '<img src="https://images.example.test/%d.png">' % index
            for index in range(3)
        )
        large_png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\0" * (12 * 1024 * 1024 - 8)
        )
        with (
            patch.object(
                pdf_reader,
                "_anki_card_operation_connect",
                side_effect=self._success(actions),
            ),
            patch.object(
                pdf_reader,
                "_fetch_public_image",
                return_value=(large_png, "image/png", "https://images.example.test/fetched.png"),
            ) as fetch,
        ):
            status, data = self._invoke({
                "operation": "update-note-fields",
                "mutationId": "update-aggregate-too-large",
                "noteId": 53,
                "fields": {"Back": images},
            })

        self.assertEqual(status, 413)
        self.assertEqual(data["code"], "anki_media_fetch_failed")
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(
            [action for action, _params in actions], ["notesInfo"]
        )

    def test_pending_image_update_never_redownloads_or_rewrites_media(self):
        actions = []
        image_url = "https://images.example.test/pending.png"

        def call(action, params=None, **_kwargs):
            actions.append((action, params or {}))
            if action == "notesInfo":
                return [{"noteId": 52, "fields": {"Back": {"value": "old"}}}]
            if action == "storeMediaFile":
                return params["filename"]
            if action == "updateNoteFields":
                raise pdf_reader._AnkiCardTransportError("lost response")
            raise AssertionError(action)

        body = {
            "operation": "update-note-fields",
            "mutationId": "update-image-pending",
            "noteId": 52,
            "fields": {"Back": '<img src="' + image_url + '">'},
        }
        image_bytes = b"\x89PNG\r\n\x1a\npending"
        with (
            patch.object(
                pdf_reader,
                "_anki_card_operation_connect",
                side_effect=call,
            ),
            patch.object(
                pdf_reader,
                "_fetch_public_image",
                return_value=(image_bytes, "image/png", image_url),
            ) as fetch,
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)

        self.assertEqual(first_status, 503)
        self.assertEqual(first["code"], "outcome_unknown")
        self.assertEqual(second_status, 409)
        self.assertEqual(second["code"], "outcome_unknown")
        self.assertEqual(
            [action for action, _params in actions],
            ["notesInfo", "storeMediaFile", "updateNoteFields"],
        )
        fetch.assert_called_once()

    def test_media_filename_is_content_addressed(self):
        first = b"\x89PNG\r\n\x1a\nfirst"
        second = b"\x89PNG\r\n\x1a\nsecond"
        self.assertNotEqual(
            pdf_reader._anki_projection_media_filename(first, "image/png"),
            pdf_reader._anki_projection_media_filename(second, "image/png"),
        )

    def test_delete_is_explicitly_note_level_and_syncs(self):
        actions = []
        with patch.object(
            pdf_reader,
            "_anki_card_operation_connect",
            side_effect=self._success(actions),
        ):
            status, data = self._invoke({
                "operation": "delete-notes",
                "mutationId": "delete-1",
                "noteIds": [41, 42],
            })

        self.assertEqual(status, 200)
        self.assertEqual(data["delete_scope"], "note")
        self.assertEqual(data["deleted_note_ids"], [41, 42])
        self.assertEqual(
            [action for action, _ in actions],
            ["notesInfo", "deleteNotes", "sync"],
        )
        self.assertEqual(actions[1][1], {"notes": [41, 42]})

    def test_answer_cards_requires_explicit_cards_and_syncs(self):
        actions = []
        answers = [{"cardId": 51, "ease": 3}, {"cardId": 52, "ease": 2}]
        with patch.object(
            pdf_reader,
            "_anki_card_operation_connect",
            side_effect=self._success(actions),
        ):
            status, data = self._invoke({
                "operation": "answer-cards",
                "mutationId": "answer-1",
                "answers": answers,
            })

        self.assertEqual(status, 200)
        self.assertEqual(data["answered_cards"], answers)
        self.assertEqual(
            [action for action, _ in actions],
            ["cardsInfo", "answerCards", "sync"],
        )

    def test_local_success_with_unknown_sync_is_200_and_not_retried(self):
        actions = []

        def call(action, params=None):
            actions.append(action)
            if action == "notesInfo":
                return [{"noteId": 61}]
            if action == "updateNoteFields":
                return None
            if action == "sync":
                raise pdf_reader._AnkiCardTransportError("sync response lost")
            raise AssertionError(action)

        body = {
            "operation": "update-note-fields",
            "mutationId": "update-sync-unknown",
            "noteId": 61,
            "fields": {"Front": "Q2"},
        }
        with patch.object(
            pdf_reader, "_anki_card_operation_connect", side_effect=call
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)

        self.assertEqual(first_status, 200)
        self.assertTrue(first["ok"])
        self.assertFalse(first["complete"])
        self.assertEqual(first["anki_local_applied"]["status"], "succeeded")
        self.assertEqual(first["anki_web_sync"]["status"], "unknown")
        self.assertEqual(second_status, 200)
        self.assertTrue(second["dedup"])
        self.assertEqual(actions, ["notesInfo", "updateNoteFields", "sync"])

    def test_mutation_transport_unknown_stays_pending_and_never_repeats(self):
        calls = 0

        def call(action, params=None):
            nonlocal calls
            if action == "notesInfo":
                return [{"noteId": 71}]
            self.assertEqual(action, "updateNoteFields")
            calls += 1
            raise pdf_reader._AnkiCardTransportError("response lost")

        body = {
            "operation": "update-note-fields",
            "mutationId": "update-unknown",
            "noteId": 71,
            "fields": {"Back": "A2"},
        }
        with patch.object(
            pdf_reader, "_anki_card_operation_connect", side_effect=call
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)

        self.assertEqual(first_status, 503)
        self.assertEqual(first["code"], "outcome_unknown")
        self.assertEqual(second_status, 409)
        self.assertEqual(second["code"], "outcome_unknown")
        self.assertEqual(calls, 1)

    def test_mutation_id_reuse_with_different_payload_is_rejected(self):
        actions = []
        first = {
            "operation": "update-note-fields",
            "mutationId": "reused-1",
            "noteId": 81,
            "fields": {"Front": "one"},
        }
        second = dict(first, fields={"Front": "different"})
        with patch.object(
            pdf_reader,
            "_anki_card_operation_connect",
            side_effect=self._success(actions),
        ):
            first_status, _ = self._invoke(first)
            second_status, data = self._invoke(second)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)
        self.assertEqual(data["code"], "mutation_reused")
        self.assertEqual(
            [action for action, _ in actions],
            ["notesInfo", "updateNoteFields", "sync"],
        )

    def test_explicit_action_rejection_releases_claim_for_safe_retry(self):
        mutation_calls = 0

        def call(action, params=None):
            nonlocal mutation_calls
            if action == "notesInfo":
                return [{"noteId": 91}]
            if action == "updateNoteFields":
                mutation_calls += 1
                if mutation_calls == 1:
                    raise pdf_reader._AnkiCardActionError("field not found")
                return None
            if action == "sync":
                return None
            raise AssertionError(action)

        body = {
            "operation": "update-note-fields",
            "mutationId": "retry-explicit",
            "noteId": 91,
            "fields": {"Front": "Q"},
        }
        with patch.object(
            pdf_reader, "_anki_card_operation_connect", side_effect=call
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)

        self.assertEqual(first_status, 502)
        self.assertEqual(first["anki_local_applied"]["status"], "not-applied")
        self.assertEqual(second_status, 200)
        self.assertTrue(second["ok"])
        self.assertEqual(mutation_calls, 2)

    def test_sync_unknown_is_never_repeated_under_same_mutation(self):
        calls = 0

        def call(action, params=None):
            nonlocal calls
            self.assertEqual(action, "sync")
            calls += 1
            raise pdf_reader._AnkiCardTransportError("sync response lost")

        body = {"operation": "sync", "mutationId": "sync-unknown"}
        with patch.object(
            pdf_reader, "_anki_card_operation_connect", side_effect=call
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)

        self.assertEqual(first_status, 503)
        self.assertEqual(first["anki_web_sync"]["status"], "unknown")
        self.assertEqual(second_status, 409)
        self.assertEqual(second["code"], "outcome_unknown")
        self.assertEqual(
            second["anki_local_applied"]["status"], "not-requested"
        )
        self.assertEqual(second["anki_web_sync"]["status"], "unknown")
        self.assertEqual(calls, 1)

    def test_terminal_receipt_failure_never_repeats_local_mutation(self):
        actions = []
        real_store = pdf_reader._anki_card_operation_receipts_store
        stores = 0

        def flaky_store(value, protect=()):
            nonlocal stores
            stores += 1
            if stores == 2:
                raise OSError("simulated terminal fsync failure")
            return real_store(value, protect)

        body = {
            "operation": "delete-notes",
            "mutationId": "delete-terminal-failure",
            "noteIds": [1001],
        }
        with (
            patch.object(
                pdf_reader,
                "_anki_card_operation_connect",
                side_effect=self._success(actions),
            ),
            patch.object(
                pdf_reader,
                "_anki_card_operation_receipts_store",
                side_effect=flaky_store,
            ),
        ):
            first_status, first = self._invoke(body)
            second_status, second = self._invoke(body)
            pdf_reader._ANKI_CARD_OPERATION_COMPLETED_MEMORY.clear()
            third_status, third = self._invoke(body)

        self.assertEqual(first_status, 200)
        self.assertFalse(first["durable"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second["dedup"])
        self.assertFalse(second["durable"])
        self.assertEqual(third_status, 409)
        self.assertEqual(third["code"], "outcome_unknown")
        self.assertEqual(
            [action for action, _ in actions].count("deleteNotes"), 1
        )

    def test_concurrent_same_mutation_calls_local_write_once(self):
        actions = []
        start = threading.Barrier(3)
        body = {
            "operation": "delete-notes",
            "mutationId": "delete-concurrent",
            "noteIds": [101],
        }

        def run():
            start.wait(timeout=2)
            return self._invoke(body)

        with patch.object(
            pdf_reader,
            "_anki_card_operation_connect",
            side_effect=self._success(actions, delay=0.1),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(run)
                second = pool.submit(run)
                start.wait(timeout=2)
                results = [first.result(timeout=5), second.result(timeout=5)]

        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(
            [action for action, _ in actions].count("deleteNotes"), 1
        )
        self.assertEqual([action for action, _ in actions].count("sync"), 1)
        self.assertEqual(
            sum(bool(data.get("dedup")) for _, data in results), 1
        )

    def test_validation_rejects_non_explicit_and_arbitrary_operations(self):
        invalid = [
            {"operation": "find-notes", "query": "deck:*"},
            {"operation": "read-notes", "noteIds": []},
            {"operation": "delete-notes", "noteIds": [1]},
            {
                "operation": "delete-notes",
                "mutationId": "delete-bad",
                "noteIds": [1],
                "cardIds": [2],
            },
            {
                "operation": "answer-cards",
                "mutationId": "answer-bad",
                "answers": [{"cardId": 1, "ease": 5}],
            },
        ]
        with patch.object(pdf_reader, "_anki_card_operation_connect") as call:
            for body in invalid:
                with self.subTest(body=body):
                    status, data = self._invoke(body)
                    self.assertEqual(status, 400)
                    self.assertFalse(data["ok"])
        call.assert_not_called()

    def test_corrupt_receipt_fails_before_anki(self):
        pdf_reader._ANKI_CARD_OPERATION_RECEIPTS.write_text("{broken", "utf-8")
        body = {"operation": "sync", "mutationId": "corrupt-1"}
        with patch.object(pdf_reader, "_anki_card_operation_connect") as call:
            status, data = self._invoke(body)

        self.assertEqual(status, 503)
        self.assertEqual(
            data["code"], "anki_card_operation_idempotency_unavailable"
        )
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
