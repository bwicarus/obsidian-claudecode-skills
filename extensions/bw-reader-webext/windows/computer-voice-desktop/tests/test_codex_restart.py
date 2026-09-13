# -*- coding: utf-8 -*-
"""codex_restart 的两条纪律：在通话就拒绝；起来不算完，要等通道与一条推送。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_restart as R  # noqa: E402


class RestartTests(unittest.TestCase):
    def test_refuses_while_in_call_unless_forced(self):
        with mock.patch.object(R, "voice_active", return_value=True), \
                mock.patch.object(R, "aumid", return_value="OpenAI.Codex_x!App"), \
                mock.patch.object(R, "stop") as stop, \
                mock.patch("sys.stdout"):
            self.assertEqual(R.main([]), 2)
            stop.assert_not_called()

    def test_restart_is_done_only_after_channel_and_warm_push(self):
        with mock.patch.object(R, "voice_active", return_value=False), \
                mock.patch.object(R, "aumid", return_value="OpenAI.Codex_x!App"), \
                mock.patch.object(R, "codex_processes", side_effect=[[1, 2], [3]]), \
                mock.patch.object(R, "stop", return_value="graceful"), \
                mock.patch.object(R, "launch"), \
                mock.patch.object(R.time, "sleep"), \
                mock.patch.object(R, "wait_channel", return_value={"ok": True, "detail": "x"}), \
                mock.patch.object(R, "warm", return_value={"ok": True, "threadId": "t"}), \
                mock.patch("builtins.print") as out:
            self.assertEqual(R.main([]), 0)
        report = json.loads(out.call_args[0][0])
        self.assertEqual(report["stop"], "graceful")
        self.assertTrue(report["channel"]["ok"] and report["warm"]["ok"])

    def test_channel_failure_means_not_done(self):
        with mock.patch.object(R, "voice_active", return_value=None), \
                mock.patch.object(R, "aumid", return_value="OpenAI.Codex_x!App"), \
                mock.patch.object(R, "codex_processes", side_effect=[[], [3]]), \
                mock.patch.object(R, "stop", return_value="not-running"), \
                mock.patch.object(R, "launch"), \
                mock.patch.object(R.time, "sleep"), \
                mock.patch.object(R, "wait_channel", return_value={"ok": False, "detail": "no pipe"}), \
                mock.patch.object(R, "warm") as warm, \
                mock.patch("builtins.print") as out:
            self.assertEqual(R.main([]), 1)
            warm.assert_not_called()
        self.assertFalse(json.loads(out.call_args[0][0])["ok"])


if __name__ == "__main__":
    unittest.main()
