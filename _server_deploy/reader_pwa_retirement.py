"""PWA 阅读器页面下线。

产品边界改了：完整阅读能力由 iOS App 和浏览器扩展提供 —— 扩展的支持面比 PWA
广得多（几乎所有桌面浏览器都支持扩展，但 PWA 支持参差），所以 Pi 不再提供
一份 App 已经能提供的阅读界面。

**只关页面入口，不动 `/pdf/api/*`。** App 和扩展仍然要用那些 API（词典、翻译、
Anki、OCR、建图这些需要服务端资源的能力），关掉它们会直接弄坏还在用的东西。

下线用 410 Gone 而不是 404，也不是静默重定向：

  · 404 会被当成"坏了"，于是有人去查为什么路由丢了
  · 静默重定向会让人以为页面还在、只是换了位置
  · 410 的语义正好是"这里以前有，现在有意撤掉了"，且带一句说明

这跟本项目一贯的做法一致：**下线也要出声**，不能让人对着一个说不清的失败排查。
"""

from __future__ import annotations

from flask import request

CONTRACT = "reader-pwa-retirement/1"

# 这些 endpoint 返回的是「PWA 阅读器界面」本身。
# 注意 endpoint 名是函数名，不是 URL —— 按 endpoint 拦可以覆盖同一处理器的
# 所有 URL 变体，按 path 拦会漏。
RETIRED_PAGE_ENDPOINTS = frozenset({
    "pdf_reader.pdf_index",        # /pdf/          书架 + PDF 阅读器
    "pdf_reader.pdf_search_page",  # /pdf/search    全局搜索页
    "pdf_reader.epub_view",        # /pdf/epub/view EPUB 阅读器
    "pdf_reader.pdf_fav_view",     # /pdf/fav/view  收藏夹（物化成 EPUB 的那本）
    "pdf_reader.html_view",        # /pdf/html/view 导入的 HTML/Markdown
})

_NOTICE_HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>阅读器已迁移</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",
              "Hiragino Sans","Noto Sans CJK SC",sans-serif;
         background:#0f1420; color:#dbe4f8; padding:24px; }
  main { max-width:34rem; }
  h1 { font-size:1.25rem; margin:0 0 .75rem; font-weight:600; }
  p { margin:0 0 .75rem; color:#9aa7c4; }
  code { background:#1a2540; padding:.1em .4em; border-radius:4px;
         font-size:.9em; color:#dbe4f8; }
</style>
<main>
  <h1>网页版阅读器已下线</h1>
  <p>完整阅读功能现在由 <strong>iOS App</strong> 与<strong>浏览器扩展</strong>提供。
     这台服务器不再提供重复的一份阅读界面。</p>
  <p>服务端能力（词典、翻译、Anki、OCR、知识图谱构建）照常在
     <code>/pdf/api/*</code> 提供，App 与扩展直接调用。</p>
</main>
"""


def retired_response():
    """410 + 说明页。带 Link 头指明这是有意下线，便于日志里区分。"""
    return (
        _NOTICE_HTML,
        410,
        {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "X-BW-Reader-Retirement": CONTRACT,
        },
    )


def gate():
    """挂在 before_request 上：命中已下线的页面入口就直接返回说明。"""
    if request.endpoint in RETIRED_PAGE_ENDPOINTS:
        return retired_response()
    return None


def register(bp) -> None:
    bp.before_request(gate)
