# -*- coding: utf-8 -*-
"""展示板卡片渲染：HTML(+页内 CSS) → 方形 PNG，全部在 Windows 上做完。

用户 2026-09-05 定的架构原话：

> ai 把内容生成后在 windows 上生成渲染后效果，通知板直接拉取这里的内容，
> 然后内容更新也是这里的内容更新后那边跟着更新，也就是说源头和渲染都放在服务器侧。

所以设备侧（小组件）是**纯显示器**：它只按 sha 取一张已经渲好的 PNG。
这不是偷懒的折中 —— WidgetKit 只有 SwiftUI，小组件进程里根本放不了 WebView，
HTML 在那边**不可能**渲染。把渲染放服务器同时解决了这个平台限制。

## 三条纪律

- **按内容指纹缓存**：文件名就是 `sha`（卡片 html 的哈希）。内容没变不重渲；
  变了自然是新文件名，消费端拿到的一定是对得上的那张图。
- **渲染环境断网断脚本**：`java_script_enabled=False` + 路由级拦截。
  渲的是 AI 写的字，任何一处把它当可信输入，这条链就有一处能执行别人写的字。
  （入库时已洗过一遍标签，这里是第二道。）
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
# 方卡。2x 是为了 iPad 上不糊；再大只是白白占体积。
CARD_SIZE = 320
CARD_SCALE = 2
RENDER_TIMEOUT_MS = 8000
MAX_CARDS_PER_PASS = 48


def default_root() -> Path:
    return Path(
        os.environ.get("LOCALAPPDATA")
        or (Path.home() / "AppData" / "Local")
    ) / "BWReader"


def store_path(root: Path | None = None) -> Path:
    return (root or default_root()) / STORE_NAME


def cache_dir(root: Path | None = None) -> Path:
    return (root or default_root()) / CACHE_DIRNAME


def card_png_path(sha: str, root: Path | None = None) -> Path:
    return cache_dir(root) / (sha + ".png")


def _document(html: str) -> str:
    """把 AI 写的片段放进一个固定的方形壳子里。

    壳子只做三件事：给一个确定的画布尺寸、给一套默认字体/配色、把内容居中。
    **不改写用户的样式** —— 卡片长什么样是 AI 决定的，这是用户要的那条。
    """
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<style>"
        "html,body{margin:0;padding:0;width:100%;height:100%}"
        "body{box-sizing:border-box;padding:14px;"
        "font-family:'Yu Gothic UI','Hiragino Sans','Microsoft YaHei',"
        "system-ui,sans-serif;"
        "color:#e6edf3;background:#161b22;"
        "display:flex;flex-direction:column;justify-content:center;"
        "overflow:hidden;word-break:break-word}"
        "*{max-width:100%}"
        "</style></head><body>" + html + "</body></html>"
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


def pending_cards(root: Path | None = None) -> list[tuple[str, str]]:
    """返回 [(sha, html)] —— 板子里有、但还没渲出图的卡片。"""
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
            if not card_png_path(sha, root).exists():
                out.append((sha, html))
            if len(out) >= MAX_CARDS_PER_PASS:
                return out
    return out


def live_shas(root: Path | None = None) -> set[str]:
    store = _read_store(root)
    if not store:
        return set()
    out: set[str] = set()
    for board in store.get("boards") or []:
        if not isinstance(board, dict):
            continue
        for card in board.get("cards") or []:
            if isinstance(card, dict) and card.get("sha"):
                out.add(str(card["sha"]))
    return out


def prune(root: Path | None = None, keep: set[str] | None = None) -> int:
    """删掉已经没有卡片引用的 PNG。

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
        if item.stem not in wanted:
            try:
                item.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def render_cards(
    cards: list[tuple[str, str]],
    root: Path | None = None,
) -> tuple[int, list[str]]:
    """渲染若干张卡。返回 (成功数, 失败说明)。"""
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
                for sha, html in cards:
                    try:
                        page.set_content(_document(html))
                        shot = page.screenshot(type="png")
                    except Exception as exc:   # noqa: BLE001
                        errors.append(sha + ": " + type(exc).__name__)
                        continue
                    # 原子落盘：半张 PNG 会被消费端当成"渲好了"。
                    handle, temporary = tempfile.mkstemp(
                        dir=str(directory), suffix=".part")
                    try:
                        with os.fdopen(handle, "wb") as stream:
                            stream.write(shot)
                        os.replace(temporary, card_png_path(sha, root))
                        done += 1
                    except OSError as exc:
                        errors.append(sha + ": 写盘失败 " + str(exc)[:80])
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
