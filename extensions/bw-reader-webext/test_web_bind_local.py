#!/usr/bin/env python3
"""Real-Chromium regression for the ordinary-web character layer and card anchoring.

为什么必须有这一份：2026-08-23 的三个文件（web-textlayer / web-bind / web-pagetext）
只用最小 DOM 桩测过就发了出去，桩全绿，而真浏览器里有五个 high —— 其中
「三击选段」这条是审计的验证者在真 Chrome 里实测复现的。
`references/silent-failure-lessons.md` 记的最贵一条教训就是"用非浏览器的东西
验证浏览器路径"。所以这里用**真手势**（真三击、真 Ctrl+A）打**真扩展**。

覆盖的不变量，逐条对应审计抓到的缺陷：

* 三击选段（endContainer 是元素）必须能折出 page-chars 锚，不能静默返回 null；
* Ctrl+A（startContainer 是 <body>）同上；
* 元素边界换算不得把子节点序号当字符偏移；
* 正文写在 div 里的站点必须认得出块，绝不返回"成功且空"；
* segments 的 from/to 必须能在字符层里切出原文；
* `::highlight` 的样式表必须落在主文档，不能落进影子树；
* 振假名 <rt> 与译页 [data-rc-tr] 不得进入字符层，已存在的锚仍解得出；
* `.rc-tr-src`（被收起的原文）必须**留在**字符层里；
* `__pageBindRemove` 真的清掉角标并重编号。

用法（Pi 上需要 X server）：

    DISPLAY=:99 python3 extensions/bw-reader-webext/test_web_bind_local.py
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright


EXT = Path(
    os.environ.get("BW_EXTENSION_ROOT", str(Path(__file__).resolve().parent))
).resolve()
DEFAULT_CHROME = (
    Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"
)
CHROME = Path(os.environ.get("BW_PLAYWRIGHT_CHROME", str(DEFAULT_CHROME)))
PAGE_URL = "http://web-bind-contract.test/article"

# 刻意混合三种正文写法：语义标签、纯 div（x.com/Gmail 那类）、以及导航噪声。
PAGE_HTML = """<!doctype html>
<html lang="ja">
<head><meta charset="utf-8"><title>web bind contract</title>
<style>
  body { margin: 0; font: 18px/1.8 sans-serif; }
  nav { padding: 8px; background: #eee; }
  #para, #jp { margin: 24px; }
  #tweet { margin: 24px; }
</style></head>
<body>
  <nav id="nav">HOME ABOUT CONTACT</nav>
  <p id="para">Thermodynamics states that energy is conserved.</p>
  <div id="tweet"><span>This paragraph lives inside a bare div.</span></div>
  <p id="jp">日本語</p>
  <p id="tail">The second law states that entropy increases.</p>
</body>
</html>
"""


class World:
    """CDP bridge into the extension's isolated world (content scripts)."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.session = page.context.new_cdp_session(page)
        self.ids: list[int] = []
        self.chosen: int | None = None
        self.session.on(
            "Runtime.executionContextCreated",
            lambda e: self.ids.append(e["context"]["id"]),
        )
        self.session.on(
            "Runtime.executionContextDestroyed",
            lambda e: self._forget(e["executionContextId"]),
        )
        self.session.on("Runtime.executionContextsCleared", lambda _e: self._clear())
        self.session.send("Runtime.enable")

    def _forget(self, cid: int) -> None:
        if cid in self.ids:
            self.ids.remove(cid)
        if self.chosen == cid:
            self.chosen = None

    def _clear(self) -> None:
        self.ids.clear()
        self.chosen = None

    def _raw(self, cid: int, expr: str) -> Any:
        res = self.session.send(
            "Runtime.evaluate",
            {"contextId": cid, "expression": expr, "returnByValue": True},
        )
        if res.get("exceptionDetails"):
            d = res["exceptionDetails"]
            raise RuntimeError(
                str(res.get("result", {}).get("description") or d.get("text"))
            )
        return res.get("result", {}).get("value")

    def find(self, timeout_ms: int = 20_000) -> int:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.chosen in self.ids:
                return int(self.chosen)
            for cid in reversed(list(self.ids)):
                try:
                    if self._raw(cid, "!!window.__bwWebTextLayer"):
                        self.chosen = cid
                        return cid
                except Exception:
                    pass
            time.sleep(0.05)
        raise RuntimeError("找不到装载了 __bwWebTextLayer 的隔离世界")

    def ev(self, expr: str) -> Any:
        return self._raw(self.find(), expr)


def launch(playwright: Any, profile: str) -> BrowserContext:
    context = playwright.chromium.launch_persistent_context(
        profile,
        executable_path=str(CHROME),
        headless=False,
        viewport={"width": 1280, "height": 900},
        args=[
            f"--disable-extensions-except={EXT}",
            f"--load-extension={EXT}",
            "--no-sandbox",
        ],
    )
    context.route(
        "http://web-bind-contract.test/**",
        lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=PAGE_HTML
        ),
    )
    return context


FAILURES: list[str] = []
PASSED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(name)
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    with sync_playwright() as playwright:
        profile = tempfile.mkdtemp(prefix="bw-web-bind-")
        context = launch(playwright, profile)
        try:
            page = context.new_page()
            page.goto(PAGE_URL, wait_until="domcontentloaded")
            page.wait_for_selector("#bw-reader-host", state="attached", timeout=20_000)
            world = World(page)
            world.find()
            page.wait_for_timeout(400)

            # ── ① 真三击选段：endContainer 是元素 ────────────────────
            # 审计验证者在真 Chrome 里实测：三击 → endContainer=<P>，
            # 老实现 indexOf 得 -1 → 返回 null → 「锁定元素」静默失效。
            page.click("#para", click_count=3)
            page.wait_for_timeout(150)
            shape = world.ev(
                """(() => {
                  const s = getSelection();
                  if (!s || !s.rangeCount) return null;
                  const r = s.getRangeAt(0);
                  return { start: r.startContainer.nodeType,
                           end: r.endContainer.nodeType,
                           text: s.toString() };
                })()"""
            )
            check(
                "三击确实产生了元素容器边界（否则这条测试没在测它想测的东西）",
                bool(shape) and shape.get("end") == 1,
                f"实得 {shape}",
            )
            got = world.ev(
                """(() => {
                  const c = window.__bwSelectionController;
                  const cur = c && c.current();
                  if (!cur || !cur.anchor) return null;
                  const a = cur.anchor;
                  const snap = window.__bwWebTextLayer.snapshot();
                  return { kind: a.kind, page: a.page, from: a.from, to: a.to,
                           slice: snap.text.slice(a.from, a.to) };
                })()"""
            )
            check("三击选段能折出 page-chars 锚", bool(got), "current() 返回 null")
            if got:
                check("三击锚 page 恒为 1", got.get("page") == 1, str(got.get("page")))
                check(
                    "三击锚的 from/to 能在字符层里切出所选文字",
                    "energy is conserved" in (got.get("slice") or ""),
                    repr(got.get("slice"))[:120],
                )

            # ── ② 真 Ctrl+A：startContainer 是 <body> ────────────────
            page.click("#para")
            page.keyboard.press("Control+a")
            page.wait_for_timeout(150)
            allsel = world.ev(
                """(() => {
                  const cur = window.__bwSelectionController.current();
                  if (!cur || !cur.anchor) return null;
                  const snap = window.__bwWebTextLayer.snapshot();
                  return {
                    from: cur.anchor.from, to: cur.anchor.to, len: snap.length,
                    // ⚠ 别断言 from===0：字符层开头是 HTML 缩进空白，
                    //   而 Ctrl+A 会把边界规范化到第一个**可见**字符。
                    //   真机实测 from=3（层首是 "
  HOME"）。语义上正确的
                    //   判据是"选区去掉首尾空白后等于整篇去掉首尾空白"。
                    trimEq: snap.text.slice(cur.anchor.from, cur.anchor.to).trim()
                            === snap.text.trim()
                  };
                })()"""
            )
            check("Ctrl+A 全选能折出锚", bool(allsel), "current() 返回 null")
            if allsel:
                check(
                    "全选锚覆盖整篇（按去空白后相等判定）",
                    allsel.get("trimEq") is True,
                    str(allsel),
                )

            # ── ③ 纯 div 站点：不能返回"成功且空" ────────────────────
            pt = world.ev("JSON.stringify(window.__bwWebPageText.read())")
            import json as _json

            data = _json.loads(pt) if pt else {}
            check("page-text 成功", data.get("ok") is True, str(data)[:160])
            check(
                "纯 div 块被认出来（不是空 text）",
                "bare div" in (data.get("text") or ""),
                repr(data.get("text"))[:200],
            )
            check(
                "导航被标成导航而不是正文",
                any(
                    s.get("region") == "导航"
                    for s in (data.get("segments") or [])
                ),
                str([s.get("region") for s in (data.get("segments") or [])]),
            )
            check(
                "块覆盖了绝大部分字符层（没盖住的文字 AI 看不见）",
                (data.get("coverage") or 0) >= 0.9,
                f"coverage={data.get('coverage')}",
            )
            seg = next(
                (s for s in (data.get("segments") or []) if "bare div" in s.get("text", "")),
                None,
            )
            check("div 块有对应 segment", bool(seg))
            if seg:
                sliced = world.ev(
                    "window.__bwWebTextLayer.snapshot().text.slice(%d, %d)"
                    % (seg["from"], seg["to"])
                )
                check(
                    "segment 的 from/to 能在字符层里切出原文",
                    "bare div" in (sliced or ""),
                    repr(sliced)[:120],
                )

            # ── ④ ::highlight 样式表必须落在主文档，不能落进影子树 ──
            world.ev(
                """window.__pageBindCard(
                     { kind: 'page-chars', page: 1,
                       from: %d, to: %d, text: %s },
                     { uid: 'probe1', label: 'p1', tone: '#ff0000' })"""
                % (
                    seg["from"] if seg else 0,
                    seg["to"] if seg else 5,
                    _json.dumps((seg or {}).get("text", "")[:20]),
                )
            )
            page.wait_for_timeout(200)
            where = world.ev(
                """(() => {
                  const el = document.getElementById('bw-web-bind-highlight-css');
                  if (!el) return 'missing';
                  return el.getRootNode() === document ? 'document' : 'shadow';
                })()"""
            )
            check(
                "::highlight 样式表落在主文档（落影子树就一个像素都不画）",
                where == "document",
                f"实得 {where}",
            )
            check(
                "CSS.highlights 里确实注册了条目",
                bool(world.ev(
                    "!!(window.CSS && CSS.highlights && "
                    "[...CSS.highlights.keys()].some(k => String(k).startsWith('bwbind_')))"
                )),
            )

            # ── ⑤ __pageBindRemove 真的清掉角标并重编号 ─────────────
            world.ev(
                """window.__pageBindCard(
                     { kind:'page-chars', page:1, from:0, to:4, text:'HOME' },
                     { uid:'probe2', label:'p2' })"""
            )
            page.wait_for_timeout(150)
            before = world.ev("Object.keys(window.__bwWebBind._marks).length")
            world.ev(
                "window.__pageBindRemove({kind:'page-chars',page:1,from:0,to:4}, 'probe2')"
            )
            page.wait_for_timeout(150)
            after = world.ev("Object.keys(window.__bwWebBind._marks).length")
            check(
                "__pageBindRemove 真的移除标记",
                before is not None and after == before - 1,
                f"{before} → {after}",
            )

            # ── ⑥ 振假名把 <rt> 插进正文流，字符层不得被污染 ────────
            baseline = world.ev(
                "window.__bwWebTextLayer.snapshot().text.includes('日本語')"
            )
            check("基线：字符层含「日本語」", bool(baseline))
            page.evaluate(
                """() => {
                  const p = document.getElementById('jp');
                  p.innerHTML =
                    '<ruby data-bw-web-ruby="1">日本<rt>にほん</rt></ruby>' +
                    '<ruby data-bw-web-ruby="1">語<rt>ご</rt></ruby>';
                }"""
            )
            page.wait_for_timeout(250)
            after_ruby = world.ev(
                """(() => {
                  const t = window.__bwWebTextLayer.snapshot().text;
                  return { hasWord: t.includes('日本語'), hasReading: t.includes('にほん') };
                })()"""
            )
            check(
                "振假名开启后「日本語」仍是连续的（读音没混进来）",
                bool(after_ruby) and after_ruby.get("hasWord") is True,
                str(after_ruby),
            )
            check(
                "读音 <rt> 不进字符层",
                bool(after_ruby) and after_ruby.get("hasReading") is False,
                str(after_ruby),
            )

            # ── ⑦ 译页把译文插进块内部，字符层不得被污染 ─────────────
            page.evaluate(
                """() => {
                  const p = document.getElementById('tail');
                  const box = document.createElement('span');
                  box.setAttribute('data-rc-tr', '1');
                  box.textContent = '第二定律说的是熵增。';
                  p.appendChild(box);
                }"""
            )
            page.wait_for_timeout(250)
            after_tr = world.ev(
                """(() => {
                  const t = window.__bwWebTextLayer.snapshot().text;
                  return { hasSrc: t.includes('entropy increases'),
                           hasTr: t.includes('第二定律') };
                })()"""
            )
            check(
                "译文不进字符层",
                bool(after_tr) and after_tr.get("hasTr") is False,
                str(after_tr),
            )
            check(
                "原文仍在字符层",
                bool(after_tr) and after_tr.get("hasSrc") is True,
                str(after_tr),
            )

            # ── ⑧ replace 样式下原文被收起（.rc-tr-src）仍必须留在层里 ──
            page.evaluate(
                """() => {
                  const p = document.getElementById('para');
                  const wrap = document.createElement('span');
                  wrap.className = 'rc-tr-src rc-tr-src-hidden';
                  while (p.firstChild) wrap.appendChild(p.firstChild);
                  p.appendChild(wrap);
                }"""
            )
            page.wait_for_timeout(250)
            check(
                "被收起的原文（.rc-tr-src）必须留在字符层 —— 排除它会让所有锚朝另一边全错",
                bool(world.ev(
                    "window.__bwWebTextLayer.snapshot().text.includes('energy is conserved')"
                )),
            )

            # ── ⑨ DOM 变了之后旧锚仍按文本找得回来 ───────────────────
            check(
                "DOM 变化后旧锚退回按文本重找仍能命中",
                bool(world.ev(
                    """(() => {
                      const hit = window.__bwWebTextLayer.locate({
                        kind:'page-chars', page:1, from: 9999, to: 10010,
                        text:'energy is conserved', rev:'stale'
                      });
                      return !!(hit && hit.how === 'refound');
                    })()"""
                )),
            )
        finally:
            context.close()

    print()
    for f in FAILURES:
        print("  X", f)
    print(f"通过 {len(PASSED)} / 失败 {len(FAILURES)}")
    if not FAILURES:
        print(
            "PASS: 网页字符层与卡片锚定在真实 Chromium 上验证通过"
            "（真三击/真 Ctrl+A/纯 div 站点/影子树样式/振假名/译页）"
        )
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
