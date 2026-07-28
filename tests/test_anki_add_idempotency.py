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

    def _invoke(self, *, aid="fc_test", front="Q", entity_id=""):
        body = {
            "aid": aid,
            "cards": [{"type": "basic", "front": front, "back": "A"}],
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
