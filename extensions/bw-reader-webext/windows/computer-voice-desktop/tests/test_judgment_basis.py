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

    def _place(self, *, age_minutes: int, watching: bool) -> str:
        now_ms = int(time.time() * 1000)
        (self.runtime / "current-place.json").write_text(json.dumps({
            "alias": "家", "state": "home", "watching": watching,
            "observedAtUtcMs": now_ms - age_minutes * 60_000,
        }), encoding="utf-8")
        return judgment_basis.render(self._basis())

    def test_watching_turns_old_into_has_not_moved(self) -> None:
        """⚠ 「没挪窝」和「不知道」是两件事。

        用户 2026-09-11：「现在这样经常会出现位置记录过旧的情况」。
        位置本来就变化慢 —— 在家坐一下午，位置一点没变，却被按时间判成
        "旧记录"，于是一条完全有效的信息每次都得当可疑的看。

        设备在后台盯着移动时，久没更新恰恰是**肯定信号**：他没挪窝。
        """
        text = self._place(age_minutes=120, watching=True)
        self.assertIn("地点：家", text)
        self.assertIn("没挪窝", text, "盯着的时候，久没更新说明没动过")
        self.assertNotIn("旧记录", text, "这不是旧记录，是没动过")

    def test_not_watching_still_judged_by_time(self) -> None:
        """没在盯就照旧按时间判 —— 那时"没消息"确实两可。"""
        text = self._place(age_minutes=120, watching=False)
        self.assertIn("旧记录", text)
        self.assertNotIn("没挪窝", text)

    def test_watching_but_absurdly_old_is_doubted(self) -> None:
        """⚠ 盯着也有盯丢的时候：权限被撤、app 被卸、链悄悄断了。

        久到离谱时要**怀疑盯的那条链**，而不是继续宣称"他一直没动" ——
        那会让半天前的位置冒充现状，比标错旧严重得多。
        """
        text = self._place(
            age_minutes=judgment_basis.WATCHING_DOUBT_MINUTES + 10,
            watching=True)
        self.assertIn("可能已经停掉", text)
        self.assertIn("别当现状", text)

    def test_watching_and_fresh_says_nothing_extra(self) -> None:
        """新鲜就别加注释 —— 每多一句都是要 AI 分辨的噪音。"""
        text = self._place(age_minutes=5, watching=True)
        self.assertIn("地点：家", text)
        self.assertNotIn("没挪窝", text)
        self.assertNotIn("旧记录", text)


if __name__ == "__main__":
    unittest.main()
