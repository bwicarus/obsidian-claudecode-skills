"""mcp_oauth.py — MCP 服务器的迷你 OAuth 2.1 授权层(单用户自托管)。

为什么存在:claude.ai / ChatGPT 的自定义连接器只支持 OAuth(UI 没有静态 Bearer/自定义
header 字段),所以公网(Tailscale Funnel)接官方 app 必须提供标准 OAuth 面:
  - RFC 9728 Protected Resource Metadata(/.well-known/oauth-protected-resource[/mcp])
  - RFC 8414 AS Metadata(/.well-known/oauth-authorization-server[/mcp] + openid-configuration 别名)
  - RFC 7591 动态客户端注册(POST /oauth/register,public client,auth method=none)
  - GET/POST /oauth/authorize(授权页要求**配对密码**)+ POST /oauth/token(PKCE S256 强制)

信任模型:URL 公开无妨——授权页必须输入配对密码(~/.config/mcp-oauth-pass,600)才发 code;
consent-phishing 缓解=授权页显著显示回调域名,密码只应在自己主动添加连接器时输入。
签发的 token 存 ~/.config/mcp-oauth-store.json(600,原子写)。
静态 token(~/.config/mcp-http-token)继续有效(Claude Code / 脚本客户端),两套并行过同一道门。
"""
import hashlib
import hmac
import html
import json
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

PASS_FILE = Path("~/.config/mcp-oauth-pass").expanduser()
STORE_FILE = Path("~/.config/mcp-oauth-store.json").expanduser()
CLIENT_FILE = Path("~/.config/mcp-oauth-client").expanduser()   # 固定 client 凭证(JSON),给官方 app Advanced settings 手填

CODE_TTL = 600
ACCESS_TTL = 7 * 86400
REFRESH_TTL = 180 * 86400
RATE_WINDOW, RATE_MAX = 600, 5   # 授权密码/换码失败限速:10 分钟 5 次

_lock = threading.Lock()
_fails: dict[str, list[float]] = {}


def _log(msg: str):
    print(f"[mcp-oauth] {msg}", file=sys.stderr, flush=True)


def _pairing_pass() -> str:
    try:
        return PASS_FILE.read_text().strip()
    except Exception:
        return ""


def _load() -> dict:
    try:
        return json.loads(STORE_FILE.read_text())
    except Exception:
        return {"clients": {}, "codes": {}, "access": {}, "refresh": {}}


def _save(st: dict):
    now = time.time()
    for k in list(st["codes"]):
        if st["codes"][k].get("exp", 0) < now:
            del st["codes"][k]
    for k in list(st["access"]):
        if st["access"][k].get("exp", 0) < now:
            del st["access"][k]
    for k in list(st["refresh"]):
        if st["refresh"][k].get("exp", 0) < now:
            del st["refresh"][k]
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STORE_FILE)
    try:
        STORE_FILE.chmod(0o600)
    except Exception:
        pass


def token_ok(tok: str) -> bool:
    """OAuth access token 校验(gate 在静态 token 不匹配时调用)。"""
    if not tok or not tok.startswith("mat_"):
        return False
    with _lock:
        rec = _load()["access"].get(tok)
    return bool(rec and rec.get("exp", 0) > time.time())


def _ratelimited(ip: str) -> bool:
    now = time.time()
    lst = [t for t in _fails.get(ip, []) if now - t < RATE_WINDOW]
    _fails[ip] = lst
    return len(lst) >= RATE_MAX


def _fail(ip: str):
    _fails.setdefault(ip, []).append(time.time())


def _client_ip(request) -> str:
    xf = request.headers.get("x-forwarded-for", "")
    if xf:
        return xf.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _ensure_fixed_client() -> tuple[str, str]:
    """固定 client(用户手填 Client ID/Secret 的路径,不走 DCR)。首次创建并写 CLIENT_FILE。"""
    with _lock:
        st = _load()
        for cid, c in st["clients"].items():
            if c.get("fixed"):
                return cid, c.get("secret", "")
        cid = "bwapp_" + secrets.token_urlsafe(8)
        sec = secrets.token_urlsafe(24)
        st["clients"][cid] = {"redirect_uris": ["https://claude.ai/api/mcp/auth_callback",
                                                "https://claude.com/api/mcp/auth_callback"],
                              "name": "手动配置(Claude 官方 app)", "fixed": True, "secret": sec,
                              "ts": int(time.time())}
        _save(st)
    try:
        CLIENT_FILE.write_text(json.dumps({"client_id": cid, "client_secret": sec}))
        CLIENT_FILE.chmod(0o600)
    except Exception:
        pass
    _log(f"fixed client created: {cid}")
    return cid, sec


# ───────────────────────── ASGI 组装 ─────────────────────────

def build_asgi(mcp_app, static_token: str, public_base: str):
    """返回完整 ASGI app:OAuth 端点 + well-known + Bearer 门禁包着的 MCP app。"""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
    from starlette.routing import Mount, Route

    base = public_base.rstrip("/")
    static_hdr = ("Bearer " + static_token).encode()
    prm_url = f"{base}/.well-known/oauth-protected-resource/mcp"

    AS_META = {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": ["mcp"],
    }
    PRM = {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    }

    async def prm(request):
        return JSONResponse(PRM)

    async def as_meta(request):
        return JSONResponse(AS_META)

    async def register(request):
        try:
            body = json.loads(await request.body() or b"{}")
        except Exception:
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        uris = body.get("redirect_uris") or []
        if not (isinstance(uris, list) and uris and
                all(isinstance(u, str) and u.startswith(("https://", "http://127.0.0.1", "http://localhost")) for u in uris)):
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        cid = "mcl_" + secrets.token_urlsafe(16)
        with _lock:
            st = _load()
            st["clients"][cid] = {"redirect_uris": uris, "name": str(body.get("client_name", ""))[:100],
                                  "ts": int(time.time())}
            _save(st)
        _log(f"DCR client={cid} name={body.get('client_name','')!r} uris={uris}")
        return JSONResponse({"client_id": cid, "client_id_issued_at": int(time.time()),
                             "redirect_uris": uris, "token_endpoint_auth_method": "none",
                             "grant_types": ["authorization_code", "refresh_token"],
                             "response_types": ["code"]}, status_code=201)

    def _authz_check(q, st):
        cid = q.get("client_id", "")
        cl = st["clients"].get(cid)
        if not cl:
            return None, "unknown_client"
        ru = q.get("redirect_uri", "")
        if ru not in cl["redirect_uris"]:
            return None, "invalid_redirect_uri"
        if q.get("response_type") != "code":
            return None, "unsupported_response_type"
        chal = q.get("code_challenge", "")
        if chal and q.get("code_challenge_method", "S256") != "S256":
            return None, "code_challenge_method(S256 only)"
        if not chal and not cl.get("secret"):
            return None, "code_challenge_required(S256)"   # public client 必须 PKCE;固定 client(有 secret)可免
        return cl, ""

    async def authorize_get(request):
        q = dict(request.query_params)
        with _lock:
            cl, err = _authz_check(q, _load())
        if err:
            _log(f"authorize GET reject: {err} client={q.get('client_id','')!r} redirect={q.get('redirect_uri','')!r}")
            return HTMLResponse(f"<h3>请求无效</h3><p>{html.escape(err)}</p>", status_code=400)
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in q.items())
        dest = html.escape(q.get("redirect_uri", ""))
        name = html.escape(cl.get("name") or q.get("client_id", ""))
        page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>连接授权 · bwicarus-app</title>
<style>body{{font-family:system-ui;max-width:26rem;margin:8vh auto;padding:0 1.2rem;color:#1a1a1a}}
.card{{border:1px solid #ddd;border-radius:12px;padding:1.4rem}}
input[type=password]{{width:100%;font-size:1.1rem;padding:.6rem;border:1px solid #bbb;border-radius:8px;box-sizing:border-box}}
button{{width:100%;margin-top:.9rem;padding:.7rem;font-size:1rem;border:0;border-radius:8px;background:#1a73e8;color:#fff}}
.deny{{background:#eee;color:#333}} .dest{{word-break:break-all;background:#f6f6f6;padding:.4rem .6rem;border-radius:6px;font-size:.85rem}}
.warn{{font-size:.8rem;color:#8a5a00;margin-top:.8rem}}</style>
<div class="card"><h2>🔗 连接授权</h2>
<p><b>{name}</b> 请求访问你的自学系统(读书/词汇/Anki/健身)。</p>
<p>授权后回调到:</p><div class="dest">{dest}</div>
<form method="post"><input type="password" name="pairing" placeholder="配对密码" autofocus autocomplete="off">{hidden}
<button type="submit">授权</button><button class="deny" name="deny" value="1" formnovalidate>拒绝</button></form>
<p class="warn">⚠ 只有当你正在 Claude / ChatGPT 里主动添加连接器时才输入密码。</p></div>"""
        return HTMLResponse(page)

    async def authorize_post(request):
        form = dict(await request.form())
        ip = _client_ip(request)
        sep = "&" if "?" in form.get("redirect_uri", "") else "?"
        if form.get("deny"):
            return RedirectResponse(
                form.get("redirect_uri", "/") + sep + urlencode(
                    {"error": "access_denied", "state": form.get("state", "")}), status_code=302)
        if _ratelimited(ip):
            return HTMLResponse("<h3>尝试过多,10 分钟后再试</h3>", status_code=429)
        real = _pairing_pass()
        if not (real and hmac.compare_digest(form.get("pairing", "").strip(), real)):
            _fail(ip)
            _log(f"authorize pairing FAIL ip={ip}")
            return HTMLResponse("<h3>密码不对</h3><p><a href='javascript:history.back()'>返回重试</a></p>",
                                status_code=403)
        with _lock:
            st = _load()
            cl, err = _authz_check(form, st)
            if err:
                return HTMLResponse(f"<h3>请求无效</h3><p>{html.escape(err)}</p>", status_code=400)
            code = "mac_" + secrets.token_urlsafe(32)
            st["codes"][code] = {"client_id": form["client_id"], "redirect_uri": form["redirect_uri"],
                                 "challenge": form.get("code_challenge", ""), "scope": form.get("scope", "mcp"),
                                 "exp": time.time() + CODE_TTL}
            _save(st)
        _log(f"authorize OK client={form['client_id']} ip={ip}")
        return RedirectResponse(
            form["redirect_uri"] + sep + urlencode(
                {"code": code, "state": form.get("state", "")}), status_code=302)

    def _issue(st, client_id, scope):
        at = "mat_" + secrets.token_urlsafe(32)
        rt = "mrt_" + secrets.token_urlsafe(32)
        now = time.time()
        st["access"][at] = {"client_id": client_id, "scope": scope, "exp": now + ACCESS_TTL}
        st["refresh"][rt] = {"client_id": client_id, "scope": scope, "exp": now + REFRESH_TTL}
        return {"access_token": at, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": rt, "scope": scope}

    async def token(request):
        form = dict(await request.form())
        ip = _client_ip(request)
        if _ratelimited(ip):
            return JSONResponse({"error": "slow_down"}, status_code=429)
        # client 认证:client_secret_post(form)或 client_secret_basic(Authorization: Basic)
        cid_req, csec = form.get("client_id", "").strip(), form.get("client_secret", "").strip()
        ah = request.headers.get("authorization", "")
        if ah.lower().startswith("basic "):
            try:
                import base64
                from urllib.parse import unquote
                u, _, pw = base64.b64decode(ah[6:]).decode().partition(":")
                cid_req, csec = unquote(u) or cid_req, unquote(pw) or csec
            except Exception:
                pass
        gt = form.get("grant_type", "")

        def _client_auth_ok(st, client_id, real_pkce=False):
            sec = (st["clients"].get(client_id) or {}).get("secret", "")
            if not sec:
                return True   # public client(PKCE 已在外层强制)
            if csec:
                return hmac.compare_digest(csec, sec)
            # 2026-07-13 实测:claude.ai 手填 ID/Secret 模式换码**不回送 secret**,只带 PKCE
            # (has_rec/pkce_ok/redirect 全对仍 REJECT 的根因)——完整 PKCE 通过即放行
            return real_pkce

        with _lock:
            st = _load()
            if gt == "authorization_code":
                rec = st["codes"].pop(form.get("code", ""), None)   # code 一次性
                ver = form.get("code_verifier", "")
                calc = ""
                if ver:
                    import base64
                    calc = base64.urlsafe_b64encode(
                        hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
                real_pkce = bool(rec and rec["challenge"] and calc
                                 and hmac.compare_digest(calc, rec["challenge"]))
                pkce_ok = real_pkce or bool(rec and not rec["challenge"])   # 无 challenge 的 code=免 PKCE 路径(此时 secret 必须在)
                if not (rec and rec["exp"] > time.time()
                        and rec["client_id"] == (cid_req or rec["client_id"])
                        and rec["redirect_uri"] == form.get("redirect_uri", "")
                        and pkce_ok and _client_auth_ok(st, rec["client_id"], real_pkce)):
                    _fail(ip)
                    _save(st)
                    _log(f"token(code) REJECT ip={ip} client={cid_req!r} has_rec={bool(rec)} "
                         f"pkce_ok={pkce_ok if rec else '-'} sec_post={bool(form.get('client_secret'))} "
                         f"csec_len={len(csec)} csec_head={csec[:4]!r} "
                         f"basic={ah[:6]!r} redirect={form.get('redirect_uri','')!r}")
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                out = _issue(st, rec["client_id"], rec["scope"])
                _save(st)
                _log(f"token issued(code) client={rec['client_id']} ip={ip}")
                return JSONResponse(out)
            if gt == "refresh_token":
                rec = st["refresh"].pop(form.get("refresh_token", ""), None)   # 轮换:旧 refresh 作废
                if not (rec and rec["exp"] > time.time()):
                    _fail(ip)
                    _save(st)
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                out = _issue(st, rec["client_id"], rec["scope"])
                _save(st)
                _log(f"token refreshed client={rec['client_id']} ip={ip}")
                return JSONResponse(out)
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    class _Gate:
        """Bearer 门禁:静态 token(Claude Code 等)或 OAuth access token 二者其一。
        401 带 WWW-Authenticate 指向 PRM——官方 app 的 OAuth 发现流程从这里开始。"""
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                auth = b""
                for k, v in (scope.get("headers") or []):
                    if k == b"authorization":
                        auth = v
                        break
                ok = hmac.compare_digest(auth, static_hdr) or (
                    auth[:7] == b"Bearer " and token_ok(auth[7:].decode("ascii", "ignore")))
                if not ok:
                    body = b'{"error":"unauthorized"}'
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(body)).encode()),
                                            (b"www-authenticate",
                                             f'Bearer resource_metadata="{prm_url}"'.encode())]})
                    await send({"type": "http.response.body", "body": body})
                    return
            await self.app(scope, receive, send)

    routes = [
        Route("/.well-known/oauth-protected-resource", prm),
        Route("/.well-known/oauth-protected-resource/mcp", prm),
        Route("/.well-known/oauth-authorization-server", as_meta),
        Route("/.well-known/oauth-authorization-server/mcp", as_meta),
        Route("/.well-known/openid-configuration", as_meta),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize_get, methods=["GET"]),
        Route("/oauth/authorize", authorize_post, methods=["POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        # 根路径别名(2026-07-13 实测):claude.ai 手填 Client ID 模式不读 AS metadata,
        # 按 MCP 授权 spec 默认端点约定直接打 <origin>/authorize|/token|/register
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize_get, methods=["GET"]),
        Route("/authorize", authorize_post, methods=["POST"]),
        Route("/token", token, methods=["POST"]),
        Mount("/", app=_Gate(mcp_app)),
    ]

    import contextlib

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        # 外层 Starlette 会消费 lifespan,不再传给 Mount 的子 app——手动桥接,
        # 否则 FastMCP 的 StreamableHTTP session manager 不启动,/mcp 全 500
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    _ensure_fixed_client()   # 固定 client(Advanced settings 手填路径)开机即备好,凭证在 CLIENT_FILE

    # CORS:claude.ai 网页端添加连接器时,发现(well-known)/DCR 部分请求从浏览器发起,
    # 无 CORS 头会静默失败且服务器看不到任何日志(本次排查的教训)。preflight 由中间件
    # 直接应答,不会撞 Gate 的 401(OPTIONS 不带 Authorization)。
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    mw = [Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                     allow_headers=["*"], expose_headers=["Mcp-Session-Id", "WWW-Authenticate"])]

    return Starlette(routes=routes, middleware=mw, lifespan=_lifespan)
