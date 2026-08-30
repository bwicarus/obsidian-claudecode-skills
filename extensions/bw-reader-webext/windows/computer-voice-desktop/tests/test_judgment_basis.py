# -*- coding: utf-8 -*-
"""judgment_basis 的测试。

守两条命门：
① **「不知道」和「否」分开** —— 文件缺失时每一项都必须说"不知道"，
   而不是默默变成"不在/没有"。混起来的话"没有数据"就成了一个方向的结论。
② 心跳陈旧要**先说** —— 状态文件停更时里面全是旧话，不标出来的话
   "语音已连"会冒充现状。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import judgment_basis  # noqa: E402


class JudgmentBasisTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="jb-"))
        self.root = base / "root"
        self.runtime = base / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()

    def _basis(self):
        return judgment_basis.collect(self.root, self.runtime)

    def test_empty_world_is_honestly_unknown(self) -> None:
        basis = self._basis()
        text = judgment_basis.render(basis)
        self.assertFalse(basis["place"]["known"])
        self.assertIn("不是「不在家」", text,
                      "地点缺数据必须点明不等于不在家")
        self.assertFalse(basis["voice"]["known"])
        self.assertIn("ReaderPC 状态：读不到", text)
        self.assertIn("复习：不知道", text)
        # 负对照：空世界不许出现任何肯定的结论。
        self.assertNotIn("链路已连", text)

    def test_full_world_renders_each_evidence(self) -> None:
        now_ms = int(time.time() * 1000)
        (self.runtime / "current-place.json").write_text(json.dumps({
            "alias": "家", "state": "home",
            "observedAtUtcMs": now_ms - 120_000}), encoding="utf-8")
        (self.root / "readerpc-server.status.json").write_text(json.dumps({
            "updatedAtEpochMs": now_ms - 30_000,
            "voice": {"readerConnected": True, "captureActive": True},
            "readerContext": {"available": True, "kind": "pdf",
                              "title": "某本书",
                              "updatedAtEpochMs": now_ms - 60_000},
        }), encoding="utf-8")
        (self.root / "replication-apply.status.json").write_text(json.dumps({
            "atUtcMs": now_ms - 10_000,
            "notifications": {"reviewDue": {"due": 7, "new": 2}},
        }), encoding="utf-8")
        (self.root / "notifications.json").write_text(json.dumps({
            "items": [
                {"audience": "user", "state": "pending"},
                {"audience": "user", "state": "acknowledged"},
                {"audience": "ai", "state": "pending"},  # 休眠档，不计
            ]}), encoding="utf-8")
        (self.root / "camera-sources.json").write_text(json.dumps({
            "sources": [{"id": "usb", "label": "书桌"}]}), encoding="utf-8")
        text = judgment_basis.render(self._basis())
        self.assertIn("地点：家", text)
        self.assertIn("语音：链路已连", text)
        self.assertIn("某本书", text)
        self.assertIn("复习：到期 7 张", text)
        self.assertIn("pending 1 条", text)
        self.assertIn("书桌", text)
        # 摄像头永远只列清单 —— 这句纪律必须写在输出里。
        self.assertIn("没有**画面", text.replace("**没有**画面", "没有**画面"))

    def test_stale_heartbeat_is_called_out_first(self) -> None:
        now_ms = int(time.time() * 1000)
        (self.root / "readerpc-server.status.json").write_text(json.dumps({
            "updatedAtEpochMs": now_ms - 30 * 60_000,
            "voice": {"readerConnected": True, "captureActive": True},
        }), encoding="utf-8")
        text = judgment_basis.render(self._basis())
        self.assertIn("分钟没更新", text,
                      "心跳停了必须先说 —— 否则「语音已连」是旧话冒充现状")

    def test_stale_place_keeps_value_with_label(self) -> None:
        now_ms = int(time.time() * 1000)
        (self.runtime / "current-place.json").write_text(json.dumps({
            "alias": "家", "state": "home",
            "observedAtUtcMs": now_ms - 2 * 3600_000}), encoding="utf-8")
        text = judgment_basis.render(self._basis())
        self.assertIn("地点：家", text, "旧记录也要给出来")
        self.assertIn("旧记录", text, "但必须注明旧")


if __name__ == "__main__":
    unittest.main()
