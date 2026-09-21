"""Native Apple sign-in, linked to the existing Reader users and sessions."""
import hashlib
import secrets
import sqlite3
import time

from flask import jsonify, request, session
import jwt
from werkzeug.security import check_password_hash, generate_password_hash

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_AUDIENCE = "space.bwicarus.bwreader2"
_keys = jwt.PyJWKClient(APPLE_ISSUER + "/auth/keys", cache_jwk_set=True, lifespan=3600, timeout=8)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_apple_identities (
    subject TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reader_apple_challenges (
    state TEXT PRIMARY KEY,
    nonce_hash TEXT NOT NULL,
    flow_hash TEXT NOT NULL,
    user_id INTEGER,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reader_apple_pending_links (
    ticket_hash TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    flow_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
"""


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_identity(token, nonce_hash):
    if not isinstance(token, str) or len(token) > 16384:
        raise ValueError("invalid token")
    key = _keys.get_signing_key_from_jwt(token)
    claims = jwt.decode(token, key.key, algorithms=["RS256"],
                        audience=APPLE_AUDIENCE, issuer=APPLE_ISSUER, leeway=30,
                        options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]})
    if not secrets.compare_digest(str(claims["nonce"]), nonce_hash):
        raise ValueError("nonce mismatch")
    subject = claims["sub"]
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise ValueError("invalid subject")
    return subject


def register_reader_apple_auth(app, get_db, user_dir):
    def response_error(message, status=400, **extra):
        return jsonify(ok=False, error=message, **extra), status

    def request_body():
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return None
        if not request.is_json or request.content_length and request.content_length > 20000:
            return None
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else None

    def establish(row):
        session.clear()
        session.permanent = True
        session.update(user_id=row["id"], username=row["username"], role=row["role"], logged_in=True)
        user_dir(row["username"]).mkdir(parents=True, exist_ok=True)
        return jsonify(ok=True, username=row["username"])

    @app.get("/login/apple/status")
    def reader_apple_status():
        db = get_db()
        account = db.execute("SELECT id,username FROM users WHERE id=?", (session.get("user_id"),)).fetchone()
        result = jsonify(ok=True, authenticated=bool(account), username=account["username"] if account else "",
                         apple_linked=bool(account and db.execute("SELECT 1 FROM reader_apple_identities WHERE user_id=?", (account["id"],)).fetchone()))
        result.headers["Cache-Control"] = "no-store, private"
        return result

    @app.post("/login/apple/challenge")
    def reader_apple_challenge():
        if request_body() is None:
            return response_error("登录请求无效")
        now = int(time.time())
        db = get_db()
        account = db.execute("SELECT username FROM users WHERE id=?", (session.get("user_id"),)).fetchone()
        if session.get("user_id") and not account:
            session.clear()
        db.execute("DELETE FROM reader_apple_challenges WHERE expires_at < ?", (now,))
        db.execute("DELETE FROM reader_apple_pending_links WHERE expires_at < ?", (now,))
        flow = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        session["reader_apple_flow"] = flow
        db.execute("INSERT INTO reader_apple_challenges VALUES (?, ?, ?, ?, ?)",
                   (state, _hash(nonce), _hash(flow), session.get("user_id"), now + 600))
        db.commit()
        return jsonify(ok=True, nonce=nonce, state=state,
                       linking=bool(account), username=account["username"] if account else "",
                       apple_linked=bool(account and db.execute("SELECT 1 FROM reader_apple_identities WHERE user_id=?", (session["user_id"],)).fetchone()),
                       expires_at=now + 600)

    @app.post("/login/apple/logout")
    def reader_apple_logout():
        if request_body() is None:
            return response_error("退出请求无效")
        flow = session.get("reader_apple_flow", "")
        if flow:
            db = get_db()
            db.execute("DELETE FROM reader_apple_challenges WHERE flow_hash=?", (_hash(flow),))
            db.execute("DELETE FROM reader_apple_pending_links WHERE flow_hash=?", (_hash(flow),))
            db.commit()
        session.clear()
        return jsonify(ok=True)

    @app.post("/login/apple/complete")
    def reader_apple_complete():
        body = request_body()
        flow = session.get("reader_apple_flow", "")
        if body is None or not flow:
            return response_error("登录已过期，请重试")
        db = get_db()
        state = str(body.get("state", ""))[:200]
        row = db.execute("SELECT * FROM reader_apple_challenges WHERE state = ?", (state,)).fetchone()
        if not row or row["expires_at"] < time.time() or not secrets.compare_digest(row["flow_hash"], _hash(flow)):
            return response_error("登录已过期，请重试")
        if row["user_id"] != session.get("user_id"):
            return response_error("当前账户已改变，请重新登录", 409)
        try:
            subject = verify_identity(body.get("identity_token"), row["nonce_hash"])
        except (jwt.PyJWTError, ValueError, TypeError):
            return response_error("无法验证 Apple 登录，请重试", 401)
        try:
            db.execute("BEGIN IMMEDIATE")
            consumed = db.execute("DELETE FROM reader_apple_challenges WHERE state = ?", (state,)).rowcount
            if consumed != 1:
                db.rollback()
                return response_error("本次登录已使用，请重试", 409)
            linked = db.execute("SELECT users.* FROM reader_apple_identities a JOIN users ON users.id=a.user_id WHERE a.subject=?", (subject,)).fetchone()
            if linked:
                if row["user_id"] is not None and linked["id"] != row["user_id"]:
                    db.commit()
                    return response_error("这个 Apple 账户已关联其他 Reader 账户", 409)
                db.commit()
                return establish(linked)
            if row["user_id"] is not None:
                db.execute("INSERT INTO reader_apple_identities VALUES (?, ?, ?)", (subject, row["user_id"], int(time.time())))
                account = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
                db.commit()
                return establish(account)
            ticket = secrets.token_urlsafe(32)
            db.execute("INSERT INTO reader_apple_pending_links (ticket_hash,subject,flow_hash,expires_at) VALUES (?,?,?,?)",
                       (_hash(ticket), subject, _hash(flow), int(time.time()) + 600))
            db.commit()
            return jsonify(ok=True, link_required=True, ticket=ticket)
        except sqlite3.IntegrityError:
            db.rollback()
            return response_error("此账户已有关联，请使用原来的 Apple 账户登录", 409)

    @app.post("/login/apple/link")
    def reader_apple_link():
        body = request_body()
        flow = session.get("reader_apple_flow", "")
        if body is None or not flow:
            return response_error("关联已过期，请重新使用 Apple 登录")
        ticket = str(body.get("ticket", ""))[:200]
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        invite_token = str(body.get("invite", "")).strip()
        if len(username) > 32 or len(password) > 1024 or len(invite_token) > 200:
            return response_error("账户信息无效")
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT * FROM reader_apple_pending_links WHERE ticket_hash=?", (_hash(ticket),)).fetchone()
            if not pending or pending["expires_at"] < time.time() or pending["attempts"] >= 5 or not secrets.compare_digest(pending["flow_hash"], _hash(flow)):
                db.rollback()
                return response_error("关联已过期，请重新使用 Apple 登录", 401)
            db.execute("UPDATE reader_apple_pending_links SET attempts=attempts+1 WHERE ticket_hash=?", (_hash(ticket),))
            account = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if invite_token:
                invite = db.execute("SELECT * FROM invites WHERE token=? AND used_by IS NULL AND (expires_at IS NULL OR expires_at > datetime('now'))", (invite_token,)).fetchone()
                if not invite or account or not 2 <= len(username) <= 32 or not all(c.isalnum() or c in "_-" for c in username):
                    db.commit()
                    return response_error("邀请码或用户名无效")
                # A random unshared password prevents a second password login path.
                cur = db.execute("INSERT INTO users(username,password_hash) VALUES (?,?)", (username, generate_password_hash(secrets.token_urlsafe(48))))
                db.execute("UPDATE invites SET used_by=?,used_at=datetime('now') WHERE token=?", (cur.lastrowid, invite_token))
                account = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            elif not account or not check_password_hash(account["password_hash"], password):
                db.commit()
                return response_error("用户名或密码错误", 401)
            db.execute("INSERT INTO reader_apple_identities VALUES (?,?,?)", (pending["subject"], account["id"], int(time.time())))
            db.execute("DELETE FROM reader_apple_pending_links WHERE ticket_hash=?", (_hash(ticket),))
            db.commit()
            return establish(account)
        except sqlite3.IntegrityError:
            db.rollback()
            return response_error("Apple 或 Reader 账户已经关联，请重新登录", 409)
