"""报错一键上传（支柱①）：采集器的三条设计约束都要钉住。

  · 采集是确定性的（不调 AI——测试里没有任何网络/AI 依赖就是证明）
  · 采集失败也要出报告（某个日志源坏了，其它源照常，状态写明）
  · 只截尾部、只看最近（整份日志既是隐私面也是噪声）
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "error_reports", ROOT / "_server_deploy" / "error_reports.py")
ER = importlib.util.module_from_spec(spec)
sys.modules["error_reports"] = ER
spec.loader.exec_module(ER)


def _make_env(tmp: Path, *, voice_lines=None, convo=None, uid=7):
    (tmp / "state" / "voice-log").mkdir(parents=True)
    (tmp / "state" / "assistant-convo").mkdir(parents=True)
    if voice_lines is not None:
        day = time.strftime("%Y-%m-%d")
        (tmp / "state" / "voice-log" / f"{day}.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in voice_lines),
            encoding="utf-8")
    if convo is not None:
        (tmp / "state" / "assistant-convo" / f"{uid}.json").write_text(
            json.dumps(convo, ensure_ascii=False), encoding="utf-8")


class CollectorTests(unittest.TestCase):
    def test_full_report_shape(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            now = int(time.time())
            _make_env(tmp,
                      voice_lines=[
                          {"kind": "tool", "tool": "goto_page", "ok": False,
                           "ts": now - 10, "book": "a.pdf"},
                          {"kind": "q", "text": "翻到下一页", "ts": now - 12,
                           "book": "a.pdf"},
                          {"kind": "rtcstats", "ts": now - 5, "book": "a.pdf"},
                      ],
                      convo=[{"role": "user", "content": "你好", "ts": now - 60}])
            r = ER.collect_report(tmp, what="翻页没反应",
                                  ctx={"file_rel": "a.pdf", "page": 3}, uid=7)
            self.assertEqual(r["contract"], ER.CONTRACT)
            self.assertEqual(r["what"], "翻页没反应")
            self.assertEqual(r["context"]["file"], "a.pdf")
            self.assertEqual(r["voiceLog"]["status"], "ok")
            kinds = [e.get("kind") for e in r["voiceLog"]["events"]]
            self.assertIn("tool", kinds)
            self.assertNotIn("rtcstats", kinds, "网络统计是纯噪声,不该进报告")
            self.assertEqual(r["conversation"]["status"], "ok")

    def test_source_failure_does_not_kill_report(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # 什么日志都没有:报告仍然生成,各源状态写 absent
            r = ER.collect_report(tmp, what="x", ctx={}, uid=1)
            self.assertEqual(r["voiceLog"]["status"], "absent")
            self.assertEqual(r["conversation"]["status"], "absent")
            self.assertEqual(r["what"], "x")

    def test_old_events_outside_window_excluded(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            old_ts = int(time.time()) - ER.RECENT_WINDOW_SECONDS - 100
            _make_env(tmp, voice_lines=[
                {"kind": "tool", "tool": "x", "ts": old_ts, "book": "a.pdf"}])
            r = ER.collect_report(tmp, what="x", ctx={"file_rel": "a.pdf"}, uid=1)
            self.assertEqual(r["voiceLog"]["events"], [],
                             "时间窗外的旧事件不该进报告")

    def test_book_filter(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            now = int(time.time())
            _make_env(tmp, voice_lines=[
                {"kind": "tool", "tool": "a", "ts": now, "book": "别的书.pdf"},
                {"kind": "tool", "tool": "b", "ts": now, "book": "这本.pdf"}])
            r = ER.collect_report(tmp, what="x",
                                  ctx={"file_rel": "这本.pdf"}, uid=1)
            tools = [e.get("tool") for e in r["voiceLog"]["events"]]
            self.assertEqual(tools, ["b"])


class StoreTests(unittest.TestCase):
    def test_save_list_read_roundtrip(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = ER.collect_report(tmp, what="测试报告", ctx={}, uid=1)
            ER.save_report(tmp, r)
            lst = ER.list_reports(tmp)
            self.assertEqual(len(lst), 1)
            self.assertEqual(lst[0]["id"], r["id"])
            back = ER.read_report(tmp, r["id"])
            self.assertEqual(back["what"], "测试报告")

    def test_since_filter_and_bad_file_is_loud(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = ER.collect_report(tmp, what="x", ctx={}, uid=1)
            ER.save_report(tmp, r)
            # since 在未来 → 空
            self.assertEqual(
                ER.list_reports(tmp, since_epoch=int(time.time()) + 10), [])
            # 坏文件不炸列表,但要出声
            (tmp / "state" / "error-reports" / "bad.json").write_text(
                "{broken", encoding="utf-8")
            rows = ER.list_reports(tmp)
            self.assertIn({"id": "bad", "error": "unreadable"}, rows)

    def test_report_id_path_traversal_blocked(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self.assertIsNone(ER.read_report(tmp, "../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
