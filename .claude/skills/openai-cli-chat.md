# Skill: openai-cli-chat

Codex CLI (`codex exec`) 可附图片的多轮对话调用模板。

## 函数模板

```python
import os, subprocess, tempfile
from pathlib import Path

CODEX    = os.environ.get("APP_CODEX", r"C:\Users\bwica\AppData\Roaming\npm\codex.cmd")
PROJECT  = r"C:\gpt"          # 调用时的工作目录
TEMP_DIR = Path(r"C:\claude\temp")


def run_hidden(cmd, **kwargs):
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    flags = kwargs.pop("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, startupinfo=si, creationflags=flags, **kwargs)


def codex_base_cmd():
    if CODEX.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/c", CODEX]
    return [CODEX]


def codex_call(prompt: str, image_path=None, model: str = "") -> str:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", dir=TEMP_DIR, delete=False) as f:
        out_path = f.name
    cmd = codex_base_cmd() + [
        "exec", "--sandbox", "read-only", "--skip-git-repo-check",
        "--color", "never", "--output-last-message", out_path, "-C", PROJECT,
    ]
    if image_path and Path(image_path).exists():
        cmd += ["--image", str(image_path)]
    if model:
        cmd += ["-m", model]
    cmd.append("-")
    try:
        r = run_hidden(cmd, input=prompt, cwd=PROJECT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
        out_file = Path(out_path)
        text = out_file.read_text(encoding="utf-8").strip() if out_file.exists() else ""
        if text:
            return text
        if r.stdout.strip():
            return r.stdout.strip()
        return (r.stderr or "").strip()
    finally:
        try: Path(out_path).unlink(missing_ok=True)
        except Exception: pass
```

## 多轮对话

`codex exec` 是一次性调用，无法保持会话状态。应用层自己维护历史，每次调用时整体拼入 prompt：

```python
def build_chat_prompt(message: str, messages: list) -> str:
    lines = ["你是一个问答助手。只回答问题，不要修改文件，不要运行命令。"]
    if messages:
        lines.append("\n对话历史：")
        for m in messages:
            label = "用户" if m.get("role") == "user" else "助手"
            lines.append(f"{label}：{m.get('text', '')}")
    lines.append(f"\n用户现在提问：{message}")
    return "\n".join(lines)


# 调用方式
state = {"messages": []}

prompt = build_chat_prompt(user_text, state["messages"])
answer = codex_call(prompt, image_path=current_image, model=current_model)
state["messages"].append({"role": "user",      "text": user_text})
state["messages"].append({"role": "assistant", "text": answer})
```

`--image` 只附加给本次调用；若后续追问仍依赖同一张图片，继续传相同路径。
返回值为空时检查 stderr（常见原因：未登录、模型不可用）。
