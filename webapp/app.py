import os
import json
import datetime
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, send_from_directory, jsonify, abort,
)
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.secret_key        = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = datetime.timedelta(days=30)

USERNAME      = "bwicarus"
PASSWORD_HASH = os.environ["PASSWORD_HASH"]

PROTECTED = ("/dashboard", "/private", "/history")


@app.before_request
def require_login():
    if request.path.startswith(("/login", "/static")):
        return
    for prefix in PROTECTED:
        if request.path.startswith(prefix):
            if not session.get("logged_in"):
                return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        user = request.form.get("username", "")
        pwd  = request.form.get("password", "")
        if user == USERNAME and check_password_hash(PASSWORD_HASH, pwd):
            session.permanent = True
            session["logged_in"] = True
            return redirect(request.args.get("next") or "/dashboard/")
        error = "用户名或密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard/")
@app.route("/dashboard/<path:filename>")
def dashboard(filename="index.html"):
    return send_from_directory("/root/webapp/data/dashboard", filename)


GRAPH_SETTINGS_FILE = Path("/root/webapp/data/dashboard/graph-settings.json")


@app.route("/dashboard/graph-settings.json", methods=["GET", "POST"])
def graph_settings():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            abort(400)
        GRAPH_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        GRAPH_SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return jsonify({"ok": True})
    if GRAPH_SETTINGS_FILE.exists():
        try:
            return jsonify(json.loads(GRAPH_SETTINGS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return jsonify({})


@app.route("/private/")
@app.route("/private/<path:filename>")
def private(filename="index.html"):
    return send_from_directory("/root/webapp/data/private", filename)


@app.route("/history/")
@app.route("/history/<path:filename>")
def history_page(filename="index.html"):
    return send_from_directory("/root/webapp/data/history", filename)


DATA_DIR = Path(os.environ.get("WEBAPP_DATA", "/root/webapp/data"))
QA_FILE  = DATA_DIR / "qa.json"


@app.route("/qa/")
@app.route("/qa")
def qa():
    if not session.get("logged_in"):
        return redirect(url_for("login", next="/qa/"))
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
