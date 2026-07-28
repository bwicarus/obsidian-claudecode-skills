#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "_server_deploy"))
os.environ.setdefault("CLAUDE_PROJECT", str(ROOT))
os.environ.setdefault("WEBAPP_DATA", tempfile.mkdtemp())

import pdf_reader


def _card(card_id, note_id=None):
    return {
        "cardId": card_id,
        "note": note_id or card_id + 1000,
        "question": "Q%d" % card_id,
        "answer": "A%d" % card_id,
        "deckName": "Deck",
        "fields": {},
    }


class _FakeService:
    def __init__(self):
        self.call = None

    def build(self, context, due_ids, **kwargs):
        self.call = (context, list(due_ids), kwargs)
        return {
            "contract": "card-candidate-service/1",
            "context_key": "context-1",
            "source_ref": "book:book.pdf#p2",
            "focus_terms": ["alpha"],
            "selected_card_ids": [10, 30, 20],
            "related_total": 2,
            "evidence_counts": {
                "direct_source": 1,
                "focus_term": 1,
                "due": 1,
            },
            "metadata": {
                "10": {
                    "score": 400,
                    "related": True,
                    "due": False,
                    "evidence": [{"kind": "direct_source"}],
                    "reason_labels": ["当前内容来源"],
                },
                "30": {
                    "score": 180,
                    "related": True,
                    "due": False,
                    "evidence": [{"kind": "focus_term"}],
                    "reason_labels": ["当前焦点：alpha"],
                },
                "20": {
                    "score": 0,
                    "related": False,
                    "due": True,
                    "evidence": [{"kind": "due"}],
                    "reason_labels": ["到期"],
                },
            },
        }


class CardCandidateRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_get_preserves_due_queue_and_clamps_negative_limit(self):
        calls = []

        def anki(action, **params):
            calls.append((action, params))
            if action == "findCards":
                self.assertEqual(params["query"], "is:due")
                return [3, 1, 2]
            if action == "cardsInfo":
                self.assertEqual(params["cards"], [3])
                return [_card(3)]
            raise AssertionError(action)

        with patch.object(pdf_reader, "_review_anki_call", side_effect=anki):
            with self.app.test_request_context(
                "/api/review-queue?limit=-9",
                method="GET",
            ):
                response = pdf_reader.pdf_api_review_queue()
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["due_total"], 3)
        self.assertEqual([card["id"] for card in data["cards"]], [3])
        self.assertEqual(data["cards"][0]["review_kind"], "due")
        self.assertEqual([call[0] for call in calls], ["findCards", "cardsInfo"])

    def test_post_reorders_cards_info_and_attaches_candidate_evidence(self):
        service = _FakeService()

        def anki(action, **params):
            if action == "findCards":
                return [20, 99]
            if action == "cardsInfo":
                self.assertEqual(params["cards"], [10, 30, 20])
                sourced = _card(10)
                sourced["fields"] = {
                    "Back": {
                        "value": (
                            "A10<hr><div>来源："
                            '<a href="obsidian://open?vault=Vault'
                            '&amp;file=%E8%B5%84%E6%BA%90%2F000-'
                            '%E7%9B%B4%E5%92%8C">'
                            "[[000-直和]]</a><br>"
                            "原因：当前内容来源</div>"
                        ),
                    },
                }
                return [_card(20), sourced, _card(30)]
            raise AssertionError(action)

        body = {
            "limit": 200,
            "exclude_card_ids": [99, "bad"],
            "context": {
                "file": "book.pdf",
                "page": 2,
                "selection": "s" * 1200,
                "visible_text": "v" * 3000,
                "kg_nodes": ["kg:Book#one"] * 30,
            },
        }
        with (
            patch.object(pdf_reader, "_review_anki_call", side_effect=anki),
            patch.object(
                pdf_reader,
                "_review_candidate_service",
                return_value=service,
            ),
        ):
            with self.app.test_request_context(
                "/api/review-queue",
                method="POST",
                json=body,
            ):
                response = pdf_reader.pdf_api_review_queue()
        data = response.get_json()
        self.assertEqual([card["id"] for card in data["cards"]], [10, 30, 20])
        self.assertEqual(data["cards"][0]["candidate_reasons"], ["当前内容来源"])
        self.assertEqual(
            data["cards"][0]["source_ref"],
            "note:资源/000-直和.md",
        )
        self.assertEqual(
            data["cards"][0]["source_url"],
            "obsidian://open?vault=Vault"
            "&file=%E8%B5%84%E6%BA%90%2F000-%E7%9B%B4%E5%92%8C",
        )
        self.assertEqual(data["cards"][2]["review_kind"], "due")
        self.assertEqual(data["related_total"], 2)
        self.assertEqual(data["contract"], "card-candidate-service/1")

        context, due_ids, kwargs = service.call
        self.assertEqual(due_ids, [20, 99])
        self.assertEqual(kwargs["limit"], 60)
        self.assertEqual(kwargs["exclude_card_ids"], [99])
        self.assertEqual(len(context["selection"]), 800)
        self.assertEqual(len(context["visible_text"]), 2400)
        self.assertEqual(len(context["kg_nodes"]), 20)


if __name__ == "__main__":
    unittest.main()
