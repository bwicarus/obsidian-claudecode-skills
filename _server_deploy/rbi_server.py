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
  client→server: {cmd:"auth", ticket}     首帧必须是服务端短期签名票
                 {cmd:"open", url}        打开/切换 URL(uid 已由票据绑定)
  server→client: {t:"ev", d:<rrweb事件字符串>}   逐条 DOM 事件(iPad Replayer.addEvent)
                 {t:"nav", url}                  真实最终 URL(重定向后)
                 {t:"err", msg}                  错误
"""
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import websockets
from playwright.async_api import async_playwright
from rbi_access import (
    RbiIdentity,
    RbiProfileLease,
    RbiTicketClaims,
    RbiTicketNonceRegistry,
    RBI_LEGACY_PROFILE_DIRNAME,
    RBI_PROFILE_DIRNAME,
    acquire_rbi_profile_lease,
    browser_request_url_error,
    load_rbi_ticket_secret,
    prepare_rbi_profile,
    public_network_url_error,
    rbi_profile_path,
    verify_rbi_ticket,
)
from web_cookie_store import cookie_store_path, playwright_cookies_for_user

CLAUDE = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
PROFILES = CLAUDE / "state" / RBI_PROFILE_DIRNAME
LEGACY_PROFILES = CLAUDE / "state" / RBI_LEGACY_PROFILE_DIRNAME
WEBCOOKIES = CLAUDE / "state" / "web-cookies"
RRWEB = (CLAUDE / "_server_deploy" / "static" / "pdf" / "rrweb-record.umd.js").read_text("utf-8")

HOST, PORT = "127.0.0.1", 8769
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"

# ── per-user 常驻浏览器(persistent context 存登录态)。单用户单 context;懒起 ──
_PW = None
_CTX: dict[int, object] = {}          # verified numeric uid → BrowserContext
_CTX_LEASES: dict[int, RbiProfileLease] = {}
_CTX_LOCK = asyncio.Lock()
_MAX_CTX = 2             # 8GB Pi 安全并发(实测 ~330MB/tab + 258MB 基线)
_RBI_SECRET = None
_TICKET_NONCES = RbiTicketNonceRegistry()


def _ticket_secret() -> bytes:
    global _RBI_SECRET
    if _RBI_SECRET is None:
        _RBI_SECRET = load_rbi_ticket_secret(CLAUDE)
    return _RBI_SECRET


def _identity_uid(identity: RbiIdentity) -> int:
    if not isinstance(identity, RbiIdentity):
        raise TypeError("verified RBI identity required")
    return identity.user_id


def _profile_path(identity: RbiIdentity) -> Path:
    """Build a profile path only from a verified numeric identity."""

    _identity_uid(identity)
    return rbi_profile_path(PROFILES, identity)


def _prepare_profile_path(identity: RbiIdentity) -> Path:
    """Prepare the shared profile, reading only the same uid's legacy tree."""

    _identity_uid(identity)
    return prepare_rbi_profile(PROFILES, LEGACY_PROFILES, identity)


def _cookie_path(identity: RbiIdentity) -> Path:
    """Build an imported-cookie path only from a verified numeric identity."""

    uid = _identity_uid(identity)
    return cookie_store_path(WEBCOOKIES, uid)


async def _pw():
    global _PW
    if _PW is None:
        _PW = await async_playwright().start()
    return _PW


def _cookies_for(identity: RbiIdentity, url: str) -> list:
    """Read the shared exact-host, HTTPS-only cookie store for this account."""

    uid = _identity_uid(identity)
    return playwright_cookies_for_user(WEBCOOKIES, uid, url)


async def _ctx_alive(ctx) -> bool:
    """context 是否还活着(浏览器可能崩溃/被 kill/OOM,缓存的对象会变死)。"""
    try:
        return len(ctx.pages) >= 0 and ctx.browser is not None and ctx.browser.is_connected()
    except Exception:
        return False


async def _drop_context_locked(uid: int) -> None:
    """Close one cached context and release its cross-process profile lease."""

    ctx = _CTX.pop(uid, None)
    lease = _CTX_LEASES.pop(uid, None)
    try:
        if ctx is not None:
            await ctx.close()
    except Exception:
        pass
    finally:
        if lease is not None:
            lease.release()


async def _guard_browser_route(route, request):
    """Apply the shared public-network guard to every Chromium request."""

    error = await asyncio.to_thread(browser_request_url_error, request.url)
    if error:
        await route.abort("blockedbyclient")
    else:
        await route.continue_()


async def _guard_browser_websocket(route):
    """Playwright routes WebSockets separately from HTTP requests."""

    error = await asyncio.to_thread(browser_request_url_error, route.url)
    if error:
        await route.close(code=1008, reason="private network target blocked")
    else:
        route.connect_to_server()


async def _context(identity: RbiIdentity):
    uid = _identity_uid(identity)
    async with _CTX_LOCK:
        ctx = _CTX.get(uid)
        if ctx is not None:
            lease = _CTX_LEASES.get(uid)
            if lease is not None and lease.active and await _ctx_alive(ctx):
                return ctx
            # 死 context:清掉重建(否则 new_page 恒报 "context has been closed")
            await _drop_context_locked(uid)
        if len(_CTX) >= _MAX_CTX:
            # 简单驱逐:关最早的一个(阶段1;阶段3 换 LRU + 闲置回收)
            old_uid = next(iter(_CTX))
            await _drop_context_locked(old_uid)
        prof = _prepare_profile_path(identity)
        pw = await _pw()
        lease = acquire_rbi_profile_lease(
            PROFILES,
            identity,
            blocking=False,
        )
        if lease is None:
            raise RuntimeError("RBI profile is already in use")
        new_ctx = None
        try:
            new_ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(prof), headless=True,
                user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="zh-CN",
                service_workers="block", offline=True,
                bypass_csp=True,   # ★ 很多站(ddg-lite/GitHub/搜索引擎)有 CSP 挡住 rrweb 脚本注入 → 零事件白屏
                args=["--disable-blink-features=AutomationControlled"])
            # Persistent profiles can contain crash/session restore state.  Keep
            # Chromium offline until every network channel has its guard, then
            # discard startup tabs so only this authenticated WS creates pages.
            await new_ctx.route("**/*", _guard_browser_route)
            await new_ctx.route_web_socket("**/*", _guard_browser_websocket)
            await new_ctx.add_init_script(_STEALTH)
            for startup_page in list(new_ctx.pages):
                await startup_page.close()
            await new_ctx.set_offline(False)
        except Exception:
            try:
                if new_ctx is not None:
                    await new_ctx.close()
            except Exception:
                pass
            lease.release()
            raise
        _CTX[uid] = new_ctx
        _CTX_LEASES[uid] = lease
        return new_ctx


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


# ── 通用事件透传(信号三角):客户端捕获所有原生交互 → {cmd:ev,t,id,…} → 这里统一分发 ──
# workflow 实测结论:合成事件 isTrusted=false,被站点 isTrusted 门拒绝 + 不激活 CSS :hover;
# 必须走 **CDP Input**(真实输入,isTrusted=true)。坐标一律 **Pi 侧** 从 node id 算 getBoundingClientRect
# (视口 CSS 坐标,已含滚动),客户端永不发坐标 → 彻底消灭布局错位。node id 会因框架重渲染失效,每次现取判空。
async def _node_rect(page, nid: int):
    """Pi 侧算节点视口中心坐标;离屏先 scrollIntoView;失效返回 None。"""
    try:
        return await page.evaluate("""(id)=>{ const m=window.__rbiMirror; if(!m) return null;
          const n=m.getNode(id); if(!n||!n.getBoundingClientRect) return null;
          let r=n.getBoundingClientRect();
          if(r.width===0 && r.height===0) return null;
          if(r.bottom<0 || r.top>innerHeight){ try{ n.scrollIntoView({block:'center'}); }catch(e){} r=n.getBoundingClientRect(); }
          return {x:r.left+r.width/2, y:r.top+r.height/2}; }""", nid)
    except Exception:
        return None


_KEYMAP = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "Delete": 46,
           "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
           "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34}


async def _rbi_dispatch(page, cdp, t: str, nid: int, msg: dict):
    """一个分发器取代原来 clicknode/setinput/submitform/scroll/click/type 六个分支。
    三后端:CDP-trusted(点击/hover/功能键)/ value-set(文本/change)/ node 方法(scroll/submit/focus)。"""
    try:
        if t in ("click", "pointerup"):
            rect = await _node_rect(page, nid) if nid else None
            if rect:
                x, y = rect["x"], rect["y"]
                await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                await cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                                            "button": "left", "clickCount": 1})
                await cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                                            "button": "left", "clickCount": 1})
        elif t == "hover":
            rect = await _node_rect(page, nid) if nid else None
            if rect:
                await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": rect["x"], "y": rect["y"]})
        elif t == "key":
            key = str(msg.get("key") or "")
            if nid:
                await page.evaluate("(id)=>{const n=window.__rbiMirror.getNode(id); if(n&&n.focus){try{n.focus()}catch(e){}}}", nid)
            vk = _KEYMAP.get(key, 0)
            base = {"key": key, "code": str(msg.get("code") or key),
                    "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
            await cdp.send("Input.dispatchKeyEvent", {**base, "type": "keyDown"})
            await cdp.send("Input.dispatchKeyEvent", {**base, "type": "keyUp"})
        elif t == "input":
            # 文本走值同步(不逐键,免 IME 逐字坑)
            await page.evaluate("""(a)=>{ const n=window.__rbiMirror.getNode(a.id); if(!n) return;
              try{ n.focus(); }catch(e){} n.value=a.value;
              n.dispatchEvent(new Event('input',{bubbles:true})); }""", {"id": nid, "value": str(msg.get("value") or "")})
        elif t == "change":
            await page.evaluate("""(a)=>{ const n=window.__rbiMirror.getNode(a.id); if(!n) return;
              if(a.checked!==undefined) n.checked=a.checked;
              if(a.value!==undefined) n.value=a.value;
              n.dispatchEvent(new Event('change',{bubbles:true})); }""",
                                {"id": nid, "checked": msg.get("checked"), "value": msg.get("value")})
        elif t == "submit":
            # Pi 侧真提交:真 Chrome 带隐藏/CSRF/JS 填充字段导航(修客户端构造 URL 漏字段的 bug)
            await page.evaluate("""(id)=>{ const n=window.__rbiMirror.getNode(id); if(!n) return;
              const f = n.tagName==='FORM' ? n : n.form; if(!f) return;
              if(f.requestSubmit) f.requestSubmit(); else f.submit(); }""", nid)
        elif t == "scroll":
            await page.evaluate("""(a)=>{ if(a.id){ const n=window.__rbiMirror.getNode(a.id);
              if(n && n.scrollTo) n.scrollTo(a.left, a.top); } else window.scrollTo(a.left, a.top); }""",
                                {"id": nid, "top": float(msg.get("top") or 0), "left": float(msg.get("left") or 0)})
        elif t == "focus":
            if nid:
                await page.evaluate("(id)=>{const n=window.__rbiMirror.getNode(id); if(n&&n.focus){try{n.focus()}catch(e){}}}", nid)
    except Exception:
        pass


async def _reject_auth(ws, message: str):
    try:
        await ws.send(json.dumps({
            "t": "err",
            "code": "AUTH",
            "msg": message,
        }))
    except Exception:
        pass
    try:
        await ws.close(code=4401, reason="RBI authorization failed")
    except Exception:
        pass


def _ws_header(ws, name: str) -> str:
    """Read handshake headers across websockets legacy/new server APIs."""

    try:
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None)
        if headers is None:
            headers = getattr(ws, "request_headers", None)
        if headers is None:
            return ""
        value = headers.get(name)
        return str(value or "").strip()
    except Exception:
        return ""


def _normalized_origin(value: str) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
    except Exception:
        return ""
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return ""
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return ""
    default_port = 443 if scheme == "https" else 80
    if ":" in host:
        host = "[" + host + "]"
    return f"{scheme}://{host}" + (
        f":{port}" if port is not None and port != default_port else ""
    )


def _valid_ws_origin(ws) -> bool:
    """Only the same HTTPS reader origin may spend an RBI ticket."""

    origin = _normalized_origin(_ws_header(ws, "Origin"))
    configured = [
        _normalized_origin(item)
        for item in os.environ.get("READER_RBI_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    ]
    if configured:
        return bool(origin and origin in configured and all(configured))
    if not origin or not origin.startswith("https://"):
        return False
    forwarded_host = _ws_header(ws, "X-Forwarded-Host")
    host = (forwarded_host or _ws_header(ws, "Host")).strip().lower().rstrip(".")
    if not host or "," in host or "/" in host or "@" in host:
        return False
    return origin == _normalized_origin("https://" + host)


async def _authenticate_ws(ws) -> RbiTicketClaims | None:
    """Authenticate once and retain all immutable ticket claims."""

    if not _valid_ws_origin(ws):
        await _reject_auth(ws, "RBI WebSocket 来源不受信任")
        return None
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
    except Exception:
        await _reject_auth(ws, "RBI 登录票无效或已过期")
        return None
    if not isinstance(msg, dict) or msg.get("cmd") != "auth":
        await _reject_auth(ws, "RBI 首条消息必须完成认证")
        return None
    claims = verify_rbi_ticket(_ticket_secret(), str(msg.get("ticket") or ""))
    if claims is None:
        await _reject_auth(ws, "RBI 登录票无效或已过期")
        return None
    if not _TICKET_NONCES.consume(claims):
        await _reject_auth(ws, "RBI 登录票已使用或已过期")
        return None
    try:
        await ws.send(json.dumps({
            "t": "ready",
            "expiresAt": claims.expires_at,
        }))
    except Exception:
        return None
    return claims


async def _close_ws_at_expiry(ws, expires_at: int) -> None:
    """Close an authenticated WebSocket when its exclusive expiry is reached."""

    delay = max(0.0, float(expires_at) - time.time())
    await asyncio.sleep(delay)
    try:
        await ws.close(code=4408, reason="RBI authorization expired")
    except Exception:
        pass


async def _serve(ws):
    """一个 WS 连接 = 一个会话 = 一个 page。断开则关 page。"""
    page = None
    claims = await _authenticate_ws(ws)
    if claims is None:
        return
    identity = claims.identity
    bound_uid = _identity_uid(identity)
    expiry_task = asyncio.create_task(
        _close_ws_at_expiry(ws, claims.expires_at)
    )
    try:
        async for raw in ws:
            if claims.expires_at <= int(time.time()):
                await ws.close(
                    code=4408,
                    reason="RBI authorization expired",
                )
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            cmd = msg.get("cmd")
            if cmd == "open":
                url = (msg.get("url") or "").strip()
                url_error = await asyncio.to_thread(public_network_url_error, url)
                if url_error:
                    await ws.send(json.dumps({
                        "t": "err",
                        "code": "URL_BLOCKED",
                        "msg": url_error,
                    }))
                    continue
                async def _emit(source, ev_str):
                    try:
                        await ws.send(json.dumps({"t": "ev", "d": ev_str}))
                    except Exception:
                        pass
                try:
                    # 死 page(context 崩过)→ 丢弃重建。整段 new_page 失败也重来一次。
                    for _attempt in (1, 2):
                        try:
                            ctx = await _context(identity)
                            cks = _cookies_for(identity, url)
                            if cks:
                                try:
                                    await ctx.add_cookies(cks)
                                except Exception:
                                    pass
                            if page is None or page.is_closed():
                                page = await ctx.new_page()
                                await page.expose_binding("__rrwebEmit", _emit)
                                cdp = await ctx.new_cdp_session(page)   # 通用事件透传的 CDP Input 通道
                                page._rbi_cdp = cdp
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
                                await _drop_context_locked(bound_uid)
                            if _attempt == 2:
                                raise
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    # record 由 domcontentloaded 钩子单次处理(见上);这里只报当前 URL
                    await ws.send(json.dumps({"t": "nav", "url": page.url}))
                except Exception as ex:
                    try:
                        await ws.send(json.dumps({"t": "err", "msg": str(ex)[:200]}))
                    except Exception:
                        pass
            elif cmd == "ev" and page is not None:
                # ★ 通用事件透传:一个入口取代逐个命令(信号三角)
                await _rbi_dispatch(page, getattr(page, "_rbi_cdp", None),
                                    str(msg.get("t") or ""), int(msg.get("id") or 0), msg)
            elif cmd == "clicknode" and page is not None:
                # ★ 按 rrweb 节点 id 点击(坐标会因客户端/Pi 布局差错位;id 是 record↔replay 共享的,精确)
                nid = int(msg.get("id") or 0)
                if nid:
                    try:
                        r = await page.evaluate("""(id)=>{ const m=window.__rbiMirror; if(!m) return {e:'nomirror'};
                          const n=m.getNode(id); if(!n) return {e:'nonode'};
                          const tag=n.tagName||'?', href=(n.href||n.getAttribute&&n.getAttribute('href'))||'';
                          if(n.focus){ try{ n.focus(); }catch(e){} }
                          if(n.click) n.click();
                          else if(n.dispatchEvent) n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
                          return {tag, href}; }""", nid)
                        await ws.send(json.dumps({"t": "clickres", "r": r}))
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
                    url_error = await asyncio.to_thread(public_network_url_error, url)
                    if url_error:
                        await ws.send(json.dumps({
                            "t": "err",
                            "code": "URL_BLOCKED",
                            "msg": url_error,
                        }))
                        continue
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
        expiry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await expiry_task
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def main():
    PROFILES.mkdir(parents=True, exist_ok=True)
    sys.stderr.write(f"[rbi] serving ws://{HOST}:{PORT}\n")
    sys.stderr.flush()
    # Client commands/tickets are tiny.  rrweb snapshots travel server→client,
    # so a large inbound frame limit only creates an unauthenticated memory sink.
    async with websockets.serve(_serve, HOST, PORT, max_size=64 * 1024, ping_interval=20):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
