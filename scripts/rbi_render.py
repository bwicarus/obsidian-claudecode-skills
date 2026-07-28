#!/usr/bin/env python3
"""rbi_render.py — 远程浏览器隔离(RBI)最小验证:用 Pi 上的**真 Chrome** 打开一个页面,
返回 JS 执行后的渲染 DOM。

这是"iframe 代理打不过真浏览器"的根本替代方案的第一块地基(用户 2026-07-19 设计):
  代理 = 服务端假装浏览器 → 缺登录态/指纹/挑战能力,每站打补丁;
  RBI = 服务端**跑真浏览器**执行页面(真实身份、过 Cloudflare、有 profile 登录态),
        把渲染后的 DOM 发给客户端展示,交互回传给真 Chrome 执行。

demo 阶段:subprocess 每次起一个 headless Chromium(慢 ~5s,但无线程安全问题、最简单)。
persistent context 与 live 共用 profile(state/rbi-profiles/<uid>)，并只读兼容旧的
state/rbi-profile/<uid> → 为后续"在 Pi 里登录一次即持久"铺路。

用法: python3 rbi_render.py <url> <uid> <scope-host>
      → stdout 一行 JSON {html, final, title, ok}
后续优化(阶段 1+):常驻浏览器池、实时 DOM 增量同步(rrweb)、交互回传。
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from rbi_access import (  # noqa: E402
    RbiIdentity,
    RBI_LEGACY_PROFILE_DIRNAME,
    RBI_PROFILE_DIRNAME,
    browser_request_url_error,
    open_rbi_demo_profile,
    prepare_rbi_profile,
    public_network_url_error,
    rbi_profile_path,
)
from web_proxy_cap import normalize_web_proxy_scope  # noqa: E402


PROFILE_ROOT = (
    Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
    / "state"
    / RBI_PROFILE_DIRNAME
)
LEGACY_PROFILE_ROOT = (
    Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
    / "state"
    / RBI_LEGACY_PROFILE_DIRNAME
)

# 真 Safari 的 UA(Pi 走 SoftBank 住宅 IP,配真浏览器指纹,过 Cloudflare 被动+主动检测)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
# 抹掉最基础的 headless 特征(navigator.webdriver);更强的反检测留到正式阶段
_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


def profile_path(user_id: int) -> Path:
    identity = RbiIdentity(int(user_id))
    return rbi_profile_path(PROFILE_ROOT, identity)


def prepare_profile_path(user_id: int) -> Path:
    identity = RbiIdentity(int(user_id))
    return prepare_rbi_profile(
        PROFILE_ROOT,
        LEGACY_PROFILE_ROOT,
        identity,
    )


def rbi_request_url_error(
    url: str,
    expected_scope_host: str,
    *,
    top_navigation: bool = False,
) -> str:
    error = browser_request_url_error(url)
    if error or not top_navigation:
        return error
    try:
        request_scope = normalize_web_proxy_scope(urlparse(url).hostname or "")
        expected_scope = normalize_web_proxy_scope(expected_scope_host)
    except ValueError:
        return "RBI 顶层导航地址无效"
    return "" if request_scope == expected_scope else "RBI 顶层导航不允许跨 capability scope"


def render(url: str, user_id: int, expected_scope_host: str) -> dict:
    from playwright.sync_api import sync_playwright

    identity = RbiIdentity(int(user_id))
    scope_host = normalize_web_proxy_scope(expected_scope_host)
    initial_error = public_network_url_error(url)
    if initial_error:
        raise ValueError(initial_error)
    if normalize_web_proxy_scope(urlparse(url).hostname or "") != scope_host:
        raise ValueError("RBI 顶层 URL 与 capability scope 不匹配")
    prepare_profile_path(identity.user_id)
    with open_rbi_demo_profile(PROFILE_ROOT, identity) as profile_use:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_use.path), headless=True,
                user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="zh-CN",
                service_workers="block", offline=True,
                args=["--disable-blink-features=AutomationControlled"])
            try:
                def guard(route, request):
                    try:
                        is_top_navigation = (
                            request.is_navigation_request()
                            and request.frame.parent_frame is None
                        )
                    except Exception:
                        is_top_navigation = False
                    error = rbi_request_url_error(
                        request.url,
                        scope_host,
                        top_navigation=is_top_navigation,
                    )
                    if error:
                        route.abort("blockedbyclient")
                    else:
                        route.continue_()

                ctx.route("**/*", guard)

                def guard_websocket(route):
                    error = rbi_request_url_error(
                        route.url,
                        scope_host,
                        top_navigation=False,
                    )
                    if error:
                        route.close(
                            code=1008,
                            reason="private network target blocked",
                        )
                    else:
                        route.connect_to_server()

                ctx.route_web_socket("**/*", guard_websocket)
                ctx.add_init_script(_STEALTH)
                for startup_page in list(ctx.pages):
                    startup_page.close()
                ctx.set_offline(False)
                pg = ctx.new_page()
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(6000)          # 等 Cloudflare 挑战通过 + SPA hydrate
                # 挑战页会自己刷新到真页;再取一次最终 DOM
                try:
                    if "just a moment" in (pg.title() or "").lower():
                        pg.wait_for_timeout(4000)
                except Exception:
                    pass
                final_error = public_network_url_error(pg.url)
                if final_error:
                    raise ValueError(final_error)
                if normalize_web_proxy_scope(urlparse(pg.url).hostname or "") != scope_host:
                    raise ValueError("RBI 最终顶层 URL 跨 capability scope")
                return {"ok": True, "html": pg.content(), "final": pg.url, "title": pg.title()}
            finally:
                ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(json.dumps({"ok": False, "error": "usage: rbi_render.py <url> <uid> <scope-host>"}))
        sys.exit(1)
    try:
        sys.stdout.write(json.dumps(
            render(sys.argv[1], int(sys.argv[2]), sys.argv[3]),
            ensure_ascii=False,
        ))
    except Exception as ex:
        sys.stdout.write(json.dumps({"ok": False, "error": str(ex)[:200]}, ensure_ascii=False))
