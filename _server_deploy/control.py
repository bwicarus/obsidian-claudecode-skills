"""控制面板（替代 Windows 客户端 EXE）

部署位置：/root/webapp/control.py
注册：app.py 末尾 `from control import register_control; register_control(app)`
      （必须在 `if __name__ == "__main__":` 之前）

提供 /control/ 网页 + /control/api/* 后台 API。
鉴权由 app.py 的 @before_request require_login_global 处理
（PROTECTED_PREFIXES 必须含 "/control"）。

源码在 git 仓库 _server_deploy/control.py，部署时 scp 到 /root/webapp/。
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from flask import jsonify, render_template, request

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/root/claude"))

# scripts/ 加入路径，让 config_schema 等业务模块能 import
sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
from config_schema import validate_partial, schema_for_ui  # noqa: E402
import task_tracker  # noqa: E402
LAST_RUN = CLAUDE_DIR / "state" / "last_run.json"
AI_SETTINGS = CLAUDE_DIR / "state" / "ai-settings.json"
SERVER_CONFIG = CLAUDE_DIR / "state" / "server-config.json"
PYTHON = os.environ.get("APP_PYTHON", "/usr/bin/python3")
ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")


def load_claude_env() -> dict:
    """合并 os.environ 和 /root/claude/.env"""
    env = os.environ.copy()
    env_file = CLAUDE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def get_systemd_statuses(units) -> dict:
    """一次 systemctl 查全部 unit（原先每 unit 各 spawn 一次，轮询路径上 5 次/请求）。"""
    if sys.platform == "win32":
        return {u: "n/a" for u in units}   # 本地实例(Windows)无 systemd:xvfb/anki-headless/obsidian-sync/qa-server/tailscaled 是 Pi 专属服务,本地不适用
    try:
        # 不看 returncode:任一 unit 非 active 整体就非零;stdout 每行对应一个参数顺序的 unit
        r = subprocess.run(
            ["systemctl", "is-active", *units],
            capture_output=True, text=True, timeout=3,
        )
        lines = r.stdout.splitlines()
        return {u: ((lines[i].strip() or "unknown") if i < len(lines) else "unknown")
                for i, u in enumerate(units)}
    except Exception as e:
        return {u: f"err:{e}" for u in units}


def ankiconnect_version():
    body = json.dumps({"action": "version", "version": 6}).encode()
    try:
        req = urllib.request.Request(
            ANKI_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            return data.get("result")
    except Exception:
        return None


# ── /api/status 慢字段缓存(5s TTL + 单飞) ────────────────────
# 面板 fast-poll 500ms 一发,systemctl spawn + AnkiConnect ping 每发都跑会吃满 CPU;
# active_tasks / last_run 不进缓存(任务进度依赖新鲜度,且读文件便宜)。
_SYSTEMD_UNITS = ("anki-headless", "xvfb-99", "obsidian-sync", "qa-server", "tailscaled")
_STATUS_TTL = 5.0
_status_cache = {"ts": 0.0, "data": None}
_status_lock = threading.Lock()


def _expensive_status() -> dict:
    st = get_systemd_statuses(_SYSTEMD_UNITS)
    return {
        "ankiconnect_version": ankiconnect_version(),
        "anki_headless": st["anki-headless"],
        "xvfb_99": st["xvfb-99"],
        "obsidian_sync": st["obsidian-sync"],
        "qa_server": st["qa-server"],
        "tailscaled": st["tailscaled"],
    }


def get_status_cached() -> dict:
    """TTL 内直接回缓存;过期由抢到锁的请求重算,其余并发请求拿旧值不阻塞(单飞)。"""
    now = time.monotonic()
    if _status_cache["data"] is not None and now - _status_cache["ts"] < _STATUS_TTL:
        return _status_cache["data"]
    if _status_lock.acquire(blocking=False):
        try:
            data = _expensive_status()
            _status_cache["data"] = data
            _status_cache["ts"] = time.monotonic()
            return data
        finally:
            _status_lock.release()
    if _status_cache["data"] is not None:
        return _status_cache["data"]
    # 冷启动且别的线程正在重算:等它算完拿结果
    with _status_lock:
        return _status_cache["data"] or _expensive_status()


def get_ai_backend() -> str:
    if not AI_SETTINGS.exists():
        return "auto-claude"
    try:
        data = json.loads(AI_SETTINGS.read_text(encoding="utf-8"))
        return data.get("backend", "auto-claude")
    except Exception:
        return "auto-claude"


def get_last_run():
    if not LAST_RUN.exists():
        return None
    try:
        return json.loads(LAST_RUN.read_text(encoding="utf-8"))
    except Exception:
        return None


def _trigger_script(script: str, args=None) -> None:
    """异步 spawn 一个 script，立即返回。"""
    args = args or []
    log_dir = CLAUDE_DIR / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "webapp_trigger.log"
    with log_file.open("a", encoding="utf-8") as logf:
        from datetime import datetime
        logf.write(f"\n=== [{datetime.now().isoformat(timespec='seconds')}] {script} {args} ===\n")
        logf.flush()
        subprocess.Popen(
            [PYTHON, str(CLAUDE_DIR / "scripts" / script), *args],
            cwd=str(CLAUDE_DIR),
            env=load_claude_env(),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def register_control(app):
    """注册 control 相关路由到 Flask app。"""

    @app.route("/control/")
    def control_home():
        return render_template("control.html")

    @app.route("/control/api/status")
    def control_status():
        # 慢字段(systemctl + AnkiConnect ping)走 5s 缓存,快字段每次现读
        out = dict(get_status_cached())
        out.update({
            "ai_backend": get_ai_backend(),
            "last_run": get_last_run(),
            "active_tasks": task_tracker.read_snapshot(),
        })
        return jsonify(out)

    @app.route("/control/api/config/schema")
    def control_config_schema():
        """前端按这个 schema 动态渲染设置 panel 字段，避免硬编码 cfg-path 跟
        scripts/config_schema.py 双写。"""
        return jsonify(schema_for_ui())

    @app.route("/control/api/ipad-config")
    def control_ipad_config():
        """返回 iPad 快捷指令需要的 host + cmd_server API key + 可触发命令清单。
        前端把这些拼成完整 URL 模板供用户复制到快捷指令的「获取 URL 内容」动作。"""
        key_file = CLAUDE_DIR / "state" / "qa-server-data" / "cmd_server_key.txt"
        api_key = key_file.read_text().strip() if key_file.exists() else ""

        # 优先用 Tailscale DNS 名（跨设备稳定），fallback 到请求 Host
        ts_dns = ""
        try:
            r = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=3,
            )
            d = json.loads(r.stdout)
            ts_dns = (d.get("Self") or {}).get("DNSName", "").rstrip(".")
        except Exception:
            pass

        return jsonify({
            "host":     ts_dns or (request.host or "").split(":")[0],
            "port_cmd": 9090,    # cmd_server (/run, /qa, /list)
            "port_qa":  9091,    # qa_browser daemon（截图问答对话页）
            "api_key":  api_key,
            "commands": [
                {"name": "register",     "desc": "登记新笔记（单步，不含必复习）"},
                {"name": "daily",        "desc": "完整 daily pipeline（10 步）"},
                {"name": "upload",       "desc": "刷新并上传仪表盘"},
                {"name": "ankiweb-sync", "desc": "触发 AnkiWeb 同步"},
            ],
        })

    @app.route("/control/api/config", methods=["GET", "POST"])
    def control_config():
        """GET 返回 server-config.json；POST 写入 + 重启 qa-server 让新 cfg 生效。"""
        if request.method == "GET":
            if not SERVER_CONFIG.exists():
                return jsonify({})
            try:
                return jsonify(json.loads(SERVER_CONFIG.read_text(encoding="utf-8")))
            except Exception as e:
                return jsonify({"_error": str(e)})

        # POST: schema 校验过滤后深度合并请求 JSON 到现有 config
        raw = request.get_json(silent=True) or {}
        if not isinstance(raw, dict):
            return jsonify({"ok": False, "msg": "需要 JSON 对象"}), 400

        # 过滤未声明字段 + 类型不匹配的字段（拼错字段名不再静默生效）
        new_cfg, errors = validate_partial(raw)

        # 加载现有 + deep merge
        existing = {}
        if SERVER_CONFIG.exists():
            try:
                existing = json.loads(SERVER_CONFIG.read_text(encoding="utf-8"))
            except Exception:
                pass

        def deep_merge(a, b):
            out = dict(a)
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = deep_merge(out[k], v)
                else:
                    out[k] = v
            return out

        merged = deep_merge(existing, new_cfg)
        SERVER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        SERVER_CONFIG.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 重启 qa-server 让新 cfg 生效（qa_browser daemon 每次请求都重新 get_cfg() 其实不需要重启，
        # 但是 qa_remote_daemon 控制 bind 行为，所以保险起见重启）
        if "qa_remote_daemon" in new_cfg or "qa_vault_path" in new_cfg:
            subprocess.run(["systemctl", "restart", "qa-server"], check=False)
            _status_cache["ts"] = 0   # 重启 qa-server 后失效缓存,状态点尽快反映

        msg = "配置已保存"
        if errors:
            msg += f"（{len(errors)} 个字段被拒）"
        return jsonify({
            "ok":      True,
            "msg":     msg,
            "config":  merged,
            "errors":  errors,   # 前端可选展示
        })

    @app.route("/control/api/concept-net", methods=["GET", "POST"])
    def control_concept_net():
        """概念网(阅读时生长)开关:gating_enabled + 按书开火,存 note-codes.json;
        GET 附 concept-graph.timer 状态。改动即存(独立于 server-config 的保存按钮)。"""
        codes = CLAUDE_DIR / "state" / "attention" / "note-codes.json"
        try:
            reg = json.loads(codes.read_text(encoding="utf-8"))
        except Exception:
            reg = {"books": {}, "codes": {}}
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if "gating_enabled" in body:
                reg["gating_enabled"] = bool(body["gating_enabled"])
            for rel, on in (body.get("books") or {}).items():
                if rel.startswith("vbook:"):
                    # 合并视图里开火:vbook 引用 → 解析到真实成员(注册表身份=canonical,绝不存 vbook 键)
                    try:
                        sys.path.insert(0, str(CLAUDE_DIR / "scripts" / "lib"))
                        import vbook as _VB
                        rel = _VB.get(rel)["members"][0]["rel"]
                    except Exception:
                        continue
                if rel not in reg.get("books", {}) and body.get("register"):
                    # 阅读器里首次对未登记书开火 → 自动分配编码(同 propose 的分配规则)
                    try:
                        from kg_runtime import (
                            import_module as _import_kg_module,
                        )
                        _PCN = _import_kg_module(
                            "propose_concept_notes"
                        )
                        reg.setdefault("books", {})
                        reg.setdefault("codes", {})
                        _PCN._book_entry(reg, rel, create=True)
                    except Exception:
                        continue
                if rel in reg.get("books", {}):
                    reg["books"][rel]["enabled"] = bool(on)
            codes.parent.mkdir(parents=True, exist_ok=True)
            tmp = codes.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(codes)
            return jsonify({"ok": True})
        def _sysctl(cmd):
            try:
                return subprocess.run(["systemctl", cmd, "concept-graph.timer"],
                                      capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception:
                return "?"
        return jsonify({
            "gating_enabled": bool(reg.get("gating_enabled")),
            "timer": {"active": _sysctl("is-active"), "enabled": _sysctl("is-enabled")},
            "books": [{"rel": rel, "code": b.get("code", ""), "subject": b.get("subject", ""),
                       "enabled": bool(b.get("enabled"))} for rel, b in (reg.get("books") or {}).items()],
        })

    @app.route("/control/api/kg-audit")
    def control_kg_audit():
        """列出 KG 书本 + 当前每本审查开关（给控制面板渲染 per-book 开关）。"""
        cfg = {}
        if SERVER_CONFIG.exists():
            try:
                cfg = json.loads(SERVER_CONFIG.read_text(encoding="utf-8"))
            except Exception:
                pass
        ka = cfg.get("kg_audit") or {}
        default_on = bool(ka.get("default", True))
        books_cfg = ka.get("books") or {}
        books = []
        kg_dir = CLAUDE_DIR / "knowledge_graph"
        if kg_dir.exists():
            for f in sorted(kg_dir.glob("*.json")):
                if f.name.endswith(".bak.json"):
                    continue
                nodes = 0
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    nodes = sum(1 for n in d.get("nodes", []) if n.get("level") == 2)
                except Exception:
                    pass
                books.append({"book": f.stem, "on": bool(books_cfg.get(f.stem, default_on)), "nodes": nodes})
        return jsonify({
            "enabled":     bool(ka.get("enabled", True)),
            "incremental": bool(ka.get("incremental", True)),
            "default":     default_on,
            "books":       books,
        })

    @app.route("/control/api/quota-log")
    def control_quota_log():
        """返回 state/quota_log.json 最近 N 条；?limit=80&ai_only=true 过滤。"""
        limit = int(request.args.get("limit", 80))
        ai_only = request.args.get("ai_only", "").lower() in ("1", "true", "yes")
        p = CLAUDE_DIR / "state" / "quota_log.json"
        if not p.exists():
            return jsonify({"entries": [], "count": 0, "total": 0})
        try:
            log = json.loads(p.read_text(encoding="utf-8"))
        except Exception as ex:
            return jsonify({"entries": [], "error": str(ex), "total": 0})
        entries = log.get("entries") or []
        total = len(entries)
        if ai_only:
            entries = [e for e in entries if e.get("ai_intensive")]
        entries = entries[-limit:][::-1]
        return jsonify({"entries": entries, "count": len(entries), "total": total})

    @app.route("/control/api/quota-now")
    def control_quota_now():
        """实时查 Claude 额度（轻量代理）。"""
        try:
            import sys as _sys
            _sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
            from lib.claude_quota import fetch_quota
            q = fetch_quota(cache_ttl=15)
            return jsonify({
                "ok": True,
                "five_hour":        (q.get("five_hour")        or {}).get("utilization"),
                "seven_day":        (q.get("seven_day")        or {}).get("utilization"),
                "seven_day_sonnet": (q.get("seven_day_sonnet") or {}).get("utilization"),
                "seven_day_opus":   (q.get("seven_day_opus")   or {}).get("utilization"),
                "five_hour_resets_at": (q.get("five_hour") or {}).get("resets_at"),
                "seven_day_resets_at": (q.get("seven_day") or {}).get("resets_at"),
            })
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500

    @app.route("/control/api/gemini-cost")
    def control_gemini_cost():
        """Gemini 累计估算花费(USD,按每条记录的模型单价算输入/输出)。Gemini 无查余额 API,这是用量估算。"""
        try:
            import sys as _sys
            _sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
            import importlib, google_api_quota as _q
            importlib.reload(_q)
            return jsonify({"ok": True, "total": _q.gemini_cost(0), "last30d": _q.gemini_cost(30)})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500

    @app.route("/control/api/doubao-cost")
    def control_doubao_cost():
        """豆包 S2S 语音通话估算花费(元;relay 收 154 UsageResponse 按天累计到 state/doubao-usage.json)。"""
        try:
            p = CLAUDE_DIR / "state" / "doubao-usage.json"
            if not p.exists():
                return jsonify({"ok": True, "total_cost_est": 0, "days": {}})
            data = json.loads(p.read_text("utf-8"))
            days = data.get("days") or {}
            import datetime as _dt
            cut = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
            last30 = round(sum(v.get("cost_est", 0) for k, v in days.items() if k >= cut), 4)
            recent = dict(sorted(days.items())[-7:])
            return jsonify({"ok": True, "total_cost_est": data.get("total_cost_est", 0),
                            "last30d_cost_est": last30, "recent_days": recent})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500

    @app.route("/control/api/trigger-log")
    def control_trigger_log():
        """返回 webapp_trigger.log 末 N 行（追踪 trigger 子进程的实时输出）。"""
        log_file = CLAUDE_DIR / "state" / "logs" / "webapp_trigger.log"
        lines = int(request.args.get("lines", 80))
        if not log_file.exists():
            return jsonify({"lines": [], "size": 0})
        try:
            with log_file.open("rb") as f:
                # 读末尾 ~64KB（足够拿到 80 行）
                f.seek(0, 2)
                size = f.tell()
                read_bytes = min(size, 65536)
                f.seek(size - read_bytes)
                data = f.read().decode("utf-8", errors="replace")
            log_lines = data.splitlines()[-lines:]
            return jsonify({"lines": log_lines, "size": size})
        except Exception as e:
            return jsonify({"lines": [f"读 log 失败: {e}"], "size": 0})

    @app.route("/control/api/trigger/<action>", methods=["POST"])
    def control_trigger(action):
        _status_cache["ts"] = 0   # 触发类操作可能改服务状态,主动失效缓存让状态点尽快刷新
        if action == "register":
            _trigger_script("register_notes.py")
            return jsonify({"ok": True, "msg": "register_notes 已启动（后台跑，日志在 state/logs/webapp_trigger.log）"})
        elif action == "daily":
            _trigger_script("daily_anki_status.py")
            return jsonify({"ok": True, "msg": "daily 流程已启动（看 state/last_run.json 实时进度）"})
        elif action == "anki-restart":
            subprocess.run(["systemctl", "restart", "anki-headless"], check=False)
            return jsonify({"ok": True, "msg": "anki-headless 已重启（等 20s AnkiConnect 上线）"})
        elif action == "ankiweb-sync":
            body = json.dumps({"action": "sync", "version": 6}).encode()
            try:
                req = urllib.request.Request(
                    ANKI_URL, data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read())
                    if data.get("error"):
                        return jsonify({"ok": False, "msg": f"sync 失败: {data['error']}"})
                    return jsonify({"ok": True, "msg": "AnkiWeb 同步已触发"})
            except Exception as e:
                return jsonify({"ok": False, "msg": f"sync 异常: {e}"})
        elif action == "switch-ai":
            data = request.get_json(silent=True) or {}
            target = data.get("target", "claude")
            if target not in ("claude", "gpt"):
                return jsonify({"ok": False, "msg": "target 必须是 claude 或 gpt"}), 400
            r = subprocess.run(
                [PYTHON, str(CLAUDE_DIR / "scripts" / "service_switch.py"), "switch", target],
                capture_output=True, text=True, env=load_claude_env(), timeout=10,
            )
            return jsonify({"ok": r.returncode == 0, "msg": (r.stdout or r.stderr).strip()})
        else:
            return jsonify({"ok": False, "msg": f"未知 action: {action}"}), 400

    # ──── 新建书本：spawn build_nodes + extract_edges 后台串行 ────
    _kg_build_jobs = {}   # job_id → {status, log_file, proc}

    @app.route("/control/api/kg-build", methods=["POST"])
    def control_kg_build():
        body = request.get_json(silent=True) or {}
        book_id = (body.get("book_id") or "").strip()
        title   = (body.get("title") or "").strip() or book_id
        prefix  = (body.get("note_prefix") or "").strip()
        pdf_rel = (body.get("pdf") or "").strip()
        pages   = (body.get("pages") or "").strip()
        model   = (body.get("model") or "sonnet").strip()
        effort  = (body.get("effort") or "medium").strip()
        if not book_id or not pdf_rel:
            return jsonify({"ok": False, "error": "缺 book_id / pdf"}), 400
        import re as _re
        if not _re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", book_id):
            return jsonify({"ok": False, "error": "book_id 格式非法"}), 400
        kg_path = CLAUDE_DIR / "knowledge_graph" / f"{book_id}.json"
        if kg_path.exists():
            return jsonify({"ok": False, "error": f"{book_id}.json 已存在"}), 400
        vault_root = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
        pdf_abs = (vault_root / pdf_rel.lstrip("/")).resolve()
        try:
            pdf_abs.relative_to(vault_root.resolve())
        except ValueError:
            return jsonify({"ok": False, "error": "PDF 路径越界"}), 400
        if not pdf_abs.exists():
            return jsonify({"ok": False, "error": "PDF 不存在"}), 400
        try:
            from kg_runtime import runtime_file as _kg_runtime_file

            build_nodes_script = _kg_runtime_file("scripts/kg/build_nodes.py")
            extract_edges_script = _kg_runtime_file("scripts/kg/extract_edges.py")
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"KG runtime 不可用: {str(exc)[:160]}",
            }), 503
        import uuid, time
        job_id = uuid.uuid4().hex[:8]
        log_dir = CLAUDE_DIR / "state" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"kg_build_{book_id}_{job_id}.log"

        def _run():
            env = load_claude_env()
            with log_file.open("w", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] === build {book_id} ===\n"); f.flush()
                # Step 1: build_nodes
                args = [PYTHON, "-u", str(build_nodes_script),
                        "--pdf", str(pdf_abs), "--book", book_id,
                        "--model", model, "--effort", effort]
                if pages: args += ["--pages", pages]
                f.write(f"[{time.strftime('%H:%M:%S')}] ▶ build_nodes {' '.join(args[2:])}\n"); f.flush()
                rc1 = subprocess.run(args, cwd=str(CLAUDE_DIR), env=env,
                                     stdout=f, stderr=subprocess.STDOUT).returncode
                f.write(f"\n[{time.strftime('%H:%M:%S')}] build_nodes rc={rc1}\n"); f.flush()
                if rc1 == 0 and kg_path.exists():
                    # Step 2: 写 note_prefix / title
                    try:
                        kg = json.loads(kg_path.read_text(encoding="utf-8"))
                        if prefix: kg["note_prefix"] = prefix
                        if title and title != book_id: kg["title"] = title
                        kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
                        f.write(f"[{time.strftime('%H:%M:%S')}] ✓ 写 note_prefix={prefix} title={title}\n"); f.flush()
                    except Exception as e:
                        f.write(f"[{time.strftime('%H:%M:%S')}] ⚠ meta 写入失败: {e}\n"); f.flush()
                    # Step 3: extract_edges
                    args2 = [PYTHON, "-u", str(extract_edges_script),
                             "--kg", str(kg_path), "--in-place"]
                    f.write(f"\n[{time.strftime('%H:%M:%S')}] ▶ extract_edges\n"); f.flush()
                    rc2 = subprocess.run(args2, cwd=str(CLAUDE_DIR), env=env,
                                         stdout=f, stderr=subprocess.STDOUT).returncode
                    f.write(f"\n[{time.strftime('%H:%M:%S')}] extract_edges rc={rc2}\n"); f.flush()
                    _kg_build_jobs[job_id]["status"] = "done" if (rc1 == 0 and rc2 == 0) else "failed"
                else:
                    _kg_build_jobs[job_id]["status"] = "failed"
                f.write(f"\n[{time.strftime('%H:%M:%S')}] === 结束 ({_kg_build_jobs[job_id]['status']}) ===\n")

        _kg_build_jobs[job_id] = {"status": "running", "log_file": str(log_file)}
        import threading
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "log_file": log_file.name})

    @app.route("/control/api/kg-build-log")
    def control_kg_build_log():
        job_id = request.args.get("job", "").strip()
        job = _kg_build_jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "unknown job", "status": "unknown", "log": ""})
        log = ""
        try:
            p = Path(job["log_file"])
            if p.exists():
                log = p.read_text(encoding="utf-8", errors="replace")
                if len(log) > 16384:
                    log = log[-16384:]   # 末尾 16KB 够看
        except Exception as e:
            log = f"读 log 失败: {e}"
        return jsonify({"ok": True, "status": job["status"], "log": log})
