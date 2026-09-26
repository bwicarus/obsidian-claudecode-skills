"""App 对话里的权限提升（2026-09-27 用户：「codex 软件中打开的对话有权限，只是 app 对话中的 ai
没有权限，我希望能有一个提升权限的请求确认后就能开启权限」）。

钉住：
  ① 越过沙盒的命令审批不再一律拒绝，而是挂起等 App 上的决定；
  ② 三个按钮映射到协议里各自的取值（现代 accept/acceptForSession/decline，旧版 approved/…/denied）；
  ③ 「本对话都允许」只属于这条线程：下一轮 turn/start 带完整权限，换线程回到只读；
  ④ 超时按拒绝处理，不会永远卡住那一轮；
  ⑤ 阅读器工具仍直接放行，不打扰用户。
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "voice_cli_runner.py"
sys.path.insert(0, str(RUNNER.parent))

_spec = importlib.util.spec_from_file_location("voice_cli_runner_permission", RUNNER)
vcr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vcr
_spec.loader.exec_module(vcr)


class _App:
    def __init__(self):
        self.calls = []

    async def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        return {}


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "permission.json"
        self.patch = patch.object(vcr, "PERMISSION_PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _runner(self, loop):
        r = object.__new__(vcr.Runner)
        r.loop = loop
        r.thread_id = "th-1"
        r.settings = {"historyEnabled": False}
        r._permission_pending = {}
        r.logs = []
        r.log = lambda kind, **kw: r.logs.append((kind, kw))
        return r

    def _ask_and_decide(self, method, decision, params=None):
        async def go():
            r = self._runner(asyncio.get_running_loop())
            task = asyncio.create_task(r.ask_permission(method, params or {"reason": "写文件", "command": "touch x"}))
            await asyncio.sleep(0)
            pending = r.permission_state()["pending"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["reason"], (params or {"reason": "写文件"}).get("reason", ""))
            self.assertNotIn("future", pending[0])
            self.assertTrue(r.decide_permission(pending[0]["id"], decision)["ok"])
            return r, await task
        return asyncio.run(go())

    def test_decisions_map_to_protocol_values(self):
        method = "item/commandExecution/requestApproval"
        self.assertEqual(self._ask_and_decide(method, "once")[1], {"decision": "accept"})
        self.assertEqual(self._ask_and_decide(method, "session")[1], {"decision": "acceptForSession"})
        self.assertEqual(self._ask_and_decide(method, "deny")[1], {"decision": "decline"})
        self.assertEqual(self._ask_and_decide("execCommandApproval", "once")[1], {"decision": "approved"})
        self.assertEqual(self._ask_and_decide("applyPatchApproval", "deny")[1], {"decision": "denied"})

    def test_permissions_request_grants_scope(self):
        grant = {"network": {"enabled": True}}
        r, result = self._ask_and_decide("item/permissions/requestApproval", "session", {"permissions": grant})
        self.assertEqual(result, {"permissions": grant, "scope": "session"})
        self.assertFalse(r.elevated())   # 只给它要的那几项，不放大成完整权限
        _, result = self._ask_and_decide("item/permissions/requestApproval", "deny", {"permissions": grant})
        self.assertEqual(result, {"permissions": {}})

    def test_session_grant_elevates_only_this_thread(self):
        r, _ = self._ask_and_decide("item/commandExecution/requestApproval", "session")
        self.assertTrue(r.elevated())
        self.assertEqual(r._thread_permission("th-1")["sandbox"], "danger-full-access")
        self.assertEqual(r._thread_permission("th-2")["sandbox"], "read-only")
        self.assertEqual(r._thread_permission(None)["sandbox"], "read-only")
        r.thread_id = "th-2"
        self.assertFalse(r.elevated())

    def test_turn_start_carries_current_sandbox(self):
        async def go():
            r = self._runner(asyncio.get_running_loop())
            r.app = _App()
            r.ensure_app = lambda: asyncio.sleep(0)
            r._ctx_inject_backend = lambda with_text=True: asyncio.sleep(0)
            await r.turn("hi")
            first = r.app.calls[-1][1]
            r._set_elevated(True, "test")
            await r.turn("again")
            second = r.app.calls[-1][1]
            await r.revoke_permission()
            await r.turn("third")
            return first, second, r.app.calls[-1][1]
        first, second, third = asyncio.run(go())
        self.assertEqual(first["approvalPolicy"], "on-request")
        self.assertEqual(first["sandboxPolicy"], {"type": "readOnly"})
        self.assertEqual(second["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertEqual(third["sandboxPolicy"], {"type": "readOnly"})

    def test_timeout_declines(self):
        async def go():
            r = self._runner(asyncio.get_running_loop())
            with patch.object(vcr, "PERMISSION_WAIT_SECONDS", 0.01):
                result = await r.ask_permission("item/fileChange/requestApproval", {"reason": "改文件"})
            return r, result
        r, result = asyncio.run(go())
        self.assertEqual(result, {"decision": "decline"})
        self.assertEqual(r.permission_state()["pending"], [])

    def test_stale_decision_is_reported(self):
        async def go():
            r = self._runner(asyncio.get_running_loop())
            return r.decide_permission("p-missing", "once")
        self.assertFalse(asyncio.run(go())["ok"])

    def test_reader_tools_still_auto_approved(self):
        source = RUNNER.read_text(encoding="utf-8")
        read = source.split("async def _read(self)")[1].split("async def _answer_later")[0]
        # 阅读器工具判断在交给用户之前
        self.assertLess(read.index('ok = "reader_" in blob'), read.index("self._answer_later"))


if __name__ == "__main__":
    unittest.main()
