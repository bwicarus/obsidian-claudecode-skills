#!/usr/bin/env python3
"""rbi_server.py — 远程浏览器隔离(RBI)服务:Pi 跑**真 Chrome** 执行网页,rrweb 把 DOM 流式桥接到 iPad。

方案见 references/rbi-remote-browser.md。阶段 1(本文件):rrweb 单向骨架 —— 常驻真 Chrome + 注入
rrweb record + WebSocket 下推 DOM 事件流。iPad 端 Replayer 重建真 DOM,查词/翻译作用其上(不变量 2)。
阶段 2 再加交互回传(CDP Input,只对网页原生交互——不变量 3)。

- 独立 asyncio 进程(不进 gunicorn:playwright async 要自己的事件循环;浏览器要跨请求常驻)。
- WebSocket server 127.0.0.1:8769,nginx 反代 /rbi-ws(参照 voice-rt 的 WS 反代)。
- per-user persistent context(state/rbi-profiles/<uid>)存登录态 → "Pi 里登录一次即持久";
  同时注入用户经 🔑 导入的 cookie(复用 html_reader 的 web-cookies)。
- 事件传输:页面内 rrweb emit → JSON.stringify 成**字符串** → expose_binding 回 Python(实测大快照
  不卡,POC 的"MB 级卡死"只在 evaluate **传入**方向)→ WS 下推给 iPad。

协议(WS,JSON 文本帧):
  client→server: {cmd:"open", url, uid}   打开/切换 URL(阶段1:点链接也走它,重开会话)
  server→client: {t:"ev", d:<rrweb事件字符串>}   逐条 DOM 事件(iPad Replayer.addEvent)
                 {t:"nav", url}                  真实最终 URL(重定向后)
                 {t:"err", msg}                  错误
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import websockets
from playwright.async_api import async_playwright

CLAUDE = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
PROFILES = CLAUDE / "state" / "rbi-profiles"
WEBCOOKIES = CLAUDE / "state" / "web-cookies"
RRWEB = (CLAUDE / "_server_deploy" / "static" / "pdf" / "rrweb-record.umd.js").read_text("utf-8")

HOST, PORT = "127.0.0.1", 8769
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"

# ── per-user 常驻浏览器(persistent context 存登录态)。单用户单 context;懒起 ──
_PW = None
_CTX: dict = {}          # uid → BrowserContext
_CTX_LOCK = asyncio.Lock()
_MAX_CTX = 2             # 8GB Pi 安全并发(实测 ~330MB/tab + 258MB 基线)


async def _pw():
    global _PW
    if _PW is None:
        _PW = await async_playwright().start()
    return _PW


def _cookies_for(uid: str, url: str) -> list:
    """用户经 🔑 导入的 cookie(html_reader web-cookies)→ playwright cookie 格式。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        store = json.loads((WEBCOOKIES / f"{uid}.json").read_text("utf-8")) or {}
    except Exception:
        return []
    out = []
    for dom, cks in store.items():
        d = str(dom).lstrip(".").lower()
        if isinstance(cks, dict) and (host == d or host.endswith("." + d)):
            for k, v in cks.items():
                out.append({"name": k, "value": str(v), "domain": "." + d, "path": "/"})
    return out


async def _context(uid: str):
    async with _CTX_LOCK:
        ctx = _CTX.get(uid)
        if ctx is not None:
            return ctx
        if len(_CTX) >= _MAX_CTX:
            # 简单驱逐:关最早的一个(阶段1;阶段3 换 LRU + 闲置回收)
            old_uid, old_ctx = next(iter(_CTX.items()))
            try:
                await old_ctx.close()
            except Exception:
                pass
            _CTX.pop(old_uid, None)
        prof = PROFILES / uid
        prof.mkdir(parents=True, exist_ok=True)
        pw = await _pw()
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(prof), headless=True,
            user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="zh-CN",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-features=IsolateOrigins,site-per-process"])
        await ctx.add_init_script(_STEALTH)
        _CTX[uid] = ctx
        return ctx


async def _serve(ws):
    """一个 WS 连接 = 一个会话 = 一个 page。断开则关 page。"""
    page = None
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            cmd = msg.get("cmd")
            if cmd == "open":
                url = (msg.get("url") or "").strip()
                uid = str(msg.get("uid") or "anon")
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                try:
                    ctx = await _context(uid)
                    # 注入导入的 cookie(登录态);persistent profile 本身也会累积
                    cks = _cookies_for(uid, url)
                    if cks:
                        try:
                            await ctx.add_cookies(cks)
                        except Exception:
                            pass
                    if page is None:
                        page = await ctx.new_page()

                        async def _emit(source, ev_str):
                            try:
                                await ws.send(json.dumps({"t": "ev", "d": ev_str}))
                            except Exception:
                                pass
                        await page.expose_binding("__rrwebEmit", _emit)
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    # 等一下 Cloudflare 挑战 + 首屏 hydrate,再启动录制(录到稳定后的 DOM)
                    await page.wait_for_timeout(3500)
                    await page.add_script_tag(content=RRWEB)
                    await page.evaluate("""() => {
                      if (window.__rbiStop) { try { window.__rbiStop(); } catch(e){} }
                      window.__rbiStop = rrweb.record({
                        emit(ev){ try{ window.__rrwebEmit(JSON.stringify(ev)); }catch(e){} },
                        inlineStylesheet: true, recordCanvas: false, collectFonts: false,
                        sampling: { mousemove: false, scroll: 150, media: 800,
                                    input: 'last' }
                      });
                    }""")
                    await ws.send(json.dumps({"t": "nav", "url": page.url}))
                except Exception as ex:
                    try:
                        await ws.send(json.dumps({"t": "err", "msg": str(ex)[:200]}))
                    except Exception:
                        pass
            elif cmd == "nav" and page is not None:
                # 阶段1:点链接 → 重开(重新 goto + 重新 record);阶段2 换 CDP Input
                url = (msg.get("url") or "").strip()
                if url:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        await page.wait_for_timeout(2500)
                        await page.add_script_tag(content=RRWEB)
                        await page.evaluate("""() => {
                          if (window.__rbiStop) { try { window.__rbiStop(); } catch(e){} }
                          window.__rbiStop = rrweb.record({ emit(ev){ try{ window.__rrwebEmit(JSON.stringify(ev)); }catch(e){} },
                            inlineStylesheet: true, recordCanvas: false, collectFonts: false,
                            sampling: { mousemove: false, scroll: 150, input: 'last' } });
                        }""")
                        await ws.send(json.dumps({"t": "nav", "url": page.url}))
                    except Exception as ex:
                        await ws.send(json.dumps({"t": "err", "msg": str(ex)[:200]}))
    except websockets.ConnectionClosed:
        pass
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def main():
    PROFILES.mkdir(parents=True, exist_ok=True)
    sys.stderr.write(f"[rbi] serving ws://{HOST}:{PORT}\n")
    sys.stderr.flush()
    async with websockets.serve(_serve, HOST, PORT, max_size=32 * 1024 * 1024, ping_interval=20):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
