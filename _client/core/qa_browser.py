#!/usr/bin/env python3
"""
截图问答器：静默截图 → 浏览器对话（Markdown + 数学公式）→ 习题笔记（双向链接）
依赖：pip install Pillow
"""

import sys, io, os, time, hashlib, ctypes, subprocess, threading, json, base64, socket, webbrowser, shutil, re, sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageGrab

# 客户端版：用 BackendAdapter 替代原 launchers/截图问答.py 里的 ai_client
from ai_backends import make_backend  # type: ignore


# ── 由 launch() 入口设置的全局 cfg getter，HTTP server 子线程通过它拿当前 backend ──
_GET_CFG: "callable" = lambda: {}


def _adapter_ask(prompt: str, image_path: str = None) -> str:
    """单次查询（无历史）— 替代原 ai_client.ask。"""
    cfg = _GET_CFG()
    backend_name = cfg.get("ai_backend", "claude_cli")
    settings = (cfg.get("ai") or {}).get(backend_name, {})
    ad = make_backend(backend_name, settings)
    msgs = [{"role": "user", "content": prompt}]
    image_bytes = None
    if image_path and Path(image_path).exists():
        try:
            image_bytes = Path(image_path).read_bytes()
        except Exception:
            pass
    return ad.chat(msgs, image=image_bytes)


class _AdapterSession:
    """多轮会话 — 替代原 ai_client.AISession。
    内部只维护一个 messages 列表，每次 send 都重新 import + chat（client 的 chat 是无状态的）。
    """

    def __init__(self, system_prompt: str = "你是一个问答助手。"):
        self.system_prompt = system_prompt
        self.messages: list[dict] = []   # [{"role":"user"|"assistant", "text":...}]

    def reset(self):
        self.messages = []

    def send(self, message: str, image_path: str = None) -> str:
        cfg = _GET_CFG()
        backend_name = cfg.get("ai_backend", "claude_cli")
        settings = (cfg.get("ai") or {}).get(backend_name, {})
        ad = make_backend(backend_name, settings)

        msgs = [{"role": "system", "content": self.system_prompt}]
        for m in self.messages:
            role = m.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            msgs.append({"role": role, "content": m.get("text", "")})
        msgs.append({"role": "user", "content": message})

        image_bytes = None
        if image_path and Path(image_path).exists():
            try:
                image_bytes = Path(image_path).read_bytes()
            except Exception:
                pass

        reply = ad.chat(msgs, image=image_bytes)
        self.messages.append({"role": "user", "text": message})
        self.messages.append({"role": "assistant", "text": reply})
        return reply

    def send_stream(self, message: str, image_path: str = None):
        """流式版 send：yield text chunks。结束（含 cancel / error）后把已生成内容
        commit 到 self.messages，保证历史跟显示一致。"""
        cfg = _GET_CFG()
        backend_name = cfg.get("ai_backend", "claude_cli")
        settings = (cfg.get("ai") or {}).get(backend_name, {})
        ad = make_backend(backend_name, settings)

        msgs = [{"role": "system", "content": self.system_prompt}]
        for m in self.messages:
            role = m.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            msgs.append({"role": role, "content": m.get("text", "")})
        msgs.append({"role": "user", "content": message})

        image_bytes = None
        if image_path and Path(image_path).exists():
            try:
                image_bytes = Path(image_path).read_bytes()
            except Exception:
                pass

        chunks: list[str] = []
        gen = ad.chat_stream(msgs, image=image_bytes)
        try:
            for chunk in gen:
                if chunk:
                    chunks.append(chunk)
                    yield chunk
        finally:
            # 关掉底层 generator（触发其 GeneratorExit → 子进程清理）
            try: gen.close()
            except Exception: pass
            # commit：即使被 cancel，已 yield 出去的部分写进历史
            final = "".join(chunks)
            if final or message:
                self.messages.append({"role": "user", "text": message})
                self.messages.append({"role": "assistant", "text": final})


# 让原文件中所有 `ai_client.xxx` 引用走我们的 wrapper
class _AiClientShim:
    AISession = _AdapterSession

    @staticmethod
    def ask(prompt: str, image_path: str = None) -> str:
        return _adapter_ask(prompt, image_path=image_path)

    @staticmethod
    def load_settings() -> dict:
        cfg = _GET_CFG()
        return {"backend": cfg.get("ai_backend", "claude_cli"), "model": ""}

    @staticmethod
    def save_settings(data: dict) -> dict:
        # 客户端 GUI 的 AI Tab 才是 backend 的真源；这里 noop，回显当前
        return _AiClientShim.load_settings()

ai_client = _AiClientShim()

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
CHROME_EXE = next((p for p in _CHROME_CANDIDATES if Path(p).exists()), None)

# ─── 路径配置（由 launch() 时根据 client cfg 设置） ─────────────────────────────

# 兜底：launch() 之前用全局默认（双击 .exe 但没配 vault 时也别让 import 炸）
_APP_LOCAL    = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
_CLIENT_HOME  = _APP_LOCAL / "bwicarus-client"

VAULT         = None              # 必填：用户的 Obsidian vault
EXERCISES_DIR = None              # vault/<qa_exercises_subdir>，默认 "习题"
WRONG_DIR     = None              # vault/<qa_wrong_subdir>，默认 "错题"
ASSETS_DIR    = None              # vault/习题/assets
INDEX_DIR     = None              # 可空：知识索引目录（笔记 frontmatter 摘要 .md）
ANKI_RECORDS_DIR = None           # 可空：anki/records/*.json（用于错题分类时附 anki 列表）
ANKI_URL         = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
TEMP_DIR      = _CLIENT_HOME / "qa-temp"      # 客户端本地：截图临时（_init_paths 里改用 STORE_DIR）


def _detect_vault() -> Path | None:
    """vault 路径自动探测：env OBSIDIAN_VAULT 优先；否则扫常见 Obsidian 位置。"""
    home = Path.home()
    env_vault = os.environ.get("OBSIDIAN_VAULT")
    candidates = []
    if env_vault:
        candidates.append(Path(env_vault))
    candidates += [
        Path(r"C:\obsidian"),
        home / "Documents" / "Obsidian Vault",
        home / "Documents" / "obsidian",
        home / "OneDrive" / "Documents" / "Obsidian Vault",
        home / "OneDrive" / "Obsidian Vault",
        home / "iCloudDrive" / "iCloud~md~obsidian",
    ]
    for p in candidates:
        if p.exists() and p.is_dir() and any(p.glob("*.md")):
            return p
    return None


def _resolve_subpath(vault: Path, value: str | None, default_subdir: str) -> Path:
    """解析"习题/错题"路径：
      - 空 / None → vault / <default_subdir>
      - 绝对路径 → 直接用（可在 vault 外）
      - 相对路径（含子目录名）→ vault / value
    """
    raw = (value or "").strip()
    if not raw:
        return vault / default_subdir
    p = Path(raw)
    if p.is_absolute():
        return p
    return vault / raw


def _init_paths(cfg: dict) -> None:
    """从 client cfg 初始化路径。launch() 入口处调用。"""
    global VAULT, EXERCISES_DIR, WRONG_DIR, ASSETS_DIR, INDEX_DIR, ANKI_RECORDS_DIR, ANKI_URL, TEMP_DIR

    raw_vault = (cfg.get("qa_vault_path") or cfg.get("vault_path") or "").strip()
    vault_path = Path(raw_vault) if raw_vault else _detect_vault()
    if vault_path:
        VAULT = vault_path
        EXERCISES_DIR = _resolve_subpath(VAULT, cfg.get("qa_exercises_subdir"), "习题")
        WRONG_DIR     = _resolve_subpath(VAULT, cfg.get("qa_wrong_subdir"),     "错题")
        ASSETS_DIR    = EXERCISES_DIR / "assets"
    else:
        VAULT = EXERCISES_DIR = WRONG_DIR = ASSETS_DIR = None

    raw_idx = (cfg.get("qa_index_dir") or "").strip()
    INDEX_DIR = Path(raw_idx) if raw_idx else None

    raw_anki = (cfg.get("qa_anki_records_dir") or "").strip()
    ANKI_RECORDS_DIR = Path(raw_anki) if raw_anki else None

    raw_anki_url = (cfg.get("anki_connect_url") or "").strip()
    if raw_anki_url:
        ANKI_URL = raw_anki_url

    # TEMP_DIR 跟 STORE_DIR 同根（paths.app_dir() 或 fallback），落在 qa-temp/
    TEMP_DIR = STORE_DIR / "qa-temp"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

# 历史存储：客户端走 paths.app_dir()（Windows 是 %LOCALAPPDATA%\bwicarus-client\，
# 服务端实例由 systemd 设 BWICARUS_APP_DIR 指向 state/qa-server-data/）。
# 旧位置 %LOCALAPPDATA%\截图问答\ 保留 fallback，首次启动时自动迁移。
try:
    from paths import app_dir as _app_dir  # type: ignore
    STORE_DIR = _app_dir()
except Exception:
    _localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    STORE_DIR = Path(_localappdata) / "bwicarus-client"
    STORE_DIR.mkdir(parents=True, exist_ok=True)

# 一次性迁移旧路径数据（Windows %LOCALAPPDATA%\截图问答\ 或 Pi 上同样 fallback 出来的目录）
_legacy_dirs = [
    Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")) / "截图问答",
]
for _legacy in _legacy_dirs:
    if _legacy.exists() and _legacy.resolve() != STORE_DIR.resolve():
        try:
            (STORE_DIR / "images").mkdir(parents=True, exist_ok=True)
            for src in _legacy.iterdir():
                dst = STORE_DIR / src.name
                if dst.exists():
                    continue
                if src.is_dir():
                    import shutil as _sh
                    _sh.copytree(src, dst)
                else:
                    src.replace(dst)
            print(f"[qa-server] 迁移旧数据 {_legacy} → {STORE_DIR}", flush=True)
        except Exception as _e:
            print(f"[qa-server] 迁移旧 STORE_DIR 失败：{_e}", flush=True)
        break

DB_PATH        = STORE_DIR / "history.db"
HIST_IMG_DIR   = STORE_DIR / "images"
QBTN_FILE           = STORE_DIR / "quick_btns.json"

_DEFAULT_QBTNS = ["解题思路是什么？", "这道题考察哪些知识点？", "请逐步详细解释", "有没有类似的题型？"]

def load_qbtns():
    if QBTN_FILE.exists():
        try: return json.loads(QBTN_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return _DEFAULT_QBTNS[:]

def save_qbtns(btns):
    QBTN_FILE.write_text(json.dumps(btns, ensure_ascii=False), encoding="utf-8")

POLL_INTERVAL      = 0.2
SCREENSHOT_TIMEOUT = 60

# ─── 全局状态 ──────────────────────────────────────────────────────────────────

_SESSION_PROMPT = (
    "你是一个截图问答助手。根据随附截图和对话历史回答用户问题。"
    "只回答问题本身，不要修改文件，不要运行命令，不要描述你的系统环境。"
)

state = {
    "img_b64":   None,
    "img_fname": None,
    "temp_path": None,
    "done":      threading.Event(),
    "session":   ai_client.AISession(_SESSION_PROMPT),
}

# 卡片更新后台任务：job_id -> {"status": "running"|"done", "result": {...}}
# 改成后台跑 + 前端轮询，移动端连接断了也不丢结果、不重复制卡
_card_jobs: dict = {}

# ─── 数据库 ────────────────────────────────────────────────────────────────────

def init_db():
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    HIST_IMG_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id          TEXT PRIMARY KEY,
                timestamp   TEXT NOT NULL,
                img_fname   TEXT,
                note        TEXT,
                messages    TEXT,
                record_type TEXT DEFAULT 'normal'
            )
        """)
        try:
            conn.execute("ALTER TABLE conversations ADD COLUMN record_type TEXT DEFAULT 'normal'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE conversations ADD COLUMN related_cards TEXT DEFAULT '[]'")
        except Exception:
            pass

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _export_history_to_webapp() -> None:
    """同步 SQLite 历史到 webapp 的 history 文件目录，让 /history/ 页面立刻看到。

    服务端实例（VPS / Pi）qa-server 跑这个；Windows 客户端时代由 export_history.py
    + uploader.upload_dataset("history") 走 HTTP 上传 —— 本机部署直接写文件更省事。

    通过 env `WEBAPP_HISTORY_DIR` 配置目标目录；未设则 no-op（保持向后兼容）。
    异常静默吞掉（save 主流程不应被同步失败拖累）。
    """
    dest_raw = os.environ.get("WEBAPP_HISTORY_DIR", "")
    if not dest_raw:
        return
    try:
        dest = Path(dest_raw)
        dest.mkdir(parents=True, exist_ok=True)
        with db() as conn:
            rows = conn.execute(
                "SELECT id, timestamp, img_fname, note, messages, record_type, related_cards "
                "FROM conversations ORDER BY timestamp DESC"
            ).fetchall()
        entries = []
        stats = {"total": 0, "normal": 0, "wrong": 0}
        kept_imgs: set[str] = set()
        for r in rows:
            msgs = json.loads(r["messages"] or "[]")
            rtype = r["record_type"] or "normal"
            entries.append({
                "id":            r["id"],
                "timestamp":     r["timestamp"],
                "img_fname":     r["img_fname"] or "",
                "note":          r["note"] or "",
                "msg_count":     len(msgs),
                "record_type":   rtype,
                "messages":      msgs,
                "related_cards": json.loads(r["related_cards"] or "[]"),
            })
            stats["total"] += 1
            stats[rtype] = stats.get(rtype, 0) + 1
            fname = r["img_fname"]
            if fname:
                kept_imgs.add(fname)
                src = HIST_IMG_DIR / fname
                # 历史页 <img src="${entry.img_fname}"> 是相对路径，浏览器解析为
                # /history/<fname>，所以图片放 dest 根（跟 VPS Windows-客户端
                # 时代布局一致）。
                dst = dest / fname
                if src.exists() and not dst.exists():
                    try: shutil.copy2(src, dst)
                    except Exception: pass
        (dest / "history.json").write_text(
            json.dumps({"entries": entries, "stats": stats},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 清理 dest 里 db 不再有的图片（保留 .json / .html / .css / .js / 子目录）
        for p in dest.iterdir():
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                if p.name not in kept_imgs:
                    try: p.unlink()
                    except Exception: pass
    except Exception as e:
        print(f"[qa-server] export history → webapp 失败：{e}", flush=True)


# AI 给出的相关度排序 → rank score（位置 1-4）
RELEVANCE_RANK_SCORES = [1.0, 0.7, 0.5, 0.3]


def _read_frontmatter(note_name: str) -> str | None:
    if VAULT is None:
        return None
    note_path = VAULT / f"{note_name}.md"
    if not note_path.exists():
        return None
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    return m.group(1) if m else None


def get_mastery(note_name: str):
    """从 frontmatter 读取掌握度（0-100），无数据返回 None。展示用。"""
    fm = _read_frontmatter(note_name)
    if fm is None:
        return None
    def get_int(key):
        km = re.search(rf'^{key}\s*:\s*(\d+)', fm, re.MULTILINE)
        return int(km.group(1)) if km else 0
    total = get_int("anki_total")
    if total == 0:
        return None
    return round((get_int("anki_review") + get_int("anki_learning") * 0.5) / total * 100)


def get_weakness(note_name: str) -> float:
    """1 - retention_avg（FSRS 留存率）。无 retention 数据时退化为 (relearning+new)/total。"""
    fm = _read_frontmatter(note_name)
    if fm is None:
        return 0.5
    rm = re.search(r'^anki_retention\s*:\s*([\d.]+)', fm, re.MULTILINE)
    if rm:
        try:
            return max(0.0, min(1.0, 1.0 - float(rm.group(1))))
        except ValueError:
            pass
    def get_int(key):
        km = re.search(rf'^{key}\s*:\s*(\d+)', fm, re.MULTILINE)
        return int(km.group(1)) if km else 0
    total = get_int("anki_total")
    if total == 0:
        return 0.5
    return (get_int("anki_relearning") + get_int("anki_new")) / total


def compute_proximity(match: str, candidate: str, index_notes: dict) -> float:
    """笔记图谱距离的轻量近似：关键词 Jaccard ∪ 显式 [[link]] 双向检测。

    - 同笔记返回 1.0
    - match 笔记中含 [[candidate]] 链接，返回 1.0
    - 否则返回关键词 Jaccard
    """
    if not match or match == candidate:
        return 1.0

    def _kw(name):
        raw = index_notes.get(name, {}).get("keywords", "")
        return frozenset(k.strip() for k in raw.split(",") if k.strip())

    a, b = _kw(match), _kw(candidate)
    jacc = len(a & b) / len(a | b) if (a or b) else 0.0

    if VAULT is None:
        return jacc
    match_path = VAULT / f"{match}.md"
    if match_path.exists():
        try:
            txt = match_path.read_text(encoding="utf-8", errors="replace")
            if f"[[{candidate}]]" in txt or f"[[{candidate}|" in txt:
                return 1.0
        except Exception:
            pass
    return jacc


def _find_card_context(local_id: str):
    """QA 页 ?card=<local_id> 反查：从 anki records 取卡片两面 + 来源。"""
    if not local_id or not (ANKI_RECORDS_DIR and ANKI_RECORDS_DIR.exists()):
        return None
    for fn in ANKI_RECORDS_DIR.glob("*.json"):
        try:
            rec = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in rec.get("cards") or []:
            if c.get("local_id") == local_id:
                source_note = rec.get("source_note", "")
                source_url  = rec.get("source_url", "")
                # source_url 为空时用 source_note 重建 obsidian:// 链接（同卡片 footer 那个）
                if not source_url and source_note:
                    import urllib.parse as _up
                    fp = source_note.replace("\\", "/")
                    if fp.lower().endswith(".md"):
                        fp = fp[:-3]
                    source_url = ("obsidian://open?vault="
                                  + _up.quote("Obsidian Vault", safe="")
                                  + "&file=" + _up.quote(fp, safe=""))
                return {
                    "local_id": local_id, "type": c.get("type"),
                    "front": c.get("front", ""), "back": c.get("back", ""),
                    "text": c.get("text", ""),
                    "anki_note_id": c.get("anki_note_id"),
                    "source_note": source_note,
                    "source_link": rec.get("source_link", ""),
                    "source_url": source_url,
                }
    return None


def _anki_request(action: str, params: dict | None = None, timeout: int = 10):
    """Thin AnkiConnect HTTP wrapper. Raises RuntimeError on Anki-level error."""
    import urllib.request as _req
    payload = json.dumps({"action": action, "version": 6,
                          "params": params or {}}).encode()
    req = _req.Request(ANKI_URL, data=payload,
                       headers={"Content-Type": "application/json"})
    with _req.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("result")


def _qa_setting(dotted: str, default=None):
    """读 state/server-config.json 里的点分键（如 card_qa.delete_original）。"""
    proj = os.environ.get("CLAUDE_PROJECT", "")
    cfg_file = Path(proj) / "state" / "server-config.json" if proj else None
    if not cfg_file or not cfg_file.exists():
        return default
    try:
        cur = json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception:
        return default
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _server_config_path() -> "Path | None":
    proj = os.environ.get("CLAUDE_PROJECT", "")
    return (Path(proj) / "state" / "server-config.json") if proj else None


def _load_ai_settings_for_ui() -> dict:
    """给 ⚙ 弹窗回显：当前 backend + 各 CLI 后端的 model/effort/command。"""
    cfg = _GET_CFG() or {}
    ai = cfg.get("ai") or {}
    claude = ai.get("claude_cli") or {}
    codex  = ai.get("codex_cli") or {}
    return {
        "backend": cfg.get("ai_backend", "claude_cli"),
        "claude": {"model": claude.get("model", ""), "effort": claude.get("effort", "")},
        "codex":  {"model": codex.get("model", "")},
        "delete_original": bool((cfg.get("card_qa") or {}).get("delete_original", False)),
    }


def _save_ai_settings_from_ui(body: dict) -> dict:
    """服务器模式：把 ⚙ 弹窗的 AI 设置写回 server-config.json。"""
    cfg_path = _server_config_path()
    if not cfg_path:
        return {"ok": False, "error": "无 server-config 路径"}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    backend = (body.get("backend") or "").strip()
    if backend in ("claude_cli", "codex_cli", "claude_api", "openai_api", "ollama"):
        cfg["ai_backend"] = backend
    ai = cfg.setdefault("ai", {})
    cl = body.get("claude") or {}
    if isinstance(cl, dict):
        c = ai.setdefault("claude_cli", {})
        c["model"]  = (cl.get("model") or "").strip()
        eff = (cl.get("effort") or "").strip().lower()
        c["effort"] = eff if eff in ("low", "medium", "high", "xhigh", "max") else ""
    cx = body.get("codex") or {}
    if isinstance(cx, dict):
        c = ai.setdefault("codex_cli", {})
        c["model"] = (cx.get("model") or "").strip()
    if "delete_original" in body:
        cfg.setdefault("card_qa", {})["delete_original"] = bool(body.get("delete_original"))
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **_load_ai_settings_for_ui()}


def _footer_html(local_id: str, source_link: str, source_url: str, reason: str) -> str:
    """重建卡片 footer（来源/原因/Local ID/QA 链接），镜像 anki_from_note.source_footer。"""
    import html as _html
    lines = [
        f'来源：<a href="{_html.escape(source_url, quote=True)}">{_html.escape(source_link)}</a>',
        f"原因：{_html.escape(reason)}",
        f"Local ID：{_html.escape(local_id)}",
    ]
    qa = os.environ.get("QA_PUBLIC_URL", "").rstrip("/")
    if qa:
        lines.append(f'<a href="{qa}/?card={local_id}">问 AI / 改进这张卡</a>')
    return '<hr><div style="font-size:0.85em;color:#666;">' + "<br>".join(lines) + "</div>"


def _override_section_hashes(rec: dict) -> bool:
    """据源笔记当前内容重算并覆盖 rec 的 section_hashes（镜像 anki_from_note 算法，不剥 frontmatter）。"""
    source_note = rec.get("source_note", "")
    if not (source_note and VAULT):
        return False
    note_path = (VAULT / source_note if not Path(source_note).is_absolute()
                 else Path(source_note))
    if not note_path.exists():
        return False
    try:
        raw = note_path.read_text(encoding="utf-8")
    except Exception:
        return False
    secs: list[tuple[str, str]] = []
    cur_h, cur_lines = "", []
    for line in raw.splitlines(keepends=True):
        m = re.match(r'^#{1,6}\s+(.+)', line)
        if m:
            content = "".join(cur_lines)
            if cur_h or content.strip():
                secs.append((cur_h, content))
            cur_h, cur_lines = m.group(1).strip(), []
        else:
            cur_lines.append(line)
    content = "".join(cur_lines)
    if cur_h or content.strip():
        secs.append((cur_h, content))
    import hashlib as _hl
    sh = rec.get("section_hashes", {})
    for h, c in secs:
        if h == "相关笔记":
            continue
        sh[h] = _hl.sha256(c.strip().encode("utf-8")).hexdigest()[:16]
    rec["section_hashes"] = sh
    return True


def _find_record_for_card(local_id: str):
    """返回 (record_path, record_dict, card_dict) 或 (None, None, None)。"""
    if not (ANKI_RECORDS_DIR and ANKI_RECORDS_DIR.exists()):
        return None, None, None
    for fn in ANKI_RECORDS_DIR.glob("*.json"):
        try:
            rec = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in rec.get("cards") or []:
            if c.get("local_id") == local_id:
                return fn, rec, c
    return None, None, None


def _pairs_text(pairs: list) -> str:
    return "\n\n".join(
        f"问：{p.get('question','').strip()}\n答：{p.get('answer','').strip()}"
        for p in pairs if p.get("answer")
    )


def _card_update_anki(local_id: str, pairs: list) -> dict:
    """据有效问答让 AI 生成一或多张新卡代替原卡。删/留原卡由 server-config 决定（默认保留）。"""
    rec_file, rec, orig = _find_record_for_card(local_id)
    if not orig:
        return {"ok": False, "error": "卡片不在 records 中"}
    note_id = orig.get("anki_note_id")
    is_cloze = orig.get("type") == "cloze"
    if is_cloze:
        cur = f"类型：cloze\n挖空文本：{orig.get('text','')}\n补充：{orig.get('back','')}"
    else:
        cur = f"类型：{orig.get('type','basic')}\n正面（问）：{orig.get('front','')}\n背面（答）：{orig.get('back','')}"
    prompt = (
        "下面是一张 Anki 记忆卡片，以及我复习时与 AI 的有效问答。"
        "请在原卡基础上生成一张或多张改进后的新卡片来代替它，使其更准确、清晰、易记。"
        "数学公式用 LaTeX（行内 \\( \\)，行间 \\[ \\]）。\n\n"
        f"=== 原卡片 ===\n{cur}\n\n=== 有效问答 ===\n{_pairs_text(pairs)}\n\n"
        "只输出 JSON 数组，不要其它文字，每个元素：\n"
        '[{"type":"basic|cloze|reverse","front":"非cloze的问题","back":"答案",'
        '"text":"cloze专用，含{{c1::...}}","reason":"制卡理由"}]'
    )
    try:
        raw = ai_client.ask(prompt)
        s, e = raw.find('['), raw.rfind(']')
        new_cards = json.loads(raw[s:e+1]) if (s != -1 and e > s) else []
    except Exception as ex:
        return {"ok": False, "error": f"AI 生成失败：{ex}"}
    if not new_cards:
        return {"ok": False, "error": "AI 未生成有效卡片"}

    source_link = rec.get("source_link", "")
    source_url  = rec.get("source_url", "")
    deck = orig.get("deck", "Obsidian::未分类")
    MODELS = {"basic": "Obsidian-basic", "reverse": "Obsidian-basic-reversed",
              "cloze": "Obsidian-cloze"}
    id_prefix = datetime.now().strftime("%Y%m%d%H%M%S")
    created = []
    try:
        _anki_request("createDeck", {"deck": deck})
        for i, card in enumerate(new_cards, 1):
            ctype = card.get("type", "basic")
            new_lid = f"{id_prefix}-{i:03d}"
            footer = _footer_html(new_lid, source_link, source_url,
                                  card.get("reason", "QA 改进"))
            if ctype == "cloze":
                extra = card.get("back", "")
                fields = {"Text": card.get("text", ""),
                          "Extra": (extra + footer) if extra else footer}
            else:
                fields = {"Front": card.get("front", ""),
                          "Back": card.get("back", "") + footer}
            note = {"deckName": deck, "modelName": MODELS.get(ctype, "Obsidian-basic"),
                    "fields": fields, "options": {"allowDuplicate": True},
                    "tags": ["obsidian", "ai_generated", "qa_improved"]}
            nid = _anki_request("addNote", {"note": note}, timeout=30)
            rec.setdefault("cards", []).append({
                "local_id": new_lid, "type": ctype, "deck": deck,
                "front": card.get("front", ""), "back": card.get("back", ""),
                "text": card.get("text", ""), "reason": card.get("reason", "QA 改进"),
                "tags": ["qa_improved"], "anki_note_id": nid, "status": "synced",
                "_qa_from": local_id, "_qa_created": datetime.now().isoformat(),
            })
            created.append({"local_id": new_lid, "type": ctype,
                            "front": card.get("front", ""), "back": card.get("back", ""),
                            "text": card.get("text", ""), "anki_note_id": nid})
    except Exception as ex:
        return {"ok": False, "error": f"创建卡片失败：{ex}"}

    deleted = False
    if bool(_qa_setting("card_qa.delete_original", False)) and note_id:
        try:
            _anki_request("deleteNotes", {"notes": [note_id]})
            orig["status"] = "deleted_by_qa"
            orig["_qa_deleted"] = datetime.now().isoformat()
            deleted = True
        except Exception:
            pass

    _override_section_hashes(rec)
    try:
        rec_file.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as ex:
        return {"ok": False, "error": f"写 records 失败：{ex}"}
    # 改完触发一次 AnkiWeb 同步，让新卡/删除即时推到其它设备（失败不阻断）
    synced = False
    try:
        _anki_request("sync", timeout=120)
        synced = True
    except Exception:
        pass
    summary = (f"生成 {len(created)} 张新卡，" + ("已删除原卡" if deleted else "保留原卡")
               + ("，已同步 AnkiWeb" if synced else "，AnkiWeb 同步失败（稍后凌晨会同步）"))
    return {"ok": True, "summary": summary, "created": created,
            "deleted": deleted, "synced": synced}


def _card_update_note(local_id: str, pairs: list) -> dict:
    """让 AI 据有效问答丰富/修改源笔记 → 写回 → 哈希覆盖（summarize/connect/anki + records）。"""
    ctx = _find_card_context(local_id)
    if not ctx:
        return {"ok": False, "error": "卡片不在 records 中"}
    source_note = ctx.get("source_note", "")
    if not (source_note and VAULT):
        return {"ok": False, "error": "无源笔记路径"}
    note_path = (VAULT / source_note if not Path(source_note).is_absolute()
                 else Path(source_note))
    if not note_path.exists():
        return {"ok": False, "error": f"笔记不存在：{source_note}"}
    try:
        original = note_path.read_text(encoding="utf-8")
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    is_cloze = ctx.get("type") == "cloze"
    if is_cloze:
        card_desc = f"挖空文本：{ctx.get('text','')}\n补充：{ctx.get('back','')}"
    else:
        card_desc = f"正面（问）：{ctx.get('front','')}\n背面（答）：{ctx.get('back','')}"
    prompt = (
        "下面有四部分信息：①我的一篇 Obsidian 笔记的完整内容；②由这篇笔记生成、"
        "我正在复习的 Anki 卡片；③我复习时问的问题；④我标记为有用的 AI 回答。\n\n"
        "请综合这四部分，自行判断【应该在笔记的哪一段补充或修正】以及【补充/修正什么内容】，"
        "对笔记做有机的丰富——不要把有用回答原样粘贴进去，而要结合笔记已有结构、上下文和"
        "卡片所考察的知识点，把新的理解自然融入最相关的位置：可在已有段落补一两句、可在"
        "合适处新增小节、可修正不准确的表述。补充应与原文风格连贯，避免重复已有内容。\n"
        "要求：①保留 frontmatter（开头 --- 之间）和「相关笔记」节原样不动；"
        "②保持 Obsidian Markdown 语法，数学公式用 $...$ 或 $$...$$；"
        "③输出修改后的完整笔记内容，不要加任何说明或代码围栏。\n\n"
        f"=== ① 当前笔记 ===\n{original}\n\n"
        f"=== ② 正在复习的 Anki 卡片 ===\n{card_desc}\n\n"
        f"=== ③问的问题 + ④有用的回答 ===\n{_pairs_text(pairs)}"
    )
    try:
        new_content = ai_client.ask(prompt).strip()
    except Exception as ex:
        return {"ok": False, "error": f"AI 生成失败：{ex}"}
    if new_content.startswith("```"):
        new_content = re.sub(r'^```[a-zA-Z]*\n', '', new_content)
        new_content = re.sub(r'\n```\s*$', '', new_content)
    if not new_content or len(new_content) < len(original) * 0.5:
        return {"ok": False, "error": "AI 返回内容异常（过短），已放弃写入"}
    try:
        note_path.write_text(new_content, encoding="utf-8")
    except Exception as ex:
        return {"ok": False, "error": f"写笔记失败：{ex}"}
    # 哈希覆盖：让「登记新笔记」忽略此次更新。
    # 只标记 note_state 真实跟踪的 skill（pending_notes 闸门是 summarize；
    # register 还跟踪 connect/anki）。不存在 img/pdf skill，别写脏条目。
    try:
        import note_state  # scripts/ 已在 sys.path（qa_server 注入）
        for sk in ("summarize", "connect", "anki"):
            try:
                note_state.mark_processed(note_path, sk)
            except Exception:
                pass
    except Exception:
        pass
    # records section_hashes 也覆盖
    rec_file, rec, _ = _find_record_for_card(local_id)
    if rec_file and rec is not None:
        _override_section_hashes(rec)
        try:
            rec_file.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:
            pass
    return {"ok": True, "summary": f"笔记已更新（{note_path.name}），哈希已覆盖防重复登记"}


def _card_delete(local_id: str) -> dict:
    """删除一张（QA 生成的）卡片：从 Anki 删 note + 从 records 移除 + 同步。"""
    rec_file, rec, card = _find_record_for_card(local_id)
    if not card:
        return {"ok": False, "error": "卡片不在 records 中"}
    nid = card.get("anki_note_id")
    try:
        if nid:
            _anki_request("deleteNotes", {"notes": [nid]})
    except Exception as ex:
        return {"ok": False, "error": f"Anki 删除失败：{ex}"}
    rec["cards"] = [c for c in rec.get("cards", []) if c.get("local_id") != local_id]
    try:
        rec_file.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as ex:
        return {"ok": False, "error": f"写 records 失败：{ex}"}
    synced = False
    try:
        _anki_request("sync", timeout=120); synced = True
    except Exception:
        pass
    return {"ok": True, "synced": synced}


def find_related_cards(note_names: list, match: str = "") -> list:
    """对每个候选笔记融合三个信号：
        relevance(AI 排序 rank score) × weakness(1 - 留存率) × proximity(图距离)
    最终按 score 降序返回。"""
    index_notes = load_index_notes()
    cards = []
    for rank_idx, name in enumerate(note_names):
        rel  = RELEVANCE_RANK_SCORES[rank_idx] if rank_idx < len(RELEVANCE_RANK_SCORES) else 0.2
        wk   = get_weakness(name)
        prox = compute_proximity(match, name, index_notes) if match else 0.5
        score = rel * wk * prox
        cards.append({
            "note":      name,
            "mastery":   get_mastery(name),
            "weakness":  round(wk, 3),
            "proximity": round(prox, 3),
            "relevance": rel,
            "score":     round(score, 4),
        })
    cards.sort(key=lambda c: -c["score"])
    return cards


def load_index_notes() -> dict:
    """解析所有索引文件，返回 {笔记名: {"keywords": str, "summary": str}}。
    INDEX_DIR 未配置时返回空 dict（关联性算法 graceful 退化）。"""
    notes = {}
    if not INDEX_DIR or not INDEX_DIR.exists():
        return notes
    for idx in sorted(f for f in INDEX_DIR.glob("**/*.md") if f.name != "knowledge-index.md"):
        try:
            text = idx.read_text(encoding="utf-8")
            for m in re.finditer(r'\[\[([^\]]+)\]\]\s*`([^`]*)`\s*[—\-]+\s*(.+)', text):
                name = m.group(1).strip()
                notes[name] = {
                    "keywords": m.group(2).strip(),
                    "summary":  m.group(3).strip()[:60],
                }
        except Exception:
            pass
    return notes


# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def msgbox(msg):
    ctypes.windll.user32.MessageBoxW(0, msg, "截图问答器", 0x10)

def get_clipboard_image():
    img = ImageGrab.grabclipboard()
    return img if isinstance(img, Image.Image) else None

def get_clipboard_hash():
    img = get_clipboard_image()
    if img is None:
        return None
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return hashlib.md5(buf.getvalue()).hexdigest()

def trigger_snip_tool():
    KEYEVENTF_KEYUP = 0x0002
    u32 = ctypes.windll.user32
    for vk in [0x5B, 0x10, 0x53]:
        u32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    for vk in [0x53, 0x10, 0x5B]:
        u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

def wait_for_screenshot(initial_hash):
    deadline = time.time() + SCREENSHOT_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        h = get_clipboard_hash()
        if h and h != initial_hash:
            return get_clipboard_image()
    return None

def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]

# ─── 后台推送到网站 ───────────────────────────────────────────────────────────────

def push_to_website(img_fname: str):
    """客户端版：noop（原版用主项目 scripts/export_history.py + scp 推 HIST_SERVER）。

    所有数据保留在本机 SQLite + HIST_IMG_DIR；浏览器历史侧栏仍能浏览本机历史。
    日后可改为通过 ApiClient.upload 推到当前用户的服务端 history/。
    """
    pass

# ─── 分类 + 保存（script化）─────────────────────────────────────────────────────

def classify_conversation() -> dict:
    anki_notes = []
    if ANKI_RECORDS_DIR and ANKI_RECORDS_DIR.exists():
        anki_notes = sorted(p.stem for p in ANKI_RECORDS_DIR.glob("000-*.json"))
    index_files = []
    if INDEX_DIR and INDEX_DIR.exists():
        index_files = sorted(f for f in INDEX_DIR.glob("*.md") if f.name != "knowledge-index.md")
    valid_names = {f.stem for f in index_files}
    notes_list  = "、".join(sorted(valid_names))
    anki_list   = "、".join(anki_notes[:60])
    msgs_text   = "\n".join(
        f"{'用户' if m['role'] == 'user' else 'Claude'}：{m['text'][:300]}"
        for m in state["session"].messages
    )
    prompt = (
        "根据以下对话完成三个任务：\n"
        "1. 从科目索引列表中选出最相关的一个，返回名称；若无返回空字符串\n"
        "2. 判断是否属于错题（用户做错了题、分析了错误解法等）\n"
        "3. 若是错题，从笔记列表中找出与错误原因最相关的 2-4 篇（按相关度降序）；不是错题返回空数组\n\n"
        f"科目索引列表：{notes_list}\n"
        f"笔记列表：{anki_list}\n\n"
        f"对话：\n{msgs_text}\n\n"
        "只输出 JSON（related 必须是数组）：\n"
        '{"match": "科目名或空字符串", "wrong": true或false, "related": ["笔记名1"]}'
    )
    raw    = ai_client.ask(prompt)
    parsed = None
    start, end = raw.find('{'), raw.rfind('}')
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
        except Exception:
            pass
    if not parsed:
        return {"match": "", "wrong": False, "related": []}
    if parsed.get("match") not in valid_names:
        parsed["match"] = ""
    if not isinstance(parsed.get("related"), list):
        parsed["related"] = []
    anki_set = set(anki_notes)
    parsed["related"] = [n for n in parsed["related"] if n in anki_set][:4]
    return parsed


def do_save(match: str, is_wrong: bool, related_cards: list = None) -> str:
    if VAULT is None or EXERCISES_DIR is None or WRONG_DIR is None:
        return "未配置 vault 路径，无法保存（请在客户端 GUI 设 qa_vault_path）"
    ts        = datetime.now().strftime("%Y-%m-%d %H:%M")
    img_fname = state["img_fname"]
    fallback  = f"未分类-{datetime.now().strftime('%Y%m%d')}"

    has_prefix = bool(match and re.match(r'^[0-9A-Fa-f]{3}-', match))
    note_name  = match if match else fallback
    ex_prefix  = "错题" if is_wrong else "习题"
    save_dir   = WRONG_DIR if is_wrong else EXERCISES_DIR
    section    = "错题本" if is_wrong else "习题本"
    exfile     = save_dir / f"{ex_prefix}-{note_name}.md"

    # 构建问答正文
    qa_parts, msgs, i = [], state["session"].messages, 0
    while i < len(msgs):
        if msgs[i]["role"] == "user":
            q = msgs[i]["text"]
            if i + 1 < len(msgs) and msgs[i + 1]["role"] == "assistant":
                a = msgs[i + 1]["text"]; i += 2
            else:
                a = ""; i += 1
            qa_parts.append(f"**问**：{q}\n\n**答**：\n\n{a}")
        else:
            i += 1
    content = f"\n# {ts}\n\n![[{img_fname}]]\n\n" + "\n\n".join(qa_parts) + "\n"
    if is_wrong and related_cards:
        lines = [
            f"- [[{c['note']}]]{'（掌握 ' + str(c['mastery']) + '%）' if c.get('mastery') is not None else ''}"
            for c in related_cards
        ]
        content += "\n**相关知识薄弱点**\n\n" + "\n".join(lines) + "\n"

    # 复制截图
    save_dir.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    dest = ASSETS_DIR / img_fname
    if not dest.exists() and state["temp_path"] and Path(state["temp_path"]).exists():
        shutil.copy2(state["temp_path"], dest)

    # 写入习题/错题文件
    if exfile.exists():
        existing = exfile.read_text(encoding="utf-8")
        exfile.write_text(existing.rstrip() + "\n\n---\n" + content, encoding="utf-8")
    else:
        header = f"相关知识点：[[{match}]]\n" if has_prefix else ""
        exfile.write_text(header + content, encoding="utf-8")

    # 更新原笔记反向链接
    if has_prefix:
        orig = VAULT / f"{match}.md"
        link = f"[[{ex_prefix}-{note_name}]]"
        if orig.exists():
            text = orig.read_text(encoding="utf-8")
            if f"## {section}" not in text:
                orig.write_text(
                    text.rstrip() + f"\n\n## {section}\n\n- {link}\n", encoding="utf-8"
                )
            elif link not in text:
                lines = text.splitlines()
                in_sec, ins = False, len(lines)
                for j, ln in enumerate(lines):
                    if ln.strip() == f"## {section}":
                        in_sec = True
                    elif in_sec and re.match(r'^##\s', ln):
                        ins = j; break
                lines.insert(ins, f"- {link}")
                orig.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return f"已保存({'错题' if is_wrong else '普通'}) → {exfile}"

# ─── 归档对话 ──────────────────────────────────────────────────────────────────

def archive_conversation(note_result: str, record_type: str = "normal", related_cards: list = None):
    HIST_IMG_DIR.mkdir(parents=True, exist_ok=True)
    hist_id   = datetime.now().strftime("%Y%m%d-%H%M%S")
    img_fname = state["img_fname"]
    src       = Path(state["temp_path"])
    dst       = HIST_IMG_DIR / img_fname
    if src.exists() and not dst.exists():
        shutil.copy2(src, dst)
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO conversations "
            "(id, timestamp, img_fname, note, messages, record_type, related_cards) "
            "VALUES (?,?,?,?,?,?,?)",
            (hist_id,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             img_fname,
             note_result,
             json.dumps(state["session"].messages, ensure_ascii=False),
             record_type,
             json.dumps(related_cards or [], ensure_ascii=False))
        )

# ─── HTML 页面 ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>截图问答</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#f0f2f5;height:100dvh;display:flex;flex-direction:column;overflow:hidden;font-size:14px}
#header{background:#fff;border-bottom:1px solid #ddd;padding:8px 16px;display:flex;align-items:center;gap:10px;flex-shrink:0}
#header h1{font-size:15px;font-weight:600;color:#111}
#note-tag{font-size:12px;color:#0078d4;background:#e8f0fe;padding:2px 8px;border-radius:10px;display:none}
#history-btn{margin-left:auto;background:none;border:1px solid #ccc;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;color:#555;transition:all .15s}
#history-btn:hover{background:#f5f5f5}
#history-btn.active{background:#e8f0fe;border-color:#0078d4;color:#0078d4}
/* 截图/卡片区现在在滚动容器 #chat 内，跟随内容自然上移划走 */
#shot-area{background:#fafafa;border-bottom:1px solid #ddd;padding:8px 16px;overflow:hidden;max-height:180px;cursor:pointer;display:flex;align-items:center}
#shot-area.expanded{max-height:60vh}
/* 划走后顶部的小行：绝对浮层（不占滚动流，显隐不推动截图区→不抖动），点击滚回展开 */
#shot-peek{display:none;position:absolute;top:0;left:0;right:0;z-index:5;background:#eef4ff;border-bottom:1px solid #cfe0ff;color:#0057b8;font-size:12px;text-align:center;padding:4px 16px;cursor:pointer;user-select:none}
#shot-peek:hover{background:#e0ecff}
#shot-wrap{display:contents}
#shot-wrap img{max-height:160px;max-width:100%;border-radius:6px;border:1px solid #ddd;display:block;object-fit:contain}
#shot-area.expanded #shot-wrap img{max-height:calc(60vh - 20px)}
#quick-bar{background:#fff;border-top:1px solid #eee;padding:5px 10px 5px 12px;flex-shrink:0;display:flex;align-items:center;gap:5px;overflow-x:auto;min-height:36px;scrollbar-width:thin}
#quick-bar::-webkit-scrollbar{height:3px}#quick-bar::-webkit-scrollbar-thumb{background:#ddd;border-radius:3px}
#quick-btns{display:flex;gap:5px;align-items:center;flex-shrink:0}
.qbtn-row{display:flex;align-items:center;gap:1px;flex-shrink:0}
.qbtn{background:#f0f4ff;border:1px solid #cce0ff;border-radius:14px;padding:3px 11px;font-size:12px;color:#0057b8;cursor:pointer;white-space:nowrap;transition:all .15s;line-height:1.6}
.qbtn:hover{background:#dbeafe;border-color:#0078d4;color:#005fa3}
.qbtn-actions{display:none;gap:1px;align-items:center}
.qbtn-row:hover .qbtn-actions{display:flex}
.qedit-btn{background:none;border:none;color:#bbb;cursor:pointer;font-size:10px;padding:2px 3px;line-height:1;transition:color .15s;border-radius:3px}
.qedit-btn:hover{color:#0078d4;background:#e8f0fe}
.qbtn-input{border:1px solid #0078d4;border-radius:14px;padding:2px 10px;font-size:12px;outline:none;font-family:inherit;min-width:60px;max-width:160px;line-height:1.6}
.qadd-btn{background:none;border:1px dashed #ccc;border-radius:14px;padding:3px 9px;font-size:12px;color:#bbb;cursor:pointer;white-space:nowrap;transition:all .15s;flex-shrink:0}
.qadd-btn:hover{border-color:#0078d4;color:#0078d4}
#chat-wrap{flex:1;min-height:0;position:relative;display:flex;flex-direction:column}
#chat{flex:1;overflow-y:auto;display:block}
#msgs{display:flex;flex-direction:column;gap:10px;padding:14px 16px}
.msg-row{display:flex;flex-direction:column}
.msg-row.user{align-items:flex-end}
.msg-row.assistant{align-items:flex-start}
.bubble-controls{display:none;gap:4px;margin-bottom:3px}
.msg-row:hover .bubble-controls{display:flex}
.ctrl-btn{background:none;border:1px solid #ddd;border-radius:4px;padding:1px 8px;font-size:11px;cursor:pointer;color:#999;line-height:1.7}
.ctrl-btn:hover{background:#f0f0f0;color:#444;border-color:#bbb}
.bubble{max-width:88%;padding:10px 14px;border-radius:12px;line-height:1.75;word-break:break-word}
.bubble.user{background:#0078d4;color:#fff;border-radius:12px 12px 2px 12px}
.bubble.assistant{background:#fff;border:1px solid #e0e0e0;color:#1a1a1a;border-radius:12px 12px 12px 2px}
.bubble.deleted{background:#e8e8e8!important;color:#b0b0b0!important;border-color:#ddd!important}
.bubble.deleted .md,.bubble.deleted .md *{color:#b0b0b0!important}
.hist-divider{display:flex;align-items:center;gap:8px;font-size:11px;color:#bbb;padding:2px 0}
.hist-divider-line{flex:1;height:1px;background:#e8e8e8}
.hist-continue-hint{text-align:center;font-size:12px;color:#0078d4;padding:6px 0}
.md p{margin:.35em 0}.md p:first-child{margin-top:0}.md p:last-child{margin-bottom:0}
.md h1,.md h2,.md h3,.md h4{margin:.6em 0 .25em;font-weight:600;font-size:1em}
.md h1{font-size:1.1em}.md h2{font-size:1.05em}
.md ul,.md ol{padding-left:1.5em;margin:.35em 0}.md li{margin:.15em 0}
.md pre{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:8px 12px;overflow-x:auto;margin:.4em 0;font-size:.87em}
.md code{font-family:'Cascadia Code',Consolas,monospace;font-size:.87em;background:#f0f0f0;padding:1px 4px;border-radius:3px}
.md pre code{background:none;padding:0;font-size:inherit}
.md blockquote{border-left:3px solid #0078d4;padding-left:10px;color:#555;margin:.4em 0}
.md strong{font-weight:600}.md em{font-style:italic}
.md table{border-collapse:collapse;margin:.4em 0;font-size:.9em}
.md td,.md th{border:1px solid #d0d0d0;padding:4px 10px}.md th{background:#f6f8fa;font-weight:600}
.md hr{border:none;border-top:1px solid #e0e0e0;margin:.6em 0}
.typing{color:#aaa;font-style:italic}
#bottom{background:#fff;border-top:1px solid #ddd;padding:10px 16px;display:flex;gap:8px;align-items:flex-end;flex-shrink:0;flex-wrap:wrap}
#bottom-btns{display:flex;gap:8px;align-items:flex-end;flex-shrink:0}
#input{flex:1;min-width:160px;border:1px solid #ccc;border-radius:8px;padding:8px 12px;font-size:14px;resize:none;height:58px;font-family:inherit;outline:none;line-height:1.5;transition:border-color .15s}
#input:focus{border-color:#0078d4;box-shadow:0 0 0 2px #e3f0fd}
#input.search-mode{border-color:#7060cc;box-shadow:0 0 0 2px #e8e4ff}
.qbtn-fixed{background:#f0f0ff;border:1px solid #bbb8ee;border-radius:14px;padding:3px 11px;font-size:12px;color:#4040bb;cursor:pointer;white-space:nowrap;transition:all .18s;line-height:1.6;flex-shrink:0}
.qbtn-fixed:hover{background:#e4e4ff;border-color:#8080dd}
.qbtn-fixed.active{background:#e0d8ff;border-color:#6050cc;color:#2020aa;font-weight:500}
.btn{border:none;border-radius:8px;padding:8px 14px;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap;transition:opacity .15s}
.btn:hover:not(:disabled){opacity:.85}.btn:disabled{opacity:.45;cursor:default}
#send-btn{background:#0078d4;color:#fff}
#mic-btn{background:#eee;color:#444;font-size:15px;padding:8px 12px}
#mic-btn.listening{background:#e74c3c;color:#fff;animation:micpulse 1s infinite}
@keyframes micpulse{0%,100%{opacity:1}50%{opacity:.55}}
#save-btn{background:#107c41;color:#fff}
#wrong-btn{background:#c0392b;color:#fff}
#discard-btn{background:#6c757d;color:#fff}
#status{font-size:12px;color:#666;padding:2px 16px;flex-shrink:0;min-height:18px}
/* 粘贴图片预览 */
#paste-row{display:none;background:#fff;border-top:1px solid #eee;padding:6px 16px 4px;align-items:center;gap:8px;flex-shrink:0}
#paste-row.has-img{display:flex}
#paste-thumb{max-height:56px;max-width:100px;border-radius:4px;border:1px solid #ddd;object-fit:contain}
#paste-hint{font-size:11px;color:#aaa;flex:1}
#paste-clear{background:none;border:1px solid #e0e0e0;border-radius:4px;color:#aaa;cursor:pointer;font-size:11px;padding:2px 7px;line-height:1.6;transition:all .15s}
#paste-clear:hover{color:#e74c3c;border-color:#e74c3c}
/* 历史抽屉 */
#hist-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.15);z-index:99}
#hist-backdrop.open{display:block}
#hist-sidebar{position:fixed;top:0;right:0;bottom:0;width:330px;background:#fff;box-shadow:-2px 0 20px rgba(0,0,0,.12);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .22s ease;z-index:100}
#hist-sidebar.open{transform:translateX(0)}
#hist-head{padding:12px 14px;border-bottom:1px solid #eee;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
#hist-head-title{font-weight:600;font-size:13px;color:#222}
#hist-close{background:none;border:none;font-size:17px;cursor:pointer;color:#bbb;line-height:1;padding:2px 4px}
#hist-close:hover{color:#555}
#hist-list{flex:1;overflow-y:auto}
.hist-entry{display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid #f5f5f5;cursor:pointer;transition:background .12s}
.hist-entry:hover{background:#f5f8ff}
.hist-entry:last-child{border-bottom:none}
.hist-thumb{width:58px;height:44px;object-fit:cover;border-radius:4px;border:1px solid #e8e8e8;flex-shrink:0;background:#f0f0f0}
.hist-thumb-blank{width:58px;height:44px;border-radius:4px;background:#f5f5f5;flex-shrink:0}
.hist-meta{flex:1;min-width:0}
.hist-ts{font-size:11px;color:#bbb;margin-bottom:2px}
.hist-note{font-size:12px;color:#444;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.4}
.hist-count{font-size:11px;color:#ccc;margin-top:2px}
.hist-del{background:none;border:none;font-size:15px;cursor:pointer;color:#ddd;flex-shrink:0;padding:4px;line-height:1;transition:color .15s}
.hist-del:hover{color:#e74c3c}
#hist-empty{padding:40px 16px;text-align:center;color:#ccc;font-size:13px}
.hist-entry.wrong{border-left:3px solid #e74c3c}
.hist-wrong-badge{font-size:10px;background:#fde8e8;color:#c0392b;border-radius:3px;padding:1px 5px;margin-left:5px;font-weight:600;vertical-align:middle;flex-shrink:0}
.hist-chips{display:flex;flex-wrap:wrap;gap:3px;margin-top:4px}
.hist-chip{font-size:10px;background:#fff3e0;border:1px solid #ffb74d;border-radius:8px;padding:1px 6px;color:#e65100;white-space:nowrap;user-select:none;touch-action:none;cursor:default;transition:background .15s}
.hist-chip.pressing{background:#ffcc80}
/* AI 设置弹窗 */
#settings-btn{background:none;border:1px solid #ccc;border-radius:6px;padding:3px 8px;font-size:14px;cursor:pointer;color:#555;transition:all .15s}
#settings-btn:hover{background:#f5f5f5;color:#333}
/* 打开原笔记 + 三个更新按钮（卡片模式，仅有勾选时显示后三个） */
.card-act-btn{border:1px solid #ccc;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;background:none;color:#555;transition:all .15s;white-space:nowrap}
.card-act-btn:hover{background:#f5f5f5}
.card-act-btn.upd{border-color:#0078d4;color:#0057b8;background:#f0f7ff}
.card-act-btn.upd:hover{background:#e0efff}
.card-act-btn.upd-all{border-color:#107c41;color:#0b5a2f;background:#eefbf3}
.card-act-btn.upd-all:hover{background:#d8f5e3}
.card-act-btn:disabled{opacity:.5;cursor:default}
/* 每条 AI 回复的 有用/无用 切换框 */
/* 子标题行的「+」按钮：圆形，点击变蓝选中，级联 */
.pick-btn{flex-shrink:0;width:20px;height:20px;line-height:18px;text-align:center;border:1px solid #cbd5e1;border-radius:50%;background:#fff;color:#94a3b8;font-size:14px;cursor:pointer;user-select:none;transition:all .12s;padding:0}
.pick-btn:hover{border-color:#0078d4;color:#0078d4}
.pick-btn.on{background:#0078d4;border-color:#0078d4;color:#fff;font-weight:700}
/* 「选用整条回答」开关：在气泡外侧，与子标题 + 区分 */
.reply-pick-all{align-self:flex-start;margin-top:4px;font-size:11px;color:#64748b;background:#fff;border:1px dashed #cbd5e1;border-radius:12px;padding:2px 12px;cursor:pointer;user-select:none;transition:all .12s}
.reply-pick-all:hover{border-color:#0078d4;color:#0078d4}
.reply-pick-all.on{background:#0078d4;border-color:#0078d4;border-style:solid;color:#fff;font-weight:600}
.bubble.assistant.picked-all{box-shadow:0 0 0 2px #0078d4 inset;background:#f5faff}
/* 子标题行的 + ：贴右侧 */
.md h1,.md h2,.md h3,.md h4,.md h5,.md h6{position:relative}
.md h1.has-pick,.md h2.has-pick,.md h3.has-pick,.md h4.has-pick,.md h5.has-pick,.md h6.has-pick{padding-right:26px}
.md .head-pick{position:absolute;right:0;top:50%;transform:translateY(-50%)}
/* 选中标题段：标题行 + 其内容段落连续高亮，左侧蓝条 */
.md .hsec-picked,.md .hsec-picked-body{background:#eef6ff;box-shadow:-6px 0 0 #cfe6ff}
.md .hsec-picked{border-top-left-radius:4px;border-top-right-radius:4px;padding-top:2px}
.md .hsec-picked-body:last-child{border-bottom-left-radius:4px;border-bottom-right-radius:4px;padding-bottom:2px}
/* 更新结果/进度面板 */
#card-result{display:none;flex-shrink:0;padding:10px 16px;border-top:1px solid #eee;border-bottom:1px solid #eee;background:#fff8e6;font-size:13px;line-height:1.6;max-height:38vh;overflow:auto}
#card-result .cr-close{float:right;background:none;border:none;font-size:15px;color:#bbb;cursor:pointer;line-height:1}
.newcard{border:1px solid #e2e8f0;border-radius:6px;margin-top:6px;background:#fff}
.newcard-head{display:flex;align-items:center;justify-content:space-between;padding:5px 9px}
.newcard-toggle{cursor:pointer;color:#0057b8;font-size:12px;font-weight:500;user-select:none;flex:1}
.newcard-del{background:#fde8e8;color:#b91c1c;border:1px solid #f0b4b4;border-radius:5px;font-size:11px;padding:2px 9px;cursor:pointer}
.newcard-del:hover{background:#f9d0d0}
.newcard-del:disabled{opacity:.5}
.newcard-body{padding:0 10px 9px;font-size:13px;line-height:1.6;border-top:1px solid #f0f0f0}
#sett-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:200}
#sett-overlay.open{display:block}
#sett-modal{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#fff;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.18);z-index:201;min-width:300px;max-width:380px;width:90%}
#sett-modal.open{display:block}
#sett-head{padding:14px 16px 10px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eee}
#sett-head span{font-weight:600;font-size:14px}
#sett-close{background:none;border:none;font-size:16px;cursor:pointer;color:#bbb;padding:2px 4px;line-height:1}
#sett-close:hover{color:#555}
#sett-body{padding:14px 16px;display:flex;flex-direction:column;gap:7px}
#sett-body label{font-size:12px;color:#666;font-weight:500;margin-top:4px}
#sett-body label:first-child{margin-top:0}
#sett-body select,#sett-body input[type=text]{border:1px solid #ddd;border-radius:7px;padding:7px 10px;font-size:13px;font-family:inherit;outline:none;transition:border-color .15s;width:100%}
#sett-body select:focus,#sett-body input:focus{border-color:#0078d4}
#sett-hint{font-size:11px;color:#aaa;margin-top:-2px}
#sett-foot{padding:10px 16px 14px;display:flex;justify-content:flex-end;gap:8px;border-top:1px solid #eee}
#sett-foot button{border:none;border-radius:7px;padding:7px 16px;font-size:13px;font-weight:500;cursor:pointer;transition:opacity .15s}
#sett-save{background:#0078d4;color:#fff}
#sett-cancel{background:#f5f5f5;color:#555}
#sett-foot button:hover{opacity:.85}
@media(max-width:540px){
  #input{flex-basis:100%;height:48px;font-size:13px}
  #bottom-btns{width:100%;justify-content:flex-end}
  .btn{padding:7px 10px;font-size:12px}
  #shot-area{max-height:120px}
  #shot-area.expanded{max-height:45vh}
  #quick-bar{padding:4px 8px}
  #header{padding:6px 12px}
  #header h1{font-size:14px}
  #chat{padding:10px 12px}
}
@media(min-width:900px){
  #shot-wrap img{max-height:none;height:100%}
}
</style>
</head>
<body>
<div id="header">
  <h1>截图问答</h1>
  <span id="note-tag"></span>
  <button id="history-btn" onclick="openSidebar()">历史记录</button>
  <span id="card-actions"></span>
  <button id="settings-btn" onclick="openSettings()" title="设置">⚙</button>
</div>
<div id="chat-wrap">
  <div id="shot-peek" onclick="revealShot()">▾ 展开截图</div>
  <div id="chat">
    <div id="shot-area">
      <div id="card-face" style="display:none;padding:14px 16px;font-size:14px;line-height:1.6;overflow:auto"></div>
      <div id="shot-wrap" title="点击展开/收起截图">
        <img id="shot" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" alt="等待截图…">
      </div>
    </div>
    <div id="msgs"></div>
  </div>
</div>
<div id="card-result"></div>
<div id="status"></div>
<div id="quick-bar">
  <button class="qbtn-fixed" id="search-btn" onclick="toggleSearch()" title="搜索当前对话相关知识点">🔍 关联知识</button>
  <div id="quick-btns"></div>
  <button class="qadd-btn" onclick="addQBtn()">＋</button>
</div>
<div id="paste-row">
  <img id="paste-thumb" src="" alt="">
  <span id="paste-hint">图片已附加，将随下条消息发送</span>
  <button id="paste-clear" onclick="clearPaste()">✕ 清除</button>
</div>
<div id="bottom">
  <textarea id="input" placeholder="输入问题… （Enter 发送，Shift+Enter 换行；粘贴图片可附图提问）"></textarea>
  <div id="bottom-btns">
    <button class="btn" id="mic-btn" onclick="toggleMic()" title="语音输入">🎤</button>
    <button class="btn" id="send-btn" onclick="send()">发送</button>
    <button class="btn" id="save-btn" onclick="save()">保存笔记</button>
    <button class="btn" id="discard-btn" onclick="discard()">放弃</button>
  </div>
</div>

<div id="sett-overlay" onclick="closeSettings()"></div>
<div id="sett-modal">
  <div id="sett-head">
    <span>设置</span>
    <button id="sett-close" onclick="closeSettings()">✕</button>
  </div>
  <div id="sett-body">
    <label for="s-backend">AI 后端</label>
    <select id="s-backend" onchange="updateSettFields()">
      <option value="claude_cli">Claude CLI</option>
      <option value="codex_cli">Codex CLI（OpenAI）</option>
    </select>
    <div id="s-claude-fields">
      <label for="s-claude-model">Claude 模型（留空＝默认）</label>
      <input type="text" id="s-claude-model" list="s-claude-models" placeholder="opus / sonnet / claude-opus-4-7">
      <datalist id="s-claude-models">
        <option value="opus"></option>
        <option value="sonnet"></option>
        <option value="haiku"></option>
      </datalist>
      <label for="s-claude-effort">思考深度 effort（留空＝默认）</label>
      <select id="s-claude-effort">
        <option value="">默认</option>
        <option value="low">low</option>
        <option value="medium">medium</option>
        <option value="high">high</option>
        <option value="xhigh">xhigh</option>
        <option value="max">max</option>
      </select>
    </div>
    <div id="s-codex-fields">
      <label for="s-codex-model">Codex 模型（留空＝默认）</label>
      <input type="text" id="s-codex-model" list="s-codex-models" placeholder="留空使用默认模型">
      <datalist id="s-codex-models">
        <option value="gpt-5.5"></option>
        <option value="gpt-5.4"></option>
        <option value="gpt-5.4-mini"></option>
        <option value="gpt-5.3-codex"></option>
      </datalist>
    </div>
    <p id="sett-hint">设置即时生效（保存后下条消息起用新模型/思考深度）</p>
    <hr style="border:none;border-top:1px solid #eee;margin:6px 0">
    <label style="display:flex;align-items:center;gap:8px;font-weight:500;cursor:pointer;margin-top:0">
      <input type="checkbox" id="s-delete-original" style="width:auto;margin:0">
      「根据此修改 Anki」时删除原卡片
    </label>
    <p id="sett-hint">不勾＝保留原卡（默认，不丢 FSRS 复习历史）；勾选＝生成新卡后删除原卡</p>
  </div>
  <div id="sett-foot">
    <button id="sett-cancel" onclick="closeSettings()">取消</button>
    <button id="sett-save" onclick="saveSettings()">保存</button>
  </div>
</div>

<div id="hist-backdrop" onclick="closeSidebar()"></div>
<div id="hist-sidebar">
  <div id="hist-head">
    <span id="hist-head-title">历史记录</span>
    <button id="hist-close" onclick="closeSidebar()">✕</button>
  </div>
  <div id="hist-list"><div id="hist-empty">加载中…</div></div>
</div>

<script>
window.MathJax = {
  tex:{inlineMath:[['$','$'],['\\(','\\)']],displayMath:[['$$','$$'],['\\[','\\]']],processEscapes:true},
  options:{skipHtmlTags:['script','noscript','style','textarea','pre']},
  startup:{typeset:false},
};
</script>
<script src="//bwicarus.taile44d0c.ts.net/static/qa/mathjax.js?v=svg1" async id="MathJax-script"></script>
<script src="//bwicarus.taile44d0c.ts.net/static/qa/marked.js"></script>
<script>
if (window.marked && marked.use) {
  marked.use({ breaks: true, gfm: true });
} else {
  console.warn('marked.js 未加载，将使用纯文本兜底渲染');
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderMd(text) {
  if (!window.marked || !marked.parse) {
    return escapeHtml(text).replace(/\n/g, '<br>');
  }
  try {
    const saved = [];
    const ph = m => { saved.push(m); return '\x02M' + (saved.length-1) + '\x02'; };
    let s = text
      .replace(/\$\$[\s\S]*?\$\$/g, ph).replace(/\\\[[\s\S]*?\\\]/g, ph)
      .replace(/\\\([\s\S]*?\\\)/g, ph)
      .replace(/(?<!\$)\$(?!\$)[^\n$]*?\$(?!\$)/g, ph);
    s = marked.parse(s);
    return s.replace(/\x02M(\d+)\x02/g, (_, i) => saved[+i]);
  } catch (e) {
    console.warn('renderMd 失败，回退到纯文本', e);
    return escapeHtml(text).replace(/\n/g, '<br>');
  }
}

// MathJax 可能因 async 加载尚未就绪；最多轮询 20×200ms = 4s 后放弃。
function typeset(el, retries) {
  if (retries === undefined) retries = 20;
  if (window.MathJax && typeof MathJax.typesetPromise === 'function') {
    return MathJax.typesetPromise([el]).catch(e => console.warn('MathJax typeset 失败', e));
  }
  if (retries > 0) setTimeout(() => typeset(el, retries - 1), 200);
}

const chat   = document.getElementById('chat');   // 滚动容器
const msgs   = document.getElementById('msgs');    // 消息容器（清空只清这里，不动截图区）
const input  = document.getElementById('input');
const status = document.getElementById('status');
// 自动跟随到底：仅当用户本就在底部附近才跟随；往上翻阅时不打扰
let autoStick = true;
chat.addEventListener('scroll', () => {
  autoStick = (chat.scrollHeight - chat.scrollTop - chat.clientHeight) < 80;
}, {passive: true});
function stickBottom() { if (autoStick) chat.scrollTop = chat.scrollHeight; }
let sending  = false;
let pendingHistory  = null;   // 加载历史后，首条消息携带上下文
let currentShotSrc  = '';     // 当前会话截图 base64
let pastedImgB64    = null;   // 粘贴的图片 base64（不含 data URL 前缀）
let searchMode      = false;  // 关联知识搜索模式
let currentAbort    = null;   // SSE 流式 AbortController；sending 时点 send 按钮中止

function pollScreenshot() {
  fetch('api/screenshot').then(r => r.json()).then(d => {
    if (d.data) {
      currentShotSrc = 'data:image/png;base64,' + d.data;
      document.getElementById('shot').src = currentShotSrc;
      document.getElementById('shot').alt = '截图';
    } else {
      document.getElementById('shot').alt = '等待截图注入…';
      setTimeout(pollScreenshot, 3000);
    }
  }).catch(() => setTimeout(pollScreenshot, 5000));
}
// 卡片模式：URL ?card=<local_id> → 反查卡片两面显示在截图位，附原笔记按钮
let cardCtx = null;

// ── 结果面板 ──────────────────────────────────────────────────────────────
function showCardResult(html) {
  const el = document.getElementById('card-result');
  el.innerHTML = '<button class="cr-close" onclick="document.getElementById(\'card-result\').style.display=\'none\'">✕</button>' + html;
  el.style.display = 'block';   // 不能用 ''（会回退到 CSS 的 display:none）
  typeset(el);
}

// 取标题自身文本（去掉 + 按钮）
function headingOwnText(h) {
  const clone = h.cloneNode(true);
  const b = clone.querySelector('.head-pick'); if (b) b.remove();
  return clone.textContent.trim();
}
// 收集某条回复里被选中的标题段落（标题行 + 其后到下个标题前的内容）
function collectSelectedSections(md) {
  const parts = [];
  md.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(h => {
    const btn = h.querySelector('.head-pick');
    if (!btn || !btn.classList.contains('on')) return;
    let txt = '#'.repeat(headLevel(h)) + ' ' + headingOwnText(h);
    let sib = h.nextElementSibling;
    while (sib && !/^H[1-6]$/.test(sib.tagName)) {
      const t = (sib.textContent || '').trim();
      if (t) txt += '\n' + t;
      sib = sib.nextElementSibling;
    }
    parts.push(txt.trim());
  });
  return parts.join('\n\n');
}
// 收集所有「有用」内容及其对应的用户问题（Q&A 对）：
//   整条 + 选中 → 整条回复；否则 → 选中的标题段落
function collectUsefulPairs() {
  const pairs = [];
  document.querySelectorAll('#chat .msg-row.assistant').forEach(row => {
    const md = row.querySelector('.md');
    if (!md) return;
    const allOn = !!row.querySelector('.reply-pick-all.on');
    let answer = '';
    if (allOn) {
      answer = (md.dataset.raw || md.textContent || '').trim();
    } else {
      answer = collectSelectedSections(md);
    }
    if (!answer) return;
    // 往前找最近的一条 user 消息作为问题
    let q = '';
    let prev = row.previousElementSibling;
    while (prev) {
      if (prev.classList.contains('user')) {
        const b = prev.querySelector('.bubble');
        q = b ? (b.textContent || '').trim() : '';
        break;
      }
      prev = prev.previousElementSibling;
    }
    pairs.push({question: q, answer: answer});
  });
  return pairs;
}

// 根据当前是否有「有用」回复，显示/隐藏三个更新按钮
function refreshUpdateButtons() {
  if (!cardCtx) return;
  const has = collectUsefulPairs().length > 0;
  document.querySelectorAll('#card-actions .upd-group').forEach(g => {
    g.style.display = has ? '' : 'none';
  });
}

const UPD_LABELS = {note: '更新到笔记', anki: '根据此修改 Anki', all: '全部更新'};
let _newCards = [];   // 暂存本次生成的新卡，供预览渲染
function renderUpdResult(res) {
  if (!res || res.ok === false) { showCardResult('✗ ' + ((res && res.error) || '失败')); return; }
  let html = '';
  _newCards = [];
  if (res.anki) {
    if (res.anki.ok) {
      html += '<div><b>✓ Anki：</b>' + (res.anki.summary || '已更新') + '</div>';
      const cards = res.anki.created || [];
      if (cards.length && typeof cards[0] === 'object') {
        _newCards = cards;
        html += '<div style="margin-top:4px;color:#666;font-size:12px">新卡（点标题预览，可删除）：</div>';
        cards.forEach((c, i) => {
          html += '<div class="newcard" data-idx="' + i + '" data-lid="' + c.local_id + '">'
            + '<div class="newcard-head">'
            + '<span class="newcard-toggle" onclick="toggleNewcard(this)">▸ 新卡 ' + (i + 1) + '（' + (c.type || 'basic') + '）</span>'
            + '<button class="newcard-del" onclick="deleteNewcard(this)">删除</button>'
            + '</div><div class="newcard-body" style="display:none"></div></div>';
        });
      }
    } else {
      html += '<div style="color:#b91c1c"><b>✗ Anki：</b>' + (res.anki.error || '失败') + '</div>';
    }
  }
  if (res.note) {
    html += res.note.ok
      ? '<div style="margin-top:6px"><b>✓ 笔记：</b>' + (res.note.summary || '已更新') + '</div>'
      : '<div style="margin-top:6px;color:#b91c1c"><b>✗ 笔记：</b>' + (res.note.error || '失败') + '</div>';
  }
  showCardResult(html || '完成');
}
function toggleNewcard(span) {
  const card = span.closest('.newcard');
  const body = card.querySelector('.newcard-body');
  const opening = body.style.display === 'none';
  if (opening && !body.dataset.filled) {
    const c = _newCards[+card.dataset.idx] || {};
    let h;
    if (c.type === 'cloze') {
      h = '<b>挖空</b><br>' + renderMd(c.text || '') + (c.back ? '<hr><b>补充</b><br>' + renderMd(c.back) : '');
    } else {
      h = '<b>问</b><br>' + renderMd(c.front || '') + '<hr><b>答</b><br>' + renderMd(c.back || '');
    }
    body.innerHTML = h; body.dataset.filled = '1'; typeset(body);
  }
  body.style.display = opening ? 'block' : 'none';
  span.textContent = (opening ? '▾' : '▸') + span.textContent.slice(1);
}
function deleteNewcard(btn) {
  const card = btn.closest('.newcard');
  const lid = card.dataset.lid;
  if (!confirm('从 Anki 删除这张新卡？不可撤销。')) return;
  btn.disabled = true; btn.textContent = '删除中…';
  fetch('api/card-delete', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({local_id: lid}),
  }).then(r => r.json()).then(j => {
    if (j.ok) {
      card.style.opacity = '.45';
      card.querySelector('.newcard-head').innerHTML =
        '<span style="color:#b91c1c;font-size:12px">已删除' + (j.synced ? '（已同步）' : '') + '</span>';
      const b = card.querySelector('.newcard-body'); if (b) b.style.display = 'none';
    } else {
      btn.disabled = false; btn.textContent = '删除';
      alert('删除失败：' + (j.error || ''));
    }
  }).catch(e => { btn.disabled = false; btn.textContent = '删除'; alert('删除失败：' + e.message); });
}
function pollCardJob(jobId, allBtns, tries) {
  tries = tries || 0;
  fetch('api/card-update-status?job=' + encodeURIComponent(jobId))
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(j => {
      if (j.status === 'running') { setTimeout(() => pollCardJob(jobId, allBtns, 0), 2000); return; }
      allBtns.forEach(b => b.disabled = false);
      renderUpdResult(j.result);
    })
    .catch(() => {
      // 轮询暂时失败（锁屏/网络抖动）→ 多试几次再放弃；任务仍在后台跑
      if (tries < 40) { setTimeout(() => pollCardJob(jobId, allBtns, tries + 1), 3000); }
      else { allBtns.forEach(b => b.disabled = false); showCardResult('⚠️ 暂时取不到结果，但任务已在后台执行，请稍后刷新页面 / 查看 Anki'); }
    });
}
function runCardUpdate(target, btn) {
  const pairs = collectUsefulPairs();
  if (!pairs.length) { showCardResult('请先在 AI 回复下方勾选「有用」的回答。'); return; }
  const allBtns = document.querySelectorAll('#card-actions .upd-group button');
  allBtns.forEach(b => b.disabled = true);
  showCardResult('正在后台执行「' + UPD_LABELS[target] + '」，纳入 ' + pairs.length + ' 条有用回答…（可耐心等待，锁屏也不影响）');
  fetch('api/card-update', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({local_id: cardCtx.local_id, target: target, pairs: pairs}),
  }).then(r => r.json()).then(j => {
    if (j.job_id) { pollCardJob(j.job_id, allBtns, 0); }
    else { allBtns.forEach(b => b.disabled = false); renderUpdResult(j); }   // 兼容旧式同步返回
  }).catch(e => {
    allBtns.forEach(b => b.disabled = false);
    showCardResult('✗ 发起失败：' + e.message);
  });
}

function loadCardContext(cid) {
  fetch('api/card-context?card=' + encodeURIComponent(cid))
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(c => {
      cardCtx = c;
      const face = document.getElementById('card-face');
      const parts = [];
      if (c.front) parts.push('<div><b>问</b><br>' + renderMd(c.front) + '</div>');
      if (c.text)  parts.push('<div>' + renderMd(c.text) + '</div>');
      if (c.back)  parts.push('<div style="margin-top:8px"><b>答</b><br>' + renderMd(c.back) + '</div>');
      face.innerHTML = parts.join('<hr style="border:none;border-top:1px solid #eee;margin:10px 0">');
      face.style.display = '';
      document.getElementById('shot-peek').textContent = '▾ 展开卡片';
      document.getElementById('shot-wrap').style.display = 'none';
      typeset(face);
      const acts = document.getElementById('card-actions');
      // 打开原笔记
      const a = document.createElement('button');
      a.textContent = '打开原笔记';
      a.className = 'card-act-btn';
      a.onclick = () => {
        if (c.source_url) location.href = c.source_url;
        else showCardResult('这张卡未记录原笔记链接（source_url 为空）。');
      };
      acts.appendChild(a);
      // 三个更新按钮（默认隐藏，有勾选「有用」时才显示）
      const group = document.createElement('span');
      group.className = 'upd-group';
      group.style.display = 'none';
      group.style.cssText += ';display:none;gap:6px';
      [
        {t: 'note', label: '更新到笔记', cls: 'card-act-btn upd'},
        {t: 'anki', label: '根据此修改 Anki', cls: 'card-act-btn upd'},
        {t: 'all',  label: '全部更新', cls: 'card-act-btn upd-all'},
      ].forEach(({t, label, cls}) => {
        const b = document.createElement('button');
        b.textContent = label; b.className = cls;
        b.style.marginLeft = '6px';
        b.onclick = () => runCardUpdate(t, b);
        group.appendChild(b);
      });
      acts.appendChild(group);
    })
    .catch(() => pollScreenshot());
}
const _cardId = new URLSearchParams(location.search).get('card');
if (_cardId) loadCardContext(_cardId); else pollScreenshot();

// ─── 粘贴图片 ────────────────────────────────────────────────────────────────

document.addEventListener('paste', e => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault();
      const blob = item.getAsFile();
      const reader = new FileReader();
      reader.onload = ev => {
        const src = ev.target.result;
        pastedImgB64 = src.split(',')[1];
        document.getElementById('paste-thumb').src = src;
        document.getElementById('paste-row').classList.add('has-img');
        input.focus();
      };
      reader.readAsDataURL(blob);
      break;
    }
  }
});

function clearPaste() {
  pastedImgB64 = null;
  document.getElementById('paste-thumb').src = '';
  document.getElementById('paste-row').classList.remove('has-img');
}

function toggleSearch() {
  if (sending) return;
  // 一键触发：进入搜索模式 + 立即调 send()，输入框可空（send() 内部会用默认 label）
  searchMode = true;
  document.getElementById('search-btn').classList.add('active');
  input.classList.add('search-mode');
  send();
}

function deactivateSearch() {
  searchMode = false;
  document.getElementById('search-btn').classList.remove('active');
  input.classList.remove('search-mode');
}
document.getElementById('shot-wrap').addEventListener('click', () => {
  document.getElementById('shot-area').classList.toggle('expanded');
});

// 截图区在滚动容器内自然上移划走；划出视野后顶部 sticky 小行出现，点击滚回展开。
function revealShot() {
  chat.scrollTo({top: 0, behavior: 'smooth'});
}
(function () {
  const shotArea = document.getElementById('shot-area');
  const peek = document.getElementById('shot-peek');
  if (!shotArea || !peek || !('IntersectionObserver' in window)) return;
  const io = new IntersectionObserver((entries) => {
    // 截图区基本离开视野 → 显示小行；回到视野 → 隐藏
    const e = entries[0];
    peek.style.display = e.isIntersecting ? 'none' : 'block';
  }, {root: chat, threshold: 0.01});
  io.observe(shotArea);
})();

// ─── 快捷提问按钮 ─────────────────────────────────────────────────────────────

async function fetchQBtns() {
  try { return (await fetch('api/qbtns').then(r => r.json())).btns || []; }
  catch(_) { return []; }
}

async function persistQBtns(arr) {
  try { await fetch('api/qbtns', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({btns:arr})}); }
  catch(_) {}
}

async function renderQBtns() {
  const container = document.getElementById('quick-btns');
  const btns = await fetchQBtns();
  container.innerHTML = '';
  btns.forEach((text, i) => {
    const row = document.createElement('div');
    row.className = 'qbtn-row';
    const btn = document.createElement('button');
    btn.className = 'qbtn';
    btn.textContent = text;
    btn.onclick = () => fillQuick(text);
    const acts = document.createElement('div');
    acts.className = 'qbtn-actions';
    acts.innerHTML =
      `<button class="qedit-btn" title="编辑" onclick="editQBtn(${i},event)">✎</button>` +
      `<button class="qedit-btn" title="删除" onclick="deleteQBtn(${i},event)">✕</button>`;
    row.appendChild(btn);
    row.appendChild(acts);
    container.appendChild(row);
  });
}

function fillQuick(text) {
  input.value = text;
  input.focus();
}

async function editQBtn(i, ev) {
  if (ev) ev.stopPropagation();
  const btns = await fetchQBtns();
  const container = document.getElementById('quick-btns');
  const row = container.children[i];
  if (!row) return;
  row.innerHTML = '';
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.className = 'qbtn-input';
  inp.value = btns[i] || '';
  const ok = document.createElement('button');
  ok.className = 'qedit-btn'; ok.title = '确认'; ok.textContent = '✓';
  ok.onclick = e => { e.stopPropagation(); confirmEditQBtn(i); };
  row.appendChild(inp); row.appendChild(ok);
  inp.focus(); inp.select();
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); confirmEditQBtn(i); }
    if (e.key === 'Escape') renderQBtns();
  });
}

async function confirmEditQBtn(i) {
  const container = document.getElementById('quick-btns');
  const row = container.children[i];
  if (!row) return;
  const inp = row.querySelector('.qbtn-input');
  const newText = inp ? inp.value.trim() : '';
  const btns = await fetchQBtns();
  if (newText) btns[i] = newText;
  await persistQBtns(btns);
  await renderQBtns();
}

async function deleteQBtn(i, ev) {
  if (ev) ev.stopPropagation();
  const btns = await fetchQBtns();
  btns.splice(i, 1);
  await persistQBtns(btns);
  await renderQBtns();
}

async function addQBtn() {
  const btns = await fetchQBtns();
  btns.push('新问题');
  await persistQBtns(btns);
  await renderQBtns();
  setTimeout(() => editQBtn(btns.length - 1), 30);
}

renderQBtns();

function addMsg(role, html, isHtml, imgSrc) {
  const row = document.createElement('div');
  row.className = 'msg-row ' + role;
  const ctrl = document.createElement('div');
  ctrl.className = 'bubble-controls';
  ctrl.innerHTML = '<button class="ctrl-btn" onclick="delMsg(this)">删除</button>'
                 + '<button class="ctrl-btn" onclick="resMsg(this)">恢复</button>';
  const d = document.createElement('div');
  d.className = 'bubble ' + role;
  if (role === 'assistant') {
    const c = document.createElement('div');
    c.className = 'md';
    c.innerHTML = isHtml ? html : renderMd(html);
    if (!isHtml) c.dataset.raw = html;   // 存原始文本供「有用」收集
    d.appendChild(c);
  } else {
    d.textContent = html;
    if (imgSrc) {
      const img = document.createElement('img');
      img.src = imgSrc;
      img.style.cssText = 'max-height:80px;max-width:180px;border-radius:4px;border:1px solid rgba(255,255,255,.3);display:block;margin-top:6px;object-fit:contain';
      d.appendChild(img);
    }
  }
  row.appendChild(ctrl);
  row.appendChild(d);
  // 卡片模式：气泡外侧放「选用整条回答」开关；子标题行的 + 在渲染后补
  if (role === 'assistant' && cardCtx) {
    const allBtn = document.createElement('button');
    allBtn.className = 'reply-pick-all';
    allBtn.textContent = '＋ 选用整条回答';
    allBtn.title = '把整条回答用于改进卡片/笔记';
    allBtn.onclick = () => {
      const on = allBtn.classList.toggle('on');
      allBtn.textContent = on ? '✓ 已选整条回答' : '＋ 选用整条回答';
      d.classList.toggle('picked-all', on);
      refreshUpdateButtons();
    };
    row.appendChild(allBtn);   // 在气泡外侧（msg-row 内、bubble 之后）
    if (!isHtml) addHeadingPickers(d.querySelector('.md'));
  }
  msgs.appendChild(row);
  stickBottom();
  if (role === 'assistant') typeset(d);
  return d;
}

// 给渲染好的 markdown 里的标题行补「+」按钮（卡片模式）。
// 仅当「单个最高级标题且它是全文第一个元素（即覆盖全文）」时，才跳过那个标题
// （由气泡外侧「选用整条回答」开关代劳）；其余情况（多个同级标题 / 标题前有内容）
// 所有标题都给 +，避免单层级标题时一个加号都没有。
function addHeadingPickers(md) {
  if (!md || !cardCtx) return;
  const heads = Array.from(md.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  if (!heads.length) return;
  const minLvl = Math.min(...heads.map(headLevel));
  const topHeads = heads.filter(h => headLevel(h) === minLvl);
  // 单个最高级标题 + 它是 md 第一个元素 → 它覆盖全文，等价整条按钮，跳过它的 +
  const singleTopCoversAll = topHeads.length === 1 && md.firstElementChild === topHeads[0];
  heads.forEach(h => {
    if (singleTopCoversAll && h === topHeads[0]) return;
    if (h.querySelector('.head-pick')) return;
    h.classList.add('has-pick');
    const btn = document.createElement('button');
    btn.className = 'pick-btn head-pick';
    btn.textContent = '+';
    btn.contentEditable = 'false';
    btn.onclick = (e) => { e.stopPropagation(); toggleHeadingPick(h, md); };
    h.appendChild(btn);
  });
}

// 标题级联选择：选中大标题时，其下所有更深层小标题一起选/取消
function headLevel(h) { return parseInt(h.tagName.slice(1), 10); }
function toggleHeadingPick(h, md) {
  const heads = Array.from(md.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  const i = heads.indexOf(h);
  const lvl = headLevel(h);
  const newOn = !h.querySelector('.head-pick').classList.contains('on');
  setHeadPick(h, newOn);
  // 级联到后续更深层标题，遇到同级或更高级标题停止
  for (let j = i + 1; j < heads.length; j++) {
    if (headLevel(heads[j]) <= lvl) break;
    setHeadPick(heads[j], newOn);
  }
  refreshUpdateButtons();
}
function setHeadPick(h, on) {
  const btn = h.querySelector('.head-pick');
  if (btn) btn.classList.toggle('on', on);
  h.classList.toggle('hsec-picked', on);
  // 整段高亮：标题行 + 其后到下个标题前的所有内容元素
  let sib = h.nextElementSibling;
  while (sib && !/^H[1-6]$/.test(sib.tagName)) {
    sib.classList.toggle('hsec-picked-body', on);
    sib = sib.nextElementSibling;
  }
}

function delMsg(btn) { btn.closest('.msg-row').querySelector('.bubble').classList.add('deleted'); }
function resMsg(btn) { btn.closest('.msg-row').querySelector('.bubble').classList.remove('deleted'); }

function setAllBtns(disabled) {
  // sending 期间 send 按钮变「中止」（不 disable），其它按钮 disable
  const sendBtn = document.getElementById('send-btn');
  document.getElementById('save-btn').disabled = disabled;
  document.getElementById('discard-btn').disabled = disabled;
  if (disabled && currentAbort) {
    sendBtn.textContent = '中止';
    sendBtn.disabled = false;
    sendBtn.dataset.mode = 'abort';
  } else {
    sendBtn.textContent = '发送';
    sendBtn.disabled = disabled;
    sendBtn.dataset.mode = 'send';
  }
}

// ─── 语音输入（Web Speech API，iPad Safari 支持 webkitSpeechRecognition）──────
let _recog = null, _recogOn = false, _recogBase = '';
function toggleMic() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = document.getElementById('mic-btn');
  if (!window.isSecureContext) {
    status.textContent = '⚠️ 语音输入需 HTTPS（当前 http，浏览器禁用麦克风）。可先用 iPad 键盘上的听写麦克风代替。';
    return;
  }
  if (!SR) { status.textContent = '此浏览器不支持语音输入（试试 iPad 键盘的听写麦克风）'; return; }
  if (_recogOn) { try { _recog.stop(); } catch(_){} return; }
  _recog = new SR();
  _recog.lang = 'zh-CN';
  _recog.interimResults = true;
  _recog.continuous = true;
  _recogBase = input.value;   // 在已有文本后追加
  _recog.onstart = () => { _recogOn = true; btn.classList.add('listening'); status.textContent = '🎤 聆听中…再次点击停止'; };
  _recog.onerror = (e) => { status.textContent = '语音输入出错：' + (e.error || ''); };
  _recog.onend = () => { _recogOn = false; btn.classList.remove('listening'); status.textContent = ''; input.focus(); };
  _recog.onresult = (ev) => {
    let txt = '';
    for (let i = 0; i < ev.results.length; i++) txt += ev.results[i][0].transcript;
    input.value = (_recogBase ? _recogBase + ' ' : '') + txt;
  };
  try { _recog.start(); } catch(_) { status.textContent = '无法启动语音输入'; }
}

async function send() {
  // sending 模式下 send 按钮 = 中止按钮
  if (sending) {
    if (currentAbort) { try { currentAbort.abort(); } catch(_){} }
    return;
  }
  const text = input.value.trim();
  if (!text && !searchMode) return;
  input.value = '';

  // ── 关联知识搜索模式 ──────────────────────────────────────────────────────
  if (searchMode) {
    const label = text || '搜索当前内容相关知识点';
    deactivateSearch();
    addMsg('user', label);
    sending = true; setAllBtns(true);
    const typing = addMsg('assistant', '<span class="typing">搜索中…</span>', true);
    status.textContent = '';
    try {
      const r = await fetch('api/search-related', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: text}),
      });
      const d = await r.json();
      typing.remove();
      addMsg('assistant', d.response);
    } catch(e) {
      typing.querySelector('.typing').textContent = '搜索失败：' + e.message;
      typing.querySelector('.typing').style.color = '#c00';
    }
    sending = false; setAllBtns(false); input.focus();
    return;
  }

  // ── 普通对话（SSE 流式） ──────────────────────────────────────────────────
  const imgSrc = pastedImgB64 ? document.getElementById('paste-thumb').src : null;
  autoStick = true;   // 用户主动发消息：先贴底，之后若上翻则停止跟随
  addMsg('user', text, false, imgSrc);
  const imgB64 = pastedImgB64;
  if (pastedImgB64) clearPaste();
  sending = true;
  currentAbort = new AbortController();   // 在 setAllBtns 前创建，让按钮显示「中止」
  setAllBtns(true);
  const typing = addMsg('assistant', '<span class="typing">思考中…</span>', true);
  status.textContent = '';

  const reqBody = { message: text };
  if (pendingHistory) { reqBody.history = pendingHistory; pendingHistory = null; }
  if (imgB64) reqBody.image_b64 = imgB64;
  // 卡片模式：把卡片两面作为上下文随请求发给后端（后端只在会话首条注入）
  if (cardCtx) reqBody.card_context = {
    type: cardCtx.type, front: cardCtx.front, back: cardCtx.back, text: cardCtx.text,
  };

  // 准备 streaming 状态
  const mdEl = typing.querySelector('.md');
  let accumulated = '';
  let lastRender = 0;
  const RENDER_MS = 120;     // 节流：每 120ms 重渲染一次（marked + MathJax）
  let renderQueued = false;
  function renderNow() {
    mdEl.innerHTML = renderMd(accumulated || ' ');
    stickBottom();   // 仅当用户在底部附近才跟随；往上翻阅时不打扰
    typeset(mdEl);
    lastRender = Date.now();
    renderQueued = false;
  }
  function maybeRender() {
    const now = Date.now();
    if (now - lastRender >= RENDER_MS) { renderNow(); }
    else if (!renderQueued) {
      renderQueued = true;
      setTimeout(renderNow, RENDER_MS - (now - lastRender));
    }
  }

  try {
    const r = await fetch('api/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept':       'text/event-stream',
      },
      body: JSON.stringify(reqBody),
      signal: currentAbort.signal,
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let streamErr = null;
    let firstChunk = true;
    outer: while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});
      let idx;
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6);
          try {
            const event = JSON.parse(data);
            if (event.error) { streamErr = event.error; }
            else if (event.done) { break outer; }
            else if (event.text) {
              if (firstChunk) {
                // 第一个 chunk 到了，清掉 "思考中..." placeholder
                mdEl.innerHTML = '';
                firstChunk = false;
              }
              accumulated += event.text;
              maybeRender();
            }
          } catch (_) {}
        }
      }
    }
    renderNow();   // 流结束，做最后一次完整渲染
    mdEl.dataset.raw = accumulated;   // 存原始文本供「整条选中」收集
    if (cardCtx) addHeadingPickers(mdEl);   // 卡片模式：给标题行补 + 按钮
    if (streamErr) {
      const err = document.createElement('div');
      err.style.cssText = 'color:#c00;margin-top:6px;font-size:13px';
      err.textContent = '⚠️ ' + streamErr;
      mdEl.appendChild(err);
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      accumulated += '\n\n_[已中止]_';
    } else {
      accumulated += '\n\n_[错误：' + e.message + ']_';
    }
    renderNow();
  } finally {
    currentAbort = null;
  }
  sending = false;
  setAllBtns(false);
  input.focus();
}

async function save() {
  if (sending) return;
  const saveBtn = document.getElementById('save-btn');
  setAllBtns(true); saveBtn.textContent = '保存中…';
  status.textContent = '正在整理对话并写入笔记…';
  try {
    const r = await fetch('api/save', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:'{}',
    });
    const d = await r.json();
    status.textContent = '✓ ' + d.result;
    saveBtn.textContent = '已保存 ✓'; saveBtn.style.background = '#666';
    addMsg('assistant', '📝 ' + d.result);
    setTimeout(() => window.close(), 2000);
  } catch(e) {
    status.textContent = '保存失败：' + e.message;
    setAllBtns(false); saveBtn.textContent = '保存笔记';
  }
}

async function discard() {
  setAllBtns(true);
  try { await fetch('api/discard', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); } catch(_){}
  document.body.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:10px;color:#888">'
    + '<p style="font-size:15px">本次对话已放弃</p><p style="font-size:13px">可以关闭此标签页</p></div>';
  window.close();
}

// ─── 历史抽屉 ─────────────────────────────────────────────────────────────────

async function openSidebar() {
  document.getElementById('hist-backdrop').classList.add('open');
  document.getElementById('hist-sidebar').classList.add('open');
  document.getElementById('history-btn').classList.add('active');
  await loadHistoryList();
}

function closeSidebar() {
  document.getElementById('hist-backdrop').classList.remove('open');
  document.getElementById('hist-sidebar').classList.remove('open');
  document.getElementById('history-btn').classList.remove('active');
  input.focus();
}

async function loadHistoryList() {
  const list  = document.getElementById('hist-list');
  const title = document.getElementById('hist-head-title');
  list.innerHTML = '<div id="hist-empty">加载中…</div>';
  try {
    const d = await fetch('api/history').then(r => r.json());
    const n = d.entries.length;
    title.textContent = n ? `历史记录（${n} 条）` : '历史记录';
    if (!n) { list.innerHTML = '<div id="hist-empty">暂无历史记录</div>'; return; }

    list.innerHTML = '';
    d.entries.forEach(e => {
      const el = document.createElement('div');
      const isWrong = e.record_type === 'wrong';
      el.className = 'hist-entry' + (isWrong ? ' wrong' : '');
      el.dataset.id = e.id;
      const thumb = e.img_fname
        ? `<img class="hist-thumb" src="api/image/${e.img_fname}" onerror="this.className='hist-thumb-blank'">`
        : `<div class="hist-thumb-blank"></div>`;
      const noteShort = e.note
        .replace(/^已保存\(错题\) → .*?\\/, '').replace(/^已保存\(错题\) → /, '')
        .replace(/^已保存 → .*?\\/, '').replace(/^已保存 → /, '');
      const badge = isWrong ? '<span class="hist-wrong-badge">错题</span>' : '';
      let chipsHtml = '';
      if (isWrong && e.related_cards && e.related_cards.length) {
        chipsHtml = '<div class="hist-chips">' + e.related_cards.map(c => {
          const short = c.note.replace(/^[0-9A-Fa-f]{3}-/, '');
          const pct   = c.mastery != null ? ' ' + c.mastery + '%' : '';
          const noteAttr = c.note.replace(/"/g, '&quot;');
          return `<span class="hist-chip" data-id="${e.id}" data-note="${noteAttr}"` +
                 ` onmousedown="chipPressStart(event,this)" onmouseup="chipPressEnd(event)" onmouseleave="chipPressEnd(event)"` +
                 ` ontouchstart="chipPressStart(event,this)" ontouchend="chipPressEnd(event)">${short}${pct}</span>`;
        }).join('') + '</div>';
      }
      el.innerHTML = `
        ${thumb}
        <div class="hist-meta">
          <div class="hist-ts">${e.timestamp}${badge}</div>
          <div class="hist-note" title="${e.note}">${noteShort || e.note || '（未保存）'}</div>
          <div class="hist-count">${e.msg_count} 条消息</div>
          ${chipsHtml}
        </div>
        <button class="hist-del" title="删除">✕</button>
      `;
      el.querySelector('.hist-del').addEventListener('click', ev => {
        ev.stopPropagation();
        deleteHistory(e.id, el);
      });
      el.addEventListener('click', () => loadConversation(e.id, e.timestamp, e.img_fname));
      list.appendChild(el);
    });
  } catch(e) {
    list.innerHTML = `<div id="hist-empty" style="color:#c00">加载失败：${e.message}</div>`;
  }
}

async function deleteHistory(id, el) {
  el.style.opacity = '0.4';
  try {
    const d = await fetch('api/history/delete', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ id }),
    }).then(r => r.json());
    if (d.ok) {
      el.remove();
      const list  = document.getElementById('hist-list');
      const title = document.getElementById('hist-head-title');
      const n = list.querySelectorAll('.hist-entry').length;
      if (!n) { list.innerHTML = '<div id="hist-empty">暂无历史记录</div>'; title.textContent = '历史记录'; }
      else title.textContent = `历史记录（${n} 条）`;
    } else {
      el.style.opacity = '';
    }
  } catch(_) { el.style.opacity = ''; }
}

async function loadConversation(id, timestamp, imgFname) {
  closeSidebar();

  let data;
  try {
    data = await fetch('api/history/' + id).then(r => r.json());
    if (data.error) throw new Error(data.error);
  } catch(e) { status.textContent = '加载失败：' + e.message; return; }

  // 重置后端会话
  await fetch('api/reset', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});

  // 切换顶部截图为历史截图
  if (imgFname) document.getElementById('shot').src = 'api/image/' + imgFname;

  // 清空并渲染历史消息（只清消息容器，截图区保留）
  msgs.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'hist-divider';
  div.innerHTML = `<div class="hist-divider-line"></div><span>${timestamp}</span><div class="hist-divider-line"></div>`;
  msgs.appendChild(div);

  for (const m of data.messages || []) addMsg(m.role, m.text);

  const hint = document.createElement('div');
  hint.className = 'hist-continue-hint';
  hint.textContent = '─ 输入问题可继续此对话 ─';
  msgs.appendChild(hint);
  chat.scrollTop = chat.scrollHeight;

  pendingHistory = data.messages || [];
  input.focus();
}

input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
input.focus();

// ─── AI 设置 ──────────────────────────────────────────────────────────────────

function updateSettFields() {
  const b = document.getElementById('s-backend').value;
  document.getElementById('s-claude-fields').style.display = (b === 'claude_cli') ? '' : 'none';
  document.getElementById('s-codex-fields').style.display  = (b === 'codex_cli')  ? '' : 'none';
}

async function openSettings() {
  try {
    const s = await fetch('api/settings').then(r => r.json());
    document.getElementById('s-backend').value      = s.backend || 'claude_cli';
    document.getElementById('s-claude-model').value  = (s.claude && s.claude.model)  || '';
    document.getElementById('s-claude-effort').value = (s.claude && s.claude.effort) || '';
    document.getElementById('s-codex-model').value   = (s.codex  && s.codex.model)   || '';
    document.getElementById('s-delete-original').checked = !!s.delete_original;
  } catch(_) {}
  updateSettFields();
  document.getElementById('sett-overlay').classList.add('open');
  document.getElementById('sett-modal').classList.add('open');
}

function closeSettings() {
  document.getElementById('sett-overlay').classList.remove('open');
  document.getElementById('sett-modal').classList.remove('open');
  input.focus();
}

async function saveSettings() {
  const backend = document.getElementById('s-backend').value;
  const payload = {
    backend,
    claude: {
      model:  document.getElementById('s-claude-model').value.trim(),
      effort: document.getElementById('s-claude-effort').value,
    },
    codex: {
      model: document.getElementById('s-codex-model').value.trim(),
    },
    delete_original: document.getElementById('s-delete-original').checked,
  };
  try {
    const r = await fetch('api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (j.ok === false) { status.textContent = '保存失败：' + (j.error || ''); return; }
    const cl = backend === 'claude_cli'
      ? `${payload.claude.model || '默认模型'}${payload.claude.effort ? ' / ' + payload.claude.effort : ''}`
      : (payload.codex.model || '默认模型');
    status.textContent = `AI 设置已保存：${backend} · ${cl}`;
  } catch(_) { status.textContent = '保存失败'; }
  closeSettings();
}

// ─── 知识关联芯片（长按删除）───────────────────────────────────────────────────

let _chipTimer = null;
let _chipEl    = null;

function chipPressStart(ev, el) {
  ev.stopPropagation();
  _chipEl = el;
  el.classList.add('pressing');
  _chipTimer = setTimeout(async () => {
    el.classList.remove('pressing');
    _chipEl = null;
    const id = el.dataset.id, note = el.dataset.note;
    el.style.opacity = '0.3';
    try {
      await fetch('api/history/cards/update', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id, note}),
      });
      const wrap = el.parentElement;
      el.remove();
      if (wrap && !wrap.querySelector('.hist-chip')) wrap.remove();
    } catch(_) { el.style.opacity = ''; }
  }, 600);
}

function chipPressEnd(ev) {
  if (ev) ev.stopPropagation();
  if (_chipTimer) { clearTimeout(_chipTimer); _chipTimer = null; }
  if (_chipEl) { _chipEl.classList.remove('pressing'); _chipEl = null; }
}
</script>
</body>
</html>"""

# ─── HTTP 服务器 ────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端已断开（移动端锁屏/切后台），结果送不回，静默忽略

    def do_GET(self):
        # 根路径（含带查询串如 /?card=<id> 的卡片模式）都返回主页面 HTML
        if self.path == "/" or self.path.startswith("/?"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/screenshot":
            self.send_json({"data": state["img_b64"]})

        elif self.path == "/api/settings":
            # 服务器模式：从 server-config 回显完整 AI 设置；客户端模式退回旧 shim
            if state.get("_server_mode"):
                self.send_json(_load_ai_settings_for_ui())
            else:
                self.send_json(ai_client.load_settings())

        elif self.path == "/api/qbtns":
            self.send_json({"btns": load_qbtns()})

        elif self.path == "/api/history":
            with db() as conn:
                rows = conn.execute(
                    "SELECT id, timestamp, img_fname, note, messages, record_type, related_cards "
                    "FROM conversations ORDER BY timestamp DESC"
                ).fetchall()
            entries = []
            for r in rows:
                msgs = json.loads(r["messages"] or "[]")
                entries.append({
                    "id":            r["id"],
                    "timestamp":     r["timestamp"],
                    "img_fname":     r["img_fname"] or "",
                    "note":          r["note"] or "",
                    "msg_count":     len(msgs),
                    "record_type":   r["record_type"] or "normal",
                    "related_cards": json.loads(r["related_cards"] or "[]"),
                })
            self.send_json({"entries": entries})

        elif self.path.startswith("/api/image/"):
            fname = self.path[len("/api/image/"):].split("?")[0]
            if re.match(r'^[A-Za-z0-9._-]+$', fname):
                img_path = HIST_IMG_DIR / fname
                if img_path.exists():
                    data = img_path.read_bytes()
                    ext = img_path.suffix.lower()
                    ct  = "image/png" if ext == ".png" else "image/jpeg"
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404); self.end_headers()
            else:
                self.send_response(400); self.end_headers()

        elif self.path.startswith("/api/history/"):
            hid = self.path[len("/api/history/"):].split("?")[0]
            if re.match(r'^[A-Za-z0-9_-]+$', hid):
                with db() as conn:
                    row = conn.execute(
                        "SELECT id, timestamp, img_fname, note, messages, related_cards "
                        "FROM conversations WHERE id=?", (hid,)
                    ).fetchone()
                if row:
                    self.send_json({
                        "id":            row["id"],
                        "timestamp":     row["timestamp"],
                        "img_fname":     row["img_fname"] or "",
                        "note":          row["note"] or "",
                        "messages":      json.loads(row["messages"] or "[]"),
                        "related_cards": json.loads(row["related_cards"] or "[]"),
                    })
                else:
                    self.send_json({"error": "not found"}, 404)
            else:
                self.send_json({"error": "invalid id"}, 400)

        elif self.path.startswith("/api/card-context"):
            from urllib.parse import urlparse, parse_qs
            cid = (parse_qs(urlparse(self.path).query).get("card") or [""])[0]
            ctx = _find_card_context(cid)
            self.send_json(ctx or {"error": "not found"}, 200 if ctx else 404)

        elif self.path.startswith("/api/card-update-status"):
            from urllib.parse import urlparse, parse_qs
            jid = (parse_qs(urlparse(self.path).query).get("job") or [""])[0]
            job = _card_jobs.get(jid)
            if not job:
                self.send_json({"status": "unknown"}, 404)
            else:
                self.send_json(job)
                if job.get("status") == "done":
                    _card_jobs.pop(jid, None)   # 取走结果后清理

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/chat":
            msg     = body.get("message", "").strip()
            history = body.get("history")
            img_b64 = body.get("image_b64")
            card_ctx = body.get("card_context")
            session = state["session"]
            stream_mode = "text/event-stream" in (self.headers.get("Accept") or "")

            if history and session._first and not session.messages:
                session.messages = list(history)
                img = None                          # 续聊历史时不传当前截图
            else:
                img = str(state["temp_path"]) if state.get("temp_path") else None

            # 卡片模式：会话首条消息时把卡片两面拼进 msg，让 AI 知道在讨论哪张卡
            if card_ctx and not session.messages:
                blk = ["以下是我正在复习的一张 Anki 记忆卡片，请基于它回答我接下来的问题。"]
                if card_ctx.get("front"): blk.append(f"【卡片正面（问）】\n{card_ctx['front']}")
                if card_ctx.get("text"):  blk.append(f"【卡片内容】\n{card_ctx['text']}")
                if card_ctx.get("back"):  blk.append(f"【卡片背面（答）】\n{card_ctx['back']}")
                blk.append(f"———\n我的问题：{msg}" if msg else "———\n请先帮我讲解这张卡片的核心内容。")
                msg = "\n\n".join(blk)

            paste_tmp = None
            if img_b64:
                try:
                    paste_tmp = TEMP_DIR / f"paste-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.png"
                    TEMP_DIR.mkdir(parents=True, exist_ok=True)
                    raw_bytes = base64.b64decode(img_b64)
                    try:
                        import pillow_heif
                        pillow_heif.register_heif_opener()
                    except ImportError:
                        pass
                    try:
                        img_obj = Image.open(io.BytesIO(raw_bytes))
                        buf = io.BytesIO()
                        img_obj.convert("RGB").save(buf, format="PNG")
                        paste_tmp.write_bytes(buf.getvalue())
                    except Exception:
                        paste_tmp.write_bytes(raw_bytes)
                    img = str(paste_tmp)
                except Exception:
                    paste_tmp = None

            try:
                if stream_mode:
                    # SSE：边产生边推
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Accel-Buffering", "no")  # 防 nginx buffer
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    def _send_event(obj: dict) -> bool:
                        """写一个 SSE event。客户端断开返回 False。"""
                        try:
                            line = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
                            self.wfile.write(line.encode("utf-8"))
                            self.wfile.flush()
                            return True
                        except (BrokenPipeError, ConnectionResetError):
                            return False

                    gen = session.send_stream(msg, image_path=img)
                    try:
                        for chunk in gen:
                            if not _send_event({"text": chunk}):
                                gen.close()
                                return
                    except Exception as e:
                        _send_event({"error": str(e)})
                    _send_event({"done": True})
                else:
                    # 兼容旧 JSON 模式
                    resp = session.send(msg, image_path=img)
                    self.send_json({"response": resp})
            finally:
                if paste_tmp:
                    try: paste_tmp.unlink(missing_ok=True)
                    except Exception: pass

        elif self.path == "/api/inject-image":
            # 远程截图注入：接收 base64，重置会话
            img_b64 = body.get("image_b64", "")
            if img_b64:
                try:
                    TEMP_DIR.mkdir(parents=True, exist_ok=True)
                    ts        = datetime.now().strftime("%Y%m%d-%H%M%S")
                    img_fname = f"remote-{ts}.png"
                    temp_path = TEMP_DIR / img_fname
                    raw_bytes = base64.b64decode(img_b64)
                    # 统一转为 PNG（兼容 HEIC / JPEG / WebP 等 iPad 格式）
                    try:
                        import pillow_heif
                        pillow_heif.register_heif_opener()
                    except ImportError:
                        pass
                    try:
                        img_obj = Image.open(io.BytesIO(raw_bytes))
                        buf = io.BytesIO()
                        img_obj.convert("RGB").save(buf, format="PNG")
                        temp_path.write_bytes(buf.getvalue())
                    except Exception:
                        temp_path.write_bytes(raw_bytes)
                    state.update({
                        "img_b64":   img_b64,
                        "img_fname": img_fname,
                        "temp_path": temp_path,
                    })
                    state["session"].reset()
                    print(f"[remote-qa] 收到新截图 {img_fname}")
                    self.send_json({"ok": True})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)})
            else:
                self.send_json({"ok": False, "error": "no image_b64"})

        elif self.path == "/api/card-update":
            # target: note | anki | all。pairs: [{question, answer}, ...]（标记有用的问答）
            local_id = (body.get("local_id") or "").strip()
            target   = (body.get("target") or "").strip()
            pairs    = body.get("pairs") or []
            if not local_id or target not in ("note", "anki", "all"):
                self.send_json({"ok": False, "error": "invalid params"}, 400)
            elif not pairs:
                self.send_json({"ok": False, "error": "没有标记为有用的回答"}, 400)
            else:
                # 后台跑（AI 调用慢，移动端连接易断）；立即返回 job_id 供前端轮询
                import uuid
                job_id = uuid.uuid4().hex[:12]
                _card_jobs[job_id] = {"status": "running"}
                def _run(job_id=job_id, local_id=local_id, target=target, pairs=pairs):
                    out = {"ok": True}
                    try:
                        if target in ("anki", "all"):
                            out["anki"] = _card_update_anki(local_id, pairs)
                        if target in ("note", "all"):
                            out["note"] = _card_update_note(local_id, pairs)
                    except Exception as e:
                        out = {"ok": False, "error": str(e)}
                    _card_jobs[job_id] = {"status": "done", "result": out}
                threading.Thread(target=_run, daemon=True).start()
                self.send_json({"ok": True, "job_id": job_id})

        elif self.path == "/api/card-delete":
            lid = (body.get("local_id") or "").strip()
            if not lid:
                self.send_json({"ok": False, "error": "missing local_id"}, 400)
            else:
                self.send_json(_card_delete(lid))

        elif self.path == "/api/save":
            try:
                cls      = classify_conversation()
                match    = cls.get("match", "")
                is_wrong = bool(cls.get("wrong", False))
                related  = find_related_cards(cls.get("related", []), match) if is_wrong else []
                result   = do_save(match, is_wrong, related)
                archive_conversation(result, "wrong" if is_wrong else "normal", related)
                threading.Thread(
                    target=push_to_website,
                    args=(state["img_fname"],),
                    daemon=True,
                ).start()
            except Exception as e:
                result = f"保存失败：{e}"
            _export_history_to_webapp()  # 服务端实例：让 webapp /history/ 立刻看到新条目
            self.send_json({"result": result})
            if not state.get("_server_mode"):
                state["done"].set()
            else:
                # server 模式：保存后重置会话继续服务
                state["session"].reset()
                state.update({"img_b64": None, "img_fname": None, "temp_path": None})

        elif self.path == "/api/discard":
            self.send_json({"ok": True})
            if not state.get("_server_mode"):
                state["done"].set()
            else:
                state["session"].reset()
                state.update({"img_b64": None, "img_fname": None, "temp_path": None})

        elif self.path == "/api/settings":
            # 服务器模式：写回 server-config.json（get_cfg 每次重读，即时生效）
            if state.get("_server_mode"):
                self.send_json(_save_ai_settings_from_ui(body))
            else:
                self.send_json(ai_client.save_settings(body))

        elif self.path == "/api/qbtns":
            btns = body.get("btns", [])
            if isinstance(btns, list):
                save_qbtns(btns)
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False})

        elif self.path == "/api/reset":
            state["session"].reset()
            self.send_json({"ok": True})

        elif self.path == "/api/history/delete":
            hid = body.get("id", "")
            # 默认连 Obsidian 笔记也清掉；调用方传 keep_note=true 时仅删数据库 + 截图。
            keep_note = bool(body.get("keep_note"))
            cleanup_results: list[str] = []
            if not re.match(r'^[A-Za-z0-9_-]+$', hid):
                self.send_json({"ok": False, "error": "invalid id"})
            else:
                with db() as conn:
                    row = conn.execute(
                        "SELECT img_fname, note FROM conversations WHERE id=?", (hid,)
                    ).fetchone()
                    conn.execute("DELETE FROM conversations WHERE id=?", (hid,))
                if row:
                    # 1. 截图文件
                    if row["img_fname"]:
                        img = HIST_IMG_DIR / row["img_fname"]
                        if img.exists():
                            try: img.unlink(); cleanup_results.append(f"截图 {img.name}")
                            except Exception as e: cleanup_results.append(f"截图删除失败: {e}")
                    # 2. Obsidian 笔记（除非 keep_note）
                    if not keep_note and row["note"]:
                        # note 字段格式：「已保存(普通) → /path/to/note.md」或
                        #               「已保存(错题) → C:\path\to\note.md」
                        m = re.search(r'→\s*([^\n]+\.md)', row["note"])
                        if m:
                            note_path_raw = m.group(1).strip()
                            note_path = Path(note_path_raw)
                            # Windows 路径在 Linux 上不可达；只删本机上存在的
                            if note_path.exists() and note_path.is_file():
                                try:
                                    note_path.unlink()
                                    cleanup_results.append(f"笔记 {note_path.name}")
                                except Exception as e:
                                    cleanup_results.append(f"笔记删除失败: {e}")
                            elif VAULT is not None:
                                # 备选：在 vault 习题/错题 目录下按文件名查找（兼容跨平台路径）
                                fname = note_path.name
                                for sub in [EXERCISES_DIR, WRONG_DIR]:
                                    if sub is None: continue
                                    candidate = sub / fname
                                    if candidate.exists():
                                        try:
                                            candidate.unlink()
                                            cleanup_results.append(f"笔记 {candidate.name}")
                                        except Exception as e:
                                            cleanup_results.append(f"笔记删除失败: {e}")
                                        break
                _export_history_to_webapp()
                self.send_json({"ok": True, "cleaned": cleanup_results})

        elif self.path == "/api/search-related":
            query = body.get("query", "").strip()
            notes_map = load_index_notes()
            if not notes_map:
                self.send_json({"response": "（索引为空，无法搜索关联知识）"})
                return

            notes_text = "\n".join(
                f"- {n}: {info['keywords']} — {info['summary']}"
                for n, info in list(notes_map.items())[:80]
            )
            msgs_ctx = "\n".join(
                f"{'用户' if m['role'] == 'user' else 'Claude'}：{m['text'][:200]}"
                for m in state["session"].messages[-6:]
            )
            if not msgs_ctx.strip() and not query:
                self.send_json({"response": "（请先进行对话，或在输入框补充搜索关键词）"})
                return
            if query:
                msgs_ctx += f"\n\n补充说明：{query}"

            prompt = (
                "根据以下对话内容，从知识库中找出最相关的 3-6 篇笔记，并说明每篇的关联原因（15 字以内）。\n\n"
                f"对话内容：\n{msgs_ctx}\n\n"
                f"知识库笔记：\n{notes_text}\n\n"
                "只输出 JSON 数组：\n"
                '[{"note": "笔记名", "reason": "关联原因"}, ...]'
            )
            raw     = ai_client.ask(prompt)
            related = []
            s, e    = raw.find('['), raw.rfind(']')
            if s != -1 and e > s:
                try:
                    related = json.loads(raw[s:e + 1])
                except Exception:
                    pass

            if not related:
                resp_text = "未找到明显关联的知识点。"
            else:
                lines = ["**关联知识点**\n"]
                for item in related:
                    note   = item.get("note", "")
                    reason = item.get("reason", "")
                    if not note:
                        continue
                    mastery = get_mastery(note)
                    m_str   = f"  掌握 {mastery}%" if mastery is not None else ""
                    lines.append(f"- [[{note}]] — {reason}{m_str}")
                resp_text = "\n".join(lines)

            user_q  = query if query else "搜索当前内容相关知识点"
            session = state["session"]
            session.messages.append({"role": "user",      "text": f"[关联搜索] {user_q}"})
            session.messages.append({"role": "assistant", "text": resp_text})
            session._first = False
            self.send_json({"response": resp_text})

        elif self.path == "/api/history/cards/update":
            hid  = body.get("id", "")
            note = body.get("note", "")
            if re.match(r'^[A-Za-z0-9_-]+$', hid) and note:
                with db() as conn:
                    row = conn.execute("SELECT related_cards FROM conversations WHERE id=?", (hid,)).fetchone()
                    if row:
                        cards = json.loads(row["related_cards"] or "[]")
                        cards = [c for c in cards if c.get("note") != note]
                        conn.execute("UPDATE conversations SET related_cards=? WHERE id=?",
                                     (json.dumps(cards, ensure_ascii=False), hid))
                _export_history_to_webapp()
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False})

        else:
            self.send_response(404); self.end_headers()

# ─── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    init_db()

    initial_hash = get_clipboard_hash()
    time.sleep(0.3)
    trigger_snip_tool()

    image = wait_for_screenshot(initial_hash)
    if image is None:
        msgbox(f"超时（{SCREENSHOT_TIMEOUT}s）：未检测到新截图，程序退出。")
        sys.exit(1)

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d-%H%M%S")
    img_fname = f"screenshot-{ts}.png"
    temp_path = TEMP_DIR / img_fname
    image.save(temp_path)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    state.update({
        "img_b64":   base64.b64encode(buf.getvalue()).decode(),
        "img_fname": img_fname,
        "temp_path": temp_path,
    })

    cfg = _GET_CFG() or {}
    remote_access = bool(cfg.get("qa_remote_access"))
    bind_host = "0.0.0.0" if remote_access else "localhost"

    port   = find_free_port()
    server = ThreadedHTTPServer((bind_host, port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    local_url = f"http://localhost:{port}"
    if remote_access:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            print(f"[qa] 本机: {local_url}    远程: http://{ip}:{port}")
        except Exception:
            pass

    # 浏览器选择优先级：cfg.qa_browser_path → 系统 Chrome（--app 模式）→ webbrowser.open
    browser = (cfg.get("qa_browser_path") or "").strip()
    try:
        if browser and Path(browser).exists():
            # 用户指定的浏览器：普通"打开 URL"方式（Chromium 系也接受 --app=URL 但 Firefox/Edge 不一样，
            # 用 plain url 兼容性最好；如果用户希望 app 模式可以在客户端 GUI 后续加选项）
            subprocess.Popen([browser, local_url])
        elif CHROME_EXE:
            subprocess.Popen([CHROME_EXE, f"--app={local_url}", "--window-size=920,720"])
        else:
            webbrowser.open(local_url)
    except Exception:
        webbrowser.open(local_url)

    state["done"].wait(timeout=1800)
    time.sleep(1.5)
    server.shutdown()
    server.server_close()
    t.join(timeout=3)


def server_mode():
    """命令行 --server 入口：阻塞式持久化服务器。"""
    start_server_daemon(get_cfg=lambda: {}, port=5001, bind="127.0.0.1")
    print(f"[remote-qa] 服务器已启动 http://127.0.0.1:5001")
    print(f"[remote-qa] 等待 iPad 截图注入...")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[remote-qa] 已停止")


def start_server_daemon(
    get_cfg, *, port: int = 9091, bind: str = "127.0.0.1"
) -> "ThreadedHTTPServer | None":
    """常驻 server 模式 — 由客户端 GUI 在启动时调起，不阻塞。

    cmd_server (9090) 收到 /qa-page、/api/* 后反代到这个 daemon。
    bind 默认 127.0.0.1（不暴露 LAN/Tailscale，安全靠 cmd_server 的 _auth）。

    返回 ThreadedHTTPServer；若启动失败返回 None（不抛，避免拖垮 GUI）。
    """
    global _GET_CFG
    _GET_CFG = get_cfg
    try:
        cfg = get_cfg() or {}
        _init_paths(cfg)
        init_db()
        state.update({
            "img_b64": None, "img_fname": None, "temp_path": None,
            "_server_mode": True,
        })
        server = ThreadedHTTPServer((bind, int(port)), Handler)
    except Exception as e:
        print(f"[qa-daemon] 启动失败: {e}")
        return None
    t = threading.Thread(target=server.serve_forever, daemon=True, name="qa-daemon")
    t.start()
    print(f"[qa-daemon] http://{bind}:{port}")
    return server


def launch(get_cfg) -> None:
    """客户端入口。GUI 在子线程调用 launch(get_cfg) 启动一次截图问答会话。

    get_cfg: callable() -> dict，返回当前 client config
    """
    global _GET_CFG
    _GET_CFG = get_cfg
    cfg = get_cfg() or {}
    _init_paths(cfg)
    if VAULT is None:
        msgbox("未配置 vault 路径\n\n请先在客户端 GUI 的「截图问答」Tab 设 Obsidian vault 目录")
        return
    main()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--server":
        server_mode()
    else:
        main()
