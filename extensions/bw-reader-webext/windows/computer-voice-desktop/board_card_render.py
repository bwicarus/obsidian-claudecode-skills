# -*- coding: utf-8 -*-
"""展示板卡片渲染：HTML(+页内 CSS) → PNG，全部在 Windows 上做完。

用户 2026-09-05 定的架构原话：

> ai 把内容生成后在 windows 上生成渲染后效果，通知板直接拉取这里的内容，
> 然后内容更新也是这里的内容更新后那边跟着更新，也就是说源头和渲染都放在服务器侧。

所以设备侧（小组件）是**纯显示器**：它只按 sha 取一张已经渲好的 PNG。
这不是偷懒的折中 —— WidgetKit 只有 SwiftUI，小组件进程里根本放不了 WebView，
HTML 在那边**不可能**渲染。把渲染放服务器同时解决了这个平台限制。

## 两种形状 + 放大填满（2026-09-05 第二版，用户实拍「版面利用率和可读性很差」）

- 方卡在 2:1 的特大号组件里放 4 张，无论怎么排都只能占一半；AI 写的 14px 字
  经小组件缩放后只剩八九个点。所以每张卡渲**两种形状**：方 320×320、宽 640×320，
  小组件按当时有几张卡挑一种把面积吃满。
- 渲染前把内容**等比放大到刚好填满**画布（CSS zoom 二分搜索，最多 3 倍，只放大
  不缩小）。AI 给的字号从"绝对大小"变成"相对比例"，写少了不再是一小团字。
  写多了不缩小 —— 超出被裁，指南里明说"写不下就拆两张卡"。
- 文件名 `<sha>.<shape>.png`。第一版的 `<sha>.png` 是旧格式，两种新图都渲好之后才删。

## 三条纪律

- **按内容指纹缓存**：文件名就是 `sha`（卡片 html 的哈希）。内容没变不重渲；
  变了自然是新文件名，消费端拿到的一定是对得上的那张图。
- **渲染环境断网断脚本**：`java_script_enabled=False` + 路由级拦截。
  渲的是 AI 写的字，任何一处把它当可信输入，这条链就有一处能执行别人写的字。
  （入库时已洗过一遍标签，这里是第二道。）测量放大用的脚本是我们自己的，
  经 Playwright `evaluate` 注入，页面自身的脚本仍然禁用 —— 实测两者不冲突。
- **渲染失败不留半张图**：先写临时文件再原子改名。半张 PNG 比没有更糟 ——
  消费端会把它当成"渲好了"。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

CONTRACT = "reader-display-boards/1"
STORE_NAME = "reader-display-boards.json"
CACHE_DIRNAME = "board-cards"
# 形状 → 逻辑像素。2x 是为了 iPad 上不糊；再大只是白白占体积。
# ⚠ 名字与 C# `ReaderDisplayBoard.WriteCardImageAsync` 接受的 shape 参数、
#   iOS `BoardWidgetView.CardShape` 三处一致；加形状要三处同改。
SHAPES: dict[str, tuple[int, int]] = {
    "square": (320, 320),
    "wide": (640, 320),
}
CARD_SIZE = 320          # 方卡边长；宽卡 = 2 × 边长 × 边长
CARD_SCALE = 2
MAX_ZOOM = 3.0           # 放大上限：再大字就糊成海报了，且说明内容太少
RENDER_TIMEOUT_MS = 8000
MAX_CARDS_PER_PASS = 48

# 放大填满：在 [1, MAX_ZOOM] 上二分找最大的 zoom，使内容（含换行后的高度）
# 仍装得进 body 的内容框。只放大不缩小 —— zoom<1 等于替 AI 把字缩小，
# 违背"写不下就拆卡"这条。
_FIT_SCRIPT = """
(maxZoom) => {
  const el = document.getElementById('bw');
  if (!el) return {zoom: 1, reason: 'no #bw'};
  const cs = getComputedStyle(document.body);
  const inner = {
    w: document.body.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
    h: document.body.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom),
  };
  const fits = (z) => {
    el.style.zoom = z;
    const box = el.getBoundingClientRect();
    return box.width <= inner.w + 0.5 && box.height <= inner.h + 0.5
      && el.scrollWidth * z <= inner.w + 0.5 && el.scrollHeight * z <= inner.h + 0.5;
  };
  let lo = 1, hi = maxZoom;
  if (!fits(1)) { el.style.zoom = 1; return {zoom: 1, reason: 'overflow at 1'}; }
  for (let i = 0; i < 9; i++) {
    const mid = (lo + hi) / 2;
    if (fits(mid)) lo = mid; else hi = mid;
  }
  el.style.zoom = lo;
  return {zoom: lo};
}
"""


def default_root() -> Path:
    return Path(
        os.environ.get("LOCALAPPDATA")
        or (Path.home() / "AppData" / "Local")
    ) / "BWReader"


def store_path(root: Path | None = None) -> Path:
    return (root or default_root()) / STORE_NAME


def cache_dir(root: Path | None = None) -> Path:
    return (root or default_root()) / CACHE_DIRNAME


def card_png_path(sha: str, shape: str = "square", root: Path | None = None) -> Path:
    if shape not in SHAPES:
        raise ValueError("unknown card shape: " + shape)
    return cache_dir(root) / (sha + "." + shape + ".png")


def legacy_png_path(sha: str, root: Path | None = None) -> Path:
    """第一版（单一方卡、无形状后缀）的文件名。只用来识别并清理。"""
    return cache_dir(root) / (sha + ".png")


def _document(html: str, shape: str = "square") -> str:
    """把 AI 写的片段放进一个固定的壳子里。

    壳子做四件事：给一个确定的画布尺寸、给一套默认字体/配色、把内容居中、
    把内容等比放大到填满（见 _FIT_SCRIPT）。**不改写用户的样式** ——
    卡片长什么样是 AI 决定的，这是用户要的那条。

    2026-09-05 第二版起壳子把形状告诉画面：`body.shape-square` / `body.shape-wide`
    和 `:root` 上的 `--card-w` / `--card-h`（逻辑像素），让同一段 HTML 能在 1:1 与
    2:1 里各排一套；`@media (min-width: 600px)` 也能区分宽卡。
    AI 想全出血就自己写 `body{padding:0}`，壳子的默认样式不阻止它。
    """
    width, height = SHAPES[shape]
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<style>"
        ":root{--card-w:" + str(width) + "px;--card-h:" + str(height) + "px}"
        "html,body{margin:0;padding:0;width:100%;height:100%}"
        "body{box-sizing:border-box;padding:14px;"
        "font-family:'Yu Gothic UI','Hiragino Sans','Microsoft YaHei',"
        "system-ui,sans-serif;"
        "color:#e6edf3;background:#161b22;"
        "display:flex;flex-direction:column;justify-content:center;"
        "overflow:hidden;word-break:break-word}"
        "#bw{zoom:1;width:100%}"
        "*{max-width:100%}"
        "</style></head><body class=\"shape-" + shape + "\"><div id=\"bw\">"
        + html + "</div></body></html>"
    )


def _read_store(root: Path | None = None) -> dict[str, Any] | None:
    path = store_path(root)
    try:
        raw = path.read_text("utf-8")
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        return None
    return value


def _live_cards(root: Path | None = None) -> list[tuple[str, str]]:
    """板子里所有 (sha, html)，按板子顺序去重。读不到存储 → 空表。"""
    store = _read_store(root)
    if not store:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for board in store.get("boards") or []:
        if not isinstance(board, dict):
            continue
        for card in board.get("cards") or []:
            if not isinstance(card, dict):
                continue
            sha = str(card.get("sha") or "")
            html = str(card.get("html") or "")
            if not sha or not html or sha in seen:
                continue
            seen.add(sha)
            out.append((sha, html))
    return out


def pending_cards(root: Path | None = None) -> list[tuple[str, str, list[str]]]:
    """返回 [(sha, html, 缺的形状)] —— 板子里有、但还没把每种形状都渲出来的卡。"""
    out: list[tuple[str, str, list[str]]] = []
    for sha, html in _live_cards(root):
        missing = [shape for shape in SHAPES
                   if not card_png_path(sha, shape, root).exists()]
        if missing:
            out.append((sha, html, missing))
        if len(out) >= MAX_CARDS_PER_PASS:
            break
    return out


def live_shas(root: Path | None = None) -> set[str]:
    return {sha for sha, _ in _live_cards(root)}


def _sha_of_filename(name: str) -> str:
    return name.split(".", 1)[0]


def prune(root: Path | None = None, keep: set[str] | None = None) -> int:
    """删掉已经没有卡片引用的 PNG，以及两种新图都齐了的旧格式 `<sha>.png`。

    ⚠ 只有**读得到板子存储**时才敢删：读不到就当作"不知道"，一张都不动。
    读失败时清空缓存，等于把一次瞬时 IO 错误变成整块板子的图全丢。
    """
    wanted = keep if keep is not None else live_shas(root)
    if not wanted and _read_store(root) is None:
        return 0
    removed = 0
    directory = cache_dir(root)
    if not directory.exists():
        return 0
    for item in directory.glob("*.png"):
        sha = _sha_of_filename(item.name)
        is_legacy = item.name.count(".") == 1
        if sha in wanted and not is_legacy:
            continue
        if sha in wanted and is_legacy:
            # 旧格式：等新格式两种都在了才删，免得渲染失败时连旧图也没了。
            if not all(card_png_path(sha, shape, root).exists() for shape in SHAPES):
                continue
        try:
            item.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def render_cards(
    cards: list[tuple[str, str, list[str]]],
    root: Path | None = None,
) -> tuple[int, list[str]]:
    """渲染若干张卡（每张只渲缺的形状）。返回 (成功的图数, 失败说明)。"""
    if not cards:
        return 0, []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:   # noqa: BLE001
        return 0, ["playwright 不可用：" + type(exc).__name__ + " " + str(exc)[:120]]

    directory = cache_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    done = 0
    errors: list[str] = []
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": CARD_SIZE, "height": CARD_SIZE},
                    device_scale_factor=CARD_SCALE,
                    java_script_enabled=False,
                    offline=True,
                    service_workers="block",
                )
                # 断网双保险：离线之外再拦一层路由，任何外部请求直接 abort。
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(RENDER_TIMEOUT_MS)
                for sha, html, shapes in cards:
                    for shape in shapes:
                        width, height = SHAPES[shape]
                        try:
                            page.set_viewport_size({"width": width, "height": height})
                            page.set_content(_document(html, shape))
                            page.evaluate(_FIT_SCRIPT, MAX_ZOOM)
                            shot = page.screenshot(type="png")
                        except Exception as exc:   # noqa: BLE001
                            errors.append(sha + "." + shape + ": " + type(exc).__name__)
                            continue
                        # 原子落盘：半张 PNG 会被消费端当成"渲好了"。
                        handle, temporary = tempfile.mkstemp(
                            dir=str(directory), suffix=".part")
                        try:
                            with os.fdopen(handle, "wb") as stream:
                                stream.write(shot)
                            os.replace(temporary, card_png_path(sha, shape, root))
                            done += 1
                        except OSError as exc:
                            errors.append(sha + "." + shape + ": 写盘失败 " + str(exc)[:80])
                            try:
                                os.unlink(temporary)
                            except OSError:
                                pass
                context.close()
            finally:
                browser.close()
    except Exception as exc:   # noqa: BLE001
        errors.append("浏览器启动失败：" + type(exc).__name__ + " " + str(exc)[:120])
    return done, errors


def sync_once(root: Path | None = None) -> dict[str, Any]:
    """一轮：渲缺的图 + 清没人引用的图。给 ReaderPC 的定时器调。"""
    started = time.monotonic()
    pending = pending_cards(root)
    rendered, errors = render_cards(pending, root)
    removed = prune(root)
    return {
        "pending": len(pending),
        "rendered": rendered,
        "removed": removed,
        "errors": errors[:4],
        "seconds": round(time.monotonic() - started, 2),
    }


def sha_of(html: str) -> str:
    """跟 C# 侧 `ReaderDisplayBoard.CardSha` 同一个算法（sha256 前 8 字节）。

    ⚠ 两处必须一致：不一致的表现是"永远有待渲染的卡"，而两边各自都自洽。
    """
    digest = hashlib.sha256(html.encode("utf-8")).digest()
    return digest[:8].hex()


if __name__ == "__main__":
    print(json.dumps(sync_once(), ensure_ascii=False))
