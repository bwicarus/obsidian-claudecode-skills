import os
import json
import base64
import hashlib
import hmac
import mimetypes
# .mjs/.js 显式注册为 JS MIME:有 nginx 时它管;**无 nginx 的本地实例(Windows/standalone)** Flask 直接服务
# 静态时,werkzeug 默认可能把 .mjs 猜成 text/plain → 浏览器拒绝当 ES module(PDF.js import 失败,整个阅读器挂)。
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
import shutil
import secrets
import sqlite3
import datetime
import time
import urllib.request
import urllib.error
from pathlib import Path
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, send_from_directory, jsonify, abort, g, Response,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from reader_sw_auth import READER_SW_AUTH_JS, READER_SW_AUTH_PLACEHOLDER
from web_proxy_cap import verify_web_proxy_cap_details

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = datetime.timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # 生产(nginx 终结 HTTPS)默认 Secure;本地裸 http 实例可设 SESSION_COOKIE_SECURE=0 关掉。
    # 注:Chrome/Firefox 对 127.0.0.1/localhost 视作 secure context,即便 True 本地也能登录;
    # 此处随环境降级是为兼容其它浏览器/边界场景,默认仍 Secure 不改生产行为。
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") not in ("0", "false", "False"),
)
# 信任 nginx 转发头：让 url_for / redirect 用真实 https scheme + 客户端 IP
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DATA_DIR  = Path(os.environ.get("WEBAPP_DATA", "/root/webapp/data"))
USERS_DIR = DATA_DIR / "users"
DB_PATH   = DATA_DIR / "app.db"
QA_FILE   = DATA_DIR / "qa.json"
ALLOWED_DATASETS = {"dashboard", "history", "private"}
READER_PROVIDER_ENTRY_PATHS = frozenset({
    "/pdf/view",
    "/pdf/epub/view",
    "/pdf/html/view",
    "/pdf/fav/open",
})


def _legacy_reader_storage_namespace(user_id: int) -> str:
    """只用于首次数据库迁移：保留已经发给测试客户端的 HMAC namespace。"""
    secret = app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    digest = hmac.new(
        bytes(secret),
        f"bw-reader-storage:v1:{int(user_id)}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return "acct-v1-" + digest


def _valid_reader_namespace(value: str) -> bool:
    value = str(value or "")
    return (
        value.startswith("acct-v1-")
        and len(value) == 72
        and all(ch in "0123456789abcdef" for ch in value[8:])
    )


def _reader_storage_namespace(user_id: int) -> str:
    """读取长期持久的随机 Vault 租户编号；不再跟会话 SECRET_KEY 共生命周期。"""
    db = get_db()
    row = db.execute(
        "SELECT storage_namespace FROM users WHERE id = ?",
        (int(user_id),),
    ).fetchone()
    current = str(row["storage_namespace"] or "") if row else ""
    if _valid_reader_namespace(current):
        return current
    candidate = "acct-v1-" + secrets.token_hex(32)
    db.execute(
        "UPDATE users SET storage_namespace = ? "
        "WHERE id = ? AND (storage_namespace IS NULL OR storage_namespace = '')",
        (candidate, int(user_id)),
    )
    db.commit()
    row = db.execute(
        "SELECT storage_namespace FROM users WHERE id = ?",
        (int(user_id),),
    ).fetchone()
    current = str(row["storage_namespace"] or "") if row else ""
    if not _valid_reader_namespace(current):
        raise RuntimeError("failed to allocate reader storage namespace")
    return current


def _reader_provider_ticket_ttl_seconds() -> int:
    """provider ticket 默认只活 15 分钟，并限制部署配置不能意外退化成永久票据。"""
    try:
        configured = int(os.environ.get("READER_PROVIDER_TICKET_TTL_SECONDS", "900"))
    except (TypeError, ValueError):
        configured = 900
    return max(60, min(configured, 3600))


def _reader_provider_ticket_signature(
    namespace: str,
    expires_at: int,
    nonce: str,
) -> str:
    if not _valid_reader_namespace(namespace):
        raise ValueError("invalid reader namespace")
    secret = os.environ.get("READER_PROVIDER_SECRET") or app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(
        bytes(secret),
        (
            f"bw-reader-provider:v2:{namespace}:{int(expires_at)}:{nonce}"
        ).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _reader_provider_ticket(
    namespace: str,
    *,
    expires_at: int | None = None,
    nonce: str | None = None,
) -> str:
    """签发短期 provider 证明；随机 nonce 让每次页面注入得到不同票据。"""
    if not _valid_reader_namespace(namespace):
        raise ValueError("invalid reader namespace")
    expires_at = int(expires_at or (
        int(time.time()) + _reader_provider_ticket_ttl_seconds()
    ))
    nonce = str(nonce or secrets.token_hex(16)).lower()
    if (
        len(nonce) != 32
        or any(ch not in "0123456789abcdef" for ch in nonce)
    ):
        raise ValueError("invalid reader provider ticket nonce")
    signature = _reader_provider_ticket_signature(namespace, expires_at, nonce)
    return f"pvt-v2-{expires_at}-{nonce}-{signature}"


def _verify_reader_provider_ticket(
    namespace: str,
    ticket: str,
    *,
    now: int | None = None,
) -> int | None:
    """验证票据签名与期限，成功时返回 Unix expires_at。"""
    if not _valid_reader_namespace(namespace):
        return None
    parts = str(ticket or "").strip().split("-")
    if (
        len(parts) != 5
        or parts[0:2] != ["pvt", "v2"]
        or not parts[2].isdigit()
        or not 10 <= len(parts[2]) <= 12
        or len(parts[3]) != 32
        or any(ch not in "0123456789abcdef" for ch in parts[3])
        or len(parts[4]) != 64
        or any(ch not in "0123456789abcdef" for ch in parts[4])
    ):
        return None
    expires_at = int(parts[2])
    now = int(time.time() if now is None else now)
    if expires_at <= now or expires_at > now + 3600:
        return None
    expected = _reader_provider_ticket_signature(namespace, expires_at, parts[3])
    if not secrets.compare_digest(parts[4], expected):
        return None
    return expires_at

# ─────────────────────────── DB ───────────────────────────

def _atomic_write_text(path, text):
    """原子写:写临时文件 + os.replace(同目录 rename,POSIX 原子)→ 崩溃/并发不会读到半截文件。
    大厂配置/状态文件标配写法。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _tune_conn(conn):
    # WAL:读写不再互相全表锁(并发读 + 单写并行) → 多请求/多线程下不再 "database is locked"。
    # busy_timeout:拿不到锁时等待而非立即报错。synchronous=NORMAL:WAL 下安全且更快(仅 OS 崩溃丢最后事务)。
    # 大厂 SQLite 标配(同 Django/Rails 的 SQLite 生产建议)。WAL 是 DB 级持久设置,每次连接重设幂等无害。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def get_db():
    if "db" not in g:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        _tune_conn(conn)
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _tune_conn(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            storage_namespace TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS invites (
            token TEXT PRIMARY KEY,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            used_by INTEGER REFERENCES users(id),
            used_at TEXT,
            expires_at TEXT,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            label TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at TEXT
        );
    """)
    # 引导：第一个用户 bwicarus 作为 admin（从环境变量 PASSWORD_HASH 一次性导入）
    cur = conn.execute("SELECT COUNT(*) AS n FROM users")
    if cur.fetchone()["n"] == 0:
        boot_hash = os.environ.get("PASSWORD_HASH", "")
        if boot_hash:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                ("bwicarus", boot_hash),
            )
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "storage_namespace" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN storage_namespace TEXT")
    # 现有测试客户端已经见过基于旧 SECRET_KEY 的值；首次迁移把那个值固化进 DB，
    # 之后即使会话密钥轮换也不会让本地 Vault 看起来“消失”。
    for row in conn.execute(
        "SELECT id FROM users WHERE storage_namespace IS NULL OR storage_namespace = ''"
    ).fetchall():
        conn.execute(
            "UPDATE users SET storage_namespace = ? WHERE id = ?",
            (_legacy_reader_storage_namespace(row["id"]), row["id"]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_storage_namespace "
        "ON users(storage_namespace)"
    )
    conn.commit()
    conn.close()

with app.app_context():
    init_db()

# ─────────────────────────── 通用左侧导航 (auto-inject) ───────────────────────────

NAV_INJECT_PREFIXES = ("/dashboard", "/history", "/private", "/profile", "/admin", "/qa", "/control", "/pdf", "/skilltree", "/insights")

# PWA「恢复上次页面」:① 每页(除首页/登录)把当前 URL 记进 localStorage['pwa:lastUrl'];
# ② PWA 冷启动(本会话首次)落在 start_url=/dashboard/ 时,跳到上次浏览的内容页。
# 放 <head> 内联(同步、早于 body 渲染 → 无闪烁);只在 standalone(装到主屏)下跳,Safari 浏览不受影响;
# sessionStorage 标记保证一次会话只跳一次(会话内再点回首页不会被跳走;关掉 PWA 重开=新会话再跳)。
# 阅读器 URL 记录时去掉 page/mode → 让阅读器自己的"上次位置恢复"(02-position.js)接管,精确到滚动位置。
# start_url 不改(仍 /dashboard/)→ 已装到主屏的 PWA 无需重装即生效。SW 只管页图,不缓存 HTML,脚本恒新鲜。
_PWA_RESUME_JS = """
(function(){try{
var P=location.pathname,Q=location.search,full=P+Q;
var standalone=navigator.standalone||(window.matchMedia&&matchMedia('(display-mode: standalone)').matches);
var isHome=(P==='/dashboard'||P==='/dashboard/');
var skip=isHome||P.indexOf('/login')===0||P.indexOf('/logout')===0||P==='/app';
if(!skip){var rec=full;
  if(P==='/pdf/view'){try{var sp=new URLSearchParams(Q);sp.delete('page');sp.delete('mode');var qs=sp.toString();rec=P+(qs?'?'+qs:'');}catch(e){}}
  try{localStorage.setItem('pwa:lastUrl',rec);}catch(e){}}
if(standalone&&isHome&&!sessionStorage.getItem('pwa:launched')){
  sessionStorage.setItem('pwa:launched','1');
  var last=null;try{last=localStorage.getItem('pwa:lastUrl');}catch(e){}
  if(last&&last.charAt(0)==='/'&&last.indexOf('//')!==0&&last!==full){location.replace(last);}}
}catch(e){}})();
"""

# PWA:让站点能「装到 iPad 主屏 + 全屏运行(无 Safari 地址栏)」,像原生 app。manifest + 苹果专用 meta。
_PWA_HEAD = (
    '<link rel="manifest" href="/manifest.webmanifest">'
    '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="apple-mobile-web-app-title" content="bwicarus">'
    '<meta name="theme-color" content="#10162a">'
    '<script>' + _PWA_RESUME_JS + '</script>'
    # 全站 service worker(scope /):静态/数据 SWR(秒开+本地缓存)、导航 network-first(离线回退)。
    # Pi 仍是唯一写入源(只缓存 GET;POST/鉴权不碰)。/pdf/ 由它自己的 SW 管。
    '<script>if("serviceWorker" in navigator){navigator.serviceWorker.register("/sw.js",{scope:"/"}).catch(function(){});}</script>'
)

@app.route("/manifest.webmanifest")
def pwa_manifest():
    """PWA 清单(可装到主屏、独立窗口运行)。公开(浏览器装应用时取,无需登录)。
    本地实例(localhost)→ 名字/主题色区分,避免跟 Pi 的「bwicarus 学习」PWA 图标撞名分不清。"""
    _h = (request.host or "").split(":")[0].lower()
    _local = _h in ("localhost", "127.0.0.1", "::1")
    return Response(json.dumps({
        "name": "bwicarus 本地" if _local else "bwicarus 学习",
        "short_name": "本地" if _local else "bwicarus",
        "start_url": "/dashboard/", "scope": "/",
        "display": "standalone", "orientation": "any",
        "background_color": "#10162a", "lang": "zh-CN",
        "theme_color": "#16351f" if _local else "#10162a",   # 本地=深绿主题条,一眼区分
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, ensure_ascii=False), mimetype="application/manifest+json")


# 全站 service worker —— 让 PC/iPad 重复打开秒回 + 数据本地缓存(Pi 仍是唯一写入源)。
# 缓存策略:静态/数据 GET → stale-while-revalidate(先返本地缓存秒开,后台向 Pi 刷新→下次新);
#           导航/HTML → network-first(在线拿最新壳,离线/Pi没醒回退上次缓存)。
# 安全:只缓存同源 GET 的 200(basic);POST/鉴权(login/logout/auth/admin)绝不缓存;/pdf/ 由 /pdf/sw.js 管。
# 更新:改 VERSION → activate 时清旧缓存;/sw.js 以 no-cache 提供,浏览器每次校验→改了即生效。
_SITE_SW_TEMPLATE = """
const VERSION='%s';
const STATIC_C='bw-static-'+VERSION;
const DATA_PREFIX='bw-data-'+VERSION+'-';
const NAV_PREFIX='bw-nav-'+VERSION+'-';
__BW_READER_SW_AUTH__
function privateCacheName(prefix, epoch){
  return _validAuthEpoch(epoch) ? prefix+epoch : '';
}
async function stableAuthState(){
  try{
    const state=await _readAuthState();
    return state&&!state.pending&&_validAuthEpoch(state.epoch) ? state : null;
  }catch(err){
    return null;
  }
}
async function sameAuthEpoch(epoch){
  const state=await stableAuthState();
  return !!(state&&state.epoch===epoch);
}
async function clearReaderPrivateCaches(){
  const keys=await caches.keys();
  await Promise.all(keys.map(function(k){
    if(k.indexOf('bw-data-')===0||
       k.indexOf('bw-nav-')===0||
       k.indexOf('pdf-private-')===0||
       k==='pdf-private-identity-v1'||
       k==='pdf-cache-v3'||
       k.indexOf('pdf-pages-')===0){
      return caches.delete(k);
    }
  }));
}
async function authTransitionFetch(req){
  const token=await _beginAuthTransition();
  try{
    await clearReaderPrivateCaches();
    return await fetch(req,{cache:'no-store'});
  }finally{
    // fetch 期间可能还有旧 epoch 的异步 put 落地；认证响应后必须再清一次。
    try{
      await clearReaderPrivateCaches();
    }finally{
      await _finishAuthTransition(token);
    }
  }
}
async function manualClear(includeStatic){
  const token=await _beginAuthTransition();
  try{
    await clearReaderPrivateCaches();
    if(includeStatic){
      const keys=await caches.keys();
      await Promise.all(keys.map(function(k){
        if(k.indexOf('bw-static-')===0) return caches.delete(k);
      }));
    }
  }finally{
    // 删除本身也可能和旧写入交错；第二遍让手动清理具有确定的完成边界。
    try{
      await clearReaderPrivateCaches();
    }finally{
      await _finishAuthTransition(token);
    }
  }
}
self.addEventListener('install', function(){ self.skipWaiting(); });
self.addEventListener('activate', function(e){
  e.waitUntil((async function(){
    const auth=await stableAuthState();
    const expectedData=auth ? privateCacheName(DATA_PREFIX,auth.epoch) : '';
    const expectedNav=auth ? privateCacheName(NAV_PREFIX,auth.epoch) : '';
    const keys=await caches.keys();
    await Promise.all(keys.map(function(k){
      if((k.indexOf('bw-static-')===0&&k!==STATIC_C)||
         (k.indexOf('bw-data-')===0&&k!==expectedData)||
         (k.indexOf('bw-nav-')===0&&k!==expectedNav)){
        return caches.delete(k);
      }
    }));
    await self.clients.claim();
  })());
});
function noCache(p){ return /^\\/(login|logout|register|auth|admin)\\b/.test(p); }
function isStatic(u){ return u.pathname.indexOf('/static/')===0 || /\\.(?:css|js|mjs|woff2?|ttf|otf|eot|png|jpe?g|gif|svg|webp|ico)$/i.test(u.pathname); }
function isData(u){ return u.pathname.indexOf('/api/')===0 || /\\.json$/i.test(u.pathname) || u.pathname.indexOf('/dashboard/')===0 || u.pathname.indexOf('/history/')===0 || u.pathname.indexOf('/insights')===0; }
self.addEventListener('fetch', function(e){
  const req=e.request;
  var u; try{ u=new URL(req.url); }catch(err){ return; }
  if(u.origin!==location.origin) return;
  if(_isAuthTransitionRequest(u,req)){
    // 注册、登录、退出和受控 Bearer 会话都共用同一个跨 scope 认证围栏。
    e.respondWith(authTransitionFetch(req));
    return;
  }
  if(req.method!=='GET') return;
  if(req.headers.has('range')) return;            // 媒体拖动(206 partial)放行,别缓存半段坏播放
  if(u.pathname.indexOf('/pdf/')===0) return;     // /pdf/ 有自己的 SW(页图)
  if(/\\.(?:mp3|m4a|wav|ogg|opus|aac|flac|mp4|webm|mov|m3u8)$/i.test(u.pathname)) return;  // 音视频不缓存(防整段大对象/206)
  if(noCache(u.pathname)) return;                 // 鉴权相关绝不缓存
  if(req.mode==='navigate'){ e.respondWith(privateNetworkFirst(req,NAV_PREFIX)); return; }
  if(isStatic(u)){ e.respondWith(swr(req,STATIC_C)); return; }
  if(isData(u)){ e.respondWith(privateSWR(e,DATA_PREFIX)); return; }
});
async function swr(req,cn){
  const c=await caches.open(cn);
  const hit=await c.match(req);
  const net=fetch(req).then(function(res){ if(res&&res.status===200&&res.type==='basic') c.put(req,res.clone()); return res; }).catch(function(){ return null; });
  if(hit){ net.catch(function(){}); return hit; }
  return (await net) || Response.error();
}
async function privateSWR(event,prefix){
  const req=event.request;
  const auth=await stableAuthState();
  if(!auth) return Response.error();
  const cn=privateCacheName(prefix,auth.epoch);
  const c=await caches.open(cn);
  let hit=await c.match(req);
  if(hit&&!await sameAuthEpoch(auth.epoch)) hit=null;
  const net=fetch(req,{cache:'no-store'}).then(async function(res){
    if(!await sameAuthEpoch(auth.epoch)) return Response.error();
    if(res&&res.status===200&&res.type==='basic'){
      await c.put(req,res.clone());
      if(!await sameAuthEpoch(auth.epoch)){
        await caches.delete(cn);
        return Response.error();
      }
    }
    return res;
  }).catch(function(){ return null; });
  if(hit){
    if(!await sameAuthEpoch(auth.epoch)) return (await net)||Response.error();
    if(event.waitUntil) event.waitUntil(net.then(function(){}));
    return hit;
  }
  const response=await net;
  if(response) return response;
  if(!await sameAuthEpoch(auth.epoch)) return Response.error();
  const fallback=await c.match(req);
  return fallback&&await sameAuthEpoch(auth.epoch) ? fallback : Response.error();
}
async function privateNetworkFirst(req,prefix){
  const auth=await stableAuthState();
  if(!auth) return Response.error();
  const cn=privateCacheName(prefix,auth.epoch);
  const c=await caches.open(cn);
  try{
    const res=await fetch(req,{cache:'no-store'});
    if(!await sameAuthEpoch(auth.epoch)) return Response.error();
    if(res&&res.status===200&&res.type==='basic'){
      await c.put(req,res.clone());
      if(!await sameAuthEpoch(auth.epoch)){
        await caches.delete(cn);
        return Response.error();
      }
    }
    return res;
  }catch(err){
    if(!await sameAuthEpoch(auth.epoch)) return Response.error();
    const hit=await c.match(req);
    return hit&&await sameAuthEpoch(auth.epoch) ? hit : Response.error();
  }
}
self.addEventListener('message', function(e){
  if(e.data==='skipWaiting'){ self.skipWaiting(); }
  if(e.data==='clearCache'){ e.waitUntil(manualClear(true)); }
  if(e.data&&e.data.type==='BW_PDF_CACHE_CLEAR_PRIVATE'){
    e.waitUntil(manualClear(false));
  }
});
self.addEventListener('notificationclick', function(e){  // 点语音任务完成通知 → 聚焦/打开 app
  e.notification.close();
  e.waitUntil((async function(){
    const all=await self.clients.matchAll({type:'window', includeUncontrolled:true});
    for(const c of all){ if('focus' in c) return c.focus(); }
    if(self.clients.openWindow) return self.clients.openWindow('/pdf/');
  })());
});
"""
import hashlib as _sw_hl
_SITE_SW_TEMPLATE = _SITE_SW_TEMPLATE.replace(
    READER_SW_AUTH_PLACEHOLDER,
    READER_SW_AUTH_JS,
)
if READER_SW_AUTH_PLACEHOLDER in _SITE_SW_TEMPLATE:
    raise RuntimeError("reader SW auth placeholder was not replaced")
# VERSION = SW 代码哈希 → 改了缓存逻辑(noCache/isData/swr 等)version 自动变 → activate 清旧缓存,
# 不靠手动 bump(复审 update-staleness 项)。代码没变则 version 稳定 → 缓存持久 → 仍秒开。
_SITE_SW_VERSION = "h" + _sw_hl.md5(_SITE_SW_TEMPLATE.encode("utf-8")).hexdigest()[:10]
_SITE_SW_JS = _SITE_SW_TEMPLATE % _SITE_SW_VERSION


@app.route("/sw.js")
def site_sw():
    return Response(_SITE_SW_JS, mimetype="application/javascript", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Service-Worker-Allowed": "/",
    })


@app.after_request
def inject_nav(response):
    if response.status_code != 200:
        return response
    uid = session.get("user_id")
    p = request.path
    # PDF 私有 CacheStorage 只信服务器响应头，不信页面自报 namespace。
    # 头仅进入四个正式书籍壳与 /pdf/api/*；退役网页/RBI 响应不能建立 SW client 绑定。
    if uid and (p in READER_PROVIDER_ENTRY_PATHS or p.startswith("/pdf/api/")):
        response.headers["X-BW-Reader-Cache-Namespace"] = (
            _reader_storage_namespace(uid)
        )
        response.vary.add("Cookie")
    if (response.mimetype or "") != "text/html":
        return response
    if not uid:
        return response
    if not any(p.startswith(x) for x in NAV_INJECT_PREFIXES):
        return response
    # 实况网页的代理输出是**别人的页面**,注全站导航进去纯属污染(实测被打成
    # ja.wikipedia.org/static/nav.js 这种跨站死链)。外壳 /pdf/web/live 才该有导航。
    if p.startswith("/pdf/web/proxy") or p.startswith("/pdf/web/res") \
            or p.startswith("/pdf/web/p/") or p.startswith("/pdf/web/r/") \
            or p == "/pdf/web/rbi":
        return response
    try:
        body = response.get_data(as_text=True)
    except Exception:
        return response
    if "</body>" not in body or "__NAV_LOADED__" in body:
        return response
    row = get_db().execute(
        "SELECT username, role FROM users WHERE id = ?", (uid,)
    ).fetchone()
    if not row:
        return response
    user_data = {
        "username": row["username"],
        "role": row["role"],
    }
    # Vault 身份与短期授权证明只进入四个正式书籍入口；dashboard、管理页、
    # API/代理输出即使同属 /pdf 前缀也拿不到 ticket。
    if p in READER_PROVIDER_ENTRY_PATHS:
        storage_namespace = _reader_storage_namespace(uid)
        user_data.update({
            "storage_namespace": storage_namespace,
            "storage_provider_ticket": _reader_provider_ticket(storage_namespace),
        })
    user_blob = json.dumps(user_data)
    try:                                  # mtime 做 cache-bust:nav.js 一更新客户端就重取(免 iOS 缓存旧版)
        nav_v = int(os.path.getmtime("/var/www/html/static/nav.js"))
    except Exception:
        nav_v = 1
    try:
        voice_v = int(os.path.getmtime("/var/www/html/static/voice.js"))
    except Exception:
        voice_v = 1
    try:
        cache_identity_v = int(os.path.getmtime(
            "/var/www/html/static/reader-runtime/pwa-cache-identity.js"
        ))
    except Exception:
        cache_identity_v = 1
    inject = (
        f'<script>window.__USER__={user_blob};</script>'
        f'<script src="/static/reader-runtime/pwa-cache-identity.js?v={cache_identity_v}"></script>'
        f'<script src="/static/nav.js?v={nav_v}" defer></script>'
        f'<script src="/static/voice.js?v={voice_v}" defer></script>'   # 全站语音助手浮窗
    )
    # PWA:全站注入 manifest/apple-touch-icon/全屏 meta(可装到主屏、全屏像 app)
    if "</head>" in body and 'rel="manifest"' not in body:
        body = body.replace("</head>", _PWA_HEAD + "</head>", 1)
    response.set_data(body.replace("</body>", inject + "</body>", 1))
    # HTML 入口 no-cache(每次revalidate取新,但允许存):防旧缓存又不踩 iOS Safari/PWA「no-store→白屏」坑
    response.headers["Cache-Control"] = "no-cache"
    return response

# ─────────────────────────── helpers ───────────────────────────

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or u["role"] != "admin":
            abort(403)
        return f(*a, **kw)
    return wrapper

def user_dir(username, dataset=""):
    base = USERS_DIR / username
    if dataset:
        base = base / dataset
    return base

PROTECTED_PREFIXES = ("/dashboard", "/private", "/history", "/qa", "/profile", "/admin", "/auth", "/control", "/pdf", "/insights",
                      "/api/assistant", "/api/fitness")   # 后两个:让 Bearer 桥也覆盖(MCP 外部 agent 冷启动直调健身/助手,此前靠先调 /pdf 拿 session cookie 的隐式顺序)
PUBLIC_PREFIXES    = ("/login", "/logout", "/register", "/static")
WEB_PROXY_CAP_EXACT_PATHS = {
    "/pdf/web/frame",
    "/pdf/web/res",
}
WEB_PROXY_CAP_REQUIRED_EXACT_PATHS = {
    "/pdf/web/res",
}


def _reader_web_proxy_secret():
    return os.environ.get("READER_WEB_PROXY_SECRET") or app.secret_key


def _reader_web_proxy_cap_path(path: str) -> bool:
    return (
        path in WEB_PROXY_CAP_EXACT_PATHS
        or path.startswith("/pdf/web/p/")
        or path.startswith("/pdf/web/r/")
    )


def _reader_web_proxy_cap_required(path: str) -> bool:
    return (
        path in WEB_PROXY_CAP_REQUIRED_EXACT_PATHS
        or path.startswith("/pdf/web/p/")
        or path.startswith("/pdf/web/r/")
    )

@app.before_request
def require_login_global():
    p = request.path
    # API token 路径自带认证，跳过 session 检查
    if p.startswith("/api/upload"):
        return
    # legacy: 截图问答 relay 用 RELAY_KEY
    if p.startswith("/qa/update"):
        return
    # 公开路径
    if any(p.startswith(x) for x in PUBLIC_PREFIXES):
        return
    # 受保护路径
    for prefix in PROTECTED_PREFIXES:
        if p.startswith(prefix):
            session_uid = session.get("user_id")
            authorization = request.headers.get("Authorization", "")
            # Any explicit Authorization header is authoritative.  A bad token
            # must never borrow an ambient browser session, and a token for B
            # must never execute inside A's session.
            if authorization:
                bearer_owner = _bearer_user()
                if not bearer_owner:
                    abort(401)
                if (
                    session_uid is not None
                    and str(session_uid) != str(bearer_owner[0])
                ):
                    abort(401)
                session["user_id"], session["username"] = bearer_owner
                session_uid = bearer_owner[0]
            cap_details = None
            if _reader_web_proxy_cap_path(p):
                candidate = verify_web_proxy_cap_details(
                    _reader_web_proxy_secret(),
                    request.args.get("__bwcap", ""),
                )
                if candidate:
                    active = get_db().execute(
                        "SELECT 1 FROM users WHERE id = ?",
                        (candidate.user_id,),
                    ).fetchone()
                    if active:
                        # 已有登录态时也必须解析 scope，供代理 cookie 舱壁使用；
                        # 但绝不能让另一个用户的 capability 覆盖当前会话身份。
                        if session_uid and str(candidate.user_id) != str(session_uid):
                            abort(403)
                        cap_details = candidate
                        g.web_proxy_user_id = int(candidate.user_id)
                        g.web_proxy_scope_host = candidate.scope_host
                        g.web_proxy_scope_scheme = candidate.scope_scheme
                        g.web_proxy_scope_port = int(candidate.scope_port)
                        g.web_proxy_cap_expires_at = int(candidate.expires_at)
                # Proxy transport never falls back to an ambient App session:
                # opaque sandbox requests may carry that cookie unexpectedly.
                if _reader_web_proxy_cap_required(p) and not cap_details:
                    abort(403)
            if not session_uid:
                if cap_details:
                    return
                return redirect(url_for("login", next=p))
            return

# ─────────────────────────── Auth ───────────────────────────

@app.route("/")
def index():
    # 主页即登录入口：已登录直接进个人仪表板，未登录显示登录界面
    if session.get("logged_in"):
        return redirect("/dashboard/")
    return redirect(url_for("login"))


# ── CSRF(会话级 token,零依赖)──
# SameSite=Lax 已挡跨站带 cookie 的 POST(CSRF 主载体);这里给公开 auth 表单再加 token 做纵深防御。
# 模板里 {{ csrf_token() }} 输出隐藏域,POST 时与 session 内 token 比对(constant-time)。JSON API 不走此校验
# (它们靠 SameSite + session/Bearer;给所有 fetch 串 token 风险高、收益低,故不做)。
def _csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok

def _csrf_ok():
    real = session.get("_csrf", "")
    sent = request.form.get("csrf_token", "")
    return bool(real) and secrets.compare_digest(str(real), str(sent))

@app.context_processor
def _inject_csrf():
    return {"csrf_token": _csrf_token}


def _safe_next(nxt, fallback="/dashboard/"):
    """开放重定向防护:登录后只跳站内相对路径,拒绝 //evil.com、http(s)://、javascript: 等外站/协议跳转。
    等价 Django url_has_allowed_host_and_scheme 的最小实现。"""
    if not nxt:
        return fallback
    nxt = nxt.strip()
    # 必须是单斜杠开头的站内绝对路径,且不是协议相对(//host)或反斜杠绕过(/\host)
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\"):
        return fallback
    from urllib.parse import urlparse
    p = urlparse(nxt)
    if p.scheme or p.netloc:   # 带 scheme/host 一律拒
        return fallback
    return nxt


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if not _csrf_ok():
            return render_template("login.html", error="会话已过期，请重试"), 400
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session.permanent = True
            session["user_id"]   = row["id"]
            session["username"]  = row["username"]
            session["role"]      = row["role"]
            session["logged_in"] = True  # legacy
            user_dir(row["username"]).mkdir(parents=True, exist_ok=True)
            return redirect(_safe_next(request.args.get("next")))
        error = "用户名或密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _invite_valid(inv):
    if not inv:
        return False
    if inv["used_by"]:
        return False
    if inv["expires_at"]:
        try:
            exp = datetime.datetime.fromisoformat(inv["expires_at"])
            if datetime.datetime.now() > exp:
                return False
        except ValueError:
            pass
    return True


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    token = (request.values.get("token") or "").strip()
    db = get_db()
    invite = db.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone() if token else None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        # 重新查一次 invite，避免 GET/POST 之间被使用
        invite = db.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone() if token else None
        if not _csrf_ok():
            error = "会话已过期，请刷新后重试"
        elif not _invite_valid(invite):
            error = "邀请码无效或已使用"
        elif not username or not all(c.isalnum() or c in "_-" for c in username):
            error = "用户名只能包含字母、数字、下划线、连字符"
        elif len(username) < 2 or len(username) > 32:
            error = "用户名长度需在 2–32 之间"
        elif len(password) < 6:
            error = "密码至少 6 位"
        elif password != password2:
            error = "两次输入的密码不一致"
        else:
            existing = db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if existing:
                error = "用户名已存在"
            else:
                pwd_hash = generate_password_hash(password)
                cur = db.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, pwd_hash),
                )
                new_id = cur.lastrowid
                db.execute(
                    "UPDATE invites SET used_by=?, used_at=datetime('now') WHERE token=?",
                    (new_id, token),
                )
                db.commit()
                user_dir(username).mkdir(parents=True, exist_ok=True)
                # 自动登录
                session.clear()
                session.permanent = True
                session["user_id"]   = new_id
                session["username"]  = username
                session["role"]      = "user"
                session["logged_in"] = True
                return redirect("/profile/")

    return render_template(
        "register.html",
        error=error,
        token=token,
        invite_valid=_invite_valid(invite),
    )

# ─────────────────────────── 静态数据路由（按用户分） ───────────────────────────

DASHBOARD_TEMPLATE_DIR = DATA_DIR / "dashboard_template"
HISTORY_TEMPLATE_DIR   = DATA_DIR / "history_template"

# dataset → 共享模板目录（用户目录没该文件时 fallback 到这里）
_TEMPLATE_FALLBACK = {
    "dashboard": DASHBOARD_TEMPLATE_DIR,
    "history":   HISTORY_TEMPLATE_DIR,
}


def _serve_user(dataset, filename):
    """按用户路由静态数据。dashboard / history 数据集额外 fallback 到共享模板：
       data/users/<u>/<dataset>/<f>  →  fallback  →  data/<dataset>_template/<f>
    这样 admin（bwicarus）上传的 HTML/JS/CSS 模板被所有用户复用，
    每个用户的 dashboard.json / history.json 等数据仍按用户私有。
    """
    from werkzeug.exceptions import NotFound

    user = current_user()
    if not user:
        return redirect(url_for("login", next=request.path))
    base = user_dir(user["username"], dataset)
    base.mkdir(parents=True, exist_ok=True)
    full = base / filename
    if filename.endswith("/") or full.is_dir():
        full = full / "index.html"
        filename = str(full.relative_to(base))

    serve_dirs = [str(base)]
    tpl = _TEMPLATE_FALLBACK.get(dataset)
    if tpl:
        serve_dirs.append(str(tpl))

    response = None
    for d in serve_dirs:
        try:
            response = send_from_directory(d, filename)
            break
        except NotFound:
            continue
    if response is None:
        abort(404)

    # HTML 文件强制 buffer，让 after_request 能注入侧边导航
    if response.mimetype == "text/html":
        response.direct_passthrough = False
        response.set_data(response.get_data())
    return response

@app.route("/dashboard/")
@app.route("/dashboard/<path:filename>")
def dashboard(filename="index.html"):
    return _serve_user("dashboard", filename)

@app.route("/history/")
@app.route("/history/<path:filename>")
def history_page(filename="index.html"):
    return _serve_user("history", filename)

@app.route("/private/")
@app.route("/private/<path:filename>")
def private(filename="index.html"):
    return _serve_user("private", filename)


# 服务端兜底：前端已切 localStorage，仅为新设备首次拉取使用
@app.route("/dashboard/graph-settings.json", methods=["GET", "POST"])
def graph_settings():
    user = current_user()
    if not user:
        abort(401)
    f = user_dir(user["username"], "dashboard") / "graph-settings.json"
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            abort(400)
        _atomic_write_text(f, json.dumps(data, ensure_ascii=False, indent=2))
        return jsonify({"ok": True})
    if f.exists():
        try:
            return jsonify(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return jsonify({})


# ─── 截图问答历史删除（webapp 加鉴权层 + proxy 到 qa-server）───
@app.route("/api/qa-history/<hid>/delete", methods=["POST"])
@login_required
def qa_history_delete(hid):
    """删一条截图问答历史。webapp cookie 鉴权后 proxy 到本机 qa-server
    daemon (:9091)。qa-server 那边做 SQLite 清理 + 删截图 + 删 Obsidian 笔记
    + 触发 _export_history_to_webapp 把变化同步回 webapp data。

    body 透传给 qa-server（可选 keep_note=true 只清 db + 截图，保留 .md）。
    """
    import re as _re
    if not _re.match(r'^[A-Za-z0-9_-]+$', hid):
        return jsonify({"ok": False, "error": "invalid id"}), 400
    raw = request.get_json(silent=True) or {}
    payload = {"id": hid, "keep_note": bool(raw.get("keep_note"))}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:9091/api/history/delete",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return jsonify(json.loads(r.read()))
    except urllib.error.URLError as e:
        return jsonify({"ok": False, "error": f"qa-server 不可达: {e}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─── 通用左侧导航的自定义链接（per-user 持久化）───
# 前端 nav.js 用，替代原 localStorage 存储。
@app.route("/api/nav-links", methods=["GET", "POST"])
def nav_links():
    user = current_user()
    if not user:
        abort(401)
    f = user_dir(user["username"]) / "nav-links.json"
    if request.method == "POST":
        data = request.get_json(force=True, silent=True)
        if not isinstance(data, list):
            abort(400)
        # 只接受 {label, url} 形式条目，过滤掉空值
        clean = []
        for item in data:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            url   = str(item.get("url",   "")).strip()
            if label and url:
                clean.append({"label": label, "url": url})
        _atomic_write_text(f, json.dumps(clean, ensure_ascii=False, indent=2))
        return jsonify({"ok": True, "links": clean})
    if f.exists():
        try:
            arr = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(arr, list):
                return jsonify(arr)
        except (json.JSONDecodeError, OSError):
            pass
    return jsonify([])


# ─────────────────────────── 个人页 + 改密码 + token 管理 ───────────────────────────

@app.route("/profile/")
@login_required
def profile():
    user = current_user()
    tokens = get_db().execute(
        "SELECT id, label, token, created_at, last_used_at FROM api_tokens "
        "WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    return render_template("profile.html", user=user, tokens=tokens)

@app.route("/api/change-password", methods=["POST"])
@login_required
def change_password():
    db = get_db()
    user = current_user()
    old  = request.form.get("old_password", "")
    new  = request.form.get("new_password", "")
    new2 = request.form.get("new_password2", "")
    if not check_password_hash(user["password_hash"], old):
        return jsonify({"ok": False, "error": "原密码不正确"}), 400
    if len(new) < 6:
        return jsonify({"ok": False, "error": "新密码至少 6 位"}), 400
    if new != new2:
        return jsonify({"ok": False, "error": "两次输入的新密码不一致"}), 400
    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(new), user["id"]),
    )
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/tokens", methods=["POST"])
@login_required
def create_api_token():
    db = get_db()
    user = current_user()
    if request.is_json:
        label = (request.get_json(silent=True) or {}).get("label", "")
    else:
        label = request.form.get("label", "")
    label = (label or "").strip()[:80] or None
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO api_tokens (user_id, token, label) VALUES (?, ?, ?)",
        (user["id"], token, label),
    )
    db.commit()
    return jsonify({"ok": True, "token": token})

@app.route("/api/tokens/<int:token_id>", methods=["DELETE", "POST"])
@login_required
def delete_api_token(token_id):
    db = get_db()
    user = current_user()
    db.execute(
        "DELETE FROM api_tokens WHERE id=? AND user_id=?",
        (token_id, user["id"]),
    )
    db.commit()
    if request.method == "POST":
        return redirect(url_for("profile"))
    return jsonify({"ok": True})

# ─────────────────────────── 客户端设备授权（OAuth-like loopback callback） ───────────────────────────

def _is_loopback_callback(url: str) -> bool:
    """callback URL 必须指向本机 loopback：防开放重定向 + 防 token 泄漏到外站。"""
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        if u.scheme != "http":
            return False
        host = (u.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        return True
    except Exception:
        return False


@app.route("/auth/device-link", methods=["GET", "POST"])
@login_required
def device_link():
    """客户端启动时：浏览器跳到这里 → 用户登录后授权 → 跳回 cb_url?token=...&state=...
    cb_url 必须是 http://127.0.0.1:<port>/... 这种 loopback 地址。"""
    cb_url = (request.values.get("cb") or "").strip()
    state  = (request.values.get("state") or "").strip()
    label  = (request.values.get("label") or "客户端").strip()[:80]

    if not cb_url or not _is_loopback_callback(cb_url):
        return render_template(
            "device_link.html",
            error="回调地址不合法（必须是本机 127.0.0.1 / localhost）",
            cb_url=cb_url, state=state, label=label, user=current_user(),
        ), 400

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "approve":
            user = current_user()
            token = secrets.token_urlsafe(32)
            get_db().execute(
                "INSERT INTO api_tokens (user_id, token, label) VALUES (?, ?, ?)",
                (user["id"], token, label),
            )
            get_db().commit()
            sep = "&" if "?" in cb_url else "?"
            redirect_to = f"{cb_url}{sep}token={token}&state={state}"
            return render_template(
                "device_link.html",
                approved=True, label=label, user=user,
                redirect_to=redirect_to,
            )
        # deny
        sep = "&" if "?" in cb_url else "?"
        redirect_to = f"{cb_url}{sep}error=denied&state={state}"
        return render_template(
            "device_link.html",
            denied=True, label=label, user=current_user(),
            redirect_to=redirect_to,
        )

    # GET：显示授权确认页
    return render_template(
        "device_link.html",
        cb_url=cb_url, state=state, label=label, user=current_user(),
    )


# ─────────────────────────── Admin: 用户与邀请码 ───────────────────────────

@app.route("/admin/")
@admin_required
def admin_home():
    db = get_db()
    users = db.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY created_at"
    ).fetchall()
    invites = db.execute("""
        SELECT i.*, u1.username AS creator, u2.username AS user
        FROM invites i
        LEFT JOIN users u1 ON i.created_by = u1.id
        LEFT JOIN users u2 ON i.used_by    = u2.id
        ORDER BY i.created_at DESC
    """).fetchall()
    return render_template("admin.html", users=users, invites=invites)

@app.route("/admin/invites", methods=["POST"])
@admin_required
def admin_create_invite():
    db = get_db()
    user = current_user()
    note = (request.form.get("note") or "").strip() or None
    expires_days = (request.form.get("expires_days") or "").strip()
    expires_at = None
    if expires_days.isdigit() and int(expires_days) > 0:
        d = datetime.datetime.now() + datetime.timedelta(days=int(expires_days))
        expires_at = d.isoformat(timespec="seconds")
    token = secrets.token_urlsafe(20)
    db.execute(
        "INSERT INTO invites (token, created_by, expires_at, note) VALUES (?, ?, ?, ?)",
        (token, user["id"], expires_at, note),
    )
    db.commit()
    return redirect(url_for("admin_home"))

@app.route("/admin/invites/<token>/delete", methods=["POST"])
@admin_required
def admin_delete_invite(token):
    get_db().execute("DELETE FROM invites WHERE token=?", (token,))
    get_db().commit()
    return redirect(url_for("admin_home"))

@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    me = current_user()
    if user_id == me["id"]:
        abort(400)
    db = get_db()
    row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return redirect(url_for("admin_home"))
    # invites 的 FK 没有 CASCADE：先解开使用者，再清掉该用户创建过的所有 invite
    db.execute("UPDATE invites SET used_by = NULL WHERE used_by = ?", (user_id,))
    db.execute("DELETE FROM invites WHERE created_by = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    # 同时清理该用户的数据目录
    shutil.rmtree(str(user_dir(row["username"])), ignore_errors=True)
    return redirect(url_for("admin_home"))

# ─────────────────────────── 客户端上传 API（Bearer token） ───────────────────────────

def _authenticate_api():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    db = get_db()
    row = db.execute(
        "SELECT t.user_id, u.username FROM api_tokens t "
        "JOIN users u ON t.user_id = u.id WHERE t.token = ?",
        (token,),
    ).fetchone()
    if not row:
        return None
    db.execute("UPDATE api_tokens SET last_used_at=datetime('now') WHERE token=?", (token,))
    db.commit()
    return row["username"]


def _bearer_user():
    """从 Bearer token → (user_id, username) 或 None。给 require_login_global:让外部 agent(Claude Code skill)
    用 API token 调受保护 API,无需浏览器 session(Bearer token 等价于该用户程序化登录)。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    row = get_db().execute(
        "SELECT t.user_id, u.username FROM api_tokens t JOIN users u ON t.user_id = u.id WHERE t.token = ?",
        (token,),
    ).fetchone()
    if not row:
        return None
    return row["user_id"], row["username"]


def _reader_authenticated_storage_identity():
    """Resolve the current request owner without trusting any client identity.

    An explicit Authorization header is authoritative and fail-closed: an invalid
    Bearer token never falls back to an ambient browser session.  If both a valid
    Bearer and session are present, they must identify the same account.
    """
    authorization = request.headers.get("Authorization", "")
    session_uid = session.get("user_id")
    if authorization:
        bearer = _bearer_user()
        if not bearer:
            return None
        try:
            bearer_uid = int(bearer[0])
            session_owner = (
                int(session_uid) if session_uid is not None else None
            )
        except (TypeError, ValueError):
            return None
        if session_owner is not None and session_owner != bearer_uid:
            return None
        uid = bearer_uid
    else:
        if session_uid is None:
            return None
        try:
            uid = int(session_uid)
        except (TypeError, ValueError):
            return None
    owner = get_db().execute(
        "SELECT 1 FROM users WHERE id = ?",
        (uid,),
    ).fetchone()
    if not owner:
        return None
    return {
        "user_id": uid,
        "storage_namespace": _reader_storage_namespace(uid),
    }


def _reader_authenticated_storage_namespace():
    """Compatibility wrapper for callers that only need the public namespace."""
    identity = _reader_authenticated_storage_identity()
    return identity["storage_namespace"] if identity else None


def _reader_legacy_sidecar_claim_authorized(identity) -> bool:
    """Authorize the one-time development-data claim, never infer it thereafter.

    The user explicitly confirmed that the current development system has one
    account and that all existing unpartitioned reader sidecars belong to it.
    This callback is only consulted while no immutable claim manifest exists.
    Once a manifest has been committed, the sidecar store compares its recorded
    uid and namespace instead of repeating the ``one row`` inference.
    """
    if isinstance(identity, dict):
        raw_uid = identity.get("user_id")
        raw_namespace = identity.get("storage_namespace")
    else:
        raw_uid = getattr(identity, "user_id", None)
        raw_namespace = getattr(identity, "storage_namespace", None)
    try:
        uid = int(raw_uid)
    except (TypeError, ValueError):
        return False
    namespace = str(raw_namespace or "")
    if uid <= 0 or not _valid_reader_namespace(namespace):
        return False
    rows = get_db().execute(
        "SELECT id, storage_namespace FROM users ORDER BY id"
    ).fetchall()
    return (
        len(rows) == 1
        and int(rows[0]["id"]) == uid
        and str(rows[0]["storage_namespace"] or "") == namespace
    )


@app.route("/api/reader/token-owner", methods=["POST"])
def reader_token_owner():
    """Return only the Vault namespace proven by an explicit Bearer token."""
    bearer = _bearer_user()
    if not bearer:
        response = jsonify({"ok": False, "error": "invalid bearer token"})
        response.status_code = 401
    else:
        response = jsonify({
            "ok": True,
            "storage_namespace": _reader_storage_namespace(int(bearer[0])),
        })
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/api/reader/provider-authorize", methods=["POST"])
def reader_provider_authorize():
    """核验 PWA 发给扩展的 Vault namespace 证明；不返回用户资料或任何凭据。"""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    namespace = str(body.get("namespace") or "").strip()
    ticket = str(body.get("ticket") or "").strip()
    if not _valid_reader_namespace(namespace):
        return jsonify({"ok": False, "error": "invalid provider authorization"}), 400
    now = int(time.time())
    expires_at = _verify_reader_provider_ticket(namespace, ticket, now=now)
    if expires_at is None:
        return jsonify({"ok": False, "error": "provider authorization denied"}), 403
    owner = get_db().execute(
        "SELECT 1 FROM users WHERE storage_namespace = ?",
        (namespace,),
    ).fetchone()
    if not owner:
        return jsonify({"ok": False, "error": "provider authorization denied"}), 403
    return jsonify({
        "ok": True,
        "storage_namespace": namespace,
        "expires_at": expires_at,
        "expires_in": max(0, expires_at - now),
    })


@app.route("/api/reader/provider-ticket", methods=["POST"])
def reader_provider_ticket():
    """登录中的正式 PWA 刷新短票；响应不得进入站点 SW 或 HTTP 缓存。"""
    uid = session.get("user_id")
    if not uid:
        response = jsonify({"ok": False, "error": "authentication required"})
        response.status_code = 401
    elif request.headers.get("X-BW-Reader-Provider") != "1":
        response = jsonify({"ok": False, "error": "same-origin PWA request required"})
        response.status_code = 403
    else:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        page = str(body.get("page") or "").strip()
        if page not in READER_PROVIDER_ENTRY_PATHS:
            response = jsonify({"ok": False, "error": "invalid reader entry"})
            response.status_code = 400
        else:
            owner = get_db().execute(
                "SELECT 1 FROM users WHERE id = ?",
                (int(uid),),
            ).fetchone()
            if not owner:
                response = jsonify({"ok": False, "error": "authentication required"})
                response.status_code = 401
            else:
                namespace = _reader_storage_namespace(uid)
                ticket = _reader_provider_ticket(namespace)
                expires_at = int(ticket.split("-", 4)[2])
                now = int(time.time())
                response = jsonify({
                    "ok": True,
                    "storage_namespace": namespace,
                    "ticket": ticket,
                    "expires_at": expires_at,
                    "expires_in": max(0, expires_at - now),
                })
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/pdf/api/cache-identity")
def reader_cache_identity():
    """只供 /pdf Service Worker 重新核验当前 session；namespace 仅在私有响应头返回。"""
    uid = session.get("user_id")
    if not uid:
        response = jsonify({"ok": False, "error": "authentication required"})
        response.status_code = 401
    elif request.headers.get("X-BW-Reader-Cache-Identity") != "1":
        response = jsonify({"ok": False, "error": "service worker identity required"})
        response.status_code = 403
    else:
        owner = get_db().execute(
            "SELECT 1 FROM users WHERE id = ?",
            (int(uid),),
        ).fetchone()
        if not owner:
            response = jsonify({"ok": False, "error": "authentication required"})
            response.status_code = 401
        else:
            response = jsonify({"ok": True})
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/api/upload/<dataset>", methods=["POST"])
def api_upload(dataset):
    if dataset not in ALLOWED_DATASETS:
        abort(400)
    username = _authenticate_api()
    if not username:
        abort(401)
    rel = (request.headers.get("X-Path") or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        abort(400)
    base = user_dir(username, dataset).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        abort(400)
    target.parent.mkdir(parents=True, exist_ok=True)
    if request.files:
        f = next(iter(request.files.values()))
        f.save(str(target))
    else:
        target.write_bytes(request.get_data())

    # admin 用户上传 dashboard / history：同步一份到共享模板，让其他用户 fallback 看到同一套 HTML/CSS/JS
    # 注意：仅 .html / .css / .js / image 才同步到模板（数据文件如 dashboard.json 是用户私有，不能跨用户共享）
    synced_template = False
    tpl_dir = _TEMPLATE_FALLBACK.get(dataset)
    if tpl_dir:
        db = get_db()
        row = db.execute(
            "SELECT role FROM users WHERE username = ?", (username,)
        ).fetchone()
        is_admin = bool(row and row["role"] == "admin")
        # 数据文件（json）只在用户私有目录；模板文件（html/css/js/字体/图标）admin 可同步
        is_template_file = rel.lower().endswith(
            (".html", ".htm", ".css", ".js", ".woff", ".woff2", ".ico", ".svg", ".png", ".jpg")
        ) and not rel.lower().endswith((".json",))
        if is_admin and is_template_file:
            try:
                tpl_target = (tpl_dir / rel).resolve()
                tpl_root = tpl_dir.resolve()
                tpl_target.relative_to(tpl_root)
                tpl_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(target), str(tpl_target))
                synced_template = True
            except (ValueError, OSError):
                pass

    res = {"ok": True, "path": str(target.relative_to(USERS_DIR))}
    if synced_template:
        res["template_synced"] = True
    return jsonify(res)

# ─────────────────────────── 兼容旧 endpoint ───────────────────────────

@app.route("/qa/")
@app.route("/qa")
@login_required
def qa():
    data = None
    if QA_FILE.exists():
        try:
            data = json.loads(QA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return render_template("qa.html", data=data)

@app.route("/qa/update", methods=["POST"])
def qa_update():
    key = request.headers.get("X-API-Key", "")
    if not key or key != os.environ.get("RELAY_KEY", ""):
        abort(403)
    data = request.get_json(force=True) or {}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    QA_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True})




# 控制面板 (替代 Windows 客户端 EXE)
from control import register_control
register_control(app)

# 知识图谱（技能树）
from skilltree import register_skilltree
register_skilltree(app)

# 网页 PDF 阅读器
def _reader_active_user(user_id):
    """Cheap endpoint-level liveness check for short-lived reader tickets."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if uid <= 0:
        return False
    return get_db().execute(
        "SELECT 1 FROM users WHERE id = ?",
        (uid,),
    ).fetchone() is not None


app.config["READER_ACTIVE_USER_CHECK"] = _reader_active_user
# pdf_reader 的 blueprint 不能反向 import app.py。通过 Flask extensions 注入当前请求的
# 真实 owner resolver，让所有 sidecar 与 sync-batch 都以 session/Bearer 身份重新计算
# uid + storage namespace；旧数据认领授权仅在持久 claim 尚不存在时调用一次。
app.extensions["reader_storage_identity_resolver"] = (
    _reader_authenticated_storage_identity
)
app.extensions["reader_storage_namespace_resolver"] = (
    _reader_authenticated_storage_namespace
)
app.extensions["reader_legacy_sidecar_claim_authorizer"] = (
    _reader_legacy_sidecar_claim_authorized
)
# HTTP and owner-bound CLI jobs must resolve the same account root.  Tests set
# WEBAPP_DATA, so they still stay inside a disposable directory.
from reader_sidecar_store import default_sidecar_root
app.extensions["reader_sidecar_root"] = default_sidecar_root(
    Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
)

from reader_sync_relay import register_reader_sync_relay
register_reader_sync_relay(app)

from shared_note import register_shared_note
register_shared_note(app)

from computer_voice_routes import register_computer_voice
register_computer_voice(app, root=DATA_DIR)

from pdf_reader import register_pdf_reader
register_pdf_reader(app)

# 健身页(/private/fitness)
from fitness import register as register_fitness
register_fitness(app)

# 学习数据看板(/insights)
from insights import register_insights
register_insights(app)

# 全站语音助手(/api/voice/*):转录(Gemini)+ agent
from voice import register_voice
register_voice(app)
from assistant import register_assistant   # PDF 侧边栏 Copilot(沙盒 agent + 工具循环)
register_assistant(app)

if __name__ == "__main__":
    # threaded=True：慢的 AI 请求（语法分析/翻译走 claude_cli）不再阻塞其他请求
    app.run(host="127.0.0.1", port=5000, threaded=True)
