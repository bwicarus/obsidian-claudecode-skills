"""subprocess 跑外部脚本，逐行 yield stdout，方便 GUI 流式显示。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator


def resolve_python(explicit: str | None = None) -> str | None:
    """找一个真的 python 解释器，优先 pythonw.exe（无控制台窗口）。

    在 PyInstaller 打包后的 .exe 中 ``sys.executable`` 指 bwicarus-client.exe 自己——
    用它去跑 .py 等于再启动一个 client。所以必须优先用：
      1. 调用方显式传入（cfg.python_path）；如果传的是 python.exe，自动同目录找 pythonw.exe
      2. 不在 frozen 模式：用 sys.executable
      3. 系统 PATH 里的 pythonw / python / py
    都找不到 → 返回 None
    """
    def _prefer_pythonw(p: Path) -> str:
        """如果给的是 python.exe，看同目录有没有 pythonw.exe（无控制台版），优先它。"""
        if p.name.lower() == "python.exe":
            pyw = p.with_name("pythonw.exe")
            if pyw.exists():
                return str(pyw)
        return str(p)

    if explicit:
        explicit = explicit.strip()
        if explicit:
            p = Path(explicit)
            if p.exists():
                return _prefer_pythonw(p)
    if not getattr(sys, "frozen", False):
        # 源码模式：sys.executable 通常是 python.exe，也尝试切 pythonw.exe
        return _prefer_pythonw(Path(sys.executable))
    for name in ("pythonw", "python", "py"):
        found = shutil.which(name)
        if found:
            return found
    return None


def run_script(
    script_path: str,
    *args: str,
    cwd: str | None = None,
    extra_env: dict | None = None,
    python_exe: str | None = None,
    timeout: int | None = None,
) -> Iterator[str]:
    """跑 python script，yield 输出行。最后 yield 一行 [exit code N]。"""
    p = Path(script_path)
    if not p.exists():
        yield f"✗ 脚本不存在：{p}"
        yield "[exit -1]"
        return

    py = resolve_python(python_exe)
    if not py:
        yield "✗ 找不到 python 解释器（frozen 模式 sys.executable 是 .exe 自己）"
        yield "  在 GUI 「笔记登记」Tab 填 python_path，或把 python.exe 加到 PATH"
        yield "[exit -1]"
        return
    cmd = [py, str(p), *args]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    yield f"$ {' '.join(cmd)}"

    # Windows: 隐藏 python 子进程的控制台窗口（GUI 客户端没控制台，subprocess 默认会弹一个空黑窗）
    popen_kwargs = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        popen_kwargs["startupinfo"] = si
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd or str(p.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # 行缓冲
            **popen_kwargs,
        )
    except Exception as e:
        yield f"✗ 启动失败：{e}"
        yield "[exit -1]"
        return

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\r\n")
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        yield "[超时，进程已终止]"
        yield "[exit -1]"
        return
    except Exception as e:
        yield f"[读取失败：{e}]"

    yield f"[exit {proc.returncode}]"


def open_program(exe_path: str) -> tuple[bool, str]:
    """启动外部程序（如 Anki），不阻塞当前进程。"""
    p = Path(exe_path)
    if not p.exists():
        return False, f"找不到程序：{exe_path}"
    try:
        kwargs = {"shell": False, "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen([str(p)], **kwargs)
        return True, f"已启动：{p.name}"
    except Exception as e:
        return False, f"启动失败：{e}"
