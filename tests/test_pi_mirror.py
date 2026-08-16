"""Pi → Windows 镜像：SSE 解析、去抖、"没同步 ≠ 没有"。

第三条是这套东西的灵魂：镜像不存在时必须抛 MirrorMissing 而不是返回空 ——
返回空的话，刚开机追赶没跑完时查"昨天划的那句"，AI 会拿空列表说"你没划过"。
数据没错、代码没错、时机错了（架构第 17 条铁律）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    import sys
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module   # dataclass 装饰器要求模块在 sys.modules 里
    spec.loader.exec_module(module)
    return module


daemon = _load("pi_mirror_daemon", ROOT / "scripts" / "pi_mirror_daemon.py")
reader = _load("pi_mirror", ROOT / "scripts" / "lib" / "pi_mirror.py")


class SseParserTests(unittest.TestCase):
    def test_parses_change_events(self) -> None:
        lines = [
            ": hello\n",
            "data: {\"kind\": \"hl\", \"file\": \"a.pdf\"}\n",
            "\n",
            "data: {\"kind\": \"pos\", \"file\": \"b.pdf\"}\n",
            "\n",
        ]
        events = [e for e in daemon.parse_sse_events(lines) if e]
        self.assertEqual([e["kind"] for e in events], ["hl", "pos"])

    def test_heartbeat_yields_none_not_silence(self) -> None:
        # 心跳必须产出 None 而不是被吞掉：调用方靠"有产出"判断连接活着。
        out = list(daemon.parse_sse_events([": ping\n", "\n"]))
        self.assertEqual(out, [None, None])

    def test_multiline_data_joined(self) -> None:
        lines = ["data: {\"kind\":\n", "data: \"note\", \"file\": \"x\"}\n", "\n"]
        events = [e for e in daemon.parse_sse_events(lines) if e]
        self.assertEqual(events[0]["kind"], "note")

    def test_bad_json_still_signals_liveness(self) -> None:
        out = list(daemon.parse_sse_events(["data: {broken\n", "\n"]))
        self.assertEqual(out, [None])


class PullPlannerTests(unittest.TestCase):
    def test_debounce_merges_burst(self) -> None:
        p = daemon.PullPlanner(debounce=2.0)
        for _ in range(5):
            p.note_event({"kind": "hl", "file": "a.pdf"}, now=100.0)
        self.assertEqual(p.due(101.0), [])          # 还没到期
        self.assertEqual(p.due(102.5), [("hl", "a.pdf")])   # 5 连发只拉一次
        self.assertEqual(p.due(103.0), [])          # 拉过即清

    def test_unknown_kind_ignored(self) -> None:
        p = daemon.PullPlanner()
        p.note_event({"kind": "fav-built", "file": "x"}, now=0.0)
        p.note_event({"kind": "client-action", "file": "x"}, now=0.0)
        self.assertEqual(p.pending_count(), 0)

    def test_pos_events_collapse_regardless_of_file(self) -> None:
        p = daemon.PullPlanner(debounce=1.0)
        p.note_event({"kind": "pos", "file": "a.pdf"}, now=0.0)
        p.note_event({"kind": "pos", "file": "b.pdf"}, now=0.0)
        self.assertEqual(p.due(2.0), [("pos", "")])  # 整份 positions 只拉一次


class MirrorMissingSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("CLAUDE_PROJECT")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("CLAUDE_PROJECT", None)
        else:
            os.environ["CLAUDE_PROJECT"] = self._saved

    def test_never_synced_raises_not_empty(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.environ["CLAUDE_PROJECT"] = d
            # 关键断言：没同步必须是异常，不能是空数据。
            with self.assertRaises(reader.MirrorMissing):
                reader.load_positions()
            self.assertTrue(reader.status()["neverSynced"])

    def test_unmirrored_book_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.environ["CLAUDE_PROJECT"] = d
            root = Path(d) / "state" / "pi-mirror"
            root.mkdir(parents=True)
            (root / "_mirror.json").write_text(json.dumps(
                {"contract": daemon.CONTRACT, "sse": {"status": "connected"},
                 "books": {}, "positionsSyncedAt": 1}), encoding="utf-8")
            with self.assertRaises(reader.MirrorMissing):
                reader.load_book("某本书.pdf", "hl")

    def test_stale_mirror_says_so(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.environ["CLAUDE_PROJECT"] = d
            root = Path(d) / "state" / "pi-mirror"
            root.mkdir(parents=True)
            (root / "_mirror.json").write_text(json.dumps(
                {"contract": daemon.CONTRACT, "sse": {"status": "reconnecting"},
                 "books": {}, "positionsSyncedAt": 1000}), encoding="utf-8")
            (root / "positions.json").write_text("{}", encoding="utf-8")
            _, fresh = reader.load_positions()
            self.assertTrue(fresh.stale)
            self.assertIn("打折扣", fresh.describe())


class DaemonManifestTests(unittest.TestCase):
    def test_manifest_roundtrip_and_status_transitions(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            m = daemon.load_manifest(directory)
            daemon._set_sse_status(m, "connected")
            daemon.save_manifest(directory, m)
            m2 = daemon.load_manifest(directory)
            self.assertEqual(m2["sse"]["status"], "connected")
            daemon._set_sse_status(m2, "reconnecting", "boom")
            self.assertEqual(m2["sse"]["lastError"], "boom")
            daemon._set_sse_status(m2, "connected")
            self.assertNotIn("lastError", m2["sse"])   # 恢复后不留旧错吓人


if __name__ == "__main__":
    unittest.main()
