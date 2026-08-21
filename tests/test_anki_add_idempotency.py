"""At-most-once contracts for ``/pdf/api/anki-add-cards``.

AnkiConnect has no mutation receipt for ``addNote``.  A durable pending claim
must therefore exist before the irreversible request, and an unknown response
must never be retried under the same aid.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

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


class AnkiAddIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        state = Path(self.temp.name)
        self.patches = [
            patch.object(
                pdf_reader,
                "_ANKI_ADD_SEEN",
                state / "anki-add-seen.json",
            ),
            patch.object(
                pdf_reader,
                "_ANKI_ADD_RECEIPTS",
                state / "anki-add-receipts.json",
            ),
            patch.object(
                pdf_reader,
                "_ANKI_ADD_LOCK_FILE",
                state / "anki-add-idempotency.lock",
            ),
            patch.object(
                pdf_reader,
                "_ANKI_ADD_AID_LOCK_DIR",
                state / "anki-add-aid-locks",
            ),
        ]
        for item in self.patches:
            item.start()
        pdf_reader._ANKI_ADD_COMPLETED_MEMORY.clear()
        self.app = Flask(__name__)
        self.app.config["TESTING"] = True

    def tearDown(self):
        pdf_reader._ANKI_ADD_COMPLETED_MEMORY.clear()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _invoke(
        self,
        *,
        aid="fc_test",
        front="Q",
        back="A",
        entity_id="",
        cards=None,
    ):
        body = {
            "aid": aid,
            "cards": cards or [
                {"type": "basic", "front": front, "back": back}
            ],
            "card_index": 0,
        }
        if entity_id:
            body["entity_id"] = entity_id
        with self.app.test_request_context(
            "/pdf/api/anki-add-cards",
            method="POST",
            json=body,
        ):
            result = pdf_reader.pdf_api_anki_add_cards()
            if isinstance(result, tuple):
                response, status = result
            else:
                response = result
                status = response.status_code
            return status, response.get_json()

    @staticmethod
    def _anki_success(actions, *, delay=0):
        lock = threading.Lock()

        def urlopen(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            action = payload["action"]
            if action == "addNote":
                with lock:
                    actions.append(action)
                if delay:
                    time.sleep(delay)
                return _UrlResponse({"result": 101, "error": None})
            if action == "modelNames":
                return _UrlResponse({
                    "result": ["Basic", "Cloze"],
                    "error": None,
                })
            if action == "modelFieldNames":
                model = payload["params"]["modelName"]
                return _UrlResponse({
                    "result": (
                        ["Text", "Extra"]
                        if model == "Cloze"
                        else ["Front", "Back"]
                    ),
                    "error": None,
                })
            if action == "findCards":
                return _UrlResponse({
                    "result": [201],
                    "error": None,
                })
            if action in ("createDeck", "changeDeck"):
                return _UrlResponse({"result": None, "error": None})
            if action == "storeMediaFile":
                with lock:
                    actions.append(action)
                return _UrlResponse({
                    "result": payload["params"]["filename"],
                    "error": None,
                })
            raise AssertionError(action)

        return urlopen

    def test_concurrent_same_aid_adds_one_note(self):
        actions = []
        start = threading.Barrier(3)

        def run():
            start.wait(timeout=2)
            return self._invoke(aid="fc_concurrent")

        with patch(
            "urllib.request.urlopen",
            side_effect=self._anki_success(actions, delay=0.15),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(run)
                second = pool.submit(run)
                start.wait(timeout=2)
                results = [
                    first.result(timeout=5),
                    second.result(timeout=5),
                ]

        self.assertEqual(actions, ["addNote"])
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(
            sum(bool(data.get("dedup")) for _, data in results),
            1,
        )
        for _, data in results:
            self.assertEqual(data["note_ids"], [101])
            self.assertEqual(data["card_ids"], [201])
            self.assertEqual(
                data["card_ids_by_note"],
                {"101": [201]},
            )

    def test_successful_response_loss_retry_returns_same_ids(self):
        actions = []
        with patch(
            "urllib.request.urlopen",
            side_effect=self._anki_success(actions),
        ):
            first_status, first = self._invoke(aid="fc_lost")
            pdf_reader._ANKI_ADD_COMPLETED_MEMORY.clear()
            second_status, second = self._invoke(aid="fc_lost")

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(second["dedup"])
        self.assertEqual(first["note_ids"], second["note_ids"])
        self.assertEqual(first["card_ids"], second["card_ids"])
        self.assertEqual(
            first["card_ids_by_note"],
            second["card_ids_by_note"],
        )
        self.assertEqual(actions, ["addNote"])

    def test_image_markdown_is_stored_before_add_and_retry_does_not_repeat(self):
        actions = []
        captured_notes = []
        base = self._anki_success(actions)

        def urlopen(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            if payload["action"] == "addNote":
                captured_notes.append(payload["params"]["note"])
            return base(request, timeout)

        image_url = "https://images.example.test/curry.jpg?width=900"
        image_bytes = b"\xff\xd8\xffprojection-test"
        with (
            patch("urllib.request.urlopen", side_effect=urlopen),
            patch.object(
                pdf_reader,
                "_fetch_public_image",
                return_value=(image_bytes, "image/jpeg", image_url),
            ) as fetch,
        ):
            first_status, first = self._invoke(
                aid="fc_image",
                back="答案\n\n![配图](" + image_url + ")",
            )
            second_status, second = self._invoke(
                aid="fc_image",
                back="答案\n\n![配图](" + image_url + ")",
            )

        filename = pdf_reader._anki_projection_media_filename(
            image_bytes, "image/jpeg"
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(second["dedup"])
        self.assertEqual(actions, ["storeMediaFile", "addNote"])
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args, (image_url,))
        self.assertEqual(
            fetch.call_args.kwargs["allowed_schemes"], ("https",)
        )
        back = captured_notes[0]["fields"]["Back"]
        self.assertIn('src="' + filename + '"', back)
        self.assertNotIn("https://", back)
        self.assertEqual(first["note_ids"], second["note_ids"])

    def test_http_image_is_rejected_before_fetch_store_or_add(self):
        actions = []
        base = self._anki_success(actions)
        with (
            patch("urllib.request.urlopen", side_effect=base),
            patch.object(pdf_reader, "_fetch_public_image") as fetch,
        ):
            first_status, first = self._invoke(
                aid="fc_http_image",
                back="![配图](http://127.0.0.1/private.png)",
            )
            second_status, second = self._invoke(
                aid="fc_http_image",
                back="![配图](http://127.0.0.1/private.png)",
            )

        self.assertEqual(first_status, 400)
        self.assertEqual(second_status, 400)
        self.assertEqual(first["code"], "card_image_url_invalid")
        self.assertEqual(second["code"], "card_image_url_invalid")
        self.assertEqual(actions, [])
        fetch.assert_not_called()

    def test_long_face_is_not_silently_truncated_and_huge_face_is_explicit(self):
        actions = []
        captured = []
        base = self._anki_success(actions)

        def urlopen(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            if payload["action"] == "addNote":
                captured.append(payload["params"]["note"])
            return base(request, timeout)

        long_front = "长" * 8_001
        with patch("urllib.request.urlopen", side_effect=urlopen):
            status, _data = self._invoke(
                aid="fc_long_face",
                front=long_front,
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            captured[0]["fields"]["Front"].count("长"), 8_001
        )

        actions.clear()
        with patch(
            "urllib.request.urlopen",
            side_effect=self._anki_success(actions),
        ):
            status, data = self._invoke(
                aid="fc_huge_face",
                front="大" * 64_001,
            )
        self.assertEqual(status, 413)
        self.assertEqual(data["code"], "anki_card_projection_too_large")
        self.assertEqual(actions, [])

    def test_add_rejects_malformed_media_and_bad_magic_before_add(self):
        cases = [
            '<img src=https://images.example.test/a.png>',
            '<img src="a.png" srcset="https://images.example.test/a@2x.png 2x">',
            '<video poster="https://images.example.test/a.png"></video>',
            '<p style=color:red>unsafe</p>',
            '<p style="color:red">unsafe</p>',
        ]
        for index, html in enumerate(cases):
            actions = []
            with (
                self.subTest(html=html),
                patch(
                    "urllib.request.urlopen",
                    side_effect=self._anki_success(actions),
                ),
                patch.object(pdf_reader, "_fetch_public_image") as fetch,
            ):
                status, data = self._invoke(
                    aid=f"fc_bad_markup_{index}",
                    back=html,
                )
            self.assertEqual(status, 400)
            self.assertEqual(data["code"], "anki_media_url_invalid")
            self.assertEqual(actions, [])
            fetch.assert_not_called()

        actions = []
        image_url = "https://images.example.test/fake.png"
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=self._anki_success(actions),
            ),
            patch.object(
                pdf_reader,
                "_fetch_public_image",
                return_value=(b"not-a-png", "image/png", image_url),
            ),
        ):
            status, data = self._invoke(
                aid="fc_bad_magic",
                back="![bad](" + image_url + ")",
            )
        self.assertEqual(status, 415)
        self.assertEqual(data["code"], "anki_media_fetch_failed")
        self.assertEqual(actions, [])

    def test_add_enforces_unique_image_operation_limit_before_fetch(self):
        actions = []
        markdown = "\n".join(
            "![img](https://images.example.test/%d.png)" % index
            for index in range(9)
        )
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=self._anki_success(actions),
            ),
            patch.object(pdf_reader, "_fetch_public_image") as fetch,
        ):
            status, data = self._invoke(
                aid="fc_too_many_images",
                back=markdown,
            )
        self.assertEqual(status, 413)
        self.assertEqual(data["code"], "anki_media_fetch_failed")
        self.assertEqual(actions, [])
        fetch.assert_not_called()

    def test_first_explicit_add_rejection_releases_claim_for_retry(self):
        actions = []
        base = self._anki_success(actions)
        add_calls = 0

        def urlopen(request, timeout):
            nonlocal add_calls
            payload = json.loads(request.data.decode("utf-8"))
            if payload["action"] == "addNote":
                add_calls += 1
                if add_calls == 1:
                    actions.append("addNote")
                    return _UrlResponse({
                        "result": None,
                        "error": "model rejected note",
                    })
            return base(request, timeout)

        with patch("urllib.request.urlopen", side_effect=urlopen):
            first_status, first = self._invoke(aid="fc_explicit_retry")
            second_status, second = self._invoke(aid="fc_explicit_retry")

        self.assertEqual(first_status, 502)
        self.assertEqual(first["code"], "anki_add_rejected")
        self.assertEqual(second_status, 200)
        self.assertTrue(second["ok"])
        self.assertEqual(actions, ["addNote", "addNote"])

    def test_partial_batch_failure_stays_pending_and_never_replays(self):
        actions = []
        base = self._anki_success(actions)
        add_calls = 0

        def urlopen(request, timeout):
            nonlocal add_calls
            payload = json.loads(request.data.decode("utf-8"))
            if payload["action"] == "addNote":
                add_calls += 1
                actions.append("addNote")
                if add_calls == 1:
                    return _UrlResponse({"result": 101, "error": None})
                return _UrlResponse({
                    "result": None,
                    "error": "second note rejected",
                })
            return base(request, timeout)

        cards = [
            {"type": "basic", "front": "Q1", "back": "A1"},
            {"type": "basic", "front": "Q2", "back": "A2"},
        ]
        with patch("urllib.request.urlopen", side_effect=urlopen):
            first_status, first = self._invoke(
                aid="fc_partial_batch", cards=cards
            )
            second_status, second = self._invoke(
                aid="fc_partial_batch", cards=cards
            )

        self.assertEqual(first_status, 503)
        self.assertEqual(
            first["code"], "anki_add_partial_outcome_unknown"
        )
        self.assertEqual(first["outcome"], "partial")
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["note_ids"], [101])
        self.assertEqual(second_status, 409)
        self.assertEqual(second["code"], "anki_add_outcome_unknown")
        self.assertEqual(add_calls, 2)

    def test_same_aid_with_different_payload_is_rejected(self):
        actions = []
        with patch(
            "urllib.request.urlopen",
            side_effect=self._anki_success(actions),
        ):
            first_status, _ = self._invoke(
                aid="fc_reuse",
                front="first",
            )
            second_status, second = self._invoke(
                aid="fc_reuse",
                front="different",
            )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)
        self.assertEqual(second["code"], "anki_add_aid_reuse")
        self.assertEqual(actions, ["addNote"])

    def test_legacy_seen_list_is_deduplicated_without_anki(self):
        pdf_reader._ANKI_ADD_SEEN.write_text(
            json.dumps({"fc_legacy": [303]}),
            "utf-8",
        )
        with patch("urllib.request.urlopen") as urlopen:
            status, data = self._invoke(aid="fc_legacy")

        self.assertEqual(status, 200)
        self.assertTrue(data["dedup"])
        self.assertEqual(data["note_ids"], [303])
        self.assertEqual(data["card_ids"], [])
        urlopen.assert_not_called()

    def test_unknown_addnote_outcome_stays_pending(self):
        calls = 0
        base = self._anki_success([])

        def urlopen(request, timeout):
            nonlocal calls
            payload = json.loads(request.data.decode("utf-8"))
            if payload["action"] == "addNote":
                calls += 1
                raise TimeoutError("response lost")
            return base(request, timeout)

        with patch("urllib.request.urlopen", side_effect=urlopen):
            first_status, first = self._invoke(aid="fc_unknown")
            second_status, second = self._invoke(aid="fc_unknown")

        self.assertEqual(first_status, 503)
        self.assertEqual(first["code"], "anki_add_outcome_unknown")
        self.assertEqual(second_status, 409)
        self.assertEqual(second["code"], "anki_add_outcome_unknown")
        self.assertEqual(calls, 1)

    def test_terminal_receipt_failure_never_repeats_addnote(self):
        actions = []
        real_store = pdf_reader._anki_add_receipts_store
        stores = 0

        def flaky_store(value, protect=()):
            nonlocal stores
            stores += 1
            if stores == 2:
                raise OSError("simulated terminal fsync failure")
            return real_store(value, protect)

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=self._anki_success(actions),
            ),
            patch.object(
                pdf_reader,
                "_anki_add_receipts_store",
                side_effect=flaky_store,
            ),
        ):
            first_status, first = self._invoke(
                aid="fc_commit_failure"
            )
            second_status, second = self._invoke(
                aid="fc_commit_failure"
            )
            pdf_reader._ANKI_ADD_COMPLETED_MEMORY.clear()
            third_status, third = self._invoke(
                aid="fc_commit_failure"
            )

        self.assertEqual(first_status, 200)
        self.assertFalse(first["durable"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second["dedup"])
        self.assertFalse(second["durable"])
        self.assertEqual(third_status, 409)
        self.assertEqual(third["code"], "anki_add_outcome_unknown")
        self.assertEqual(actions, ["addNote"])

    def test_corrupt_seen_ledger_fails_before_anki(self):
        pdf_reader._ANKI_ADD_SEEN.write_text("{broken", "utf-8")
        with patch("urllib.request.urlopen") as urlopen:
            status, data = self._invoke(aid="fc_corrupt")

        self.assertEqual(status, 503)
        self.assertEqual(
            data["code"],
            "anki_add_idempotency_unavailable",
        )
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
