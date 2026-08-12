"""
ai_client.py — 统一 AI 调用模块

支持 Claude CLI、Codex CLI（OpenAI）及自动切换。
提供一次性查询（ask）和多轮对话（AISession）两种接口。
"""

import json, os, re, shutil, subprocess, sys, tempfile, threading
from pathlib import Path

# ── 路径常量（跨平台，复用 config.py 的 env-aware 路径） ────────────────────────

sys.path.insert(0, str(Path(__file__).parent))
from config import (  # noqa: E402
    CLAUDE_CLI as CLAUDE,
    CODEX_CLI as CODEX,
    PROJECT_DIR,
    TEMP_DIR,
    AI_SETTINGS_FILE as SETTINGS_FILE,
)

PROJECT   = str(PROJECT_DIR)
STORE_DIR = SETTINGS_FILE.parent
WINDOWS   = sys.platform == "win32"


def _resolve_cli(configured: str, name: str) -> str:
    """配置的 CLI 路径不存在时,回退到 PATH 里的同名可执行文件。

    claude CLI 会自更新并迁移安装位置(如 /usr/bin → ~/.local/bin),
    硬编码 APP_CLAUDE 路径会失效。Linux 上若配置路径缺失就用 shutil.which
    兜底,避免 webapp/daily 整条 AI 链路因 CLI 搬家而崩。
    """
    if WINDOWS:
        return configured
    try:
        # 配置的是存在的真路径 → 直接用
        if configured and os.sep in configured and os.path.exists(configured):
            return configured
        # 否则按 name 在增强 PATH 里找:并入 ~/.local/bin + 常见安装位,
        # 解决 sudo/systemd 最小 PATH(不含 ~/.local/bin)下 which 找不到。
        extra = [os.path.expanduser("~/.local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
        search = os.pathsep.join([os.environ.get("PATH", "")] + extra)
        found = shutil.which(name, path=search)
        if found:
            return found
    except Exception:
        pass
    return configured


CLAUDE = _resolve_cli(CLAUDE, "claude")
CODEX  = _resolve_cli(CODEX, "codex")


# ── 脱壳 cwd ───────────────────────────────────────────────────────────────────
# claude CLI 从 cwd 沿目录树向上找 CLAUDE.md(项目记忆,本项目这份很大)。原来 cwd=PROJECT
# → 每次制卡/摘要/关联调用都把整个 CLAUDE.md 灌进上下文(纯浪费:这些 prompt 自带全部所需
# 内容,不依赖项目记忆)。改钉到项目树外稳定空目录 → 不加载 CLAUDE.md;再配 --setting-sources ""
# 不加载设置/插件。--continue(多轮)按 cwd 维护会话,cwd 固定不变故续写照常。登录凭证另存不受影响。
def _strip_cwd() -> str:
    d = os.path.join(tempfile.gettempdir(), "bwicarus-cli-cwd")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return tempfile.gettempdir()


CLI_CWD = _strip_cwd()

DEFAULT_SETTINGS = {"backend": "auto-claude", "model": ""}
_VALID_BACKENDS  = frozenset(("auto-claude", "auto-codex", "claude", "codex"))

# ── 设置 ──────────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    data = DEFAULT_SETTINGS.copy()
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update({k: str(v).strip() for k, v in saved.items() if k in data})
        except Exception:
            pass
    if data.get("backend") not in _VALID_BACKENDS:
        data["backend"] = "auto-claude"
    return data

def save_settings(data: dict) -> dict:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    merged = DEFAULT_SETTINGS.copy()
    merged.update({k: str(v).strip() for k, v in data.items() if k in merged})
    if merged.get("backend") not in _VALID_BACKENDS:
        merged["backend"] = "auto-claude"
    SETTINGS_FILE.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    return merged

# ── 底层调用 ──────────────────────────────────────────────────────────────────

def _run_hidden(cmd, **kwargs):
    """Windows：隐藏窗口跑子进程；Linux：直接跑（不需要隐藏）。"""
    if WINDOWS:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        flags = kwargs.pop("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return subprocess.run(cmd, startupinfo=si, creationflags=flags, **kwargs)
    kwargs.pop("creationflags", None)  # Linux 不支持
    return subprocess.run(cmd, **kwargs)


# ── AI 调用日志 ────────────────────────────────────────────────────────────────

import time
import datetime

_LOG_FILE   = Path(PROJECT) / "state" / "logs" / "ai_calls.log"
_LOG_LOCK   = threading.Lock()
_LOG_HEAD   = 200   # prompt / response 截断长度
_LOG_LIMIT  = 5_000_000  # 5 MB 滚动


def _truncate(text: str, n: int = _LOG_HEAD) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n] + f"…[+{len(text) - n} chars]"


def _log_ai_call(backend: str, label: str, prompt: str, response: str, duration: float) -> None:
    try:
        with _LOG_LOCK:
            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            # 滚动：超过限制就备份并清空
            try:
                if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _LOG_LIMIT:
                    bak = _LOG_FILE.with_suffix(".log.1")
                    bak.unlink(missing_ok=True)
                    _LOG_FILE.replace(bak)
            except OSError:
                pass

            ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            with _LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"--- {ts}  backend={backend}  {label}  {duration:.1f}s ---\n")
                f.write(f"PROMPT: {_truncate(prompt)}\n")
                f.write(f"RESPONSE: {_truncate(response)}\n\n")
    except Exception:
        pass  # 日志失败不能影响主流程


_claude_lock = threading.Lock()


def claude_raw(prompt: str, first: bool = False,
               model: str = "", effort: str = "") -> str:
    """调用 Claude CLI；first=True 开新会话，False 使用 --continue 续写。

    Args:
        model: "" 默认 / "opus" / "sonnet" / "haiku" / 完整 model id
        effort: "" 默认 / "low" / "medium" / "high" / "xhigh" / "max"
                深度思考用 xhigh/max。

    Windows 加 --dangerously-skip-permissions 跳过本机交互式权限确认。
    Linux/root 下 Claude CLI 拒绝该 flag（"cannot be used with root/sudo"），
    所以仅 Windows 加；服务器 root 走默认模式，简单 prompt 不会触发权限提示。
    """
    with _claude_lock:
        cmd = [CLAUDE]
        if WINDOWS:
            cmd += ["--dangerously-skip-permissions"]
        # 脱壳:不加载 user/project 设置 + 插件(配合下面 cwd=CLI_CWD 不加载 CLAUDE.md)→ 省 token
        cmd += ["--setting-sources", "", "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if not first:
            cmd.append("--continue")
        cmd += ["-p", prompt]
        t0 = time.time()
        try:
            r = _run_hidden(cmd, cwd=CLI_CWD, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=900)   # 15 分钟,够 Opus max effort
        except subprocess.TimeoutExpired as e:
            tag = f"raw TIMEOUT model={model} effort={effort}"
            _log_ai_call("claude", tag, prompt, f"TIMEOUT after {e.timeout}s", time.time() - t0)
            return ""
        out = (r.stdout or "").strip()
        # CLI 失败时(returncode != 0)记录 stderr 帮调试
        if r.returncode != 0 and not out:
            stderr = (r.stderr or "").strip()
            tag = f"raw FAIL rc={r.returncode} model={model} effort={effort}"
            _log_ai_call("claude", tag, prompt,
                         f"STDERR: {stderr[:500]}", time.time() - t0)
            return ""
        tag = "raw" + ("" if first else " --continue")
        if model: tag += f" model={model}"
        if effort: tag += f" effort={effort}"
        _log_ai_call("claude", tag, prompt, out, time.time() - t0)
        return out


def codex_raw(prompt: str, image_path: str = None, model: str = "") -> str:
    """调用 Codex CLI；可附带图片，model 为空时使用 Codex 默认模型。"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    base = ["cmd.exe", "/d", "/c", CODEX] if CODEX.lower().endswith((".cmd", ".bat")) else [CODEX]
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", dir=TEMP_DIR, delete=False) as f:
        out_path = f.name
    cmd = base + [
        "exec", "--sandbox", "read-only", "--skip-git-repo-check",
        "--color", "never", "--output-last-message", out_path, "-C", PROJECT,
    ]
    if image_path and Path(image_path).exists():
        cmd += ["--image", str(image_path)]
    if model:
        cmd += ["-m", model]
    cmd.append("-")
    t0 = time.time()
    try:
        r = _run_hidden(cmd, input=prompt, cwd=PROJECT, capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
        out_file = Path(out_path)
        text = out_file.read_text(encoding="utf-8").strip() if out_file.exists() else ""
        result = text or r.stdout.strip() or (r.stderr or "").strip()
        label = f"raw{' +img' if image_path else ''}{' '+model if model else ''}"
        _log_ai_call("codex", label, prompt, result, time.time() - t0)
        return result
    finally:
        try: Path(out_path).unlink(missing_ok=True)
        except Exception: pass

# ── 限流检测 & 路由 ───────────────────────────────────────────────────────────

def is_rate_limited(response: str) -> bool:
    low = (response or "").lower()
    return any(kw in low for kw in [
        "rate limit", "too many requests", "429", "overloaded",
        "capacity exceeded", "quota", "usage limit",
    ])


def is_backend_unavailable(response: str) -> bool:
    """Return True only for explicit provider/session failures worth rerouting.

    Auto routing used to switch providers only for rate limits.  A dead OAuth
    session therefore leaked its error text back as if it were a model answer,
    so callers such as the Japanese dictionary silently fell through to a
    lower-quality machine translator.  Keep the match list deliberately tied
    to infrastructure errors; ordinary content mentioning authentication must
    not cause a second model call.
    """
    low = (response or "").strip().lower()
    if not low:
        return True
    if is_rate_limited(low):
        return True
    # CLI failures appear as their own line (sometimes prefixed with ``Error:``).
    # Do not substring-match the whole answer: a valid explanation can naturally
    # discuss phrases such as "401 Unauthorized" or "authentication failed".
    error_line = re.compile(
        r"(?:error\s*:\s*)?(?:"
        r"failed to authenticate:\s*oauth session expired and could not be refreshed\.?|"
        r"oauth session expired(?: and could not be refreshed)?\.?|"
        r"authentication failed:\s*(?:invalid authentication credentials|oauth session expired)\.?|"
        r"authentication_error:\s*(?:invalid authentication credentials|oauth session expired)\.?|"
        r"invalid authentication credentials\.?|"
        r"could not refresh (?:the )?oauth session\.?|"
        r"please run /login\.?|"
        r"please log in to continue\.?|"
        r"login required:\s*(?:please )?(?:run /login|log in to continue)\.?"
        r")"
    )
    for line in low.splitlines():
        line = line.strip()
        if error_line.fullmatch(line):
            return True
        if re.fullmatch(r"(?:error\s*:\s*)?401 unauthorized(?:[.!:]\s*.*)?", line):
            return True
    return False

def route(backend: str, try_claude, try_codex) -> str:
    """按 backend 路由；Auto 模式在首选限流时自动切换。"""
    if backend == "claude": return try_claude()
    if backend == "codex":  return try_codex()
    if backend == "auto-codex":
        r = try_codex()
        if is_backend_unavailable(r):
            r2 = try_claude()
            return r2 if r2 and not is_backend_unavailable(r2) else r
        return r
    # auto-claude（默认）
    r = try_claude()
    if is_backend_unavailable(r):
        r2 = try_codex()
        return r2 if r2 and not is_backend_unavailable(r2) else r
    return r

# ── 高层接口 ──────────────────────────────────────────────────────────────────

def ask(prompt: str, image_path: str = None,
        claude_model: str = "", claude_effort: str = "") -> str:
    """一次性查询，无历史上下文。

    Args:
        claude_model: 强制 Claude 用某模型 ("opus" / "sonnet" / "haiku")
        claude_effort: 强制 Claude 用某 effort ("low"/"medium"/"high"/"xhigh"/"max")

    Codex 不受这两个参数影响,Codex 用 settings.model。
    """
    s = load_settings()
    m = s.get("model", "")
    return route(
        s.get("backend", "auto-claude"),
        lambda: claude_raw(prompt, first=True, model=claude_model, effort=claude_effort),
        lambda: codex_raw(prompt, image_path=image_path, model=m),
    )


class AISession:
    """
    多轮对话会话。
    - Claude 后端：首次调用开新会话，后续用 --continue 续写
    - Codex 后端：每次调用将完整历史拼入 prompt
    - Auto 后端：首选限流时自动切换

    用法：
        session = AISession()
        r1 = session.send("什么是矩阵？", image_path="screen.png")
        r2 = session.send("能举个例子吗？")
        session.reset()
    """

    def __init__(self, system_prompt: str = "你是一个问答助手。只回答问题，不要修改文件，不要运行命令。"):
        self.system_prompt = system_prompt
        self.messages: list[dict] = []
        self._first = True

    def reset(self):
        self.messages = []
        self._first = True

    def send(self, message: str, image_path: str = None) -> str:
        s = load_settings()
        backend, model = s.get("backend", "auto-claude"), s.get("model", "")
        first = self._first
        msgs  = list(self.messages)          # 调用前的历史快照

        def try_claude() -> str:
            if first and msgs:               # 加载历史后的首次发送
                lines = ["以下是之前的对话记录，请阅读后继续这个对话：\n"]
                for m in msgs:
                    label = "用户" if m["role"] == "user" else "Claude"
                    lines.append(f"**{label}**：{m['text']}")
                lines.append(f"\n用户现在继续提问：{message}")
                return claude_raw("\n\n".join(lines), first=True)
            if image_path:
                return claude_raw(f"请读取图片 {image_path}，然后回答：{message}", first=first)
            return claude_raw(message, first=first)

        def try_codex() -> str:
            lines = [self.system_prompt]
            if msgs:
                lines.append("\n对话历史：")
                for m in msgs:
                    label = "用户" if m["role"] == "user" else "助手"
                    lines.append(f"{label}：{m['text']}")
            lines.append(f"\n用户现在提问：{message}")
            return codex_raw("\n".join(lines), image_path=image_path, model=model)

        result = route(backend, try_claude, try_codex)
        self.messages.append({"role": "user",      "text": message})
        self.messages.append({"role": "assistant",  "text": result})
        self._first = False
        return result
