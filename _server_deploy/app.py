import os
import json
import base64
import shutil
import secrets
import sqlite3
import datetime
import urllib.request
import urllib.error
from pathlib import Path
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, send_from_directory, jsonify, abort, g,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = datetime.timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,  # nginx 终结 HTTPS
)
# 信任 nginx 转发头：让 url_for / redirect 用真实 https scheme + 客户端 IP
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DATA_DIR  = Path(os.environ.get("WEBAPP_DATA", "/root/webapp/data"))
USERS_DIR = DATA_DIR / "users"
DB_PATH   = DATA_DIR / "app.db"
QA_FILE   = DATA_DIR / "qa.json"
ALLOWED_DATASETS = {"dashboard", "history", "private"}

# ─────────────────────────── DB ───────────────────────────

def get_db():
    if "db" not in g:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
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
    conn.commit()
    conn.close()

with app.app_context():
    init_db()

# ─────────────────────────── 通用左侧导航 (auto-inject) ───────────────────────────

NAV_INJECT_PREFIXES = ("/dashboard", "/history", "/private", "/profile", "/admin", "/qa", "/control")

@app.after_request
def inject_nav(response):
    if response.status_code != 200:
        return response
    if (response.mimetype or "") != "text/html":
        return response
    if not session.get("user_id"):
        return response
    p = request.path
    if not any(p.startswith(x) for x in NAV_INJECT_PREFIXES):
        return response
    try:
        body = response.get_data(as_text=True)
    except Exception:
        return response
    if "</body>" not in body or "__NAV_LOADED__" in body:
        return response
    uid = session["user_id"]
    row = get_db().execute(
        "SELECT username, role FROM users WHERE id = ?", (uid,)
    ).fetchone()
    if not row:
        return response
    user_blob = json.dumps({"username": row["username"], "role": row["role"]})
    inject = (
        f'<script>window.__USER__={user_blob};</script>'
        '<script src="/static/nav.js" defer></script>'
    )
    response.set_data(body.replace("</body>", inject + "</body>", 1))
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

PROTECTED_PREFIXES = ("/dashboard", "/private", "/history", "/qa", "/profile", "/admin", "/auth", "/control")
PUBLIC_PREFIXES    = ("/login", "/logout", "/register", "/static")

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
            if not session.get("user_id"):
                return redirect(url_for("login", next=p))
            return

# ─────────────────────────── Auth ───────────────────────────

@app.route("/")
def index():
    # 主页即登录入口：已登录直接进个人仪表板，未登录显示登录界面
    if session.get("logged_in"):
        return redirect("/dashboard/")
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
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
            return redirect(request.args.get("next") or "/dashboard/")
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
        if not _invite_valid(invite):
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
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
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

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
