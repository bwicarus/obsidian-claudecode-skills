#!/usr/bin/env python3
"""把阅读器/书架的真实 UI 组件抠成独立预览 HTML(给 Claude Design 用)。
真实 CSS(模板 <style> + 各 reader.src 注入样式)+ 真实 HTML 片段(按 id 抽),零手抠 → 100% 还原线上长相。
每个组件:doctype + 内联 theme + 一段 show-override(把 display:none/绝对定位的浮层在卡片里显出来)+ 真实片段 + @dsCard 标记。
输出 components/*.html + manifest.json(供 register_assets)。
"""
import re, json, pathlib

ROOT = pathlib.Path("/home/bwicarus/claude")
READER_TPL = ROOT / "_server_deploy/templates/pdf_reader.html"
INDEX_TPL  = ROOT / "_server_deploy/templates/pdf_index.html"
SRC        = ROOT / "_server_deploy/static/pdf/reader.src"
OUT        = ROOT / "state/design-kit"
(OUT / "components").mkdir(parents=True, exist_ok=True)


def style_of(p):
    m = re.search(r"<style>(.*?)</style>", p.read_text("utf-8"), re.S)
    return m.group(1) if m else ""


def js_css(js):
    parts = re.findall(r"'((?:\\.|[^'\\\n])*)'", js)
    css = [p for p in parts if "{" in p and ":" in p and "}" in p]
    return "\n".join(css).replace("\\'", "'").replace('\\"', '"')


def build_theme():
    t = style_of(READER_TPL) + "\n/* ==== pdf_index ==== */\n" + style_of(INDEX_TPL)
    for f in sorted(SRC.glob("*.js")):
        c = js_css(f.read_text("utf-8"))
        if c.strip():
            t += f"\n/* ==== {f.name} ==== */\n" + c
    return t


def extract_div(html, anchor):
    """从 html 里按 'id="X"'(anchor) 抽出**配平的 <div>…</div>** 片段。"""
    i = html.find(anchor)
    if i < 0:
        return None
    start = html.rfind("<div", 0, i)
    if start < 0:
        return None
    depth, j = 0, start
    for m in re.finditer(r"<div\b|</div>", html[start:]):
        if m.group() == "<div":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                j = start + m.end()
                return html[start:j]
    return html[start:]


# (anchor, 输出文件, 分组, 名称, 副标题, 卡片宽, 卡片高, 来源模板, 额外 show-override)
SHOW = ("html,body{height:auto!important;overflow:visible!important;display:block!important}"
        ".stage{padding:14px;display:flex;align-items:flex-start;justify-content:center}")
def vis(sel):   # 把某个浮层从 display:none/绝对定位 强制显示在卡片里
    return (f"{sel}{{display:block!important;position:static!important;inset:auto!important;"
            f"margin:0 auto!important;transform:none!important;visibility:visible!important;opacity:1!important;"
            f"max-height:none!important;width:auto}}")

COMPONENTS = [
    ('id="header"',          "top-toolbar.html",   "导航",      "顶部工具栏",       "返回/页码/缩放/模式/搜索/设置",      980, 110, READER_TPL, "#header{position:static!important;display:flex!important}"),
    ('id="sel-toolbar"',     "selection-toolbar.html","选中与查词","选中工具栏",      "选中预览 + 查词/翻译/高亮/问AI",      520, 150, READER_TPL, vis("#sel-toolbar")),
    ('id="word-pop"',        "dict-popover.html",  "选中与查词", "字典小框",         "音标/音调线/释义/例句/加 Anki",      400, 460, READER_TPL, vis("#word-pop")),
    ('id="hl-popover"',      "highlight-editor.html","选中与查词","高亮编辑浮层",     "4 色板 + 备注 + 删除",               340, 260, READER_TPL, vis("#hl-popover")),
    ('id="result-modal"',    "explain-box.html",   "AI 助手",   "翻译/解释结果框",   "流式 Markdown+公式 + 底部追问",       760, 560, READER_TPL, vis("#result-modal")+vis("#result-mask")+"#result-mask{background:transparent!important}"),
    ('id="page-scrub-pop"',  "page-scrubber.html", "导航",      "页码滑块",         "拖动跳页 + 页码气泡",                420, 130, READER_TPL, vis("#page-scrub-pop")),
    ('id="settings-mask"',   "settings-panel.html","面板与抽屉","设置面板",         "model/effort/颜色/语言/调试 多 tab",  800, 620, READER_TPL, vis("#settings-mask")+vis("#settings-panel")+"#settings-mask{background:transparent!important}"),
    ('id="draft-modal"',     "draft-modal.html",   "标注",      "草稿 → 笔记/Anki", "选段草稿 + 圆圈卡 + 左滑删除",        560, 480, READER_TPL, vis("#draft-modal")),
    ('id="search-panel"',    "search-panel.html",  "面板与抽屉","全文搜索",         "FTS5 命中列表 + 跳页",               560, 540, READER_TPL, vis("#search-panel")),
    ('id="side-pane-kg"',    "kg-drawer.html",     "面板与抽屉","知识点抽屉",       "本页知识点 + 跟踪",                  400, 560, READER_TPL, "#side-pane-kg{display:block!important}"),
    ('id="side-pane-vocab"', "vocab-pane.html",    "面板与抽屉","生词本",           "生词列表 + 掌握度",                  400, 560, READER_TPL, "#side-pane-vocab{display:block!important}"),
    ('id="ink-toolbar"',     "ink-toolbar.html",   "标注",      "手写笔工具栏",     "笔/橡皮/颜色/撤销",                  440, 110, READER_TPL, vis("#ink-toolbar")),
    # 书架页(pdf_index 模板)
    ('class="pdf-item"',     "bookshelf-row.html", "书架与处理","书架书本行",       "书名/大小 + 文档/漫画/清晰度/压缩/预热/🧮公式 + 进度条", 980, 150, INDEX_TPL, ".prep-prog{display:flex!important}.prep-bar{width:62%!important}"),
]

# 模板里是 JS 运行时填的空壳 → 用真实子类手搓 mock 内容(让卡片有东西看)
MOCK = {
 'id="word-pop"': '''<div id="word-pop" style="display:block;position:static;margin:0 auto">
  <div class="wp-head"><span class="wp-word">resilient</span><span class="wp-phon">/rɪˈzɪliənt/</span><button class="wp-speak">🔊</button></div>
  <div style="padding:0 14px 6px;color:#cfe6ff;font-size:13px">adj. 有韧性的;能迅速恢复的</div>
  <div class="wp-ex" style="margin:2px 14px 10px"><div class="wp-ex-ja">a resilient material that springs back to its shape</div></div>
  <div style="padding:9px 14px;border-top:1px solid #243049;display:flex;gap:8px">
    <button style="background:#244470;border:1px solid #3b6db5;color:#fff;border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer">🎴 加入 Anki</button>
    <button style="background:transparent;border:1px solid #3b6db5;color:#a8cdff;border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer">展开详解 ›</button></div></div>''',
 'id="hl-popover"': '''<div id="hl-popover" class="open" style="display:block;position:static;margin:0 auto">
  <div class="row"><span class="row-lbl">颜色</span>
    <span class="swatch cur" style="background:#ffd54a"></span><span class="swatch" style="background:#7fd1ff"></span>
    <span class="swatch" style="background:#a0e57f"></span><span class="swatch" style="background:#ff9db0"></span></div>
  <div class="row" style="flex-direction:column;align-items:stretch"><span class="row-lbl">备注</span>
    <textarea placeholder="给这段高亮加点备注…">这是开普勒第三定律的关键</textarea></div>
  <div class="hl-snip-wrap">
    <div class="hl-snip"><div class="hl-snip-circle">📝</div><div class="hl-snip-content">整理成笔记</div></div>
    <div class="hl-snip"><div class="hl-snip-circle">🎴</div><div class="hl-snip-content">做成 Anki 卡</div></div></div></div>''',
}
def jinja_clean(s):
    s = s.replace("{{ p.name }}", "费恩曼物理学讲义（第1卷）：新千年版 (R.P.Feynman)").replace("{{ p.name|lower }}", "feynman")
    s = s.replace("{{ p.dir }}", "Excalidraw").replace("{{ p.size_kb }}", "318240").replace("{{ p.rel }}", "feynman.pdf")
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    s = re.sub(r"\{%[^%]*%\}", "", s)
    return s

theme = build_theme()
(OUT / "theme.css").write_text(theme, "utf-8")
manifest = []
for anchor, fn, group, name, sub, w, h, tpl, extra in COMPONENTS:
    frag = MOCK.get(anchor) or extract_div(tpl.read_text("utf-8"), anchor)
    if not frag:
        print("⚠ 抽不到:", anchor); continue
    frag = jinja_clean(frag)
    html = (f'<!-- @dsCard group="{group}" -->\n'
            '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{name}</title><style>\n:root{{color-scheme:dark}}\n'
            'body{margin:0;background:#0a0e16;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}\n'
            f'{theme}\n/* ---- 卡片预览 show-override ---- */\n{SHOW}\n{extra}\n'
            '</style></head><body><div class="stage">' + frag + '</div></body></html>')
    (OUT / "components" / fn).write_text(html, "utf-8")
    manifest.append({"name": name, "path": f"components/{fn}", "subtitle": sub,
                     "viewport": {"width": w, "height": h}, "group": group})
    print(f"✓ {fn}  ({len(html)//1024}KB)  [{group}] {name}")
(OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
print(f"\n生成 {len(manifest)} 个组件 → {OUT}/components/  (manifest.json 已写)")
