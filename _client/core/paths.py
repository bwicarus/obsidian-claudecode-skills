"""客户端本地数据目录（与 launcher.app_dir() 同步）。

所有"客户端自己生成的"持久状态都放这里，用户不需要在 GUI 里指定路径：
  config.json          launcher 创建
  core/<version>/      launcher 下载并解压
  launcher.log         launcher 写
  client.lock          launcher 单例锁
  cmd_server_key.txt   cmd_server 启动时生成（迁移自旧路径 %LOCALAPPDATA%\\截图问答\\）

GUI 里那些"用户数据目录"字段（vault / dashboard / history / scripts）仍由用户填，
因为它们是用户自己的工作目录、位置因人而异；这里只管客户端自己的运行时数据。
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "bwicarus-client"


def app_dir() -> Path:
    """返回客户端数据目录。优先读 launcher 设置的环境变量；
    其次读 %APPDATA%\\bwicarus-client\\datadir.txt 指针；
    都没有就退化到默认 %LOCALAPPDATA%\\bwicarus-client\\。"""
    env = os.getenv("BWICARUS_APP_DIR")
    if env:
        p = Path(env)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    # 自行读指针（万一 core 被独立调用、不经 launcher）
    appdata = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    pointer = appdata / APP_NAME / "datadir.txt"
    if pointer.exists():
        try:
            target = Path(pointer.read_text(encoding="utf-8").strip())
            if target:
                target.mkdir(parents=True, exist_ok=True)
                return target
        except OSError:
            pass
    # 默认
    base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    p = base / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(name: str) -> Path:
    """app_dir 下的子文件路径。"""
    return app_dir() / name


# ── 用户数据：两个根目录派生其余 ──────────────────────────────────────────
# 主项目根（含 scripts / dashboard / history / state / index / anki 等子目录）
DEFAULT_PROJECT_ROOT = os.environ.get("CLAUDE_PROJECT", r"C:\claude")
# Obsidian vault 根（用户笔记目录）。env 优先，让服务器/Linux 上跑时直接命中。
_VAULT_ENV = os.environ.get("OBSIDIAN_VAULT")
COMMON_VAULT_GUESSES = ([_VAULT_ENV] if _VAULT_ENV else []) + [
    r"C:\obsidian",
    str(Path.home() / "Documents" / "Obsidian Vault"),
    str(Path.home() / "Obsidian"),
]


def derive_paths(project_root: str | None) -> dict[str, str]:
    """从主项目根目录派生出所有子路径。
    没指定 project_root 就用 DEFAULT_PROJECT_ROOT；不要求实际存在。"""
    root = Path(project_root or DEFAULT_PROJECT_ROOT)
    return {
        "scripts_dir":            str(root / "scripts"),
        "dashboard_dir":          str(root / "dashboard"),
        "history_dir":            str(root / "history"),
        "register_script":        str(root / "scripts" / "register_notes.py"),
        "dashboard_export_script": str(root / "scripts" / "export_dashboard.py"),
        "task_state_file":        str(root / "state" / "active_tasks.json"),
        "qa_index_dir":           str(root / "index"),
        "qa_anki_records_dir":    str(root / "anki" / "records"),
    }


def guess_vault() -> str:
    for p in COMMON_VAULT_GUESSES:
        if Path(p).exists():
            return p
    return ""
