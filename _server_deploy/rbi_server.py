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


async def _ctx_alive(ctx) -> bool:
    """context 是否还活着(浏览器可能崩溃/被 kill/OOM,缓存的对象会变死)。"""
    try:
        return len(ctx.pages) >= 0 and ctx.browser is not None and ctx.browser.is_connected()
    except Exception:
        return False


async def _context(uid: str):
    async with _CTX_LOCK:
        ctx = _CTX.get(uid)
        if ctx is not None:
            if await _ctx_alive(ctx):
                return ctx
            # 死 context:清掉重建(否则 new_page 恒报 "context has been closed")
            try:
                await ctx.close()
            except Exception:
                pass
            _CTX.pop(uid, None)
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
            bypass_csp=True,   # ★ 很多站(ddg-lite/GitHub/搜索引擎)有 CSP 挡住 rrweb 脚本注入 → 零事件白屏
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-features=IsolateOrigins,site-per-process"])
        await ctx.add_init_script(_STEALTH)
        _CTX[uid] = ctx
        return ctx


_RECORD_JS = """() => {
  if (window.__rbiStop) { try { window.__rbiStop(); } catch(e){} }
  window.__rbiStop = rrweb.record({
    emit(ev){ try{ window.__rrwebEmit(JSON.stringify(ev)); }catch(e){} },
    inlineStylesheet: true, recordCanvas: false, collectFonts: false,
    sampling: { mousemove: false, scroll: 150, media: 800, input: 'last' }
  });
  window.__rbiMirror = rrweb.record.mirror;   // node↔id 映射,供交互回传按 id 精确定位(坐标不可靠)
}"""


async def _inject_record(page, ws):
    """注入 rrweb record(首屏 + 每次整页导航后)。幂等:先停旧的再启新的。"""
    try:
        await page.add_script_tag(content=RRWEB)
        await page.evaluate(_RECORD_JS)
        await ws.send(json.dumps({"t": "nav", "url": page.url}))
    except Exception:
        pass


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
                async def _emit(source, ev_str):
                    try:
                        await ws.send(json.dumps({"t": "ev", "d": ev_str}))
                    except Exception:
                        pass
                try:
                    # 死 page(context 崩过)→ 丢弃重建。整段 new_page 失败也重来一次。
                    for _attempt in (1, 2):
                        try:
                            ctx = await _context(uid)
                            cks = _cookies_for(uid, url)
                            if cks:
                                try:
                                    await ctx.add_cookies(cks)
                                except Exception:
                                    pass
                            if page is None or page.is_closed():
                                page = await ctx.new_page()
                                await page.expose_binding("__rrwebEmit", _emit)
                                # ⚠ 每页**单次** record(靠 domcontentloaded 钩子统一):重复 record 会重置
                                #   node id 空间 → 客户端 replay 的 id ≠ Pi record 的 id → 点击按 id 定位全错。
                                async def _rec_on_ready():
                                    try:
                                        await page.wait_for_timeout(1600)   # 等 CF 挑战 + 首屏 hydrate
                                        await _inject_record(page, ws)
                                    except Exception:
                                        pass
                                page.on("domcontentloaded", lambda: asyncio.create_task(_rec_on_ready()))
                            break
                        except Exception:
                            if page is not None:
                                try:
                                    await page.close()
                                except Exception:
                                    pass
                            page = None
                            async with _CTX_LOCK:      # 强制下一轮重建 context
                                _CTX.pop(uid, None)
                            if _attempt == 2:
                                raise
                            # 整页导航(点链接跳新 URL)后要**自动重录**新页面 —— 挂一次 load 钩子
                            if not getattr(page, "_rbi_load_hooked", False):
                                page._rbi_load_hooked = True
                                def _on_load():
                                    asyncio.create_task(_inject_record(page, ws))
                                page.on("load", lambda: _on_load())
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    # record 由 domcontentloaded 钩子单次处理(见上);这里只报当前 URL
                    await ws.send(json.dumps({"t": "nav", "url": page.url}))
                except Exception as ex:
                    try:
                        await ws.send(json.dumps({"t": "err", "msg": str(ex)[:200]}))
                    except Exception:
                        pass
            elif cmd == "clicknode" and page is not None:
                # ★ 按 rrweb 节点 id 点击(坐标会因客户端/Pi 布局差错位;id 是 record↔replay 共享的,精确)
                nid = int(msg.get("id") or 0)
                if nid:
                    try:
                        await page.evaluate("""(id)=>{ const m=window.__rbiMirror; if(!m) return;
                          const n=m.getNode(id); if(!n) return;
                          if(n.focus) { try{ n.focus(); }catch(e){} }
                          if(n.click) n.click();
                          else if(n.dispatchEvent) n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})); }""", nid)
                    except Exception:
                        pass
            elif cmd == "click" and page is not None:
                # ★ 网页原生交互回传(不变量3):在重建 DOM 上的点击 → Pi 真 Chrome 对应坐标点击。
                #   坐标是 rrweb iframe 内容坐标系(录制 viewport 1280×900),直接对应真 Chrome。
                #   点 <a> → 真 Chrome 导航 → load 钩子自动重录;点按钮/输入框 → 聚焦/展开 → 增量流回。
                try:
                    await page.mouse.click(float(msg.get("x", 0)), float(msg.get("y", 0)))
                except Exception:
                    pass
            elif cmd == "setinput" and page is not None:
                # 逐字把客户端输入同步进 Pi 对应输入框(按 node id 定位,派发 input 事件让页面 JS 感知)
                try:
                    nid = int(msg.get("id") or 0); txt = str(msg.get("text") or "")
                    if nid:
                        await page.evaluate("""(a)=>{ const m=window.__rbiMirror; if(!m)return;
                          const n=m.getNode(a.id); if(!n)return; try{n.focus()}catch(e){}
                          n.value=a.text; n.dispatchEvent(new Event('input',{bubbles:true}));
                          n.dispatchEvent(new Event('change',{bubbles:true})); }""", {"id": nid, "text": txt})
                except Exception:
                    pass
            elif cmd == "submitform" and page is not None:
                # 设值 + 提交表单(POST/SPA);GET 表单在客户端已转成 open URL 不走这
                try:
                    nid = int(msg.get("id") or 0); txt = str(msg.get("text") or "")
                    if nid:
                        await page.evaluate("""(a)=>{ const m=window.__rbiMirror; if(!m)return;
                          const n=m.getNode(a.id); if(!n)return; try{n.focus()}catch(e){}
                          if(a.text!==undefined){ n.value=a.text; n.dispatchEvent(new Event('input',{bubbles:true})); }
                          const f=n.form; if(f){ if(f.requestSubmit) f.requestSubmit(); else f.submit(); }
                          else { n.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:13,which:13,bubbles:true}));
                                 n.dispatchEvent(new KeyboardEvent('keyup',{key:'Enter',keyCode:13,which:13,bubbles:true})); } }""", {"id": nid, "text": txt})
                except Exception:
                    pass
            elif cmd == "type" and page is not None:
                # 键盘输入回传:聚焦的输入框已由 click 在真 Chrome 里聚焦,直接打字
                try:
                    txt = str(msg.get("text") or "")
                    if msg.get("key"):
                        await page.keyboard.press(str(msg["key"]))
                    elif txt:
                        await page.keyboard.type(txt)
                except Exception:
                    pass
            elif cmd == "scroll" and page is not None:
                # 客户端滚动位置 → Pi 真 Chrome 同位置 scrollTo → 触发页面无限滚动/懒加载(新内容 record 流回)
                try:
                    await page.evaluate("(y)=>window.scrollTo(0, y)", float(msg.get("y", 0)))
                except Exception:
                    pass
            elif cmd == "nav" and page is not None:
                url = (msg.get("url") or "").strip()
                if url:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        await page.wait_for_timeout(1800)
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
