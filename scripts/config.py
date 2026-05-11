"""
config.py — 集中管理项目路径和常量。

所有硬编码路径优先从这里读取。环境变量可覆盖：
    OBSIDIAN_VAULT  → VAULT_ROOT
    CLAUDE_PROJECT  → PROJECT_DIR
    APP_PYTHON      → PYTHON
    APP_PYTHONW     → PYTHONW
    APP_CLAUDE      → CLAUDE_CLI
    APP_CODEX       → CODEX_CLI
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 项目目录 ─────────────────────────────────────────────────────────────────

PROJECT_DIR  = Path(os.environ.get("CLAUDE_PROJECT", r"C:\claude"))
VAULT_ROOT   = Path(os.environ.get("OBSIDIAN_VAULT", r"C:\obsidian"))

SCRIPTS_DIR    = PROJECT_DIR / "scripts"
LAUNCHERS_DIR  = PROJECT_DIR / "launchers"
DIST_DIR       = LAUNCHERS_DIR / "dist"
REFERENCES_DIR = PROJECT_DIR / "references"
PROMPTS_DIR    = REFERENCES_DIR / "prompts"
INDEX_DIR      = PROJECT_DIR / "index"
STATE_DIR      = PROJECT_DIR / "state"
ANKI_DIR       = PROJECT_DIR / "anki"
RECORDS_DIR    = ANKI_DIR / "records"
TEMP_DIR       = PROJECT_DIR / "temp"
DASHBOARD_DIR  = PROJECT_DIR / "dashboard"
HISTORY_DIR    = PROJECT_DIR / "history"
LOGS_DIR       = STATE_DIR / "logs"
BACKUP_DIR     = STATE_DIR / "backup"

# ── 状态文件 ────────────────────────────────────────────────────────────────

NOTE_STATES_FILE   = STATE_DIR / "note-states.json"
ACTIVE_TASKS_FILE  = STATE_DIR / "active_tasks.json"
AI_CALLS_LOG       = LOGS_DIR / "ai_calls.log"

# ── 可执行文件路径 ──────────────────────────────────────────────────────────

PYTHON  = os.environ.get("APP_PYTHON",  r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe")
PYTHONW = os.environ.get("APP_PYTHONW", r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\pythonw.exe")

CLAUDE_CLI = os.environ.get(
    "APP_CLAUDE",
    r"C:\Users\bwica\AppData\Local\Microsoft\WinGet\Packages"
    r"\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe",
)
CODEX_CLI = os.environ.get("APP_CODEX", r"C:\Users\bwica\AppData\Roaming\npm\codex.cmd")

ANKI_EXE_CANDIDATES = (
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Anki\anki.exe"),
    r"C:\Program Files\Anki\anki.exe",
)
OBSIDIAN_EXE = r"C:\Users\bwica\AppData\Local\Programs\Obsidian\Obsidian.com"

# ── AI 设置 ─────────────────────────────────────────────────────────────────

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
AI_SETTINGS_FILE = Path(_LOCALAPPDATA) / "截图问答" / "settings.json"

# ── 网络 ────────────────────────────────────────────────────────────────────

ANKI_CONNECT_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
CMD_SERVER_PORT  = 9090
REMOTE_QA_PORT   = 5001

REMOTE_HOST       = "bwicarus.space"
REMOTE_SCP_TARGET = "root@31.220.31.30"
DASHBOARD_DEST    = f"{REMOTE_SCP_TARGET}:/root/webapp/data/dashboard/"
HISTORY_DEST      = f"{REMOTE_SCP_TARGET}:/root/webapp/data/history/"
TUNNEL_MATCH      = f"-R {REMOTE_QA_PORT}:127.0.0.1:{REMOTE_QA_PORT} root@{REMOTE_HOST}"

# ── 杂项 ────────────────────────────────────────────────────────────────────

import re as _re
NOTE_PATTERN = _re.compile(r"^[0-9A-Fa-f]{3}-.+\.md$")
