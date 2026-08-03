"""Static contract for ordered seed-tts end-of-turn delivery."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "_server_deploy" / "voice_realtime_relay.py"


class VoiceRelayTTSEndContractTest(unittest.TestCase):
    def test_uni_done_is_serialized_behind_speak_items(self) -> None:
        source = RELAY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        channel = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_tts_channel"
        )
        nested = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in channel.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        worker = nested["_uni_worker"]
        done = nested["done"]
        self.assertIn("if item is uni_end", worker)
        self.assertIn('"event": "tts_end"', worker)
        self.assertIn('await uni["q"].put(uni_end)', done)


if __name__ == "__main__":
    unittest.main()
