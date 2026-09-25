#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mac 服务器部署：从仓库做一份版本目录、编译桥、切换 current、写 launchd 并重载服务。

用法（在 Mac 上，仓库任意位置都行）：
    python3 extensions/bw-reader-webext/mac/deploy_mac.py            # 部署并重启全部服务
    python3 extensions/bw-reader-webext/mac/deploy_mac.py --only webapp,mcp
    python3 extensions/bw-reader-webext/mac/deploy_mac.py --no-restart  # 只准备版本目录与配置
    python3 extensions/bw-reader-webext/mac/deploy_mac.py --rollback    # current 指回上一版并重启

目录约定（全部在内置盘，外接盘拔掉服务也照常跑）：
    ~/BW/runtime/releases/<时间>-<提交>/   代码副本（服务从 ~/BW/runtime/current 跑）
    ~/BW/data/                             全部数据：state / webapp-data / obsidian / BWReader / bridge …
    ~/BW/config/server.env                 服务环境变量（含密钥，权限 600，不进 git）
    ~/BW/venv/server/                      服务专用 Python
    ~/BW/logs/<服务>.log                   各服务日志
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BW = HOME / "BW"
RELEASES = BW / "runtime" / "releases"
CURRENT = BW / "runtime" / "current"
DATA = BW / "data"
CONFIG = BW / "config" / "server.env"
LOGS = BW / "logs"
PY = BW / "venv" / "server" / "bin" / "python"
DOTNET_ROOT = HOME / ".dotnet"
NODE_BIN = HOME / ".local" / "opt" / "node" / "bin"
BRIDGE_ROOT = DATA / "bridge"
LAUNCH_AGENTS = HOME / "Library" / "LaunchAgents"
LABEL_PREFIX = "space.bwicarus."

REPO = Path(__file__).resolve().parents[3]

# 版本目录里会被服务写入的目录 → 链到 ~/BW/data 下（换版本不丢数据）
DATA_LINKS = {
    "state": DATA / "state",
    "webapp-data": DATA / "webapp-data",
    "anki": DATA / "project" / "anki",
    "index": DATA / "project" / "index",
    "dashboard": DATA / "project" / "dashboard",
    "history": DATA / "project" / "history",
    "temp": DATA / "project" / "temp",
}

# 不进版本目录的东西（体积大、平台相关、或是数据）
# ⚠ 以 / 开头 = 只匹配仓库根下那一个；不带 / 的会匹配任意层级的同名文件/目录。
EXCLUDES = [
    "node_modules", "__pycache__", "*.pyc", ".DS_Store",
    "/.git", "/ios", "/state", "/webapp-data", "/spacy-venv", "/temp",
    "/extensions/bw-reader-webext/windows/ComputerVoiceAudio/bin",
    "/extensions/bw-reader-webext/windows/ComputerVoiceAudio/obj",
    "/extensions/bw-reader-webext/windows/candidates",
    "/extensions/bw-reader-webext/windows/readerpc-candidates",
    "/extensions/bw-reader-webext/mac/ReaderBridge/bin",
    "/extensions/bw-reader-webext/mac/ReaderBridge/obj",
]

DEPLOY = "_server_deploy"
DESKTOP = "extensions/bw-reader-webext/windows/computer-voice-desktop"


def services() -> dict[str, dict]:
    cur = str(CURRENT)
    return {
        "webapp": {"args": [str(PY), "app.py"], "cwd": f"{cur}/{DEPLOY}"},
        "voice-rt": {"args": [str(PY), "voice_realtime_relay.py"], "cwd": f"{cur}/{DEPLOY}"},
        "rbi": {"args": [str(PY), "rbi_server.py"], "cwd": f"{cur}/{DEPLOY}"},
        "mcp": {"args": [str(PY), "mcp_server.py", "--http", "8766"], "cwd": f"{cur}/{DEPLOY}"},
        "bridge": {
            "args": [f"{cur}/bridge/bw-reader-bridge", "--direct-serve", "--config",
                     str(BRIDGE_ROOT / "native-host" / "computer-voice-direct.config.json")],
            "cwd": str(BRIDGE_ROOT),
        },
        "voice-core": {"args": [str(PY), f"{cur}/{DESKTOP}/voice_cli_runner.py"],
                       "cwd": str(DATA / "BWReader" / "voice-cli")},
        "supervisor": {"args": [str(PY), f"{cur}/extensions/bw-reader-webext/mac/bw_mac_supervisor.py"],
                       "cwd": str(DATA / "BWReader")},
        # Obsidian 无头同步（2026-09-25 从 Windows 迁来）：登录令牌与库配置在 ~/.obsidian-headless
        "obsidian-sync": {
            "args": [str(NODE_BIN / "node"),
                     str(NODE_BIN.parent / "lib" / "node_modules" / "obsidian-headless" / "cli.js"),
                     "sync", "--continuous", "--path", str(DATA / "obsidian")],
            "cwd": str(DATA / "obsidian"),
        },
    }


def log(message: str) -> None:
    print(f"[deploy] {message}", flush=True)


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kw)


def read_env() -> dict[str, str]:
    if not CONFIG.is_file():
        sys.exit(f"缺少 {CONFIG}（服务环境变量）。先按迁移文档生成它。")
    env: dict[str, str] = {}
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    # 进程级的必需项（server.env 里写了就以它为准）
    env.setdefault("HOME", str(HOME))
    env.setdefault("LANG", "en_US.UTF-8")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("LOCALAPPDATA", str(DATA))
    env.setdefault("DOTNET_ROOT", str(DOTNET_ROOT))
    env.setdefault("BW_PYTHON", str(PY))
    env.setdefault("PATH", ":".join([str(PY.parent), str(NODE_BIN), str(DOTNET_ROOT),
                                     "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin",
                                     "/usr/sbin", "/sbin"]))
    return env


def make_release() -> Path:
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "nogit"
    release = RELEASES / f"{time.strftime('%Y%m%d-%H%M%S')}-{sha}"
    release.mkdir(parents=True)
    rsync = ["rsync", "-a"] + [f"--exclude={e}" for e in EXCLUDES] + [f"{REPO}/", f"{release}/"]
    run(rsync)
    # 数据目录：首次部署时用仓库里的内容初始化，之后一律链接
    for name, target in DATA_LINKS.items():
        inside = release / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if inside.is_dir():
                shutil.copytree(inside, target)
            else:
                target.mkdir(parents=True)
        if inside.is_symlink() or inside.is_file():
            inside.unlink()
        elif inside.is_dir():
            shutil.rmtree(inside)
        inside.symlink_to(target)
    (release / ".env.local").symlink_to(CONFIG)
    # .NET 8 在 macOS 上把 LocalApplicationData 映射到 ~/Library/Application Support（不是 ~/.local/share）。
    # 桥的书库 / 用户状态 / 卡片资源 / runner.pid 都在它下面的 BWReader 里 —— 指到真正的数据目录，
    # 否则桥读的是一个空目录（2026-09-25 实测：App 报「语音核心没在跑」、切换后写入落到错位置）。
    for alias in (HOME / "Library" / "Application Support" / "BWReader", HOME / ".local" / "share" / "BWReader"):
        if not alias.is_symlink():
            if alias.exists():
                sys.exit(f"{alias} 是实体目录：先把里面的文件合并进 {DATA / 'BWReader'} 再部署")
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(DATA / "BWReader")
    log(f"版本目录 {release}")
    return release


def build_bridge(release: Path) -> None:
    env = dict(os.environ, DOTNET_ROOT=str(DOTNET_ROOT), DOTNET_CLI_TELEMETRY_OPTOUT="1",
               DOTNET_NOLOGO="1")
    project = release / "extensions" / "bw-reader-webext" / "mac" / "ReaderBridge" / "ReaderBridge.csproj"
    run([str(DOTNET_ROOT / "dotnet"), "publish", str(project), "-c", "Release", "-r", "osx-arm64",
         "--self-contained", "false", "-o", str(release / "bridge"), "-v", "q", "-nologo"], env=env)
    # 编译中间产物不留在版本目录里
    for leftover in (project.parent / "bin", project.parent / "obj"):
        shutil.rmtree(leftover, ignore_errors=True)
    log("桥已编译")


def switch_current(release: Path) -> None:
    tmp = CURRENT.with_name("current.tmp")
    if tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(release)
    os.replace(tmp, CURRENT)
    log(f"current → {release.name}")


def write_plists(env: dict[str, str], names: list[str]) -> None:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    for name, spec in services().items():
        if name not in names:
            continue
        Path(spec["cwd"]).mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": LABEL_PREFIX + name,
            "ProgramArguments": spec["args"],
            "WorkingDirectory": spec["cwd"],
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ProcessType": "Interactive",
            "StandardOutPath": str(LOGS / f"{name}.log"),
            "StandardErrorPath": str(LOGS / f"{name}.log"),
        }
        path = LAUNCH_AGENTS / f"{LABEL_PREFIX}{name}.plist"
        with path.open("wb") as handle:
            plistlib.dump(plist, handle)
        os.chmod(path, 0o600)   # 环境变量里有密钥


def restart(names: list[str]) -> None:
    domain = f"gui/{os.getuid()}"
    for name in names:
        label = LABEL_PREFIX + name
        plist = LAUNCH_AGENTS / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        # bootout 是异步的；紧接着 bootstrap 偶尔会撞上"服务还在"
        for _ in range(20):
            if subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                              capture_output=True).returncode != 0:
                break
            time.sleep(0.25)
        # 刚 bootout 的服务偶尔还没从 launchd 里退干净，bootstrap 会报 5（Input/output error）：等一会儿重试
        for attempt in range(6):
            done = subprocess.run(["launchctl", "bootstrap", domain, str(plist)],
                                  capture_output=True, text=True)
            if done.returncode == 0:
                break
            time.sleep(1)
        state = "已启动" if done.returncode == 0 else f"启动失败：{(done.stderr or '').strip()}"
        log(f"{name}: {state}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="逗号分隔的服务名")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--keep", type=int, default=5, help="保留最近几个版本目录")
    args = parser.parse_args()

    names = [n for n in args.only.split(",") if n] or list(services())
    unknown = set(names) - set(services())
    if unknown:
        sys.exit(f"未知服务：{', '.join(sorted(unknown))}")
    env = read_env()

    if args.rollback:
        releases = sorted(p for p in RELEASES.iterdir() if p.is_dir())
        now = CURRENT.resolve()
        older = [p for p in releases if p.name < now.name]
        if not older:
            sys.exit("没有更早的版本可回退")
        switch_current(older[-1])
    else:
        release = make_release()
        build_bridge(release)
        switch_current(release)
        # 清理旧版本（保留最近 keep 个，当前那个永远保留）
        releases = sorted(p for p in RELEASES.iterdir() if p.is_dir())
        for stale in releases[:-args.keep]:
            if stale != CURRENT.resolve():
                shutil.rmtree(stale, ignore_errors=True)

    write_plists(env, names)
    if not args.no_restart:
        restart(names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
