"""AI 后端 adapter 抽象 + 5 个实现。

仅用 stdlib（urllib / subprocess）。客户端 GUI 只需要 ping() 验证连通；
chat() 留作后续业务接入用，第一档不在 GUI 里直接调。

调用模式：
    backend = make_backend("claude_cli", settings={"command": "claude"})
    ok, msg = backend.ping()
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


def _resolve_exec(command: str) -> str:
    """配置的可执行路径不存在时,在增强 PATH 里按 basename 找回。

    解决两类问题:
    1. CLI 自更新搬家(如 claude 从 /usr/bin/claude 迁到 ~/.local/bin/claude),
       硬编码的绝对路径失效。
    2. systemd 服务的受限 PATH(/usr/local/bin:/usr/bin:/bin,不含 ~/.local/bin),
       导致 bare name `claude` 或默认 shutil.which 都找不到。
    增强 PATH 并入 ~/.local/bin + 常见安装位,确保服务里也能解析。
    Windows 不动(客户端 GUI 用完整路径或 .cmd)。
    """
    if os.name == "nt" or not command:
        return command
    if os.sep in command and os.path.exists(command):
        return command   # 已是存在的绝对/相对路径,直接用
    name = os.path.basename(command)
    extra = [os.path.expanduser("~/.local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    search = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    return shutil.which(name, path=search) or command
from abc import ABC, abstractmethod
from pathlib import Path


# ── 抽象接口 ────────────────────────────────────────────────────────
class BackendAdapter(ABC):
    """所有 AI 后端实现的公共接口。"""

    name: str = "base"

    def __init__(self, settings: dict):
        self.settings = settings or {}

    @abstractmethod
    def ping(self) -> tuple[bool, str]:
        """连通 / 配置正确性测试，返回 (ok, 描述)。"""
        ...

    @abstractmethod
    def chat(self, messages: list[dict], image: bytes | None = None) -> str:
        """主对话接口。
        messages: [{"role": "system"|"user"|"assistant", "content": str}]
                  约定：role==system 至多一条；OpenAI 风格
        image:    可选附图 PNG/JPG 字节流（仅最后一条 user 消息生效）
        return:   AI 回复文本
        """
        ...

    def chat_stream(self, messages: list[dict], image: bytes | None = None):
        """流式对话：yield 一连串 text chunk（拼起来 = 完整回复）。
        默认 fallback：调阻塞 chat() 然后 yield 一个 chunk。子类覆盖以实现真 streaming。
        GeneratorExit（调用方提前 close generator，例如客户端断开）时应清理子进程 / 连接。
        """
        text = self.chat(messages, image=image)
        if text:
            yield text


# ── 工具：消息序列 → 单串 prompt（CLI backend 用） ──
def _flatten_messages(messages: list[dict], image_path: str | None = None) -> str:
    lines: list[str] = []
    system_lines: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_lines.append(content)
        else:
            label = {"user": "用户", "assistant": "助手"}.get(role, role)
            lines.append(f"{label}：{content}")
    head = "\n\n".join(system_lines)
    body = "\n\n".join(lines)
    parts = [p for p in (head, body) if p]
    text = "\n\n".join(parts)
    if image_path:
        text += f"\n\n（请读取图片 {image_path} 后回答上面的最新问题）"
    return text


def _spool_image(image: bytes) -> str:
    """图字节落盘到 temp 文件，返回路径。调用方负责清理（CLI backend 自己清）。"""
    tmp_dir = Path(tempfile.gettempdir()) / "bwicarus-client"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    f = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=str(tmp_dir))
    f.write(image)
    f.close()
    return f.name


# ── 进程隐藏窗口（Windows pythonw 子进程） ──
def _run_hidden(cmd, **kwargs):
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        flags = kwargs.pop("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return subprocess.run(cmd, startupinfo=si, creationflags=flags, **kwargs)
    return subprocess.run(cmd, **kwargs)


# ── 脱壳 cwd ───────────────────────────────────────────────────────
# claude CLI 会从 cwd 沿目录树向上找 CLAUDE.md(项目记忆,本项目这份巨大),把 cwd 钉在
# 项目树外的空目录 → 不加载 CLAUDE.md;再配 `--setting-sources ""` → 不加载 user/project
# 设置 + engineering 插件 MCP。两者合计省掉每次调用的大头 token(登录/凭证另存,不受影响)。
# 跟 _server_deploy/assistant.py 的 _ASST_CWD 同一思路。Windows 客户端进程一般在项目外起,
# 影响小,但仍指向稳定空目录无害。
def _strip_cwd() -> str:
    d = os.path.join(tempfile.gettempdir(), "bwicarus-cli-cwd")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return tempfile.gettempdir()


_STRIP_CWD = _strip_cwd()


def _verified_codex_capability(model: str) -> dict:
    """Resolve one model against the reader's verified live Codex catalog.

    A persisted ``fast`` bit is not a capability proof.  The execution adapter
    rechecks the canonical assistant catalog immediately before constructing
    CLI flags; a standalone client without that catalog therefore fails closed.
    """
    try:
        import assistant as reader_assistant  # type: ignore
    except ImportError:
        server_dir = Path(__file__).resolve().parents[2] / "_server_deploy"
        if server_dir.is_dir() and str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        try:
            import assistant as reader_assistant  # type: ignore
        except ImportError as error:
            raise RuntimeError(
                "无法读取 Codex 实时模型目录，普通截图问答已停止调用"
            ) from error
    try:
        capability = dict(reader_assistant._codex_capability(model) or {})
    except Exception as error:
        raise RuntimeError(
            "无法验证 Codex 型号能力，普通截图问答已停止调用"
        ) from error
    selectable = (
        capability.get("selectable") is True
        or (
            "selectable" not in capability
            and capability.get("available") is True
        )
    )
    if not selectable:
        reason = str(
            capability.get("reason")
            or "当前 Codex 运行环境没有验证这个型号"
        )
        raise RuntimeError(f"Codex 型号不可用：{model}（{reason}）")
    return capability


# ── Gemini 兜底 ────────────────────────────────────────────────────
# claude CLI 失败/限流/返回空时用 Gemini 顶上(省 Claude 额度 + 防单边挂)。纯 stdlib urllib。
# key 文件跟 PDF 助手(assistant.py)同一组:免费档优先,付费档兜底。失败一律返回 ""。
def _gemini_keys() -> list[str]:
    out: list[str] = []
    for p in ("~/.config/gemini-api-key-free", "~/.config/gemini-api-key"):
        try:
            k = Path(os.path.expanduser(p)).read_text(encoding="utf-8").strip()
            if k and k not in out:
                out.append(k)
        except Exception:
            pass
    return out


def _gemini_chat(prompt: str, image_path: str | None = None, model: str = "",
                 timeout: int = 120) -> str:
    keys = _gemini_keys()
    if not keys or not (prompt or "").strip():
        return ""
    mdl = (model or "").strip() or "gemini-3.5-flash"
    parts: list[dict] = [{"text": prompt}]
    if image_path:
        try:
            raw = Path(image_path).read_bytes()
            ext = (Path(image_path).suffix or ".png").lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/png")
            parts.append({"inline_data": {"mime_type": mime,
                          "data": base64.b64encode(raw).decode("ascii")}})
        except Exception:
            pass
    body = json.dumps({"contents": [{"role": "user", "parts": parts}]}).encode("utf-8")
    for key in keys:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               + mdl + ":generateContent?key=" + key)
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            cands = d.get("candidates") or []
            if cands:
                txt = "".join(
                    p.get("text", "")
                    for p in ((cands[0].get("content") or {}).get("parts") or []))
                if txt.strip():
                    return txt.strip()
        except Exception:
            continue
    return ""


# ── CLI 类 ─────────────────────────────────────────────────────────
class CliBackend(BackendAdapter):
    """通用 CLI adapter：执行 `<command> --version` 验证可用性。"""

    name = "cli"
    default_command = "echo"

    def _command(self) -> str:
        raw = (self.settings.get("command") or self.default_command).strip()
        return _resolve_exec(raw)

    def ping(self) -> tuple[bool, str]:
        cmd = self._command()
        if not cmd:
            return False, f"未配置 {self.name} 命令"
        # Windows .cmd / .bat 需要通过 cmd.exe 调起，否则 FileNotFoundError
        if os.name == "nt" and cmd.lower().endswith((".cmd", ".bat")):
            argv = ["cmd.exe", "/d", "/c", cmd, "--version"]
        else:
            argv = [cmd, "--version"]
        try:
            r = _run_hidden(
                argv, capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                shell=False, check=False,
            )
        except FileNotFoundError:
            return False, f"找不到可执行文件：{cmd}（请填写完整路径）"
        except subprocess.TimeoutExpired:
            return False, f"{cmd} --version 超时（15s）"
        except Exception as e:
            return False, f"启动 {cmd} 失败：{e}"
        out = (r.stdout or r.stderr or "").strip().splitlines()
        head = out[0] if out else "(no output)"
        if r.returncode == 0:
            return True, f"{cmd}: {head}"
        return False, f"{cmd} 返回 {r.returncode}: {head}"


class ClaudeCli(CliBackend):
    name = "claude_cli"
    default_command = "claude"

    def _model_effort_flags(self) -> list[str]:
        """从 settings 取 model / effort（思考深度），拼成 claude CLI flags。"""
        flags: list[str] = []
        model = (self.settings.get("model") or "").strip()
        if model:
            flags += ["--model", model]
        effort = (self.settings.get("effort") or "").strip().lower()
        if effort in ("low", "medium", "high", "xhigh", "max"):
            flags += ["--effort", effort]
        return flags

    def chat(self, messages: list[dict], image: bytes | None = None) -> str:
        cmd = self._command()
        image_path = _spool_image(image) if image else None
        try:
            prompt = _flatten_messages(messages, image_path=image_path)
            claude_err = ""
            try:
                # 脱壳:--setting-sources "" 不加载设置/插件,cwd=项目树外 → 不加载 CLAUDE.md;
                # --allowedTools Read 保留(带图时模型需 Read 落盘图片)。
                full = [cmd, "--allowedTools", "Read", "--setting-sources", "",
                        *self._model_effort_flags(),
                        "--output-format", "text", "-p", prompt]
                r = _run_hidden(full, cwd=_STRIP_CWD, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=int(self.settings.get("timeout", 180)))
                out = (r.stdout or "").strip()
                if out:
                    return out
                claude_err = (r.stderr or "").strip()[:300] or f"claude_cli 空(exit {r.returncode})"
            except Exception as e:
                claude_err = str(e)[:300]
            # ── claude 失败/限流/空 → Gemini 兜底(省额度 + 防单边挂)──
            g = _gemini_chat(_flatten_messages(messages), image_path=image_path,
                             model=(self.settings.get("gemini_model") or ""))
            if g:
                return g
            raise RuntimeError(claude_err or "claude_cli 与 Gemini 兜底均失败")
        finally:
            if image_path:
                try: Path(image_path).unlink(missing_ok=True)
                except Exception: pass

    def chat_stream(self, messages: list[dict], image: bytes | None = None):
        cmd = self._command()
        image_path = _spool_image(image) if image else None
        prompt = _flatten_messages(messages, image_path=image_path)
        # 脱壳:同 chat() —— --setting-sources "" + cwd 项目树外,不加载 CLAUDE.md/插件。
        full = [cmd, "--allowedTools", "Read", "--setting-sources", "",
                *self._model_effort_flags(),
                "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "-p", prompt]
        popen_kw = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "cwd": _STRIP_CWD,
        }
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            popen_kw["startupinfo"] = si
            popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(full, **popen_kw)
        emitted_any = False
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "stream_event":
                    continue
                event = ev.get("event") or {}
                if event.get("type") != "content_block_delta":
                    continue
                delta = event.get("delta") or {}
                if delta.get("type") != "text_delta":
                    continue
                txt = delta.get("text") or ""
                if txt:
                    emitted_any = True
                    yield txt
            rc = proc.wait()
            if not emitted_any:
                # claude 一个字都没吐(失败/限流/空)→ Gemini 兜底,整段一次性 yield。
                g = _gemini_chat(_flatten_messages(messages), image_path=image_path,
                                 model=(self.settings.get("gemini_model") or ""))
                if g:
                    yield g
                elif rc != 0:
                    err = (proc.stderr.read() or "").strip()[:300]
                    raise RuntimeError(err or f"claude_cli exit {rc}")
        except GeneratorExit:
            # 客户端中途断开
            try: proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass
            raise
        finally:
            try: proc.stdout.close()
            except Exception: pass
            try: proc.stderr.close()
            except Exception: pass
            if image_path:
                try: Path(image_path).unlink(missing_ok=True)
                except Exception: pass


class CodexCli(CliBackend):
    name = "codex_cli"
    default_command = "codex"

    def _model_runtime_flags(self) -> list[str]:
        """Build only live-catalog-verified model/depth/priority flags."""
        model = str(self.settings.get("model") or "").strip()
        effort = str(self.settings.get("effort") or "").strip().lower()
        fast = self.settings.get("fast") is True
        if not model:
            raise RuntimeError(
                "未选择经过实时目录验证的 Codex 型号，已停止普通截图问答调用"
            )
        capability = _verified_codex_capability(model)
        if effort not in (capability.get("depths") or []):
            raise RuntimeError(
                f"Codex 型号 {model} 不支持思考深度：{effort or '(空)'}"
            )
        flags = [
            "-m", model,
            "-c", f'model_reasoning_effort="{effort}"',
        ]
        if fast:
            if capability.get("priority") is not True:
                raise RuntimeError(f"Codex 型号 {model} 不支持 priority/Fast")
            flags += ["-c", 'service_tier="priority"']
        return flags

    def chat(self, messages: list[dict], image: bytes | None = None) -> str:
        cmd = self._command()
        image_path = _spool_image(image) if image else None
        out_file = Path(tempfile.gettempdir()) / "bwicarus-client" / f"codex-out-{os.getpid()}.txt"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            prompt = _flatten_messages(messages)  # codex 用 --image 单独传图，不写进 prompt
            base = ["cmd.exe", "/d", "/c", cmd] if cmd.lower().endswith((".cmd", ".bat")) else [cmd]
            full = base + ["exec", "--skip-git-repo-check", "--color", "never",
                           "-c", 'sandbox_mode="read-only"',
                           "--output-last-message", str(out_file),
                           *self._model_runtime_flags()]
            if image_path:
                full += ["--image", image_path]
            full += ["-"]   # 从 stdin 读 prompt
            r = _run_hidden(full, input=prompt, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=180)
            text = ""
            if out_file.exists():
                text = out_file.read_text(encoding="utf-8").strip()
            if not text:
                text = (r.stdout or "").strip()
            if not text:
                err = (r.stderr or "").strip()
                raise RuntimeError(err[:300] or f"codex_cli 返回空（exit {r.returncode}）")
            return text
        finally:
            try: out_file.unlink(missing_ok=True)
            except Exception: pass
            if image_path:
                try: Path(image_path).unlink(missing_ok=True)
                except Exception: pass


# ── HTTP API 类 ────────────────────────────────────────────────────
def _http_get_json(url: str, headers: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_post_json(url: str, headers: dict, body: dict, timeout: int = 30) -> dict:
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _attach_image_anthropic(messages: list[dict], image: bytes) -> list[dict]:
    """把 image 加到最后一条 user 消息（anthropic content array 格式）。"""
    out = []
    for i, m in enumerate(messages):
        if i == len(messages) - 1 and m.get("role") == "user" and image:
            out.append({
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image).decode("ascii"),
                    }},
                    {"type": "text", "text": m.get("content", "")},
                ],
            })
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _attach_image_openai(messages: list[dict], image: bytes) -> list[dict]:
    """OpenAI vision 格式：image_url + text。"""
    out = []
    for i, m in enumerate(messages):
        if i == len(messages) - 1 and m.get("role") == "user" and image:
            out.append({
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}",
                    }},
                    {"type": "text", "text": m.get("content", "")},
                ],
            })
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


class ClaudeApi(BackendAdapter):
    name = "claude_api"

    def ping(self) -> tuple[bool, str]:
        key = (self.settings.get("api_key") or "").strip()
        model = (self.settings.get("model") or "claude-opus-4-7").strip()
        if not key:
            return False, "未配置 api_key"
        # 用最小 messages 调用一次，cap max_tokens=1
        try:
            res = _http_post_json(
                "https://api.anthropic.com/v1/messages",
                {
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
                {
                    "model": model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ok"}],
                },
                timeout=20,
            )
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:200]
            return False, f"HTTP {e.code}: {err}"
        except Exception as e:
            return False, f"请求失败：{e}"
        if res.get("type") == "message":
            return True, f"claude_api OK · model={model}"
        return False, f"意外响应：{str(res)[:160]}"

    def chat(self, messages: list[dict], image: bytes | None = None) -> str:
        key = (self.settings.get("api_key") or "").strip()
        model = (self.settings.get("model") or "claude-opus-4-7").strip()
        if not key:
            raise RuntimeError("未配置 api_key")

        # 把 system 单独抽出来；其余按 anthropic 格式
        system_text = ""
        rest: list[dict] = []
        for m in messages:
            if m.get("role") == "system":
                system_text = (system_text + "\n\n" + (m.get("content") or "")).strip()
            else:
                rest.append(m)
        api_messages = _attach_image_anthropic(rest, image) if image else \
                       [{"role": m["role"], "content": m.get("content", "")} for m in rest]

        body = {
            "model": model,
            "max_tokens": int(self.settings.get("max_tokens") or 4096),
            "messages": api_messages,
        }
        if system_text:
            body["system"] = system_text

        try:
            res = _http_post_json(
                "https://api.anthropic.com/v1/messages",
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
                body, timeout=180,
            )
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code}: {err}") from e

        parts = res.get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        if not text:
            raise RuntimeError(f"意外响应：{str(res)[:200]}")
        return text

    def chat_stream(self, messages: list[dict], image: bytes | None = None):
        key = (self.settings.get("api_key") or "").strip()
        model = (self.settings.get("model") or "claude-opus-4-7").strip()
        if not key:
            raise RuntimeError("未配置 api_key")

        system_text = ""
        rest: list[dict] = []
        for m in messages:
            if m.get("role") == "system":
                system_text = (system_text + "\n\n" + (m.get("content") or "")).strip()
            else:
                rest.append(m)
        api_messages = _attach_image_anthropic(rest, image) if image else \
                       [{"role": m["role"], "content": m.get("content", "")} for m in rest]
        body = {
            "model": model,
            "max_tokens": int(self.settings.get("max_tokens") or 4096),
            "messages": api_messages,
            "stream": True,
        }
        if system_text:
            body["system"] = system_text

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code}: {err}") from e
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    return
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        txt = delta.get("text") or ""
                        if txt:
                            yield txt
        except GeneratorExit:
            try: resp.close()
            except Exception: pass
            raise
        finally:
            try: resp.close()
            except Exception: pass


class OpenAiApi(BackendAdapter):
    name = "openai_api"

    def ping(self) -> tuple[bool, str]:
        key = (self.settings.get("api_key") or "").strip()
        base = (self.settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        model = (self.settings.get("model") or "gpt-5").strip()
        if not key:
            return False, "未配置 api_key"
        try:
            # 用 list models 做最便宜的连通测试
            res = _http_get_json(
                f"{base}/models",
                {"Authorization": f"Bearer {key}"},
                timeout=15,
            )
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:200]
            return False, f"HTTP {e.code}: {err}"
        except Exception as e:
            return False, f"请求失败：{e}"
        models = res.get("data") or []
        if not isinstance(models, list):
            return False, f"意外响应：{str(res)[:160]}"
        names = {m.get("id") for m in models if isinstance(m, dict)}
        if model in names:
            return True, f"openai_api OK · model {model} 可用"
        return True, f"openai_api OK · 但配置的 model '{model}' 不在 {len(names)} 个可用 model 列表里"

    def chat(self, messages: list[dict], image: bytes | None = None) -> str:
        key = (self.settings.get("api_key") or "").strip()
        base = (self.settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        model = (self.settings.get("model") or "gpt-5").strip()
        if not key:
            raise RuntimeError("未配置 api_key")

        api_messages = _attach_image_openai(messages, image) if image else \
                       [{"role": m["role"], "content": m.get("content", "")} for m in messages]

        body = {"model": model, "messages": api_messages}
        try:
            res = _http_post_json(
                f"{base}/chat/completions",
                {"Authorization": f"Bearer {key}"},
                body, timeout=180,
            )
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code}: {err}") from e

        try:
            return res["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"意外响应：{str(res)[:200]}")

    def chat_stream(self, messages: list[dict], image: bytes | None = None):
        key = (self.settings.get("api_key") or "").strip()
        base = (self.settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        model = (self.settings.get("model") or "gpt-5").strip()
        if not key:
            raise RuntimeError("未配置 api_key")

        api_messages = _attach_image_openai(messages, image) if image else \
                       [{"role": m["role"], "content": m.get("content", "")} for m in messages]
        body = {"model": model, "messages": api_messages, "stream": True}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code}: {err}") from e
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    return
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = ev.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content
        except GeneratorExit:
            try: resp.close()
            except Exception: pass
            raise
        finally:
            try: resp.close()
            except Exception: pass


class Ollama(BackendAdapter):
    name = "ollama"

    def ping(self) -> tuple[bool, str]:
        base = (self.settings.get("base_url") or "http://localhost:11434").rstrip("/")
        model = (self.settings.get("model") or "llama3.1").strip()
        try:
            res = _http_get_json(f"{base}/api/tags", {}, timeout=10)
        except Exception as e:
            return False, f"连不上 Ollama（{base}）：{e}"
        models = res.get("models") or []
        names = [m.get("name") for m in models if isinstance(m, dict)]
        if any(n == model or (n or "").startswith(model + ":") for n in names):
            return True, f"ollama OK · model '{model}' 已就绪（共 {len(names)} 个 model）"
        if names:
            return True, f"ollama OK · 但 '{model}' 未拉取，本地有：{', '.join(names[:5])}"
        return True, f"ollama OK · 但本地没有任何 model；先 `ollama pull {model}`"

    def chat(self, messages: list[dict], image: bytes | None = None) -> str:
        base = (self.settings.get("base_url") or "http://localhost:11434").rstrip("/")
        model = (self.settings.get("model") or "llama3.1").strip()

        api_messages = []
        for i, m in enumerate(messages):
            msg = {"role": m["role"], "content": m.get("content", "")}
            # ollama 把 images 放在最后一条 user 上（base64 list）
            if image and i == len(messages) - 1 and m.get("role") == "user":
                msg["images"] = [base64.b64encode(image).decode("ascii")]
            api_messages.append(msg)

        body = {"model": model, "messages": api_messages, "stream": False}
        try:
            res = _http_post_json(f"{base}/api/chat", {}, body, timeout=180)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code}: {err}") from e
        msg = res.get("message") or {}
        content = msg.get("content")
        if not content:
            raise RuntimeError(f"意外响应：{str(res)[:200]}")
        return content

    def chat_stream(self, messages: list[dict], image: bytes | None = None):
        base = (self.settings.get("base_url") or "http://localhost:11434").rstrip("/")
        model = (self.settings.get("model") or "llama3.1").strip()

        api_messages = []
        for i, m in enumerate(messages):
            msg = {"role": m["role"], "content": m.get("content", "")}
            if image and i == len(messages) - 1 and m.get("role") == "user":
                msg["images"] = [base64.b64encode(image).decode("ascii")]
            api_messages.append(msg)

        body = {"model": model, "messages": api_messages, "stream": True}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/chat", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code}: {err}") from e
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = ev.get("message") or {}
                content = msg.get("content")
                if content:
                    yield content
                if ev.get("done"):
                    return
        except GeneratorExit:
            try: resp.close()
            except Exception: pass
            raise
        finally:
            try: resp.close()
            except Exception: pass


# ── 注册表 ─────────────────────────────────────────────────────────
_BACKENDS: dict[str, type[BackendAdapter]] = {
    "claude_cli":  ClaudeCli,
    "codex_cli":   CodexCli,
    "claude_api":  ClaudeApi,
    "openai_api":  OpenAiApi,
    "ollama":      Ollama,
}


def list_backends() -> list[str]:
    return list(_BACKENDS.keys())


def make_backend(name: str, settings: dict | None = None) -> BackendAdapter:
    cls = _BACKENDS.get(name)
    if not cls:
        raise ValueError(f"unknown backend: {name}")
    return cls(settings or {})


def backend_default_settings(name: str) -> dict:
    """每个 backend 的默认 settings，给 GUI 渲染用。"""
    return {
        "claude_cli":  {"command": "claude", "gemini_model": "gemini-3.5-flash"},
        "codex_cli":   {"command": "codex"},
        "claude_api":  {"api_key": "", "model": "claude-opus-4-7"},
        "openai_api":  {"api_key": "", "model": "gpt-5", "base_url": "https://api.openai.com/v1"},
        "ollama":      {"base_url": "http://localhost:11434", "model": "llama3.1"},
    }.get(name, {})


def backend_setting_fields(name: str) -> list[tuple[str, str, bool]]:
    """GUI 展示给每个 backend 的字段：[(key, label, secret), ...]"""
    return {
        "claude_cli":  [("command", "claude 命令", False), ("gemini_model", "Gemini 兜底型号", False)],
        "codex_cli":   [("command", "codex 命令", False)],
        "claude_api":  [("api_key", "API key", True), ("model", "model", False)],
        "openai_api":  [("api_key", "API key", True), ("model", "model", False), ("base_url", "base URL", False)],
        "ollama":      [("base_url", "base URL", False), ("model", "model", False)],
    }.get(name, [])
