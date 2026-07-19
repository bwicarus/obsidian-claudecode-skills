#!/usr/bin/env python3
"""rbi_render.py — 远程浏览器隔离(RBI)最小验证:用 Pi 上的**真 Chrome** 打开一个页面,
返回 JS 执行后的渲染 DOM。

这是"iframe 代理打不过真浏览器"的根本替代方案的第一块地基(用户 2026-07-19 设计):
  代理 = 服务端假装浏览器 → 缺登录态/指纹/挑战能力,每站打补丁;
  RBI = 服务端**跑真浏览器**执行页面(真实身份、过 Cloudflare、有 profile 登录态),
        把渲染后的 DOM 发给客户端展示,交互回传给真 Chrome 执行。

demo 阶段:subprocess 每次起一个 headless Chromium(慢 ~5s,但无线程安全问题、最简单)。
persistent context 存 profile(state/rbi-profile)→ 为后续"在 Pi 里登录一次即持久"铺路。

用法:  python3 rbi_render.py <url>  → stdout 一行 JSON {html, final, title, ok}
后续优化(阶段 1+):常驻浏览器池、实时 DOM 增量同步(rrweb)、交互回传。
"""
import json
import os
import sys
from pathlib import Path

PROFILE = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "state" / "rbi-profile"

# 真 Safari 的 UA(Pi 走 SoftBank 住宅 IP,配真浏览器指纹,过 Cloudflare 被动+主动检测)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
# 抹掉最基础的 headless 特征(navigator.webdriver);更强的反检测留到正式阶段
_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


def render(url: str) -> dict:
    from playwright.sync_api import sync_playwright
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=True,
            user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="zh-CN",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-features=IsolateOrigins,site-per-process"])
        try:
            ctx.add_init_script(_STEALTH)
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(6000)          # 等 Cloudflare 挑战通过 + SPA hydrate
            # 挑战页会自己刷新到真页;再取一次最终 DOM
            try:
                if "just a moment" in (pg.title() or "").lower():
                    pg.wait_for_timeout(4000)
            except Exception:
                pass
            return {"ok": True, "html": pg.content(), "final": pg.url, "title": pg.title()}
        finally:
            ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "no url"}))
        sys.exit(1)
    try:
        sys.stdout.write(json.dumps(render(sys.argv[1]), ensure_ascii=False))
    except Exception as ex:
        sys.stdout.write(json.dumps({"ok": False, "error": str(ex)[:200]}, ensure_ascii=False))
