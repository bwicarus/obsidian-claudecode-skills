"""高亮唯一实现门禁：手动 / 助手(MCP) / 已有标注 三个入口必须共用同一数据模型、
同一渲染组件与同一触摸交互路径。

背景（2026-07-26 三个真实 bug 的根因，都是"没有收敛为唯一实现"的后果）：
  ① 助手写入 color="yellow"（命名色）→ 渲染端 `_hlRgba` 只给 #rrggbb 加 alpha，
     命名色原样透传成**不透明实色**盖住正文；手动路径因为走色板 hex 而正常。
  ② PDF 把 `RC.highlight.gesture` 实例建在 **per-rect 循环体内**，一条高亮的多个重叠
     rect 各自算"第一击" → iPad 上要三击才弹编辑框（EPUB 早就是单实例委托，没这问题）。
  ③ **跨模块 CSS class 碰撞**（2026-07-26 复查修正：先前把它归因为"抄漏 flex-shrink"是错的）：
     `rc-turncard.js` 的**裸选择器** `.rc-hl-sw{width:10px;height:10px}` 与共享高亮弹层
     `rc-highlight.js` 的 `.rc-hl-pop .rc-hl-sw`（色板行容器）**同名**；后者特异性虽高，但从不
     声明 width/height → turncard 那条无人竞争地生效，把应为 358×28 的布局容器压成 10×10。
     用过一次助手编排（注入 rc-hlcard CSS）后：圆点被压扁成"细长条"；给圆点加上
     `flex:0 0 auto`+`flex-wrap` 后症状转为**纵向溢出 178px**，砸在 textarea 与按钮行上
     ＝用户报的"弹窗内元素彼此重叠"。根因是命名碰撞，不是圆点自身样式。

本门禁全部是静态契约检查（无浏览器、无网络），跑在 CI/handoff 里防复发。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PDF_READER = ROOT / "_server_deploy" / "pdf_reader.py"
HL_SRC = ROOT / "_server_deploy" / "static" / "pdf" / "reader.src" / "17-highlight.js"
READER_JS = ROOT / "_server_deploy" / "static" / "pdf" / "reader.js"
RC_HL = ROOT / "_server_deploy" / "static" / "pdf" / "rc-highlight.js"


class HighlightColorSingleSource(unittest.TestCase):
    """① 颜色：所有写入入口收敛到同一色板规范，不透明色永不落库。"""

    def setUp(self) -> None:
        self.py = PDF_READER.read_text("utf-8")

    def test_normalizer_exists_and_matches_frontend_palette(self):
        self.assertIn("def hl_norm_color(", self.py, "服务端必须有唯一的高亮色归一化函数")
        palette = re.search(r"HL_PALETTE\s*=\s*\(([^)]*)\)", self.py)
        self.assertIsNotNone(palette, "缺少 HL_PALETTE 唯一色板常量")
        server_colors = set(c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}", palette.group(1)))
        js = HL_SRC.read_text("utf-8")
        front = re.search(r"DEFAULT_HL_COLORS\s*=\s*\[([^\]]*)\]", js)
        self.assertIsNotNone(front, "前端缺少 DEFAULT_HL_COLORS")
        front_colors = set(c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}", front.group(1)))
        self.assertEqual(
            server_colors, front_colors,
            "服务端 HL_PALETTE 必须与前端 DEFAULT_HL_COLORS 逐色一致（唯一色板）",
        )

    def test_every_write_path_normalizes(self):
        raw = re.findall(r"""color["']?\s*[:=]\s*\(?\s*data\.get\(["']color["']\)""", self.py)
        self.assertEqual(
            raw, [],
            "发现未经 hl_norm_color 的高亮 color 写入点；所有入口必须走唯一归一化",
        )
        self.assertGreaterEqual(
            self.py.count("hl_norm_color("), 3,
            "创建/编辑/重做等写入点都应调用 hl_norm_color",
        )

    def test_named_and_bad_colors_map_into_palette(self):
        ns = {"re": re}
        src = self.py
        start = src.index("HL_PALETTE = (")
        end = src.index("def register_pdf_reader(")
        exec(compile(src[start:end], "<hl>", "exec"), ns)
        norm = ns["hl_norm_color"]
        palette = set(ns["HL_PALETTE"])
        for bad in ["yellow", "YELLOW", "红", "", None, "rgb(255,0,0)", "not-a-color", "#xyzxyz"]:
            got = norm(bad)
            self.assertRegex(got, r"^#[0-9a-f]{6}$", f"{bad!r} 归一化结果必须是 #rrggbb")
            self.assertIn(got, palette, f"{bad!r} 必须落到唯一色板内")
        self.assertEqual(norm("#A3D4FF"), "#a3d4ff", "合法 hex 应保留（仅小写化）")
        self.assertEqual(norm("a7f3d0"), "#a7f3d0", "缺 # 的 hex 应补全")


class HighlightGestureSingleInstance(unittest.TestCase):
    """② 交互：整个阅读器只有一个手势实例，跨 rect 按 id 配对（iPad 双击而非三击）。"""

    def setUp(self) -> None:
        self.js = HL_SRC.read_text("utf-8")

    def test_gesture_is_module_singleton_not_per_rect(self):
        self.assertIn("_hlGestureSingleton", self.js, "手势必须是模块级单例")
        body = self.js[self.js.index("for (const r of (h.rects"):]
        self.assertNotIn(
            "RC.highlight.gesture(", body,
            "per-rect 循环内不得创建手势实例（这正是 iPad 三击的根因）",
        )

    def test_double_tap_resolves_highlight_by_id(self):
        self.assertIn("_openHlEditorById", self.js, "双击应按高亮 id 反查，而非闭包捕获单个 rect")
        self.assertIn("onDoubleTap: (id) =>", self.js, "onDoubleTap 必须消费 gesture 回传的 id")

    def test_native_dblclick_delegated(self):
        self.assertIn("document.addEventListener('dblclick'", self.js)
        self.assertIn("elementFromPoint", self.js, "委托需按落点反查 .hl-saved")

    def test_built_reader_js_contains_fix(self):
        built = READER_JS.read_text("utf-8")
        for token in ("_hlGestureSingleton", "_openHlEditorById"):
            self.assertIn(token, built, f"reader.js 未包含 {token}；请重跑 build_pdf_reader_js.sh")


class HighlightPopoverSwatchShape(unittest.TestCase):
    """③ 样式：共享编辑弹层的色板圆点在窄视口（iPad）恒为圆形且可点。"""

    def setUp(self) -> None:
        self.css = RC_HL.read_text("utf-8")
        m = re.search(r"\.rc-hl-pop \.rc-hl-sw-i\{([^}]*)\}", self.css)
        self.assertIsNotNone(m, "找不到 .rc-hl-sw-i 规则")
        self.swatch = m.group(1)

    def test_swatch_never_shrinks(self):
        flat = self.swatch.replace(" ", "")
        self.assertIn("flex:00auto", flat, "圆点必须 flex:0 0 auto，否则窄视口被压成细长条")
        self.assertIn("min-width", self.swatch, "需 min-width 兜底")
        self.assertIn("aspect-ratio:1/1", flat, "需 aspect-ratio 保持正圆")
        self.assertIn("border-radius:50%", flat)

    def test_swatch_touch_target(self):
        w = re.search(r"width:(\d+)px", self.swatch)
        self.assertIsNotNone(w)
        self.assertGreaterEqual(int(w.group(1)), 28, "触控命中盒至少 28px")

    def test_swatch_row_wraps(self):
        row = re.search(r"\.rc-hl-pop \.rc-hl-sw\{([^}]*)\}", self.css)
        self.assertIsNotNone(row)
        self.assertIn("flex-wrap:wrap", row.group(1).replace(" ", ""),
                      "色板行必须换行，色多时不得挤压圆点")

    def test_popover_has_min_width(self):
        pop = re.search(r"\.rc-hl-pop\{([^}]*)\}", self.css)
        self.assertIsNotNone(pop)
        self.assertIn("min-width", pop.group(1), "弹层需 min-width，避免 shrink-to-fit 压扁色板行")


class HighlightRenderingSingleComponent(unittest.TestCase):
    """跨入口一致性：助手创建的高亮不得自带渲染/样式旁路，必须走同一组件与 class。"""

    def test_assistant_does_not_ship_own_highlight_css(self):
        asst = (ROOT / "_server_deploy" / "assistant.py").read_text("utf-8")
        self.assertNotIn("mix-blend-mode", asst, "助手侧不得自带高亮样式")
        self.assertNotIn(".hl-saved{", asst, "助手侧不得复制高亮 CSS")

    def test_single_hl_class_in_renderer(self):
        js = HL_SRC.read_text("utf-8")
        self.assertIn("'hl-saved'", js, "渲染端只用唯一 .hl-saved 组件类")

    def test_alpha_applied_at_render(self):
        js = HL_SRC.read_text("utf-8")
        self.assertIn("_hlRgba(h.color, 0.4)", js,
                      "渲染必须统一加 alpha；服务端归一化 + 此处 alpha 共同保证正文可读")


class HighlightPopoverNoCssCollision(unittest.TestCase):
    """④ 防复发：弹层内部件的 class 不得被其它模块的裸选择器命中（本次重叠的根因）。

    共享层里同名 class 极易碰撞：特异性高的一方若没声明某属性，低特异性的裸选择器就会
    "无人竞争地生效"。所以约定：其它文件里凡用到弹层同名 class，必须带自己的祖先作用域。
    """

    #: rc-highlight.js 弹层/列表内部使用、且已知与别处重名的 class
    SHARED_NAMES = ("rc-hl-sw", "rc-hl-row", "rc-hl-tx")
    #: 允许裸用的例外：该 class 的宿主节点不在 .rc-hlcard 内（齿轮按钮挂在 .rc-flow-meta）
    ALLOW_BARE = {"rc-hl-b"}

    def test_other_modules_scope_shared_class_names(self):
        pdf_dir = ROOT / "_server_deploy" / "static" / "pdf"
        offenders = []
        for js in sorted(pdf_dir.glob("rc-*.js")):
            if js.name == "rc-highlight.js":
                continue
            text = js.read_text("utf-8")
            for name in self.SHARED_NAMES:
                if name in self.ALLOW_BARE:
                    continue
                # 匹配作为"选择器最左首个 compound"出现的裸 class：'.rc-hl-sw{ 或 '.rc-hl-sw.x{ 等
                for m in re.finditer(r"'\.(" + re.escape(name) + r")([.:\[][^'{]*)?\{", text):
                    offenders.append(f"{js.name}: '.{m.group(1)}…' 未加祖先作用域")
        self.assertEqual(
            offenders, [],
            "以下裸选择器会命中共享高亮弹层内部件，必须加自己的祖先作用域（如 .rc-hlcard）：\n"
            + "\n".join(offenders),
        )

    def test_popover_layout_row_has_auto_size(self):
        """判据 2：布局容器显式 auto，对同名弱选择器免疫。"""
        css = RC_HL.read_text("utf-8")
        m = re.search(r"\.rc-hl-pop \.rc-hl-sw\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        flat = m.group(1).replace(" ", "")
        self.assertIn("width:auto", flat, "色板行需显式 width:auto")
        self.assertIn("height:auto", flat, "色板行需显式 height:auto")

    def test_note_textarea_matches_native(self):
        """判据 3：备注框逐字对齐原生 #hl-popover textarea，不得固定像素宽。"""
        css = RC_HL.read_text("utf-8")
        m = re.search(r"\.rc-hl-pop \.rc-hl-note\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        rule = m.group(1)
        self.assertIn("width:100%", rule.replace(" ", ""), "textarea 应 width:100%（原生同款）")
        self.assertIn("box-sizing:border-box", rule.replace(" ", ""))
        self.assertNotRegex(rule, r"width:\s*\d+px", "textarea 不得用固定像素宽")

    def test_no_vw_width_inside_popover(self):
        """判据 4：弹层后代不得用 vw 宽度（父已有 max-width，vw 恒不生效，只会误导排查）。"""
        css = RC_HL.read_text("utf-8")
        bad = re.findall(r"\.rc-hl-pop [^{]*\{[^}]*max-width:\s*\d+vw", css)
        self.assertEqual(bad, [], f"弹层后代仍在用 vw 宽度：{bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
