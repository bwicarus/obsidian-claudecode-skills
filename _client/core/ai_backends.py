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
import subprocess
import tempfile
import urllib.error
import urllib.request
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


# ── CLI 类 ─────────────────────────────────────────────────────────
class CliBackend(BackendAdapter):
    """通用 CLI adapter：执行 `<command> --version` 验证可用性。"""

    name = "cli"
    default_command = "echo"

    def _command(self) -> str:
        return (self.settings.get("command") or self.default_command).strip()

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
            full = [cmd, "--allowedTools", "Read", *self._model_effort_flags(),
                    "--output-format", "text", "-p", prompt]
            r = _run_hidden(full, capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=int(self.settings.get("timeout", 180)))
            out = (r.stdout or "").strip()
            if not out:
                err = (r.stderr or "").strip()
                raise RuntimeError(err[:300] or f"claude_cli 返回空（exit {r.returncode}）")
            return out
        finally:
            if image_path:
                try: Path(image_path).unlink(missing_ok=True)
                except Exception: pass

    def chat_stream(self, messages: list[dict], image: bytes | None = None):
        cmd = self._command()
        image_path = _spool_image(image) if image else None
        prompt = _flatten_messages(messages, image_path=image_path)
        full = [cmd, "--allowedTools", "Read", *self._model_effort_flags(),
                "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "-p", prompt]
        popen_kw = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
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
            if rc != 0 and not emitted_any:
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

    def chat(self, messages: list[dict], image: bytes | None = None) -> str:
        cmd = self._command()
        image_path = _spool_image(image) if image else None
        out_file = Path(tempfile.gettempdir()) / "bwicarus-client" / f"codex-out-{os.getpid()}.txt"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            prompt = _flatten_messages(messages)  # codex 用 --image 单独传图，不写进 prompt
            base = ["cmd.exe", "/d", "/c", cmd] if cmd.lower().endswith((".cmd", ".bat")) else [cmd]
            full = base + ["exec", "--sandbox", "read-only",
                           "--skip-git-repo-check", "--color", "never",
                           "--output-last-message", str(out_file)]
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
        "claude_cli":  {"command": "claude"},
        "codex_cli":   {"command": "codex"},
        "claude_api":  {"api_key": "", "model": "claude-opus-4-7"},
        "openai_api":  {"api_key": "", "model": "gpt-5", "base_url": "https://api.openai.com/v1"},
        "ollama":      {"base_url": "http://localhost:11434", "model": "llama3.1"},
    }.get(name, {})


def backend_setting_fields(name: str) -> list[tuple[str, str, bool]]:
    """GUI 展示给每个 backend 的字段：[(key, label, secret), ...]"""
    return {
        "claude_cli":  [("command", "claude 命令", False)],
        "codex_cli":   [("command", "codex 命令", False)],
        "claude_api":  [("api_key", "API key", True), ("model", "model", False)],
        "openai_api":  [("api_key", "API key", True), ("model", "model", False), ("base_url", "base URL", False)],
        "ollama":      [("base_url", "base URL", False), ("model", "model", False)],
    }.get(name, [])
