#!/usr/bin/env python3
"""转换层v2 第2步强制清单:/pdf 域每条路由必须在 vbook_route_policy 声明策略。
新增路由不登记 → 本测试直接失败(防翻译层漏网);策略表里有已删除路由 → 同样失败(防腐)。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "_server_deploy"))

ALLOWED = {"PAGE", "BOOK_REP", "BOOK_FANIN", "ID_ROUTED", "MEMBER_REQUIRED", "JOB_OR_RANGE",
           "GLOBAL", "NONE", "EPUB"}


class RoutePolicyComplete(unittest.TestCase):
    def test_every_pdf_route_declared(self):
        # app.py now also derives the computer-voice pairing pepper from the
        # Flask secret.  Keep this integration fixture representative of a
        # deployable app instead of bypassing that fail-closed boundary.
        os.environ.setdefault(
            "SECRET_KEY",
            "vbook-route-policy-integration-secret",
        )
        os.environ.setdefault("WEBAPP_DATA", tempfile.mkdtemp())
        os.environ.setdefault("CLAUDE_PROJECT", str(ROOT))
        import app as A  # noqa: E402
        from vbook_route_policy import ROUTE_POLICY  # noqa: E402
        live = {r.endpoint for r in A.app.url_map.iter_rules() if str(r).startswith("/pdf")}
        missing = sorted(live - set(ROUTE_POLICY))
        orphans = sorted(set(ROUTE_POLICY) - live)
        bad = sorted(ep for ep, p in ROUTE_POLICY.items() if p not in ALLOWED)
        self.assertFalse(missing, "新增 /pdf 路由未声明 vbook 策略(必须登记后才能合入):%s" % missing)
        self.assertFalse(orphans, "策略表含已删除路由(清理):%s" % orphans)
        self.assertFalse(bad, "非法策略值:%s" % bad)


if __name__ == "__main__":
    unittest.main()
