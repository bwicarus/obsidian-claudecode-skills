"""PWA 页面下线的行为合同。

一条边界压倒一切：**只关页面，不关 API**。
`/pdf/api/*` 是 App 与扩展赖以工作的服务端能力（词典、翻译、Anki、OCR、建图），
误伤它们等于为了下线一个没人用的界面，弄坏了正在用的东西。

另一条：下线要出声。410 而不是 404 —— 404 会被当成"坏了"，让人去查为什么
路由丢了；410 的语义是"这里以前有，现在有意撤掉了"。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "_server_deploy" / "reader_pwa_retirement.py"

try:
    import flask
except Exception:  # pragma: no cover
    flask = None


def _load():
    spec = importlib.util.spec_from_file_location(
        "reader_pwa_retirement_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RETIRE = _load() if MODULE_PATH.exists() else None


@unittest.skipIf(RETIRE is None or flask is None, "需要 flask 与下线模块")
class RetirementTest(unittest.TestCase):
    def setUp(self):
        app = flask.Flask(__name__)
        bp = flask.Blueprint("pdf_reader", __name__, url_prefix="/pdf")

        # 页面入口（应被拦）
        @bp.route("/")
        def pdf_index():
            return "<html>书架</html>"

        @bp.route("/search")
        def pdf_search_page():
            return "<html>搜索</html>"

        @bp.route("/epub/view")
        def epub_view():
            return "<html>EPUB</html>"

        @bp.route("/fav/view")
        def pdf_fav_view():
            return "<html>收藏夹</html>"

        @bp.route("/html/view")
        def html_view():
            return "<html>HTML</html>"

        # API（绝不能被拦）
        @bp.route("/api/dict")
        def dictionary():
            return {"ok": True, "who": "dict api"}

        @bp.route("/api/highlights", methods=["GET", "POST"])
        def highlights():
            return {"ok": True, "who": "highlights api"}

        @bp.route("/api/translate", methods=["POST"])
        def translate():
            return {"ok": True, "who": "translate api"}

        RETIRE.register(bp)
        app.register_blueprint(bp)
        self.client = app.test_client()

    def test_page_entries_are_gone_with_410(self):
        for path in ("/pdf/", "/pdf/search", "/pdf/epub/view",
                     "/pdf/fav/view", "/pdf/html/view"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(
                    response.status_code, 410,
                    "下线要用 410（有意撤除），不是 404（会被当成坏了）",
                )
                self.assertIn("已下线", response.get_data(as_text=True))

    def test_apis_are_untouched(self):
        # 这条是整个改动的安全边界：误伤 API 等于为了关一个没人用的界面，
        # 弄坏了 App 与扩展正在用的东西。
        cases = [
            ("GET", "/pdf/api/dict", "dict api"),
            ("GET", "/pdf/api/highlights", "highlights api"),
            ("POST", "/pdf/api/highlights", "highlights api"),
            ("POST", "/pdf/api/translate", "translate api"),
        ]
        for method, path, who in cases:
            with self.subTest(path=path, method=method):
                response = self.client.open(path, method=method)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.get_json()["who"], who,
                    "API 必须原样到达真实处理器",
                )

    def test_retirement_is_identifiable_in_logs(self):
        response = self.client.get("/pdf/")
        self.assertEqual(
            response.headers.get("X-BW-Reader-Retirement"), RETIRE.CONTRACT,
            "要能在日志里把「有意下线」和「真的坏了」分开",
        )

    def test_notice_is_not_cached(self):
        # 缓存住 410 的话，将来若要恢复入口，用户端会拿着旧响应不放。
        response = self.client.get("/pdf/")
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_unknown_endpoint_is_not_swallowed(self):
        # 闸门只认名单里的 endpoint；名单外的一律放行，
        # 包括本来就不存在的路径 —— 那该是 404，不该被伪装成 410。
        response = self.client.get("/pdf/nonexistent-page")
        self.assertEqual(response.status_code, 404)

    def test_gate_returns_none_for_everything_else(self):
        with self.client.application.test_request_context("/pdf/api/dict"):
            self.assertIsNone(
                RETIRE.gate(),
                "非下线目标必须返回 None，否则会短路整个 blueprint",
            )


@unittest.skipIf(RETIRE is None, "下线模块不在此工作树")
class EndpointListTest(unittest.TestCase):
    def test_only_page_endpoints_are_listed(self):
        # 名单里出现任何 api 字样的 endpoint，说明有人把服务端能力也关了。
        for endpoint in RETIRE.RETIRED_PAGE_ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.assertNotIn(
                    "api", endpoint.lower(),
                    f"{endpoint} 看起来是 API，不该出现在页面下线名单里",
                )

    def test_all_known_pwa_entries_are_covered(self):
        expected = {
            "pdf_reader.pdf_index",
            "pdf_reader.pdf_search_page",
            "pdf_reader.epub_view",
            "pdf_reader.pdf_fav_view",
            "pdf_reader.html_view",
        }
        self.assertEqual(
            set(RETIRE.RETIRED_PAGE_ENDPOINTS), expected,
            "漏掉一个入口，PWA 就还能从那里进去",
        )


if __name__ == "__main__":
    unittest.main()
