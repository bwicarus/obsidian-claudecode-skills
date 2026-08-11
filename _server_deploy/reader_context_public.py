# -*- coding: utf-8 -*-
"""把「当前阅读上下文」快照渲染成一个可被外部 AI 抓取的公开页面。

用途：用户希望外部 AI（网页版 ChatGPT / Claude 等）能通过一个 URL 读到
「我此刻在读什么」。数据来自 scripts/reader_context_snapshot.py —— 与推送到
Windows 的是同一份快照，这里只是多了一个只读出口，不改变它的产生方式。

三个设计约束，每一条都来自一个具体的失败模式：

**服务端渲染，不靠 JavaScript。** 多数 AI 抓取器不执行脚本，一个靠前端 fetch
填充的页面在它们眼里是空壳。正文直接写进 HTML。

**capability URL 而非登录。** 这个页面必须能被没有凭据的抓取器读到，所以不能
挂在 PROTECTED_PREFIXES 后面；取而代之的是一段不可猜测的路径。令牌错误一律
回 404 而不是 401 —— 401 等于告诉扫描者"这里有东西"。

**noindex 而不是 robots.txt Disallow。** 两者作用不同：Disallow 会把守规矩的
AI 抓取器一并挡在门外（而它们正是目标读者），noindex 只阻止收录进搜索索引。
用错方向会变成"该挡的没挡住、该让的没让过"。

不包含页面图像：快照本身带 assets/，但公开出口只给文本。书页图片既是版权内容
也是更强的隐私暴露，而 AI 抓取需要的只是文字。
"""
from __future__ import annotations

import hmac
import html
import os
import sys
import threading
import time
from pathlib import Path

from flask import Response, abort

# 快照生成器住在主项目的 scripts/ 下，webapp 在另一个目录，所以要显式加进来。
_PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
_SCRIPTS = _PROJECT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# 令牌与产物都放 state/ 下；令牌文件不进 git。
_TOKEN_FILE = _PROJECT / "state" / "reader-context-public-token.txt"
_BUILD_DIR = _PROJECT / "state" / "reader-context-public"

# 抓取器可能连着请求几次（正文一次、引用一次）。快照本身每秒最多变一次，
# 这个窗口只是不让一次抓取触发多轮 build，不影响新鲜度。
_CACHE_TTL_S = 10.0

_lock = threading.Lock()
_cache = {"at": 0.0, "text": ""}


def _expected_token() -> str:
    try:
        return _TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _token_ok(given: str) -> bool:
    want = _expected_token()
    # 没配置令牌时一律拒绝：缺配置必须表现为关闭，而不是敞开。
    if not want or len(want) < 32:
        return False
    return hmac.compare_digest(given.strip(), want)


def _snapshot_text() -> str:
    """返回当前快照的 Markdown 正文；失败时抛异常由调用方处理。"""
    now = time.monotonic()
    with _lock:
        if _cache["text"] and (now - _cache["at"]) < _CACHE_TTL_S:
            return _cache["text"]
        import reader_context_snapshot as snap  # 延迟导入：无令牌时根本不必加载

        _BUILD_DIR.mkdir(parents=True, exist_ok=True)
        snap.build(_BUILD_DIR)
        text = (_BUILD_DIR / "context.md").read_text(encoding="utf-8")
        _cache["at"] = now
        _cache["text"] = text
        return text


def _page(text: str) -> str:
    body = html.escape(text)
    updated = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<meta name=\"robots\" content=\"noindex,nofollow\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>当前阅读上下文</title>"
        "<style>body{margin:0;padding:1.2rem;background:#0f1420;color:#dbe6f6;"
        "font:14px/1.7 ui-monospace,Menlo,Consolas,monospace}"
        "pre{white-space:pre-wrap;word-break:break-word;margin:0}"
        "h1{font-size:1rem;color:#8fb8ff;margin:0 0 .8rem}</style></head><body>"
        f"<h1>当前阅读上下文 · 生成于 {html.escape(updated)}</h1>"
        f"<pre>{body}</pre></body></html>"
    )


def _no_store(resp: Response) -> Response:
    # noindex 阻止收录但不阻止抓取，这正是这个页面要的组合。
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def register_reader_context_public(app) -> None:
    """挂上只读的公开快照出口。

    故意不加进 PROTECTED_PREFIXES / NAV_INJECT_PREFIXES：前者会要求登录、
    把目标读者挡在外面，后者会往页面里注入导航，对抓取只是噪声。
    """

    @app.route("/ctx/<token>/", methods=["GET"])
    def reader_context_public_page(token: str):
        if not _token_ok(token):
            abort(404)
        try:
            text = _snapshot_text()
        except Exception as exc:  # noqa: BLE001
            # 说出是什么坏了。这个出口没有控制台可看，沉默等于不可诊断。
            return _no_store(Response(
                _page("快照暂时不可用：" + repr(exc)),
                status=503, mimetype="text/html; charset=utf-8",
            ))
        return _no_store(Response(
            _page(text), mimetype="text/html; charset=utf-8"))

    @app.route("/ctx/<token>/ping", methods=["GET"])
    def reader_context_public_ping(token: str):
        """零计算的静态页，用来把往返成本和生成成本分开量。

        真快照页每次可能要跑一次 build()。若只测那一个地址，网络慢和生成慢
        会混在同一个数字里，而两者的修法毫不相干。这个地址除了鉴权什么都不做，
        两边一减就是生成的开销。
        """
        if not _token_ok(token):
            abort(404)
        now = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        text = (
            "这是连通性与响应速度的测试页面。\n\n"
            "它不读取任何阅读数据，也不生成快照，只由服务端直接输出这段固定文本"
            "和下面这个时间戳。因此它测到的是网络往返本身。\n\n"
            f"服务端时间：{now}\n\n"
            "把本页与同目录下的快照页对比，两者之差即为生成快照的开销。\n"
        )
        return _no_store(Response(
            _page(text), mimetype="text/html; charset=utf-8"))

    @app.route("/ctx/<token>/raw.md", methods=["GET"])
    def reader_context_public_raw(token: str):
        if not _token_ok(token):
            abort(404)
        try:
            text = _snapshot_text()
        except Exception as exc:  # noqa: BLE001
            return _no_store(Response(
                "快照暂时不可用：" + repr(exc),
                status=503, mimetype="text/plain; charset=utf-8",
            ))
        return _no_store(Response(
            text, mimetype="text/plain; charset=utf-8"))
