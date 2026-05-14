"""
/api/nav-links 路由 + nav.js 部署的烟雾测试。

设计取舍：完整 round-trip（登录 → GET → POST → GET）需要 session cookie，
带 SECRET_KEY env 才能 import app.py，比较繁琐。Smoke test 只验证：
  1) 路由已注册（未授权返回 401，不是 404）
  2) nav.js 静态资源可访问且含关键函数（防止部署忘 cp 到 /var/www/html/）

跑在跑着的 webapp 上（127.0.0.1:5000）；CI 环境如果没起 webapp，测试会被 skip。
"""
from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

WEBAPP_BASE = "http://127.0.0.1:5000"
NAV_JS_URL  = "http://127.0.0.1/static/nav.js"  # nginx 静态目录


def _request(url, method="GET", data=None, timeout=3):
    req = urllib.request.Request(url, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
    else:
        body = None
    try:
        return urllib.request.urlopen(req, data=body, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e


class WebappReachableMixin:
    """如果 webapp 没跑，整组 test 跳过（在 CI / 离线机器上也能干净通过）。"""
    @classmethod
    def setUpClass(cls):  # type: ignore[override]
        try:
            urllib.request.urlopen(WEBAPP_BASE + "/login", timeout=2)
        except Exception as e:
            raise unittest.SkipTest(f"webapp 不可达，跳过：{e}")


class NavLinksRouteTest(WebappReachableMixin, unittest.TestCase):

    def test_GET_unauthenticated_returns_401(self) -> None:
        r = _request(f"{WEBAPP_BASE}/api/nav-links", "GET")
        self.assertEqual(r.code, 401, "未登录 GET 应该 401，不是 404 或 200")

    def test_POST_unauthenticated_returns_401(self) -> None:
        r = _request(f"{WEBAPP_BASE}/api/nav-links", "POST", data=[])
        self.assertEqual(r.code, 401, "未登录 POST 应该 401")


class NavJsDeploymentTest(unittest.TestCase):
    """部署 sanity：nginx 静态目录里的 nav.js 是含新 API 调用的版本。"""

    def test_nav_js_served_and_has_fetch_logic(self) -> None:
        try:
            r = urllib.request.urlopen(NAV_JS_URL, timeout=3)
        except Exception as e:
            self.skipTest(f"nginx 不可达，跳过：{e}")
            return
        self.assertEqual(r.status, 200)
        body = r.read().decode("utf-8", errors="replace")
        self.assertIn("/api/nav-links", body, "nav.js 不含 /api/nav-links 调用，可能没部署新版")
        self.assertIn("fetchLinks",      body, "nav.js 不含 fetchLinks 函数")
        self.assertIn("saveLinks",       body, "nav.js 不含 saveLinks 函数")


if __name__ == "__main__":
    unittest.main()
