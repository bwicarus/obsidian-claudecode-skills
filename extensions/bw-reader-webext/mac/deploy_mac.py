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
# webapp 本机端口（2026-09-26 从 5000 换来：5000 被 macOS「隔空播放接收器」占着，iPad 请求随机 403）
WEBAPP_PORT = 5055

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
        # Anki：官方桌面版隐藏启动（-g 不抢焦点、-j 隐藏窗口），AnkiConnect 在 127.0.0.1:8765。
        # open 会立刻返回，所以不常驻；Anki 退出或崩溃后由后台守护每分钟检查并重新隐藏拉起。
        "anki": {"args": ["/usr/bin/open", "-g", "-j", "-a", "/Applications/Anki.app"],
                 "cwd": str(HOME), "keepalive": False},
        # KJ 知识节点 ↔ Anki：每 15 分钟吸收卡片绑定台账 + 拉复习快照进掌握度（同 Windows 的「KJ Anki Sync」）
        "kj-anki-sync": {"args": [str(PY), f"{cur}/scripts/kj/cli.py", "anki-sync"],
                         "cwd": cur, "keepalive": False, "interval": 900},
        # 「PC 预处理」高质量 OCR worker（2026-09-27 从 Windows 迁来：CUDA → Apple MPS，
        # 同一份 scripts/reader_pc_preprocess_worker.py）。独立 venv（torch/DocLayout/UniMERNet/manga-ocr），
        # 模型在 ~/BW/data/BWReader/models（从 Windows 拷来，不自动下载）。经 tailscale serve 回连本机 webapp 认领任务。
        "pc-ocr": {"args": [str(BW / "venv" / "pc-ocr" / "bin" / "python"),
                            f"{cur}/scripts/reader_pc_preprocess_worker.py", "--project-root", cur,
                            "--recycle-after-job"],
                   "cwd": cur, "background": True, "env": {
                       "BW_READER_PC_OCR_BASE_URL": "https://bwicarus-2.taile44d0c.ts.net",
                       "BW_READER_PC_FORMULA_BACKEND": "unimernet-base",
                       "BW_READER_PC_UNIMERNET_ADAPTER": "reader_unimernet_adapter:create_model",
                       "BW_READER_PC_UNIMERNET_CONFIG": f"{cur}/scripts/reader_unimernet_base.yaml",
                       "BW_READER_PC_UNIMERNET_MODEL_DIR": str(DATA / "BWReader" / "models" / "unimernet_base"),
                       "BW_READER_PC_DOCLAYOUT_MODEL": str(DATA / "BWReader" / "models" / "doclayout_yolo"
                                                           / "doclayout_yolo_docstructbench_imgsz1024.pt"),
                       "HF_HOME": str(DATA / "BWReader" / "models" / "hf-cache"),
                       "XDG_CACHE_HOME": str(DATA / "BWReader" / "models" / "cache"),
                       "PYTORCH_ENABLE_MPS_FALLBACK": "0",
                       "PYTHONUTF8": "1",
                   }},
        # 每日数据备份 → 外接盘 BWDev（在用数据留内置盘，只把快照放外接盘；没挂载就跳过）
        "backup": {"args": [str(PY), f"{cur}/extensions/bw-reader-webext/mac/backup_mac.py"],
                   "cwd": cur, "keepalive": False, "run_at_load": False, "background": True,
                   "calendar": {"Hour": 3, "Minute": 30}},
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
    # webapp 端口（2026-09-26）：5000 被 macOS「隔空播放接收器」占着，iPad 请求会随机收到它的 403。
    # 所有服务都从这个变量推 webapp 地址；改端口后 tailscale serve 的 Flask 路由也要跟着改（见 mac-server.md）。
    env.setdefault("BW_WEBAPP_PORT", str(WEBAPP_PORT))
    # MCP 门面的「ReaderPC 实时工具」代理（reader_pc_tools / reader_pc_call_tool / voice_brief）要起
    # 桥的 --reader-context-mcp 模式；以前只认 Windows 的 EXE 路径，迁到 Mac 后一直 READER_PC_UNAVAILABLE。
    env.setdefault("READER_CONTEXT_MCP_COMMAND", str(CURRENT / "bridge" / "bw-reader-bridge"))
    env.setdefault("READER_CONTEXT_MCP_STATE", str(BRIDGE_ROOT / "runtime" / "reader-context-snapshot.json"))
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
    # ~/BWReader：Python 侧脚本的退路是 `$LOCALAPPDATA/BWReader`，没设时退回 `~/BWReader`。
    # launchd 的服务都带 LOCALAPPDATA，但 Codex 桌面版 / 终端里的 Codex 不带 —— 照能力指南
    # 跑 judgment_basis.py 就读成空目录（2026-09-25 实测「ReaderPC 状态：读不到」）。
    for alias in (HOME / "Library" / "Application Support" / "BWReader", HOME / ".local" / "share" / "BWReader",
                  HOME / "BWReader"):
        if not alias.is_symlink():
            if alias.exists():
                sys.exit(f"{alias} 是实体目录：先把里面的文件合并进 {DATA / 'BWReader'} 再部署")
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(DATA / "BWReader")
    log(f"版本目录 {release}")
    return release


def releases_in_use(releases: list[Path]) -> set[Path]:
    """仍有 launchd 服务在跑的版本目录。

    ⚠ 2026-09-26 实录：`--only` 只重启点名的服务，却照样按"最近 5 个"删旧目录 ——
    没被重启的守护进程还在用 17:15 那个版本，目录被删后网页界面报 missing index.html
    （代码早已读进内存，所以进程没死，只是一读文件就坏）。判据：服务启动那一刻
    `current` 指向的，就是名字时间戳 ≤ 进程启动时间的最新一个版本。
    """
    stamps = []
    for release in releases:
        try:
            stamps.append((time.mktime(time.strptime(release.name[:15], "%Y%m%d-%H%M%S")), release))
        except ValueError:
            continue
    used: set[Path] = set()
    listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[2].startswith(LABEL_PREFIX) or not parts[0].isdigit():
            continue
        started = subprocess.run(["ps", "-o", "lstart=", "-p", parts[0]], capture_output=True, text=True).stdout.strip()
        try:
            started_at = time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        candidates = [release for stamp, release in stamps if stamp <= started_at]
        if candidates:
            used.add(candidates[-1])
    return used


def install_stable_scripts(release: Path) -> None:
    """把 AI / 语音运行时直接跑的脚本铺到 ~/BW/data/BWReader（Windows 上是 %LOCALAPPDATA%\\BWReader）。

    布局只在 windows/package_readerpc_server.py 的 stable_install_layout() 定义一处，这里读同一份。
    ⚠ 2026-09-25 之前 Mac 没有这一步：这些副本一直停在从 Windows 搬过来那一刻（里面还是
    C:\\… 路径），仓库里的修复到不了 AI 真正运行的地方。
    """
    import importlib.util
    packager = release / "extensions" / "bw-reader-webext" / "windows" / "package_readerpc_server.py"
    spec = importlib.util.spec_from_file_location("bw_packager_for_mac", packager)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = DATA / "BWReader"
    changed = []
    for dest_rel, runtime_rel in module.stable_install_layout():
        source = Path(module.RUNTIME_SOURCES[runtime_rel])
        target = root / dest_rel
        data = source.read_bytes()
        if target.exists() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(target.name + ".deploying")
        staging.write_bytes(data)
        os.chmod(staging, 0o755 if dest_rel.endswith(".py") else 0o644)
        os.replace(staging, target)
        changed.append(dest_rel)
    log(f"运行时脚本 → {root}：更新 {len(changed)} 个" + (f"（{', '.join(changed)}）" if changed else ""))


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
            "EnvironmentVariables": {**env, **spec.get("env", {})},
            "RunAtLoad": spec.get("run_at_load", True),
            "KeepAlive": spec.get("keepalive", True),
            "ThrottleInterval": 10,
            "ProcessType": "Background" if spec.get("background") else "Interactive",
            "StandardOutPath": str(LOGS / f"{name}.log"),
            "StandardErrorPath": str(LOGS / f"{name}.log"),
        }
        if spec.get("interval"):
            plist["StartInterval"] = spec["interval"]
        if spec.get("calendar"):
            plist["StartCalendarInterval"] = spec["calendar"]
        if spec.get("background"):
            plist["LowPriorityIO"] = True
            plist["Nice"] = 10
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
        install_stable_scripts(release)
        # 清理旧版本（保留最近 keep 个；当前那个、以及**仍有服务在跑**的版本永远保留）
        releases = sorted(p for p in RELEASES.iterdir() if p.is_dir())
        in_use = releases_in_use(releases)
        for stale in releases[:-args.keep]:
            if stale != CURRENT.resolve() and stale not in in_use:
                shutil.rmtree(stale, ignore_errors=True)
        if in_use - set(releases[-args.keep:]):
            log("保留仍在使用的旧版本：" + ", ".join(sorted(p.name for p in in_use - set(releases[-args.keep:]))))

    write_plists(env, names)
    if not args.no_restart:
        restart(names)
    check_webapp_port(int(env.get("BW_WEBAPP_PORT") or WEBAPP_PORT))
    return 0


def check_webapp_port(port: int) -> None:
    """webapp 端口若还有别的进程在听（2026-09-26：5000 被 macOS「隔空播放接收器」占着，iPad 请求随机 403，
    Mac 本机 curl 却一切正常），部署完大声提醒；另核对 tailscale serve 的 Flask 路由是否已指向这个端口。"""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    except OSError as exc:
        log(f"⚠ 没能检查 {port} 端口占用（{exc}）")
        return
    others = sorted({line.split()[0] for line in out.splitlines()[1:] if line and not line.startswith("python")})
    if others:
        log(f"⚠⚠ webapp 端口 {port} 还被这些进程监听：" + ", ".join(others))
    ts = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    if ts.exists():
        status = subprocess.run([str(ts), "serve", "status"], capture_output=True, text=True).stdout
        stale = [line.split()[0].lstrip("|-").strip() for line in status.splitlines() if "127.0.0.1:5000" in line]
        if stale and port != 5000:
            log(f"⚠⚠ tailscale serve 还有 {len(stale)} 条路由指向旧端口 5000（{', '.join(stale[:6])}…）："
                f"跑 extensions/bw-reader-webext/mac/retarget_serve.sh {port} 改过去")


if __name__ == "__main__":
    raise SystemExit(main())
