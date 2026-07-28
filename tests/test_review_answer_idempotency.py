"""`/pdf/api/review-answer` must never rate one aid twice.

The endpoint crosses an irreversible AnkiConnect side effect, so ordinary
"read seen -> call -> write seen" idempotency is insufficient.  These tests
exercise the real Flask handler with concurrent request contexts and temporary
durable ledgers.
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


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class ReviewAnswerIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        state = Path(self.temp.name)
        self.patches = [
            patch.object(
                pdf_reader,
                "_REVIEW_SEEN_FILE",
                state / "review-answers-seen.json",
            ),
            patch.object(
                pdf_reader,
                "_REVIEW_RECEIPTS_FILE",
                state / "review-answer-receipts.json",
            ),
            patch.object(
                pdf_reader,
                "_REVIEW_ANSWER_LOCK_FILE",
                state / "review-answer-idempotency.lock",
            ),
            patch.object(
                pdf_reader,
                "_REVIEW_ANSWER_AID_LOCK_DIR",
                state / "review-answer-aid-locks",
            ),
        ]
        for item in self.patches:
            item.start()
        pdf_reader._REVIEW_COMPLETED_MEMORY.clear()
        self.app = Flask(__name__)
        self.app.config["TESTING"] = True

    def tearDown(self):
        pdf_reader._REVIEW_COMPLETED_MEMORY.clear()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _invoke(self, aid="rv_test", card_id=101, ease=3):
        with self.app.test_request_context(
            "/pdf/api/review-answer",
            method="POST",
            json={"aid": aid, "card_id": card_id, "ease": ease},
        ):
            result = pdf_reader.pdf_api_review_answer()
            if isinstance(result, tuple):
                response, status = result
            else:
                response = result
                status = response.status_code
            return status, response.get_json()

    def _invoke_note(self, aid="rv_note", note_id=202, ease=3):
        with self.app.test_request_context(
            "/pdf/api/review-answer",
            method="POST",
            json={"aid": aid, "note_id": note_id, "ease": ease},
        ):
            result = pdf_reader.pdf_api_review_answer()
            if isinstance(result, tuple):
                response, status = result
            else:
                response = result
                status = response.status_code
            return status, response.get_json()

    @staticmethod
    def _success_post(counter, *, answer_delay=0):
        lock = threading.Lock()

        def fake_post(_url, *, json, timeout):
            action = json["action"]
            if action == "answerCards":
                with lock:
                    counter.append(json["params"]["answers"][0])
                if answer_delay:
                    time.sleep(answer_delay)
                return _Response({"result": [True], "error": None})
            if action == "cardsInfo":
                return _Response({
                    "result": [{
                        "interval": 7,
                        "due": 42,
                        "queue": 2,
                        "type": 2,
                    }],
                    "error": None,
                })
            raise AssertionError(action)

        return fake_post

    def test_two_concurrent_requests_call_answer_cards_once(self):
        answers = []
        fake_post = self._success_post(answers, answer_delay=0.15)
        start = threading.Barrier(3)

        def run():
            start.wait(timeout=2)
            return self._invoke(aid="rv_concurrent")

        with patch("requests.post", side_effect=fake_post):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(run)
                second = pool.submit(run)
                start.wait(timeout=2)
                results = [first.result(timeout=5), second.result(timeout=5)]

        self.assertEqual(len(answers), 1)
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(
            sum(bool(data.get("dedup")) for _, data in results),
            1,
        )
        receipt = json.loads(
            pdf_reader._REVIEW_RECEIPTS_FILE.read_text("utf-8")
        )
        self.assertEqual(
            receipt["entries"]["rv_concurrent"]["state"],
            "done",
        )

    def test_successful_lost_response_retry_is_deduplicated(self):
        answers = []
        with patch(
            "requests.post",
            side_effect=self._success_post(answers),
        ):
            first_status, first_data = self._invoke(aid="rv_lost")
            # Simulate a response lost after the server completed and then a
            # worker restart: only the durable receipt may prove the retry.
            pdf_reader._REVIEW_COMPLETED_MEMORY.clear()
            second_status, second_data = self._invoke(aid="rv_lost")

        self.assertEqual(first_status, 200)
        self.assertTrue(first_data["ok"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second_data["dedup"])
        self.assertTrue(second_data["durable"])
        self.assertEqual(len(answers), 1)

    def test_same_aid_with_different_payload_is_rejected(self):
        answers = []
        with patch(
            "requests.post",
            side_effect=self._success_post(answers),
        ):
            first_status, _ = self._invoke(
                aid="rv_reuse", card_id=101, ease=3
            )
            second_status, second_data = self._invoke(
                aid="rv_reuse", card_id=101, ease=4
            )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)
        self.assertEqual(
            second_data["code"],
            "review_answer_aid_reuse",
        )
        self.assertEqual(len(answers), 1)

    def test_note_with_multiple_cards_requires_exact_card_id(self):
        actions = []

        def fake_post(_url, *, json, timeout):
            actions.append(json["action"])
            self.assertEqual(json["action"], "findCards")
            return _Response({
                "result": [301, 302],
                "error": None,
            })

        with patch("requests.post", side_effect=fake_post):
            status, data = self._invoke_note()

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertIn("exact card_id required", data["error"])
        self.assertEqual(actions, ["findCards"])

    def test_explicit_anki_rejection_releases_claim_for_safe_retry(self):
        answers = []
        calls = 0

        def fake_post(_url, *, json, timeout):
            nonlocal calls
            action = json["action"]
            if action == "answerCards":
                calls += 1
                answers.append(json["params"]["answers"][0])
                if calls == 1:
                    return _Response({"result": [False], "error": None})
                return _Response({"result": [True], "error": None})
            if action == "cardsInfo":
                return _Response({"result": [{}], "error": None})
            raise AssertionError(action)

        with patch("requests.post", side_effect=fake_post):
            first_status, first_data = self._invoke(aid="rv_rejected")
            second_status, second_data = self._invoke(
                aid="rv_rejected"
            )

        self.assertEqual(first_status, 404)
        self.assertFalse(first_data["ok"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second_data["ok"])
        self.assertEqual(len(answers), 2)

    def test_unknown_transport_outcome_stays_pending_and_never_repeats(self):
        calls = 0

        def fake_post(_url, *, json, timeout):
            nonlocal calls
            self.assertEqual(json["action"], "answerCards")
            calls += 1
            raise TimeoutError("response lost")

        with patch("requests.post", side_effect=fake_post):
            first_status, first_data = self._invoke(
                aid="rv_unknown"
            )
            pdf_reader._REVIEW_COMPLETED_MEMORY.clear()
            second_status, second_data = self._invoke(
                aid="rv_unknown"
            )

        self.assertEqual(first_status, 503)
        self.assertEqual(
            first_data["code"],
            "review_answer_outcome_unknown",
        )
        self.assertEqual(second_status, 409)
        self.assertEqual(
            second_data["code"],
            "review_answer_outcome_unknown",
        )
        self.assertEqual(calls, 1)

    def test_success_then_terminal_receipt_failure_remains_at_most_once(
        self,
    ):
        answers = []
        real_store = pdf_reader._review_receipts_store
        stores = 0

        def flaky_store(value, protect=()):
            nonlocal stores
            stores += 1
            if stores == 2:
                raise OSError("simulated terminal fsync failure")
            return real_store(value, protect)

        with (
            patch(
                "requests.post",
                side_effect=self._success_post(answers),
            ),
            patch.object(
                pdf_reader,
                "_review_receipts_store",
                side_effect=flaky_store,
            ),
        ):
            first_status, first_data = self._invoke(
                aid="rv_commit_failure"
            )
            second_status, second_data = self._invoke(
                aid="rv_commit_failure"
            )
            # Another process would not share the success memory.  Its only
            # safe action is to reject the unresolved durable pending claim.
            pdf_reader._REVIEW_COMPLETED_MEMORY.clear()
            third_status, third_data = self._invoke(
                aid="rv_commit_failure"
            )

        self.assertEqual(first_status, 200)
        self.assertFalse(first_data["durable"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second_data["dedup"])
        self.assertFalse(second_data["durable"])
        self.assertEqual(third_status, 409)
        self.assertEqual(
            third_data["code"],
            "review_answer_outcome_unknown",
        )
        self.assertEqual(len(answers), 1)

    def test_corrupt_seen_ledger_fails_before_anki_side_effect(self):
        pdf_reader._REVIEW_SEEN_FILE.write_text("{broken", "utf-8")
        answers = []
        with patch(
            "requests.post",
            side_effect=self._success_post(answers),
        ):
            status, data = self._invoke(aid="rv_corrupt")
        self.assertEqual(status, 503)
        self.assertEqual(
            data["code"],
            "review_answer_idempotency_unavailable",
        )
        self.assertEqual(answers, [])


if __name__ == "__main__":
    unittest.main()
