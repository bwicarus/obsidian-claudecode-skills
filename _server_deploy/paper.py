"""paper.py — 纸张模型 + 格子布局器(用户设计,2026-07-15)。

═══ 核心思想 ═══
一张纸有**字号**和**行高** → 它就被切成一张**网格**(rows × cols,以全角字符为单位)。
元素用**行/列**定位与定尺(按钮 = 1 行高 × 6 字宽;图 = 8 行 × 16 列),**不用像素坐标**。

三个收益:
① **AI 好写**:它想的是"给我 20 个填空 + 一个按钮",不是 x=142px。即兴发挥也不容易排坏。
② **bbox 变成纯算术**:每个元素的页归一化 bbox 由 (row, col, span) **直接算出** ——
   服务端自己就知道每个填空在哪,**不需要前端渲染完再量出来写回**(那一环又丑又不可靠)。
   这是"手写填空 → AI 按格裁图批改"能成立的根基。
③ **插入前就能算溢出**:每个元素的 span 已知 → 放不下 → 自动换行 / 自动补下一张纸。

⚠ **前端必须严格按格子绝对定位**渲染(不能用自由流式 CSS),否则 ② 的算术就对不上真实位置。

═══ 布局模式(用户拍板:A 默认 / B 可选)═══
- **A(默认)**:AI **不管布局** —— 只给一串元素,布局器按游标流式排、自动换行、自动补页。
- **B(可选)**:AI 想精确控制时,给元素写 `at: [row, col]`。
"""
import math

# ── 纸张预设:用**目标行/列数(观感)**定,不用绝对 px ────────────────────────────
# ⚠ 血的教训(2026-07-15):最初用 "line_h=28px / char_w=字号" 算格子。在 595pt 的 A4 上合理,
#   但这本书的 PDF 页是 **2230×3225pt**(超大扫描件)→ 一格才 14pt=整页 0.6% → 每行 8pt、
#   字比蚂蚁小、横线全挤顶部。根因:格子物理大小不该由"字号 px"定(那是 CSS 像素、相对视口),
#   而该由"页面上看起来占几行几列"定。所以改成:给**目标行列数**,行高/字宽 = 页尺寸 ÷ 目标数。
# rows/cols = 一页大致放多少行、多少全角字(观感固定,与页面物理尺寸无关);
# font_ratio = 字号占行高的比例(渲染时前端按 rect 高度 × 它算实际字号)。
PAPERS = {
    "dictation": {"label": "听写纸", "bg": "#fffdf7", "rows": 20, "cols": 24, "margin": 0.05, "font_ratio": 0.42, "rule": "line"},
    "exam":      {"label": "试卷纸", "bg": "#ffffff", "rows": 30, "cols": 34, "margin": 0.05, "font_ratio": 0.5, "rule": "none"},
    "math":      {"label": "数学演草纸", "bg": "#fbfdff", "rows": 32, "cols": 30, "margin": 0.04, "font_ratio": 0.5, "rule": "grid"},
    "draw":      {"label": "绘画纸", "bg": "#fffefa", "rows": 24, "cols": 26, "margin": 0.03, "font_ratio": 0.5, "rule": "none"},
    "note":      {"label": "笔记纸", "bg": "#fffdf7", "rows": 26, "cols": 28, "margin": 0.05, "font_ratio": 0.45, "rule": "line"},
}
DEFAULT_KIND = "note"


def spec(kind: str, page_w: float, page_h: float) -> dict:
    """由 纸张预设(目标行列数)+ 页面物理尺寸 算出格子的物理大小。
    格子按**比例**切:留 margin 边距后,把可用区平分成 rows × cols 格。
    这样不管页面是 A4(595)还是超大扫描件(2230),观感一致(都是 ~30 行 ~34 列)。"""
    p = dict(PAPERS.get(kind) or PAPERS[DEFAULT_KIND])
    rows, cols = int(p["rows"]), int(p["cols"])
    mgn = float(p["margin"])
    mx = page_w * mgn
    my = page_h * mgn
    char_w = (page_w - 2 * mx) / cols       # 一格的物理宽(pt)
    line_h = (page_h - 2 * my) / rows       # 一格的物理高(pt)
    p.update({"kind": kind if kind in PAPERS else DEFAULT_KIND,
              "cols": cols, "rows": rows, "char_w": char_w, "line_h": line_h,
              "font_ratio": float(p.get("font_ratio") or 0.45),
              "mx": mx, "my": my, "page_w": float(page_w), "page_h": float(page_h)})
    return p


# ── 元素的默认尺寸(以格子为单位)──────────────────────────────────────────────
def _wide(s):
    """字符串占几个"全角格"(ASCII 半角算 0.5,向上取整)。"""
    n = 0.0
    for ch in str(s or ""):
        n += 0.5 if ord(ch) < 0x2E80 else 1.0
    return int(math.ceil(n))


def default_span(b: dict, sp: dict) -> list:
    """[行数, 列数]。这些默认值就是用户举的例子:按钮=1行高·几个字宽;图=80%页宽。"""
    k = b.get("kind")
    C = sp["cols"]
    if k == "text":
        w = int(b.get("cols") or C)
        need = max(1, int(math.ceil(_wide(b.get("text")) / max(1, w))))
        if b.get("style") == "h1":
            need += 1                                   # 大标题占两行(视觉留白)
        return [need, w]
    if k == "blank":
        return [1, C]                                   # 填空:整行(留足手写宽度)
    if k == "button":
        return [1, min(C, _wide(b.get("label")) + 3)]   # 按钮:1 行高 · 文字宽 + 内边距
    if k == "checkbox":
        return [1, min(C, _wide(b.get("label")) + 4)]
    if k == "image":
        return [8, max(1, int(C * 0.8))]                # 图:80% 页宽 ≈ 0.8*cols 列
    if k == "hr":
        return [1, C]
    return [1, C]


# ── 布局器 ────────────────────────────────────────────────────────────────────
def _rect(sp: dict, r: int, c: int, h: int, w: int) -> list:
    """★ 格子 → **页归一化 bbox(0-1)**。纯算术,不需要前端量。
    与墨迹坐标(RCInk.norm)、与服务端裁图 box(_figure_crop_png) **同一坐标系**。"""
    x0 = (sp["mx"] + c * sp["char_w"]) / sp["page_w"]
    y0 = (sp["my"] + r * sp["line_h"]) / sp["page_h"]
    x1 = (sp["mx"] + (c + w) * sp["char_w"]) / sp["page_w"]
    y1 = (sp["my"] + (r + h) * sp["line_h"]) / sp["page_h"]
    f = lambda v: round(max(0.0, min(1.0, v)), 4)
    return [f(x0), f(y0), f(x1), f(y1)]


def layout(blocks: list, sp: dict) -> list:
    """流式排版 + 自动补页。返回 **每张纸一个 list**:[[block,...], [block,...]]

    A(默认):元素不带 at → 按游标流式排,放不下换行,一页放不下 → 开下一张纸。
    B(可选):元素带 at:[row,col] → 就摆那儿(不推游标;越界则夹到页内)。
    每个元素都会被补上 at / span / rect。
    """
    pages, cur = [], []
    r, c = 0, 0
    for b0 in (blocks or []):
        b = dict(b0)
        h, w = (b.get("span") or default_span(b, sp))
        h = max(1, min(int(h), sp["rows"]))
        w = max(1, min(int(w), sp["cols"]))

        if b.get("at"):                                   # ── B:精确定位
            ar, ac = int(b["at"][0]), int(b["at"][1])
            ar = max(0, min(ar, sp["rows"] - h))
            ac = max(0, min(ac, sp["cols"] - w))
            b["at"], b["span"] = [ar, ac], [h, w]
            b["rect"] = _rect(sp, ar, ac, h, w)
            cur.append(b)
            continue

        if c + w > sp["cols"]:                            # ── A:放不下 → 换行
            r += 1
            c = 0
        if r + h > sp["rows"]:                            # ── 一页放不下 → 开新纸
            if cur:
                pages.append(cur)
            cur, r, c = [], 0, 0
        b["at"], b["span"] = [r, c], [h, w]
        b["rect"] = _rect(sp, r, c, h, w)
        cur.append(b)
        # 推进游标:整行宽的元素(填空/标题/分隔线)独占若干行;窄元素(按钮)可并排
        if w >= sp["cols"]:
            r += h
            c = 0
        else:
            c += w
            if c >= sp["cols"]:
                r += h
                c = 0
    if cur:
        pages.append(cur)
    return pages or [[]]


def plan(kind: str, blocks: list, page_w: float, page_h: float) -> dict:
    """一次算完:规格 + 每张纸的块(含 at/span/rect)。
    → 调用方据此知道**要建几张纸**,可以一次性建好,而不是边填边插页(那会反复触发插页 job)。"""
    sp = spec(kind, page_w, page_h)
    pgs = layout(blocks, sp)
    return {"spec": sp, "papers": pgs, "n_pages": len(pgs)}
