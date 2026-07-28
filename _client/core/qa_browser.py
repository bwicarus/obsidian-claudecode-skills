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
from card_improvement_service import (  # type: ignore
    CardImprovementError,
    CardReference,
    JsonEntityRegistryResolver,
    pairs_text as _shared_pairs_text,
)


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
    "**数学公式严格用 Markdown 数学语法**：行内公式 $...$，行间公式 $$...$$；"
    "**不要**用反引号 ` 包裹数学表达式（这样在前端会被当成代码块灰底显示而非公式），"
    "也不要用 \\(...\\) 或 \\[...\\]。"
    "例如：要写 $F^S$ 而不是 `F^S`，要写 $a_1, \\ldots, a_n$ 而不是 `a_1,...,a_n`。"
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


def _entity_registry_path() -> "Path | None":
    """Legacy QA daemon's explicitly configured card-entity registry.

    The account-scoped reader must inject its authorized registry path when it
    calls the shared resolver.  This old standalone daemon has no authenticated
    reader account context, so it only uses an explicit config path or the
    historical single-user ``state/assets/registry.json`` fallback.
    """
    cfg = _GET_CFG() or {}
    explicit = str(cfg.get("reader_entity_registry") or "").strip()
    if explicit:
        return Path(explicit)
    project = str(os.environ.get("CLAUDE_PROJECT") or "").strip()
    return (Path(project) / "state" / "assets" / "registry.json") if project else None


def _entity_card_context(local_id: str, index=None):
    try:
        ref = CardReference.parse(local_id, index)
    except CardImprovementError:
        return None
    if not ref.is_entity:
        return None
    path = _entity_registry_path()
    if not path:
        return None
    try:
        return JsonEntityRegistryResolver(path).resolve(ref)
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError):
        return None


def _find_card_context(local_id: str, index=None):
    """QA ``?card=...&index=N`` lookup with lossless legacy fallbacks."""
    entity_ctx = _entity_card_context(local_id, index)
    if entity_ctx:
        return entity_ctx
    if not local_id or not (ANKI_RECORDS_DIR and ANKI_RECORDS_DIR.exists()):
        return _card_context_from_anki(local_id, index)
    try:
        ref = CardReference.parse(local_id, index)
    except CardImprovementError:
        ref = CardReference(str(local_id or "").strip(), None)
    record_ids = [local_id]
    if ref.is_entity and ref.index is not None:
        record_ids.extend((f"{ref.card_id}:{ref.index}", f"{ref.card_id}/{ref.index}"))
    for fn in ANKI_RECORDS_DIR.glob("*.json"):
        try:
            rec = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in rec.get("cards") or []:
            if c.get("local_id") in record_ids:
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
                    "deck": c.get("deck", "Obsidian::未分类"),
                    "anki_note_id": c.get("anki_note_id"),
                    "source_note": source_note,
                    "source_link": rec.get("source_link", ""),
                    "source_url": source_url,
                    "entity_id": ref.card_id if ref.is_entity else "",
                    "entity_index": ref.index,
                }
    # records 没有 → 直接查 Anki（覆盖未被 records 跟踪的游离卡），否则 QA 页空白
    return _card_context_from_anki(local_id, index)


def _card_context_from_anki(local_id: str, index=None):
    """records 查不到时，按 footer 里的 Local ID 从 Anki 直接取卡两面 + 来源。"""
    import html as _html
    _FOOTER = '<hr><div style="font-size:0.85em;color:#666;">'
    try:
        ref = CardReference.parse(local_id, index)
        query_ids = [str(local_id or "").strip()]
        if ref.is_entity and ref.index is not None:
            query_ids = [f"{ref.card_id}:{ref.index}", f"{ref.card_id}/{ref.index}", ref.card_id]
        nids = []
        for query_id in dict.fromkeys(q for q in query_ids if q):
            nids = _anki_request("findNotes", {"query": f'"{query_id}"'}) or []
            if nids:
                break
        if not nids:
            return None
        info = (_anki_request("notesInfo", {"notes": nids[:1]}) or [None])[0]
        if not info or "fields" not in info:
            return None
        fields = info["fields"]
        def _strip(v):
            i = v.find(_FOOTER);
            return (v[:i] if i != -1 else v).strip()
        is_cloze = "Text" in fields and "Front" not in fields
        if is_cloze:
            front, back, text = "", _strip(fields.get("Extra", {}).get("value", "")), \
                                _strip(fields.get("Text", {}).get("value", ""))
            footer_src = fields.get("Extra", {}).get("value", "")
        else:
            front, back, text = _strip(fields.get("Front", {}).get("value", "")), \
                                _strip(fields.get("Back", {}).get("value", "")), ""
            footer_src = fields.get("Back", {}).get("value", "")
        m   = re.search(r'来源：<a href="([^"]*)"', footer_src)
        src_url = _html.unescape(m.group(1)) if m else ""
        m2  = re.search(r'\[\[([^\]]+)\]\]', footer_src)
        src_link = f"[[{m2.group(1)}]]" if m2 else ""
        src_note = ""
        mm = re.search(r'file=([^&"]+)', src_url)
        if mm:
            import urllib.parse as _up
            src_note = _up.unquote(mm.group(1)) + ".md"
        return {
            "local_id": local_id, "type": "cloze" if is_cloze else "basic",
            "front": front, "back": back, "text": text,
            "deck": "QA",
            "anki_note_id": nids[0], "source_note": src_note,
            "source_link": src_link, "source_url": src_url,
            "entity_id": ref.card_id if ref.is_entity else "",
            "entity_index": ref.index,
        }
    except Exception:
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
    """读 state/server-config.json 里的点分键。"""
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


def _reader_assistant_module():
    """Load the reader's canonical live model catalog.

    The legacy QA page must not grow a second Codex model registry.  Source
    checkouts can resolve ``_server_deploy/assistant.py`` directly; packaged
    clients that do not ship it fail closed and keep only the required Spark
    compatibility label visible as unavailable.
    """
    try:
        import assistant as reader_assistant  # type: ignore
        return reader_assistant
    except ImportError:
        server_dir = Path(__file__).resolve().parents[2] / "_server_deploy"
        if server_dir.is_dir() and str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        import assistant as reader_assistant  # type: ignore
        return reader_assistant


def _normal_codex_catalog(saved_model: str = "") -> dict:
    """Project the assistant's verified Codex catalog for screenshot chat."""
    compatibility_depths = ["low", "medium", "high", "xhigh"]
    try:
        reader_assistant = _reader_assistant_module()
        payload = dict(reader_assistant._codex_catalog_payload())
        declared_compatibility = getattr(
            reader_assistant,
            "_CODEX_COMPAT_DEPTHS",
            {},
        ) or {}
        declared_depths = list(
            declared_compatibility.get(
                "gpt-5.3-codex-spark",
                (),
            )
        )
        if declared_depths:
            compatibility_depths = declared_depths
    except Exception as error:
        payload = {
            "variants": ["gpt-5.3-codex-spark"],
            "capabilities": {
                "gpt-5.3-codex-spark": {
                    "available": False,
                    "catalog_advertised": False,
                    "selectable": False,
                    "depths": [],
                    "service_tiers": [],
                    "priority": False,
                    "fast": False,
                    "reason": "暂时无法读取 Codex 实时模型目录",
                },
            },
            "fast_models": [],
            "depths_by_model": {},
            "verified": False,
            "error": str(error)[:160],
        }
    variants = list(payload.get("variants") or [])
    raw_capabilities = dict(payload.get("capabilities") or {})
    capabilities = {}
    for model, raw in raw_capabilities.items():
        capability = dict(raw or {})
        capability["available"] = capability.get("available") is True
        capability["catalog_advertised"] = (
            capability.get("catalog_advertised") is True
            or (
                "catalog_advertised" not in capability
                and capability["available"]
            )
        )
        capability["selectable"] = capability.get("selectable") is True
        capability["depths"] = list(capability.get("depths") or [])
        capability["service_tiers"] = list(
            capability.get("service_tiers") or []
        )
        capability["priority"] = capability.get("priority") is True
        capability["fast"] = capability.get("fast") is True
        capability["reason"] = str(capability.get("reason") or "")
        capabilities[str(model)] = capability
    if "gpt-5.3-codex-spark" not in variants:
        variants.append("gpt-5.3-codex-spark")
    capabilities.setdefault("gpt-5.3-codex-spark", {
        "available": False,
        "catalog_advertised": False,
        "selectable": True,
        "depths": compatibility_depths,
        "service_tiers": [],
        "priority": False,
        "fast": False,
        "reason": "Spark 兼容型号；实时目录未声明 priority/Fast",
    })
    saved_model = str(saved_model or "").strip()
    if saved_model and saved_model not in variants:
        variants.append(saved_model)
        capabilities[saved_model] = {
            "available": False,
            "catalog_advertised": False,
            "selectable": False,
            "depths": [],
            "service_tiers": [],
            "priority": False,
            "fast": False,
            "reason": "没有取得这个已保存型号的能力声明",
        }
    return {
        "variants": variants,
        "capabilities": capabilities,
        "fast_models": [
            model for model in variants
            if (capabilities.get(model) or {}).get("selectable") is True
            and (
                (capabilities.get(model) or {}).get("priority") is True
                or (capabilities.get(model) or {}).get("fast") is True
            )
        ],
        "depths_by_model": {
            model: list((capabilities.get(model) or {}).get("depths") or [])
            for model in variants
        },
        "verified": payload.get("verified") is True,
        "error": str(payload.get("error") or ""),
    }


def _load_ai_settings_for_ui() -> dict:
    """给 ⚙ 弹窗回显：当前 backend + 各 CLI 后端的 model/effort/command。"""
    cfg = _GET_CFG() or {}
    ai = cfg.get("ai") or {}
    claude = ai.get("claude_cli") or {}
    codex  = ai.get("codex_cli") or {}
    model = str(codex.get("model") or "").strip()
    catalog = _normal_codex_catalog(model)
    capability = (catalog.get("capabilities") or {}).get(model) or {}
    effort = str(codex.get("effort") or "").strip().lower()
    selectable = capability.get("selectable") is True
    effort_ok = selectable and effort in (capability.get("depths") or [])
    fast_ok = selectable and (
        capability.get("priority") is True
        or capability.get("fast") is True
    )
    return {
        "backend": cfg.get("ai_backend", "claude_cli"),
        "claude": {"model": claude.get("model", ""), "effort": claude.get("effort", "")},
        "codex": {
            "model": model,
            "effort": effort if effort_ok else "",
            "fast": (
                cfg.get("ai_backend") == "codex_cli"
                and codex.get("fast") is True
                and fast_ok
            ),
        },
        "codex_catalog": catalog,
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
    if backend not in ("claude_cli", "codex_cli", "claude_api", "openai_api", "ollama"):
        return {"ok": False, "error": "普通截图问答后端无效，未保存"}
    ai = cfg.setdefault("ai", {})
    cl = body.get("claude") or {}
    if isinstance(cl, dict):
        c = ai.setdefault("claude_cli", {})
        c["model"]  = (cl.get("model") or "").strip()
        eff = (cl.get("effort") or "").strip().lower()
        c["effort"] = eff if eff in ("low", "medium", "high", "xhigh", "max") else ""
    cx = body.get("codex")
    if backend == "codex_cli":
        if not isinstance(cx, dict):
            return {"ok": False, "error": "Codex 设置缺失，未保存"}
        model = str(cx.get("model") or "").strip()
        effort = str(cx.get("effort") or "").strip().lower()
        requested_fast = cx.get("fast") is True
        if not model:
            return {"ok": False, "error": "请选择可用的 Codex 型号，未保存"}
        catalog = _normal_codex_catalog(model)
        capability = (catalog.get("capabilities") or {}).get(model) or {}
        if capability.get("selectable") is not True:
            reason = str(
                capability.get("reason")
                or "这个 Codex 型号当前不可选择"
            )
            return {"ok": False, "error": reason + "，未保存"}
        if effort not in (capability.get("depths") or []):
            return {"ok": False, "error": "所选 Codex 型号不支持这个思考深度，未保存"}
        if requested_fast and not (
            capability.get("priority") is True
            or capability.get("fast") is True
        ):
            return {"ok": False, "error": "所选 Codex 型号不支持 priority/Fast，未保存"}
        c = ai.setdefault("codex_cli", {})
        c["model"] = model
        c["effort"] = effort
        c["fast"] = requested_fast
    cfg["ai_backend"] = backend
    # Fast belongs only to this normal screenshot Codex profile.  Selecting a
    # different backend never turns a truthy legacy/string value into priority.
    if backend != "codex_cli":
        ai.setdefault("codex_cli", {})["fast"] = False
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **_load_ai_settings_for_ui()}


def _load_card_improvement_settings_for_ui() -> dict:
    """Return the reader's canonical ``card_improve`` model preference.

    Screenshot chat may keep its legacy settings, but card improvement consumes
    assistant action-prefs.  Read the catalog and capabilities from
    ``assistant.py`` so this old page cannot become a second model registry.
    """
    _runtime, reader_assistant = _card_improvement_runtime_modules()
    uid = _legacy_card_user_id()
    pref = reader_assistant._ap_get(uid, "card_improve")
    effective = reader_assistant._resolve("card_improve", uid)
    default = dict(reader_assistant._AP_DEFAULTS["card_improve"])
    default["fast"] = (
        default.get("fast") is True
        and default.get("backend") == "codex"
        and reader_assistant._codex_fast_ok(default.get("variant"))
    )
    codex = reader_assistant._codex_catalog_payload()
    variants = {
        "claude": list(reader_assistant._CLAUDE_VARIANTS),
        "gemini": list(reader_assistant._gemini_models()),
        "codex": list(codex.get("variants") or []),
    }
    # Keep a saved dynamic model visible when a live catalog refresh is
    # temporarily incomplete.
    for profile in (pref, effective, default):
        backend = str((profile or {}).get("backend") or "")
        variant = str((profile or {}).get("variant") or "")
        if backend in variants and variant and variant not in variants[backend]:
            variants[backend].append(variant)
    codex_capabilities = dict(codex.get("capabilities") or {})
    for model in variants["codex"]:
        codex_capabilities.setdefault(model, {
            "available": False,
            "catalog_advertised": False,
            "selectable": False,
            "depths": [],
            "service_tiers": [],
            "priority": False,
            "fast": False,
            "reason": "没有取得此型号的能力声明",
        })
    depth_candidates = tuple(dict.fromkeys(
        ("auto", "none", "think")
        + tuple(reader_assistant._EFFORTS)
        + tuple(reader_assistant._CODEX_DEPTHS)
    ))
    depths = {
        backend: [
            depth
            for depth in depth_candidates
            if reader_assistant._depth_ok(backend, depth)
        ]
        for backend in reader_assistant._BACKENDS
        if backend != "codex"
    }
    depths["codex"] = list(reader_assistant._CODEX_DEPTHS)
    return {
        "ok": True,
        "action": "card_improve",
        "label": reader_assistant._AP_LABELS.get(
            "card_improve", "复习卡改进"
        ),
        "pref": pref,
        "effective": effective,
        "default": default,
        "catalog": {
            "backends": list(reader_assistant._BACKENDS),
            "variants": variants,
            "depths": depths,
            "codex_capabilities": codex_capabilities,
            "fast_models": list(codex.get("fast_models") or []),
            "codex_catalog_verified": codex.get("verified") is True,
            "codex_catalog_error": str(codex.get("error") or ""),
        },
    }


def _save_card_improvement_settings_from_ui(body: dict) -> dict:
    """Persist and verify the canonical ``card_improve`` action-pref."""
    _runtime, reader_assistant = _card_improvement_runtime_modules()
    uid = _legacy_card_user_id()
    candidate = {
        "backend": body.get("backend"),
        "variant": body.get("variant"),
        "depth": body.get("depth"),
        "fast": body.get("fast") is True,
    }
    normalized = reader_assistant._ap_norm(candidate)
    if not normalized:
        return {
            "ok": False,
            "error": "卡片改进模型、型号或思考深度无效，未保存",
        }
    reader_assistant._ap_set(
        uid,
        "card_improve",
        normalized["backend"],
        normalized["variant"],
        normalized["depth"],
        fast=normalized["fast"],
    )
    # _ap_set deliberately absorbs disk errors for the main reader.  This old
    # page must not turn that into a false "saved" message, so read it back.
    persisted = reader_assistant._ap_get(uid, "card_improve")
    if persisted != normalized:
        return {
            "ok": False,
            "error": "卡片改进设置未能写入共享 action-pref，旧设置仍保持不变",
        }
    return {
        "ok": True,
        "action": "card_improve",
        "pref": persisted,
        "saved": persisted,
        "effective": reader_assistant._resolve("card_improve", uid),
    }


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
    return _shared_pairs_text(pairs)


def _card_improvement_runtime_modules():
    """Load the reader's one shared runtime without making it a client hard dependency.

    The persistent QA daemon is started by ``_server_deploy/qa_server.py``, so
    the shared runtime and assistant model registry are present.  A packaged
    client that does not ship that runtime must fail closed instead of silently
    resurrecting the old one-shot/direct-write workflow.
    """
    try:
        import card_improvement_runtime as runtime  # type: ignore
        import assistant as reader_assistant  # type: ignore
        return runtime, reader_assistant
    except ImportError:
        server_dir = Path(__file__).resolve().parents[2] / "_server_deploy"
        if server_dir.is_dir() and str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        try:
            import card_improvement_runtime as runtime  # type: ignore
            import assistant as reader_assistant  # type: ignore
            return runtime, reader_assistant
        except ImportError as error:
            raise CardImprovementError(
                "当前 QA 客户端缺少统一 card-improvement runtime；"
                "已停止旧的一次性直写流程，请升级客户端或使用阅读器复习模式。"
            ) from error


def _legacy_card_user_id() -> str:
    """Account whose reader ``card_improve`` action preset the old page follows."""
    value = _qa_setting("qa_user_id", "1")
    return str(value or "1").strip()[:80] or "1"


def _legacy_card_owner(client_token: str = "", client_fingerprint: str = "") -> str:
    """Create a non-secret owner key for the runtime's signed draft handle."""
    token = str(client_token or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32,128}", token):
        token = str(client_fingerprint or "legacy-client")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return f"legacy-qa:{_legacy_card_user_id()}:{digest}"


def _safe_vault_note_path(source_note: str) -> Path:
    """Resolve a source note inside the configured Vault, never outside it."""
    if not (source_note and VAULT):
        raise CardImprovementError("无源笔记路径")
    root = Path(VAULT).resolve()
    raw = Path(str(source_note))
    path = (raw if raw.is_absolute() else root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise CardImprovementError("源笔记不在当前 Vault 中") from error
    if not path.exists() and not path.suffix:
        path = path.with_suffix(".md")
    try:
        path.relative_to(root)
    except ValueError as error:
        raise CardImprovementError("源笔记不在当前 Vault 中") from error
    if not path.is_file():
        raise CardImprovementError(f"笔记不存在：{source_note}")
    return path


def _prepare_legacy_card_draft(
    local_id: str,
    pairs: list,
    target: str,
    *,
    index=None,
    verbosity: str = "verbose",
    owner: str,
) -> dict:
    """Prepare only.  Uses the reader's exact model preset/runtime and writes nothing."""
    runtime, reader_assistant = _card_improvement_runtime_modules()
    card = _find_card_context(local_id, index)
    if not card:
        raise CardImprovementError("找不到卡片上下文")
    rec_file, rec, rec_card = _find_record_for_card(local_id)
    if rec_card:
        card.setdefault("deck", rec_card.get("deck") or "Obsidian::未分类")
    if rec:
        card.setdefault("source_note", rec.get("source_note") or "")
        card.setdefault("source_link", rec.get("source_link") or "")
        card.setdefault("source_url", rec.get("source_url") or "")
    original_note = None
    if target in ("note", "all"):
        original_note = _safe_vault_note_path(
            str(card.get("source_note") or "")
        ).read_text(encoding="utf-8")

    uid = _legacy_card_user_id()
    profile = reader_assistant._resolve("card_improve", uid)
    backend = profile.get("backend")
    variant = profile.get("variant")
    depth = profile.get("depth")
    service_tier = (
        "priority"
        if profile.get("fast") is True
        and reader_assistant._codex_fast_ok(variant)
        else ""
    )
    if backend == "codex":
        def one_shot(prompt):
            return reader_assistant._codex_exec_text(
                prompt,
                model=variant,
                effort=depth,
                timeout=240,
                service_tier=service_tier,
            )

        codex_app = reader_assistant._codex_app
    else:
        def one_shot(prompt):
            return reader_assistant._deep_ask(
                prompt,
                backend=backend,
                variant=variant,
                depth=depth,
                timeout=240,
            )

        codex_app = None

    return runtime.prepare_card_improvement_draft(
        owner=owner,
        card=card,
        pairs=pairs,
        target=target,
        original_note=original_note,
        verbosity=verbosity,
        codex_app=codex_app,
        one_shot=one_shot,
        model=variant if backend == "codex" else "gpt-5.6-luna",
        effort=depth if backend == "codex" else "low",
        timeout=240,
        service_tier=service_tier if backend == "codex" else "",
    )


def _commit_legacy_anki_draft(
    *,
    draft_id: str,
    identity: dict,
    new_cards: list,
) -> dict:
    """Commit server-frozen cards.  The original card is never deleted."""
    runtime, _reader_assistant = _card_improvement_runtime_modules()
    local_id = str(identity.get("local_id") or "").strip()
    rec_file, rec, orig = _find_record_for_card(local_id)
    if not (rec_file and rec is not None and orig):
        return {"ok": False, "error": "卡片不在 records 中，不能安全登记新卡"}

    source_link = rec.get("source_link", "")
    source_url  = rec.get("source_url", "")
    if not source_url and rec.get("source_note"):   # 重建 obsidian 链接，避免空 href
        import urllib.parse as _up
        _fp = rec["source_note"].replace("\\", "/")
        if _fp.lower().endswith(".md"): _fp = _fp[:-3]
        source_url = ("obsidian://open?vault=" + _up.quote("Obsidian Vault", safe="")
                      + "&file=" + _up.quote(_fp, safe=""))
    deck = orig.get("deck", "Obsidian::未分类")
    MODELS = {"basic": "Obsidian-basic", "reverse": "Obsidian-basic-reversed",
              "cloze": "Obsidian-cloze"}
    id_prefix = "qa-" + hashlib.sha256(
        (draft_id + "\0anki").encode("utf-8")
    ).hexdigest()[:12]
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
            # Crash-safe retry: the deterministic local id is embedded in the
            # footer, so a replay can recover a note created before bookkeeping
            # completed instead of creating another copy.
            existing = _anki_request(
                "findNotes",
                {"query": f'"Local ID：{new_lid}"'},
                timeout=20,
            ) or []
            nid = existing[0] if existing else _anki_request(
                "addNote", {"note": note}, timeout=30
            )
            if not nid:
                raise RuntimeError(f"addNote 未返回 note id（{new_lid}）")
            # AnkiConnect × Anki 25:addNote 的 deckName 不生效(notetype 缓存被 requireReset 清掉)
            # → 卡落「系统默认」。显式归位。
            if nid:
                try:
                    _cids = _anki_request("findCards", {"query": f"nid:{nid}"}, timeout=20) or []
                    if _cids:
                        _anki_request("changeDeck", {"cards": _cids, "deck": deck}, timeout=20)
                except Exception:
                    pass
            if not any(
                str(row.get("local_id") or "") == new_lid
                for row in (rec.get("cards") or [])
                if isinstance(row, dict)
            ):
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

    _override_section_hashes(rec)
    try:
        runtime.atomic_replace_text(
            rec_file,
            json.dumps(rec, ensure_ascii=False, indent=2),
        )
    except Exception as ex:
        return {"ok": False, "error": f"写 records 失败：{ex}"}
    # 改完触发一次 AnkiWeb 同步，让新卡/删除即时推到其它设备（失败不阻断）
    synced = False
    try:
        _anki_request("sync", timeout=120)
        synced = True
    except Exception:
        pass
    deleted = False
    summary = (
        f"生成 {len(created)} 张新卡，保留原卡"
        + ("，已同步 AnkiWeb" if synced else "，AnkiWeb 同步失败（稍后凌晨会同步）")
    )
    return {"ok": True, "summary": summary, "created": created,
            "deleted": deleted, "synced": synced}


def _after_legacy_note_commit(
    identity: dict,
    note_path: Path,
    _original: str,
    _content: str,
    _note: dict,
) -> None:
    """Refresh legacy bookkeeping after the shared coordinator wrote the note."""
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
    local_id = str(identity.get("local_id") or "")
    rec_file, rec, _ = _find_record_for_card(local_id)
    if rec_file and rec is not None:
        _override_section_hashes(rec)
        try:
            runtime, _reader_assistant = _card_improvement_runtime_modules()
            runtime.atomic_replace_text(
                rec_file,
                json.dumps(rec, ensure_ascii=False, indent=2),
            )
        except Exception:
            pass


def _commit_legacy_card_draft(
    *,
    draft_id: str,
    target: str,
    owner: str,
) -> dict:
    """Commit one target from a signed immutable draft; never accepts draft text."""
    runtime, _reader_assistant = _card_improvement_runtime_modules()
    try:
        result = runtime.commit_card_improvement_draft(
            draft_id=draft_id,
            target=target,
            owner=owner,
            commit_anki=lambda frozen_id, identity, cards: (
                _commit_legacy_anki_draft(
                    draft_id=frozen_id,
                    identity=identity,
                    new_cards=cards,
                )
            ),
            resolve_note_path=lambda identity: _safe_vault_note_path(
                str(identity.get("source_note") or "")
            ),
            after_note_commit=_after_legacy_note_commit,
        )
    except runtime.CardImprovementCommitConflict as error:
        return {"ok": False, "conflict": True, "error": str(error)}
    except Exception as error:
        return {"ok": False, "error": str(error)}

    # Keep the retained page's compact, reader-friendly success wording while
    # all validation, locking, idempotency and primary writes stay centralized.
    if result.get("ok") and target == "note" and not result.get("dedup"):
        metadata = (
            result.get("result")
            if isinstance(result.get("result"), dict)
            else {}
        )
        path = Path(str(metadata.get("path") or "笔记.md"))
        verbosity = (
            "concise"
            if metadata.get("verbosity") == "concise"
            else "verbose"
        )
        mode_label = "精炼" if verbosity == "concise" else "详细"
        before_chars = int(metadata.get("before_chars") or 0)
        after_chars = int(metadata.get("after_chars") or 0)
        result["summary"] = (
            f"笔记已更新（{path.name}）【{mode_label}】，"
            f"净变化 {after_chars - before_chars:+d} 字"
        )
    return result


def _sanitize_note_name(name: str) -> str:
    """清理用户输入的笔记名（去非法字符、长度限制）。"""
    name = (name or "").strip().strip(".")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    return name[:120] or "untitled"


def _create_note_from_qa(name: str, pairs: list, image_b64) -> dict:
    """从 QA 选中的问答 + 当前截图，让 AI 整理成 Markdown，写到 vault 根目录。
    - 不触发 register（命名不带 [0-9A-Fa-f]{3}- 前缀，避开 pending_notes 闸门）
    - 同名追加 -1/-2/…
    - 截图保存到 vault/attachments，笔记顶部加 ![[...]] 引用
    返回 {ok, note_path (vault 相对路径), obsidian_url}
    """
    if not VAULT:
        return {"ok": False, "error": "VAULT 未配置"}
    safe_stem = _sanitize_note_name(name)
    fname = safe_stem + ".md"
    note_path = VAULT / fname
    if note_path.exists():
        for i in range(1, 200):
            cand = VAULT / f"{safe_stem}-{i}.md"
            if not cand.exists():
                note_path = cand; break
    # AI 整理 pairs → Markdown
    prompt = (
        "请把以下从截图问答中收集到的关键内容整理成一篇结构清晰的 Obsidian Markdown 学习笔记。\n"
        f"笔记主题：{name}\n\n"
        "整理要求：\n"
        "1. 去掉对话冗余（如“我来回答你”之类），保留实质知识点。\n"
        "2. 用 `## 标题` 分小节；若只有一两个知识点，可以不用标题直接铺开。\n"
        "3. 数学公式严格用 $...$ 或 $$...$$（Obsidian 支持）；不要 \\(...\\) 或 \\[...\\]。\n"
        "4. 不要添加用户没问的引申内容、额外例子、自己的发挥。\n"
        "5. 直接输出 Markdown 正文，**第一行就是笔记内容**；不加代码围栏、不加前言说明。\n\n"
        f"=== 选中内容（问答对） ===\n{_pairs_text(pairs)}"
    )
    try:
        content = ai_client.ask(prompt).strip()
    except Exception as ex:
        return {"ok": False, "error": f"AI 整理失败：{ex}"}
    if content.startswith("```"):
        content = re.sub(r'^```[a-zA-Z]*\n', '', content)
        content = re.sub(r'\n```\s*$', '', content)
    if not content:
        return {"ok": False, "error": "AI 返回内容为空"}
    final_content = content
    # 处理截图：保存到 vault/attachments，笔记顶部加引用
    if image_b64:
        try:
            b64 = image_b64.split(",", 1)[-1] if "," in image_b64 else image_b64
            img_bytes = base64.b64decode(b64)
            img_dir = VAULT / "attachments"
            img_dir.mkdir(parents=True, exist_ok=True)
            img_path = img_dir / (note_path.stem + ".png")
            i = 1
            while img_path.exists():
                img_path = img_dir / f"{note_path.stem}-{i}.png"; i += 1
            img_path.write_bytes(img_bytes)
            final_content = f"![[attachments/{img_path.name}]]\n\n" + final_content
        except Exception:
            pass   # 截图保存失败不致命，仍然写笔记
    try:
        note_path.write_text(final_content, encoding="utf-8")
    except Exception as ex:
        return {"ok": False, "error": f"写文件失败：{ex}"}
    rel = note_path.relative_to(VAULT).as_posix()
    rel_no_ext = rel[:-3] if rel.endswith(".md") else rel
    import urllib.parse as _up
    vault_name = os.environ.get("OBSIDIAN_VAULT_NAME", "Obsidian Vault")
    obsidian_url = (
        f"obsidian://open?vault={_up.quote(vault_name, safe='')}"
        f"&file={_up.quote(rel_no_ext, safe='/')}"
    )
    return {"ok": True, "note_path": rel, "obsidian_url": obsidian_url}


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
    # AnkiWeb sync 最长 120s,不值得占住请求线程（移动端早断连）→ 后台 fire-and-forget
    # deleteNotes + records 写盘已在上面同步完成,sync 失败也会被 15min anki-sync-refresh 兜住
    def _bg_sync():
        try:
            _anki_request("sync", timeout=120)
        except Exception:
            pass
    threading.Thread(target=_bg_sync, daemon=True).start()
    return {"ok": True, "synced": "pending"}


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

def classify_conversation(msgs: list = None) -> dict:
    # msgs=会话快照（server 模式异步保存时传入；reset 后 state 里已是空会话，不能再读全局）
    if msgs is None:
        msgs = state["session"].messages
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
        for m in msgs
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


def do_save(match: str, is_wrong: bool, related_cards: list = None,
            msgs: list = None, img_fname: str = None, temp_path=None) -> str:
    # msgs/img_fname/temp_path=会话快照（server 模式异步保存传入，避免 reset 后读到空 state）
    if VAULT is None or EXERCISES_DIR is None or WRONG_DIR is None:
        return "未配置 vault 路径，无法保存（请在客户端 GUI 设 qa_vault_path）"
    ts        = datetime.now().strftime("%Y-%m-%d %H:%M")
    if img_fname is None:
        img_fname = state["img_fname"]
    if temp_path is None:
        temp_path = state["temp_path"]
    fallback  = f"未分类-{datetime.now().strftime('%Y%m%d')}"

    has_prefix = bool(match and re.match(r'^[0-9A-Fa-f]{3}-', match))
    note_name  = match if match else fallback
    ex_prefix  = "错题" if is_wrong else "习题"
    save_dir   = WRONG_DIR if is_wrong else EXERCISES_DIR
    section    = "错题本" if is_wrong else "习题本"
    exfile     = save_dir / f"{ex_prefix}-{note_name}.md"

    # 构建问答正文
    if msgs is None:
        msgs = state["session"].messages
    qa_parts, i = [], 0
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
    if not dest.exists() and temp_path and Path(temp_path).exists():
        shutil.copy2(temp_path, dest)

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

def archive_conversation(note_result: str, record_type: str = "normal", related_cards: list = None,
                         msgs: list = None, img_fname: str = None, temp_path=None):
    # msgs/img_fname/temp_path=会话快照（server 模式异步保存传入，reset 后不能再读全局 state）
    HIST_IMG_DIR.mkdir(parents=True, exist_ok=True)
    hist_id   = datetime.now().strftime("%Y%m%d-%H%M%S")
    if img_fname is None:
        img_fname = state["img_fname"]
    if temp_path is None:
        temp_path = state["temp_path"]
    if msgs is None:
        msgs = state["session"].messages
    src       = Path(temp_path)
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
             json.dumps(msgs, ensure_ascii=False),
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
/* 子标题行的 + ：贴右侧（真标题 + 假标题=粗体段落） */
.md h1,.md h2,.md h3,.md h4,.md h5,.md h6,.md p.fake-head,.md li.fake-head{position:relative}
.md h1.has-pick,.md h2.has-pick,.md h3.has-pick,.md h4.has-pick,.md h5.has-pick,.md h6.has-pick,
.md p.fake-head.has-pick,.md li.fake-head.has-pick{padding-right:26px}
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
.draft-note-preview{max-height:240px;overflow:auto;white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:9px;margin:7px 0;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}
.draft-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px;padding-top:8px;border-top:1px solid #ead9a8}
.draft-actions button{border:1px solid #107c41;border-radius:6px;padding:5px 11px;background:#eefbf3;color:#0b5a2f;cursor:pointer;font-size:12px}
.draft-actions button:disabled{opacity:.55;cursor:default}
#draft-commit-status{margin-top:7px}
#sett-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:200}
#sett-overlay.open{display:block}
#sett-modal{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#fff;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.18);z-index:201;min-width:300px;max-width:440px;width:92%;max-height:90vh;overflow:hidden}
#sett-modal.open{display:block}
#sett-head{padding:14px 16px 10px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eee}
#sett-head span{font-weight:600;font-size:14px}
#sett-close{background:none;border:none;font-size:16px;cursor:pointer;color:#bbb;padding:2px 4px;line-height:1}
#sett-close:hover{color:#555}
#sett-body{padding:14px 16px;display:flex;flex-direction:column;gap:7px;max-height:68vh;overflow:auto}
#sett-body label{font-size:12px;color:#666;font-weight:500;margin-top:4px}
#sett-body label:first-child{margin-top:0}
#sett-body select,#sett-body input[type=text]{border:1px solid #ddd;border-radius:7px;padding:7px 10px;font-size:13px;font-family:inherit;outline:none;transition:border-color .15s;width:100%}
#sett-body select:focus,#sett-body input:focus{border-color:#0078d4}
#sett-hint{font-size:11px;color:#aaa;margin-top:-2px}
.sett-section{display:flex;flex-direction:column;gap:7px;padding:10px 0 2px;border-top:1px solid #e8edf4}
.sett-section:first-child{padding-top:0;border-top:0}
.sett-section-title{font-size:13px;font-weight:700;color:#26364c}
.sett-section-note{font-size:11px;color:#77849a;line-height:1.45;margin:0}
.sett-normal-row{display:grid;grid-template-columns:1.45fr 1fr;gap:6px}
.sett-card-row{display:grid;grid-template-columns:1fr 1.35fr 1fr;gap:6px}
.sett-card-fast{display:flex;align-items:center;gap:7px;font-size:12px;color:#44546a!important;cursor:pointer}
.sett-card-fast input{width:auto}
.sett-card-fast.is-disabled{opacity:.55;cursor:not-allowed}
#s-codex-state{min-height:16px;font-size:11px;color:#68778b}
#s-codex-state.error{color:#b42318}
#s-card-state{min-height:16px;font-size:11px;color:#68778b}
#s-card-state.error{color:#b42318}
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
    <div class="sett-section">
      <div class="sett-section-title">普通截图问答（旧设置）</div>
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
        <label>Codex 型号与思考深度（实时目录）</label>
        <div class="sett-normal-row">
          <select id="s-codex-model" aria-label="普通截图 Codex 型号"></select>
          <select id="s-codex-effort" aria-label="普通截图 Codex 思考深度"></select>
        </div>
        <label class="sett-card-fast is-disabled" id="s-codex-fast-wrap">
          <input type="checkbox" id="s-codex-fast" disabled>
          <span>⚡ Fast（只影响普通截图问答）</span>
        </label>
        <div id="s-codex-state">正在读取 Codex 实时目录…</div>
      </div>
      <p class="sett-section-note">只影响这个旧页面的普通截图问答。</p>
    </div>
    <div class="sett-section" id="s-card-section">
      <div class="sett-section-title">复习卡改进（与阅读器共用）</div>
      <div class="sett-card-row">
        <select id="s-card-backend" aria-label="卡片改进后端"></select>
        <select id="s-card-model" aria-label="卡片改进模型"></select>
        <select id="s-card-depth" aria-label="卡片改进思考深度"></select>
      </div>
      <label class="sett-card-fast is-disabled" id="s-card-fast-wrap">
        <input type="checkbox" id="s-card-fast" disabled>
        <span>⚡ Fast（仅当前模型确实支持 priority 时可用）</span>
      </label>
      <div id="s-card-state">正在读取共享 card_improve 设置…</div>
      <p class="sett-section-note">这里直接读写助手的 card_improve action-pref；旧独立页和阅读器复习模式实际使用同一份设置。</p>
    </div>
    <p class="sett-section-note">卡片改进始终保留原卡，避免丢失 FSRS 复习历史。</p>
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
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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
// URL hash 注入：#q=<text> 让外部页面（如 PDF 阅读器）把选中文本带过来填到输入框
(function () {
  const h = (location.hash || '').replace(/^#/, '');
  if (!h) return;
  const params = new URLSearchParams(h);
  const q = params.get('q');
  if (q) {
    input.value = q;
    // 清掉 hash，避免刷新重复注入
    history.replaceState(null, '', location.pathname + location.search);
    input.focus();
  }
})();
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
const cardDraftOwnerToken = (() => {
  const key = 'bw-card-improvement-owner-v1';
  try {
    let token = localStorage.getItem(key) || '';
    if (!/^[a-f0-9]{64}$/.test(token)) {
      const bytes = new Uint8Array(32);
      crypto.getRandomValues(bytes);
      token = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
      localStorage.setItem(key, token);
    }
    return token;
  } catch (_) {
    // Runtime still binds the signed draft to the remote fingerprint if
    // persistent browser storage is unavailable.
    return '';
  }
})();

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
  // 真标题 + 假标题（粗体段落代标题）都参与收集
  md.querySelectorAll('h1,h2,h3,h4,h5,h6,p.fake-head,li.fake-head').forEach(h => {
    const btn = h.querySelector('.head-pick');
    if (!btn || !btn.classList.contains('on')) return;
    let txt;
    if (/^H[1-6]$/.test(h.tagName)) {
      txt = '#'.repeat(headLevel(h)) + ' ' + headingOwnText(h);
    } else {
      // 假标题：直接取文本（不加 # 前缀，避免乱）
      txt = (h.textContent || '').trim();
      // 去掉 + 按钮文本（"+"）
      txt = txt.replace(/\+\s*$/, '').trim();
    }
    let sib = h.nextElementSibling;
    while (sib && !/^H[1-6]$/.test(sib.tagName) && !(sib.classList && sib.classList.contains('fake-head'))) {
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

// 根据当前是否有「有用」回复，显示/隐藏右上角按钮
function refreshUpdateButtons() {
  const has = collectUsefulPairs().length > 0;
  if (cardCtx) {
    document.querySelectorAll('#card-actions .upd-group').forEach(g => {
      g.style.display = has ? '' : 'none';
    });
  } else {
    const btn = document.getElementById('create-note-btn');
    if (btn) btn.style.display = has ? '' : 'none';
  }
}

const UPD_LABELS = {note: '更新到笔记', anki: '根据此修改 Anki', all: '全部更新'};
let _newCards = [];   // 暂存本次生成的新卡，供预览渲染
let _activeDraft = null;

function renderDraftPreview(res) {
  _activeDraft = res;
  _newCards = ((res.drafts || {}).cards || []).slice();
  let html = '<div><b>草稿预览（尚未写入）</b></div>';
  const runner = res.runner || {};
  const runnerLabel = runner.native_multiturn_used
    ? ('Codex app-server 连续 ' + (runner.native_turns || 0) + ' 轮')
    : (runner.mode === 'codex_app_thread'
        ? 'Codex app-server 单轮'
        : ('独立调用兜底' + (runner.fallback_reason ? '：' + escapeHtml(runner.fallback_reason) : '')));
  html += '<div style="font-size:11px;color:#777;margin-top:2px">生成方式：' + escapeHtml(runnerLabel) + '</div>';
  if (_newCards.length) {
    html += '<div style="margin-top:7px;color:#555">Anki 新卡草稿（原卡会保留）：</div>';
    _newCards.forEach((c, i) => {
      const body = c.type === 'cloze'
        ? '<b>挖空</b><br>' + escapeHtml(c.text || c.cloze || '')
          + (c.back ? '<hr><b>补充</b><br>' + escapeHtml(c.back) : '')
        : '<b>问</b><br>' + escapeHtml(c.front || '')
          + '<hr><b>答</b><br>' + escapeHtml(c.back || '');
      html += '<div class="newcard"><div class="newcard-head">'
        + '<span class="newcard-toggle" onclick="toggleNewcard(this)">▸ 新卡 '
        + (i + 1) + '（' + escapeHtml(c.type || 'basic') + '）</span>'
        + '</div><div class="newcard-body" data-preview="1" style="display:none">'
        + body + '</div></div>';
    });
  }
  const note = (res.drafts || {}).note;
  if (note && typeof note.content === 'string') {
    html += '<div style="margin-top:8px;color:#555">原笔记完整替换草稿：</div>'
      + '<pre class="draft-note-preview">' + escapeHtml(note.content) + '</pre>';
  }
  html += '<div class="draft-actions">';
  if (_newCards.length) {
    html += '<button id="draft-commit-anki" onclick="commitCardDraft(\'anki\',this)">确认写入 Anki 新卡</button>';
  }
  if (note && typeof note.content === 'string') {
    html += '<button id="draft-commit-note" onclick="commitCardDraft(\'note\',this)">确认更新原笔记</button>';
  }
  html += '</div><div id="draft-commit-status">确认前不会修改 Anki 或笔记。</div>';
  showCardResult(html);
}

function renderUpdResult(res) {
  if (!res || res.ok === false) { showCardResult('✗ ' + ((res && res.error) || '失败')); return; }
  if (res.draft_id && res.drafts) { renderDraftPreview(res); return; }
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
  if (opening && !body.dataset.filled && !body.dataset.preview) {
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

function renderCardCommitResult(res, target, btn) {
  const statusEl = document.getElementById('draft-commit-status');
  if (!res || res.ok === false) {
    if (btn) { btn.disabled = false; btn.textContent = target === 'anki' ? '确认写入 Anki 新卡' : '确认更新原笔记'; }
    if (statusEl) {
      statusEl.innerHTML = '<span style="color:#b91c1c">✗ '
        + escapeHtml((res && res.error) || '提交失败') + '</span>';
    }
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.textContent = target === 'anki' ? '✓ 已写入 Anki' : '✓ 已更新笔记';
  }
  if (statusEl) {
    statusEl.innerHTML = '<span style="color:#0b5a2f">✓ '
      + escapeHtml(res.summary || '提交完成') + '</span>';
  }
}

function pollCardCommitJob(jobId, target, btn, tries) {
  tries = tries || 0;
  fetch('api/card-update-status?job=' + encodeURIComponent(jobId))
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(j => {
      if (j.status === 'running') {
        setTimeout(() => pollCardCommitJob(jobId, target, btn, 0), 2000);
        return;
      }
      renderCardCommitResult(j.result, target, btn);
    })
    .catch(() => {
      if (tries < 40) {
        setTimeout(() => pollCardCommitJob(jobId, target, btn, tries + 1), 3000);
      } else {
        renderCardCommitResult({ok:false,error:'暂时取不到提交结果；可稍后重试同一草稿，服务端会幂等去重。'}, target, btn);
      }
    });
}

function commitCardDraft(target, btn) {
  if (!_activeDraft || !_activeDraft.draft_id) {
    renderCardCommitResult({ok:false,error:'草稿已失效，请重新生成。'}, target, btn);
    return;
  }
  const promptText = target === 'anki'
    ? '确认把预览中的新卡写入 Anki？原卡会保留。'
    : '确认用预览中的完整草稿更新原笔记？';
  if (!window.confirm(promptText)) return;
  btn.disabled = true; btn.textContent = '提交中…';
  fetch('api/card-update-commit', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      draft_id: _activeDraft.draft_id,
      target: target,
      owner_token: cardDraftOwnerToken,
    }),
  }).then(r => r.json()).then(j => {
    if (j.job_id) pollCardCommitJob(j.job_id, target, btn, 0);
    else renderCardCommitResult(j, target, btn);
  }).catch(e => renderCardCommitResult({ok:false,error:e.message}, target, btn));
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
      // synced 三态：true=已同步 / 'pending'=后台同步中 / falsy=未同步（'pending' 是 truthy，不能直接 j.synced ?）
      const syncLabel = j.synced === 'pending' ? '（同步中）' : (j.synced ? '（已同步）' : '');
      card.querySelector('.newcard-head').innerHTML =
        '<span style="color:#b91c1c;font-size:12px">已删除' + syncLabel + '</span>';
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
// 复习侧栏和旧页面共用同一工作区；入口只传模式/实体元数据，不复制 prompt。
// 非法或缺省值仍回落为详细模式。
let noteVerbosity = new URLSearchParams(location.search).get('verbosity') === 'concise'
  ? 'concise' : 'verbose';
function runCardUpdate(target, btn) {
  const pairs = collectUsefulPairs();
  if (!pairs.length) { showCardResult('请先在 AI 回复下方勾选「有用」的回答。'); return; }
  const allBtns = document.querySelectorAll('#card-actions .upd-group button');
  allBtns.forEach(b => b.disabled = true);
  const verbLabel = (target === 'anki') ? '' : (noteVerbosity === 'verbose' ? '【详细】' : '【精炼】');
  showCardResult('正在后台生成「' + UPD_LABELS[target] + verbLabel + '」草稿，纳入 ' + pairs.length + ' 条有用回答…（确认前不会写入）');
  fetch('api/card-update', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      local_id: cardCtx.local_id, target: target, pairs: pairs,
      index: cardCtx.entity_index,
      verbosity: noteVerbosity,
      owner_token: cardDraftOwnerToken,
    }),
  }).then(r => r.json()).then(j => {
    if (j.job_id) { pollCardJob(j.job_id, allBtns, 0); }
    else { allBtns.forEach(b => b.disabled = false); renderUpdResult(j); }   // 兼容旧式同步返回
  }).catch(e => {
    allBtns.forEach(b => b.disabled = false);
    showCardResult('✗ 发起失败：' + e.message);
  });
}

function loadCardContext(cid, index) {
  const iq = (index === null || index === undefined || index === '') ? '' : '&index=' + encodeURIComponent(index);
  fetch('api/card-context?card=' + encodeURIComponent(cid) + iq)
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(c => {
      // source 仅作为入口上下文保留；真正写入时仍由服务端按 entity_id/index
      // 回查可信来源，不接受客户端 source 参数覆盖。
      const entrySource = new URLSearchParams(location.search).get('source') || '';
      if (entrySource && !c.source_ref) c.entry_source_ref = entrySource;
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
      // verbosity toggle（影响"更新到笔记"和"全部更新"，不影响"修改 Anki"）
      const verbBtn = document.createElement('button');
      verbBtn.id = 'verb-toggle';
      verbBtn.className = 'card-act-btn';
      verbBtn.style.marginLeft = '6px';
      const renderVerb = () => {
        verbBtn.textContent = noteVerbosity === 'verbose' ? '📝 详细' : '✂️ 精炼';
        verbBtn.title = noteVerbosity === 'verbose'
          ? '更新到笔记/全部更新：保留 ④ 全部内容 + 仅连贯化（间断选中也通顺）。点击切到精炼'
          : '更新到笔记/全部更新：允许提炼合并（核心信息不丢）+ 连贯化。点击切到详细';
      };
      renderVerb();
      verbBtn.onclick = () => {
        noteVerbosity = noteVerbosity === 'verbose' ? 'concise' : 'verbose';
        renderVerb();
      };
      group.appendChild(verbBtn);
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
const _cardParams = new URLSearchParams(location.search);
const _cardId = _cardParams.get('card');
const _cardIndex = _cardParams.get('index');
if (_cardId) loadCardContext(_cardId, _cardIndex); else { pollScreenshot(); setupCreateNoteButton(); }

// 非 cardCtx 模式：在 header #card-actions 加「📝 创建新笔记」按钮，用户勾选内容后显示
function setupCreateNoteButton() {
  const acts = document.getElementById('card-actions');
  if (!acts || document.getElementById('create-note-btn')) return;
  const btn = document.createElement('button');
  btn.id = 'create-note-btn';
  btn.className = 'card-act-btn';
  btn.textContent = '📝 创建新笔记';
  btn.title = '把勾选的回答内容用 AI 整理 + 当前截图 写到一篇新笔记';
  btn.style.display = 'none';
  btn.style.marginLeft = '6px';
  btn.onclick = runCreateNote;
  acts.appendChild(btn);
}

function runCreateNote() {
  const pairs = collectUsefulPairs();
  if (!pairs.length) { showCardResult('请先在 AI 回复下方勾选「有用」的部分（标题旁的 + 或 ＋ 选用整条回答）。'); return; }
  const name = prompt('请输入笔记名（不含 .md）：', '');
  if (name === null) return;
  const trimmed = (name || '').trim();
  if (!trimmed) { showCardResult('未输入笔记名，已取消。'); return; }
  const btn = document.getElementById('create-note-btn');
  btn.disabled = true; btn.textContent = '⏳ AI 整理中…';
  showCardResult('正在用 AI 整理选中内容并写入新笔记…（10-30s，锁屏也不影响）');
  // 取当前截图 base64（如果有真截图，不是占位 gif）
  const shotImg = document.getElementById('shot');
  let img_b64 = null;
  if (shotImg && shotImg.src && shotImg.src.startsWith('data:image/') && !shotImg.src.startsWith('data:image/gif')) {
    img_b64 = shotImg.src;
  }
  fetch('api/create-note', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: trimmed, pairs: pairs, image_b64: img_b64}),
  }).then(r => r.json()).then(j => {
    // 后台 job：轮询期间按钮保持 disabled，防重复点击产生 name-1.md 垃圾
    if (j.job_id) { pollCreateNoteJob(j.job_id, btn, 0); return; }
    btn.disabled = false; btn.textContent = '📝 创建新笔记';
    renderCreateNoteResult(j);   // 兼容旧式同步返回
  }).catch(e => {
    btn.disabled = false; btn.textContent = '📝 创建新笔记';
    showCardResult('✗ 请求失败：' + e.message);
  });
}

function renderCreateNoteResult(j) {
  if (j && j.ok) {
    const safeUrl = (j.obsidian_url || '').replace(/'/g, "\\'");
    const html = '<div><b>✓ 笔记已创建：</b>' + (j.note_path || '') + '</div>' +
      '<button class="card-act-btn" onclick="location.href=\'' + safeUrl + '\'" style="margin-top:8px">📂 在 Obsidian 中打开</button>';
    showCardResult(html);
  } else {
    showCardResult('✗ 创建失败：' + ((j && j.error) || '未知'));
  }
}

function pollCreateNoteJob(jobId, btn, tries) {
  // 复用 card-update-status 端点（零新端点）；封顶 60 次轮询
  const reset = () => { btn.disabled = false; btn.textContent = '📝 创建新笔记'; };
  fetch('api/card-update-status?job=' + encodeURIComponent(jobId))
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(j => {
      if (j.status === 'running') {
        if (tries >= 60) { reset(); showCardResult('⚠️ 整理超时，任务仍在后台执行，稍后可在 vault 查看'); return; }
        setTimeout(() => pollCreateNoteJob(jobId, btn, tries + 1), 2000); return;
      }
      reset();
      renderCreateNoteResult(j.result);
    })
    .catch(() => {
      // 轮询暂时失败（锁屏/网络抖动）→ 重试；任务仍在后台跑
      if (tries < 60) { setTimeout(() => pollCreateNoteJob(jobId, btn, tries + 1), 3000); }
      else { reset(); showCardResult('⚠️ 暂时取不到结果，任务已在后台执行，稍后可在 vault 查看'); }
    });
}

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
  // 所有 AI 回答都加「选用整条回答」开关 + 子标题 +（cardCtx 用于改卡片/笔记，非 cardCtx 用于创建新笔记）
  if (role === 'assistant') {
    const allBtn = document.createElement('button');
    allBtn.className = 'reply-pick-all';
    allBtn.textContent = '＋ 选用整条回答';
    allBtn.title = cardCtx ? '把整条回答用于改进卡片/笔记' : '把整条回答用于创建新笔记';
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
  if (!md) return;
  const realHeads = Array.from(md.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  // 假标题：<p> 或 <li> 里只含一个 <strong> 且 strong 占绝大部分文本 —— AI 经常用粗体段落代替 ## 标题
  const fakeHeads = [];
  md.querySelectorAll('p, li').forEach(el => {
    if (el.closest('h1,h2,h3,h4,h5,h6')) return;
    const strongs = el.querySelectorAll('strong');
    if (strongs.length !== 1) return;
    const ptext = (el.textContent || '').trim();
    const stext = (strongs[0].textContent || '').trim();
    if (ptext.length >= 3 && stext.length >= 2 && stext.length / ptext.length >= 0.85) {
      el.classList.add('fake-head');
      fakeHeads.push(el);
    }
  });
  const heads = [...realHeads, ...fakeHeads];
  if (!heads.length) return;
  // 单个最高级真标题 + 它是 md 第一个元素 → 它覆盖全文，等价整条按钮，跳过它的 +
  let singleTopCoversAll = false, singleTop = null;
  if (realHeads.length) {
    const minLvl = Math.min(...realHeads.map(headLevel));
    const topHeads = realHeads.filter(h => headLevel(h) === minLvl);
    if (topHeads.length === 1 && md.firstElementChild === topHeads[0]) {
      singleTopCoversAll = true; singleTop = topHeads[0];
    }
  }
  heads.forEach(h => {
    if (singleTopCoversAll && h === singleTop) return;
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

// 标题级联选择：真标题（H1-H6）按级别级联；假标题（粗体段落）只选自己 + 后续段落到下一个标题
function headLevel(h) { return parseInt(h.tagName.slice(1), 10); }
function isFakeHead(h) { return h.classList && h.classList.contains('fake-head'); }
function toggleHeadingPick(h, md) {
  const newOn = !h.querySelector('.head-pick').classList.contains('on');
  if (!isFakeHead(h) && /^H[1-6]$/.test(h.tagName)) {
    const heads = Array.from(md.querySelectorAll('h1,h2,h3,h4,h5,h6'));
    const i = heads.indexOf(h);
    const lvl = headLevel(h);
    setHeadPick(h, newOn);
    for (let j = i + 1; j < heads.length; j++) {
      if (headLevel(heads[j]) <= lvl) break;
      setHeadPick(heads[j], newOn);
    }
  } else {
    // 假标题：只选自己一段
    setHeadPick(h, newOn);
  }
  refreshUpdateButtons();
}
function setHeadPick(h, on) {
  const btn = h.querySelector('.head-pick');
  if (btn) btn.classList.toggle('on', on);
  h.classList.toggle('hsec-picked', on);
  // 整段高亮：标题行 + 其后到下个标题（真或假）前的所有内容元素
  let sib = h.nextElementSibling;
  while (sib && !/^H[1-6]$/.test(sib.tagName) && !(sib.classList && sib.classList.contains('fake-head'))) {
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
    addHeadingPickers(mdEl);   // 给标题行补 + 按钮（cardCtx → 改卡片/笔记；非 cardCtx → 创建新笔记）
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

function pollSaveJob(jobId) {
  // server 模式异步保存：复用 card-update-status 端点轮询；弱网/锁屏失败重试不放弃
  return new Promise((resolve, reject) => {
    let fails = 0, tries = 0;
    const tick = () => {
      fetch('api/card-update-status?job=' + encodeURIComponent(jobId))
        .then(r => r.ok ? r.json() : Promise.reject())
        .then(j => {
          fails = 0;
          if (j.status === 'done') { resolve((j.result && j.result.result) || '已保存'); return; }
          if (++tries > 120) { reject(new Error('保存超时，任务仍在后台执行，稍后可在历史记录查看')); return; }
          setTimeout(tick, 2000);
        })
        .catch(() => {
          if (++fails > 40) { reject(new Error('轮询失败，任务仍在后台执行，稍后可在历史记录查看')); return; }
          setTimeout(tick, 3000);
        });
    };
    tick();
  });
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
    // server 模式返回 job_id（AI 分类数十秒，连接易断）→ 轮询拿最终结果
    const resultText = d.job_id ? await pollSaveJob(d.job_id) : d.result;
    status.textContent = '✓ ' + resultText;
    saveBtn.textContent = '已保存 ✓'; saveBtn.style.background = '#666';
    addMsg('assistant', '📝 ' + resultText);
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
  if (normalCodexCatalog) syncNormalCodexFast();
}

let normalCodexCatalog = null;
let cardImprovementCatalog = null;

function replaceSelectOptions(el, values, selected, disabledValues, labels) {
  el.innerHTML = '';
  const disabled = disabledValues || [];
  (values || []).forEach(value => {
    const op = document.createElement('option');
    op.value = value;
    op.textContent = (labels && labels[value]) || value;
    op.disabled = disabled.includes(value);
    if (labels && labels[value]) op.title = labels[value];
    if (value === selected) op.selected = true;
    el.appendChild(op);
  });
}

function normalCodexFastSupported() {
  if (!normalCodexCatalog ||
      document.getElementById('s-backend').value !== 'codex_cli') return false;
  const model = document.getElementById('s-codex-model').value;
  const caps = normalCodexCatalog.capabilities || {};
  return !!(caps[model] && caps[model].selectable === true &&
            (caps[model].priority === true || caps[model].fast === true));
}

function syncNormalCodexFast(resetUnsupported) {
  const fast = document.getElementById('s-codex-fast');
  const wrap = document.getElementById('s-codex-fast-wrap');
  const supported = normalCodexFastSupported();
  if (!supported && resetUnsupported !== false) fast.checked = false;
  fast.disabled = !supported;
  wrap.classList.toggle('is-disabled', !supported);
  fast.title = supported
    ? '只为普通截图问答启用 Codex priority 服务层'
    : '只有实时目录明确支持 priority 的当前 Codex 型号可启用';
}

function rebindNormalCodexDepth(keepCurrent, depth) {
  const model = document.getElementById('s-codex-model').value;
  const caps = (normalCodexCatalog && normalCodexCatalog.capabilities) || {};
  const depths = (caps[model] && caps[model].selectable === true)
    ? (caps[model].depths || [])
    : [];
  const depthEl = document.getElementById('s-codex-effort');
  replaceSelectOptions(
    depthEl,
    depths.length ? depths : [''],
    keepCurrent && depths.includes(depth) ? depth : (depths[0] || '')
  );
  depthEl.disabled = depths.length === 0;
  syncNormalCodexFast();
}

function renderNormalCodexSettings(data) {
  const stateEl = document.getElementById('s-codex-state');
  const current = (data && data.codex) || {};
  normalCodexCatalog = (data && data.codex_catalog) || {
    variants: ['gpt-5.3-codex-spark'],
    capabilities: {
      'gpt-5.3-codex-spark': {
        available: false,
        catalog_advertised: false,
        selectable: false,
        reason: 'Codex 实时目录不可用',
      },
    },
    verified: false,
    error: 'Codex 实时目录不可用',
  };
  const variants = normalCodexCatalog.variants || [];
  const caps = normalCodexCatalog.capabilities || {};
  const modelEl = document.getElementById('s-codex-model');
  modelEl.innerHTML = '';
  const defaultOption = document.createElement('option');
  defaultOption.value = '';
  defaultOption.textContent = '请选择可用型号';
  defaultOption.disabled = true;
  modelEl.appendChild(defaultOption);
  variants.forEach(model => {
    const cap = caps[model] || {};
    const op = document.createElement('option');
    op.value = model;
    op.disabled = cap.selectable !== true;
    const reason = String(cap.reason || '');
    op.textContent = model + (reason ? ' · ' + reason : '');
    op.title = reason;
    modelEl.appendChild(op);
  });
  const firstSelectable = variants.find(model =>
    caps[model] && caps[model].selectable === true);
  modelEl.value = variants.includes(current.model)
    ? current.model
    : (firstSelectable || '');
  rebindNormalCodexDepth(true, current.effort || '');
  document.getElementById('s-codex-fast').checked =
    current.fast === true && normalCodexFastSupported();
  syncNormalCodexFast(false);
  stateEl.className = normalCodexCatalog.verified === true ? '' : 'error';
  if (normalCodexCatalog.verified === true) {
    const cap = caps[modelEl.value] || {};
    stateEl.textContent = modelEl.value
      ? (cap.reason ||
        (cap.catalog_advertised === true || cap.available === true
          ? '此型号能力来自 Codex 实时目录'
          : '此型号使用兼容能力声明'))
      : '没有可选择的 Codex 型号，普通截图 Codex 调用保持禁用';
  } else {
    stateEl.textContent = firstSelectable
      ? 'Codex 实时目录暂不可用；兼容型号仍可普通调用，Fast 保持禁用'
      : 'Codex 能力目录暂不可用，型号、深度与 Fast 保持安全禁用';
  }
}

function cardFastSupported() {
  if (!cardImprovementCatalog) return false;
  const backend = document.getElementById('s-card-backend').value;
  const model = document.getElementById('s-card-model').value;
  if (backend !== 'codex') return false;
  const caps = cardImprovementCatalog.codex_capabilities || {};
  return !!(caps[model] && caps[model].selectable === true &&
            (caps[model].fast === true || caps[model].priority === true));
}

function syncCardFast(resetUnsupported) {
  const fast = document.getElementById('s-card-fast');
  const wrap = document.getElementById('s-card-fast-wrap');
  const supported = cardFastSupported();
  if (!supported && resetUnsupported !== false) fast.checked = false;
  fast.disabled = !supported;
  wrap.classList.toggle('is-disabled', !supported);
  fast.title = supported
    ? '只为卡片改进启用 Codex priority 服务层'
    : (document.getElementById('s-card-backend').value === 'codex'
      ? '当前 Codex 型号不支持 priority/Fast'
      : '只有支持 priority 的 Codex 型号可启用');
}

function cardDepthOptions(backend, model) {
  if (!cardImprovementCatalog) return [];
  if (backend === 'codex') {
    const caps = cardImprovementCatalog.codex_capabilities || {};
    return (caps[model] && caps[model].depths) || [];
  }
  return cardImprovementCatalog.depths[backend] || [];
}

function rebindCardDepth(keepCurrent, depth) {
  if (!cardImprovementCatalog) return;
  const backend = document.getElementById('s-card-backend').value;
  const model = document.getElementById('s-card-model').value;
  const depths = cardDepthOptions(backend, model);
  replaceSelectOptions(
    document.getElementById('s-card-depth'),
    depths,
    keepCurrent && depths.includes(depth) ? depth : depths[0]
  );
}

function rebindCardModelDepth(keepCurrent, model, depth) {
  if (!cardImprovementCatalog) return;
  const backend = document.getElementById('s-card-backend').value;
  const variants = cardImprovementCatalog.variants[backend] || [];
  const caps = cardImprovementCatalog.codex_capabilities || {};
  const unselectable = backend === 'codex'
    ? variants.filter(value => !caps[value] || caps[value].selectable !== true)
    : [];
  const firstSelectable = variants.find(value => !unselectable.includes(value));
  const labels = {};
  if (backend === 'codex') {
    variants.forEach(value => {
      const reason = String((caps[value] && caps[value].reason) || '');
      labels[value] = value + (reason ? ' · ' + reason : '');
    });
  }
  replaceSelectOptions(
    document.getElementById('s-card-model'),
    variants,
    keepCurrent && variants.includes(model)
      ? model
      : (firstSelectable || variants[0]),
    unselectable,
    labels
  );
  rebindCardDepth(keepCurrent, depth);
  syncCardFast();
}

function renderCardImprovementSettings(data) {
  const stateEl = document.getElementById('s-card-state');
  if (!data || !data.ok || !data.catalog) {
    cardImprovementCatalog = null;
    stateEl.className = 'error';
    stateEl.textContent = (data && data.error) ||
      '共享 card_improve 设置加载失败';
    return;
  }
  cardImprovementCatalog = data.catalog;
  const current = data.effective || data.default || {};
  replaceSelectOptions(
    document.getElementById('s-card-backend'),
    cardImprovementCatalog.backends || [],
    current.backend
  );
  rebindCardModelDepth(true, current.variant, current.depth);
  document.getElementById('s-card-fast').checked =
    current.fast === true && cardFastSupported();
  syncCardFast(false);
  stateEl.className = '';
  stateEl.textContent = '当前生效：' + current.backend + ' · ' +
    current.variant + ' · ' + current.depth +
    (current.fast === true ? ' · Fast' : '') +
    (cardImprovementCatalog.codex_catalog_verified === false
      ? '（Codex 实时目录暂不可用；可选性以型号能力说明为准，Fast 保持严格校验）'
      : '');
}

async function openSettings() {
  const cardState = document.getElementById('s-card-state');
  cardState.className = '';
  cardState.textContent = '正在读取共享 card_improve 设置…';
  cardImprovementCatalog = null;
  updateSettFields();
  document.getElementById('sett-overlay').classList.add('open');
  document.getElementById('sett-modal').classList.add('open');
  try {
    const s = await fetch('api/settings').then(r => r.json());
    document.getElementById('s-backend').value      = s.backend || 'claude_cli';
    document.getElementById('s-claude-model').value  = (s.claude && s.claude.model)  || '';
    document.getElementById('s-claude-effort').value = (s.claude && s.claude.effort) || '';
    renderNormalCodexSettings(s);
  } catch(_) {
    renderNormalCodexSettings(null);
  }
  try {
    const r = await fetch('api/card-improvement-settings');
    renderCardImprovementSettings(await r.json());
  } catch(_) {
    renderCardImprovementSettings({
      ok: false, error: '共享 card_improve 设置加载失败',
    });
  }
  updateSettFields();
}

function closeSettings() {
  document.getElementById('sett-overlay').classList.remove('open');
  document.getElementById('sett-modal').classList.remove('open');
  input.focus();
}

async function saveSettings() {
  const cardSettingsReady = !!cardImprovementCatalog;
  const backend = document.getElementById('s-backend').value;
  const payload = {
    backend,
    claude: {
      model:  document.getElementById('s-claude-model').value.trim(),
      effort: document.getElementById('s-claude-effort').value,
    },
    codex: {
      model: document.getElementById('s-codex-model').value.trim(),
      effort: document.getElementById('s-codex-effort').value,
      fast: document.getElementById('s-codex-fast').checked === true &&
        normalCodexFastSupported(),
    },
  };
  const cardPayload = cardSettingsReady ? {
    backend: document.getElementById('s-card-backend').value,
    variant: document.getElementById('s-card-model').value,
    depth: document.getElementById('s-card-depth').value,
    fast: document.getElementById('s-card-fast').checked === true &&
      cardFastSupported(),
  } : null;
  try {
    const [legacyResponse, cardResponse] = await Promise.all([
      fetch('api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      }),
      cardSettingsReady ? fetch('api/card-improvement-settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(cardPayload),
      }) : Promise.resolve(null),
    ]);
    const [j, card] = await Promise.all([
      legacyResponse.json(),
      cardResponse ? cardResponse.json() : Promise.resolve(null),
    ]);
    if (!legacyResponse.ok || j.ok === false) {
      status.textContent = '普通截图问答设置保存失败：' + (j.error || '');
      return;
    }
    const cl = backend === 'claude_cli'
      ? `${payload.claude.model || '默认模型'}${payload.claude.effort ? ' / ' + payload.claude.effort : ''}`
      : `${payload.codex.model || '默认模型'}` +
        `${payload.codex.effort ? ' / ' + payload.codex.effort : ''}` +
        `${payload.codex.fast === true ? ' / Fast' : ''}`;
    if (!cardResponse) {
      status.textContent = `普通截图问答设置已保存：${backend} · ${cl}；` +
        '共享 card_improve 未加载，卡片改进设置保持不变';
      closeSettings();
      return;
    }
    if (!cardResponse.ok || !card.ok) {
      status.textContent = '普通截图设置已保存，但卡片改进未保存：' +
        (card.error || '共享 action-pref 写入失败');
      document.getElementById('s-card-state').className = 'error';
      document.getElementById('s-card-state').textContent =
        card.error || '共享 action-pref 写入失败';
      return;
    }
    card.catalog = card.catalog || cardImprovementCatalog;
    renderCardImprovementSettings(card);
    const effective = card.effective || card.saved || cardPayload;
    status.textContent = `设置已保存：截图问答 ${backend} · ${cl}；卡片改进 ` +
      `${effective.variant} · ${effective.depth}${effective.fast === true ? ' · Fast' : ''}`;
  } catch(_) {
    status.textContent = '保存失败：设置服务不可用';
    return;
  }
  closeSettings();
}

document.getElementById('s-card-backend').addEventListener('change', () => {
  rebindCardModelDepth(false);
});
document.getElementById('s-card-model').addEventListener('change', () => {
  rebindCardDepth(false);
  syncCardFast();
});
document.getElementById('s-codex-model').addEventListener('change', () => {
  rebindNormalCodexDepth(false);
});

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

        elif self.path == "/api/card-improvement-settings":
            try:
                self.send_json(_load_card_improvement_settings_for_ui())
            except (CardImprovementError, RuntimeError) as error:
                self.send_json({"ok": False, "error": str(error)}, 503)

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
            query = parse_qs(urlparse(self.path).query)
            cid = (query.get("card") or [""])[0]
            index = (query.get("index") or [None])[0]
            ctx = _find_card_context(cid, index)
            self.send_json(ctx or {"error": "not found"}, 200 if ctx else 404)

        elif self.path.startswith("/api/card-update-status"):
            from urllib.parse import urlparse, parse_qs
            jid = (parse_qs(urlparse(self.path).query).get("job") or [""])[0]
            job = _card_jobs.get(jid)
            # 不 pop：done 的 job 保留 30 分钟，让弱网下「done 响应丢了」的轮询能重取，
            # 避免 job 被删后轮询一直 404 → 前端「暂时取不到结果」。懒清理过期项。
            now = time.time()
            for k, v in list(_card_jobs.items()):
                if v.get("status") == "done" and now - v.get("_t", now) > 1800:
                    _card_jobs.pop(k, None)
            self.send_json(job if job else {"status": "unknown"}, 200 if job else 404)

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
            # 卡片模式：要求 AI 用 Markdown 小标题给多知识点分层，便于按要点挑选（+ 按钮）
            _FMT_HINT = ("（回答时若涉及多个知识点或分多步，请用 Markdown 小标题"
                         "（## / ###）给每个要点分层，每个标题下写该点的内容；"
                         "单一要点的简短回答不必加标题。）")
            if card_ctx and not session.messages:
                blk = ["以下是我正在复习的一张 Anki 记忆卡片，请基于它回答我接下来的问题。"]
                if card_ctx.get("front"): blk.append(f"【卡片正面（问）】\n{card_ctx['front']}")
                if card_ctx.get("text"):  blk.append(f"【卡片内容】\n{card_ctx['text']}")
                if card_ctx.get("back"):  blk.append(f"【卡片背面（答）】\n{card_ctx['back']}")
                blk.append(_FMT_HINT)
                blk.append(f"———\n我的问题：{msg}" if msg else "———\n请先帮我讲解这张卡片的核心内容。")
                msg = "\n\n".join(blk)
            elif card_ctx:
                # 后续消息附简短提醒，避免长对话里 AI 忘记分层格式
                msg = f"{msg}\n\n{_FMT_HINT}"

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
                        # ⚠ 存**转换后的 PNG** base64:前端 pollScreenshot 硬编码 `data:image/png;base64,`+data,
                        # 若这里仍存原始 b64(iPad 快捷指令发的是 JPEG),就成了「声明 PNG 却塞 JPEG 数据」的
                        # data URL —— iOS Safari 严格校验直接不显示 = 「页面能开但没有截图」的真因(2026-07-14 实测确证)。
                        img_b64 = base64.b64encode(buf.getvalue()).decode()
                    except Exception:
                        temp_path.write_bytes(raw_bytes)   # 转换失败:保留原始字节与原始 b64(浏览器多数能嗅探)
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
            # Prepare only: target note | anki | all.  The returned signed
            # draft must be previewed and explicitly committed separately.
            local_id  = (body.get("local_id") or "").strip()
            target    = (body.get("target") or "").strip()
            pairs     = body.get("pairs") or []
            index     = body.get("index")
            verbosity = (body.get("verbosity") or "verbose").strip()
            fingerprint = (
                str((self.client_address or ("",))[0])
                + "\0"
                + str(self.headers.get("User-Agent") or "")
            )
            owner = _legacy_card_owner(
                body.get("owner_token") or "",
                fingerprint,
            )
            if verbosity not in ("verbose", "concise"):
                verbosity = "verbose"
            if not local_id or target not in ("note", "anki", "all"):
                self.send_json({"ok": False, "error": "invalid params"}, 400)
            elif not pairs:
                self.send_json({"ok": False, "error": "没有标记为有用的回答"}, 400)
            else:
                # 后台跑（AI 调用慢，移动端连接易断）；立即返回 job_id 供前端轮询
                import uuid
                job_id = uuid.uuid4().hex[:12]
                _card_jobs[job_id] = {"status": "running"}
                def _run(job_id=job_id, local_id=local_id, target=target, pairs=pairs,
                         index=index, verbosity=verbosity, owner=owner):
                    try:
                        out = _prepare_legacy_card_draft(
                            local_id,
                            pairs,
                            target,
                            index=index,
                            verbosity=verbosity,
                            owner=owner,
                        )
                    except Exception as e:
                        out = {"ok": False, "error": str(e)}
                    _card_jobs[job_id] = {"status": "done", "result": out, "_t": time.time()}
                threading.Thread(target=_run, daemon=True).start()
                self.send_json({"ok": True, "job_id": job_id})

        elif self.path == "/api/card-update-commit":
            # Commit accepts only an opaque signed draft id + target.  Draft
            # card/note content is never accepted from the browser.
            draft_id = str(body.get("draft_id") or "").strip()
            target = str(body.get("target") or "").strip().lower()
            fingerprint = (
                str((self.client_address or ("",))[0])
                + "\0"
                + str(self.headers.get("User-Agent") or "")
            )
            owner = _legacy_card_owner(
                body.get("owner_token") or "",
                fingerprint,
            )
            if not draft_id or target not in ("anki", "note"):
                self.send_json({"ok": False, "error": "invalid params"}, 400)
            else:
                import uuid
                job_id = uuid.uuid4().hex[:12]
                _card_jobs[job_id] = {"status": "running"}

                def _run_commit(
                    job_id=job_id,
                    draft_id=draft_id,
                    target=target,
                    owner=owner,
                ):
                    try:
                        out = _commit_legacy_card_draft(
                            draft_id=draft_id,
                            target=target,
                            owner=owner,
                        )
                    except Exception as error:
                        out = {"ok": False, "error": str(error)}
                    _card_jobs[job_id] = {
                        "status": "done",
                        "result": out,
                        "_t": time.time(),
                    }

                threading.Thread(target=_run_commit, daemon=True).start()
                self.send_json({"ok": True, "job_id": job_id})

        elif self.path == "/api/create-note":
            # 非 cardCtx 模式：把选中问答 + 截图 用 AI 整理写到一篇新笔记
            name = (body.get("name") or "").strip()
            pairs = body.get("pairs") or []
            img_b64 = body.get("image_b64")
            if not name:
                self.send_json({"ok": False, "error": "缺少笔记名"}, 400)
            elif not pairs:
                self.send_json({"ok": False, "error": "没有标记为有用的回答"}, 400)
            else:
                # 后台跑（AI 整理 10-30s，移动端连接易断）；复用 _card_jobs + card-update-status 轮询
                import uuid
                job_id = uuid.uuid4().hex[:12]
                _card_jobs[job_id] = {"status": "running"}
                def _run(job_id=job_id, name=name, pairs=pairs, img_b64=img_b64):
                    try:
                        out = _create_note_from_qa(name, pairs, img_b64)
                    except Exception as ex:
                        out = {"ok": False, "error": str(ex)}
                    _card_jobs[job_id] = {"status": "done", "result": out, "_t": time.time()}
                threading.Thread(target=_run, daemon=True).start()
                self.send_json({"ok": True, "job_id": job_id})

        elif self.path == "/api/card-delete":
            lid = (body.get("local_id") or "").strip()
            if not lid:
                self.send_json({"ok": False, "error": "missing local_id"}, 400)
            else:
                self.send_json(_card_delete(lid))

        elif self.path == "/api/save":
            # 先快照会话（classify 是同步 AI 调用，可达数十秒；server 模式下立刻 reset 继续服务，
            # 后台全链路只用快照 —— 任何一处仍读全局都会在 reset 后拿到空会话）
            msgs_snap = [dict(m) for m in state["session"].messages]
            img_snap  = state["img_fname"]
            tmp_snap  = state["temp_path"]

            def _do_full_save():
                try:
                    cls      = classify_conversation(msgs_snap)
                    match    = cls.get("match", "")
                    is_wrong = bool(cls.get("wrong", False))
                    related  = find_related_cards(cls.get("related", []), match) if is_wrong else []
                    result   = do_save(match, is_wrong, related,
                                       msgs=msgs_snap, img_fname=img_snap, temp_path=tmp_snap)
                    archive_conversation(result, "wrong" if is_wrong else "normal", related,
                                         msgs=msgs_snap, img_fname=img_snap, temp_path=tmp_snap)
                    threading.Thread(
                        target=push_to_website,
                        args=(img_snap,),
                        daemon=True,
                    ).start()
                except Exception as e:
                    result = f"保存失败：{e}"
                _export_history_to_webapp()  # 服务端实例：让 webapp /history/ 立刻看到新条目
                return result

            if state.get("_server_mode"):
                # server 模式异步：job 化（移动端锁屏断连也不丢结果），复用 _card_jobs 轮询端点
                import uuid
                job_id = uuid.uuid4().hex[:12]
                _card_jobs[job_id] = {"status": "running"}
                def _run(job_id=job_id):
                    res = _do_full_save()
                    _card_jobs[job_id] = {"status": "done", "result": {"result": res}, "_t": time.time()}
                threading.Thread(target=_run, daemon=True).start()
                # 快照已取，立即重置会话继续服务
                state["session"].reset()
                state.update({"img_b64": None, "img_fname": None, "temp_path": None})
                self.send_json({"ok": True, "job_id": job_id})
            else:
                # 本地模式保持同步（state['done'].set() 时机依赖响应已送出）
                result = _do_full_save()
                self.send_json({"result": result})
                state["done"].set()

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

        elif self.path == "/api/card-improvement-settings":
            try:
                result = _save_card_improvement_settings_from_ui(body)
                self.send_json(result, 200 if result.get("ok") else 400)
            except (CardImprovementError, RuntimeError) as error:
                self.send_json({"ok": False, "error": str(error)}, 503)

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
