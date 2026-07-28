#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「当前阅读/助手上下文」快照持续同步到 Windows PC，供本地 Codex 每轮开局读取。

为什么监听本地 sidecar 而不是 reader_events(SSE)：
  SSE 总线只发结构/内容类事件（ink/text/userpage/client-action/run/assistant-history/fav），
  **恰恰不发 reading-pos / read-dwell / 高亮 / 便签**——而这些才是上下文最需要跟随的。
  且外部订阅 HTTP SSE 会占用户 per-uid 名额并吃掉一个 gthread。
  所有阅读状态最终都原子落盘到 state/ 下的 sidecar，inotify 监听它们 = 拿到 100% 信号、
  零 webapp 耦合、不占 webapp 资源。

节流：单一完整快照 + **1 秒 trailing debounce**（用户拍板）。
  连续翻页/连续变化期间只合并不推；最后一次变化后约 1 秒生成并同步完整 context.md + 页图。
  同一时刻只允许一次同步在途（单次在途），在途期间的新变化置 dirty，结束后再跑一轮。

Windows 侧原子替换：scp 到唯一临时名 → MoveFileEx(REPLACE_EXISTING|WRITE_THROUGH) + 重试。
  ⚠ 禁用 Move-Item -Force / cmd move /Y：实测它们在目标被读者持有时走"先删后改名"的非原子
  路径（存在文件消失窗口）；MoveFileEx 则干净失败，目标保持旧版完整可读，下轮再推。

安全：目标目录**硬编码白名单**（SSH 会话在 PC 上是 Administrator，绝不接受外部拼接路径）。
启停：systemd `reader-context-push.service`（见 references/systemd/）。
依赖：只用系统自带 inotifywait（inotify-tools），不引入 Python 新包。
回滚：停 service + 删 Windows 目录即可；本脚本只读 state/，不改动阅读器任何数据。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reader_context_snapshot as SNAP           # noqa: E402

PC = "bwicarus@100.99.9.124"
# —— 硬编码白名单：只允许推到这一个目录 ——
WIN_DIR_NATIVE = r"C:\Users\bwica\bw-reader-context"
WIN_DIR_POSIX = "C:/Users/bwica/bw-reader-context"
WIN_MOVE_PS1 = "C:/Users/bwica/bw-reader-context/_atomic_move.ps1"

# 时序合同(用户拍板 2026-07-27):**默认即时推**。唯一合并的是「连续翻页/快速滚动」
# 产生的临时位置——持续导航期间不推中间页,停手约 1s 只推最终页完整快照。
# 选区建立/清空、换书、笔记/高亮、侧栏消息都属即时,不许被导航防抖拖住。
NAV_DEBOUNCE_S = 1.0      # 导航专用合并窗
NOW_DEBOUNCE_S = 0.15     # 其余变更:极短窗只为把同一瞬间的多次写盘并成一次(不是节流)
DEBOUNCE_S = NAV_DEBOUNCE_S   # 兼容旧引用
UNREACHABLE_BACKOFF_S = 60.0   # PC 不可达时的退避（避免每轮硬撞 ConnectTimeout）
# ControlMaster 复用连接：实测把一轮同步从 ~930ms 压到 ~540ms（省 42%）。
# socket 放 /run/user 下（tmpfs，重启自清）；ControlPersist 让连接在空闲期保活。
_CM = f"/run/user/{os.getuid()}/bw-ctx-ssh-%r@%h:%p" if os.path.isdir(f"/run/user/{os.getuid()}") \
      else str(Path(tempfile.gettempdir()) / "bw-ctx-ssh-%r@%h:%p")
_MUX = ["-o", "ControlMaster=auto", "-o", f"ControlPath={_CM}", "-o", "ControlPersist=300",
        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"] + _MUX
SCP_BASE = ["scp", "-q", "-o", "BatchMode=yes"] + _MUX

ST = SNAP.ST
ACCT = SNAP.SIDECAR_ACCT     # 账户分区实时目录(webapp 真正在写的那份;legacy state/ 是冻结副本)
# ⚠ 必须同时盯账户分区:webapp 现在写 reader-sidecars/by-user/<uid>/,state/ 下那份是
# 认领时冻结的 legacy 副本,只盯 legacy 会一辈子等不到变化(快照也就永远是旧世界)。
_ACCT_WATCH = [
    "reader-active.json", "reader-context-sync.json", "reader-positions.json",
    "pdf-highlights", "epub-highlights", "html-highlights", "reader-notes",
] if ACCT else []

WATCH_PATHS = [ACCT / n for n in _ACCT_WATCH] + [
    ST / "reader-active.json",          # 当前活动文档(权威源:书/页变化即刻触发)
    ST / "reader-context-sync.json",    # 总开关:开/关本身也要立刻反映到快照抬头
    ST / "reader-positions.json",
    ST / "assistant-convo",
    ST / "epub-convo",
    ST / "cli-tasks",
    ST / "pdf-highlights",
    ST / "epub-highlights",
    ST / "html-highlights",
    ST / "reader-notes",
    ST / "pdf-ink",
    ST / "epub-ink",
    ST / "reader-userpages",
    ST / "pdf-page-brief",
    ST / "reader-toolshots",
]

_ATOMIC_PS1 = r'''param([string]$Part, [string]$Target)
$ErrorActionPreference='Stop'
Add-Type -Namespace BW -Name IO -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
public static extern bool MoveFileEx(string a, string b, int f);
'@
for ($i=0; $i -lt 8; $i++) {
  if ([BW.IO]::MoveFileEx($Part, $Target, 9)) { Write-Output "ATOMIC_OK attempt=$i"; exit 0 }
  Start-Sleep -Milliseconds 150
}
$e = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
Remove-Item $Part -ErrorAction SilentlyContinue
Write-Output "ATOMIC_BUSY win32=$e"
exit 3
'''


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def pc_reachable() -> bool:
    try:
        r = _run(SSH_BASE + [PC, "cmd /c echo OK"], timeout=15)
        return "OK" in (r.stdout or "")
    except Exception:
        return False


def ensure_remote() -> None:
    """建目标目录 + 安装原子替换脚本（幂等）。"""
    _run(SSH_BASE + [PC, f'cmd /c "mkdir {WIN_DIR_NATIVE} 2>nul & '
                         f'mkdir {WIN_DIR_NATIVE}\\assets 2>nul & exit 0"'], timeout=20)
    with tempfile.TemporaryDirectory() as td:
        ps = Path(td) / "_atomic_move.ps1"
        ps.write_bytes(b"\xef\xbb\xbf" + _ATOMIC_PS1.encode("utf-8"))
        _run(SCP_BASE + [str(ps), f"{PC}:{WIN_MOVE_PS1}"], timeout=30)


def push(src: Path) -> bool:
    """同步一份快照。返回 True=已落地；False=不可达或目标被占用（下轮重试）。"""
    assets = sorted((src / "assets").glob("*")) if (src / "assets").exists() else []
    if assets:
        r = _run(SCP_BASE + [str(a) for a in assets]
                 + [f"{PC}:{WIN_DIR_POSIX}/assets/"], timeout=120)
        if r.returncode != 0:
            log(f"assets scp 失败: {(r.stderr or '').strip()[:120]}")
            return False
    stamp = str(int(time.time() * 1000))
    part = f"{WIN_DIR_POSIX}/context.md.{stamp}.part"
    r = _run(SCP_BASE + [str(src / "context.md"), f"{PC}:{part}"], timeout=60)
    if r.returncode != 0:
        log(f"context scp 失败: {(r.stderr or '').strip()[:120]}")
        return False
    r = _run(SSH_BASE + [PC, f'powershell -NoProfile -ExecutionPolicy Bypass -File '
                             f'"{WIN_MOVE_PS1.replace("/", chr(92))}" '
                             f'-Part "{part}" -Target "{WIN_DIR_POSIX}/context.md"'], timeout=60)
    out = (r.stdout or "").strip()
    if "ATOMIC_OK" in out:
        return True
    log(f"原子替换未完成（{out or (r.stderr or '').strip()[:80]}）；目标保持旧版，下轮重试")
    return False


class Pusher:
    """1s trailing debounce + 单次在途 + 不可达退避。"""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None
        self.running = False
        self.dirty = False
        self.blocked_until = 0.0
        self._pending_delay = NAV_DEBOUNCE_S
        self._last_active: dict = {}

    def touch(self, why: str = "") -> None:
        delay = NAV_DEBOUNCE_S if self._is_nav(why) else NOW_DEBOUNCE_S
        with self.lock:
            # 已排队的导航窗遇到即时事件 → 缩短到即时(选区不能被上一次翻页的 1s 拖住);
            # 反之即时窗不会被导航拉长。
            if self.timer:
                if delay >= self._pending_delay:
                    return                      # 同类或更慢:沿用已排的窗,自然合并
                self.timer.cancel()
            self._pending_delay = delay
            self.timer = threading.Timer(delay, self._fire)
            self.timer.daemon = True
            self.timer.start()

    def _is_nav(self, path: str) -> bool:
        """这次变更算不算「导航」:只有 reader-active.json 里**仅页码**变了才算。

        换书 / 选区建立或清空 / 其它字段变化都不算——它们要即时推。判定靠跟上一次
        看到的记录逐字段比对,而不是"凡是 active 文件变了就当翻页"(那会把选区拖慢 1s)。
        """
        if "reader-active" not in (path or ""):
            return False
        try:
            cur = SNAP.jload(SNAP.sc("reader-active.json"), {}) or {}
        except Exception:
            return False
        prev, self._last_active = self._last_active, dict(cur)
        if not prev or prev.get("file") != cur.get("file"):
            return False                        # 换书=即时
        keys = set(prev) | set(cur)
        for k in keys:
            if k in ("pos", "ts", "member_pos", "reason"):
                continue
            if prev.get(k) != cur.get(k):
                return False                    # 选区等任何其它字段变了=即时
        return prev.get("pos") != cur.get("pos")

    def _fire(self) -> None:
        with self.lock:
            if self.running:
                self.dirty = True          # 在途期间的变化：结束后补一轮
                return
            self.running = True
        try:
            self._once()
        finally:
            with self.lock:
                self.running = False
                again = self.dirty
                self.dirty = False
            if again:
                self.touch("in-flight-followup")   # 非 nav → 即时窗补发

    def _once(self) -> None:
        now = time.time()
        if now < self.blocked_until:
            return
        # 总开关关闭 → 这一方向也停:不生成快照、不连 SSH。跟前端读同一个文件,
        # 不会出现「前端以为关了、后台还在往 Windows 推」的错位。
        if not SNAP._ctx_sync_enabled():
            if not getattr(self, "_off_logged", False):
                log("双向上下文同步已关闭：暂停生成与推送（开关一开即恢复）")
                self._off_logged = True
            return
        if getattr(self, "_off_logged", False):
            log("双向上下文同步已开启：恢复生成与推送")
            self._off_logged = False
        t0 = time.time()
        SNAP.build(self.workdir)
        t_snap = time.time() - t0
        if not pc_reachable():
            self.blocked_until = time.time() + UNREACHABLE_BACKOFF_S
            log(f"PC 不可达，退避 {UNREACHABLE_BACKOFF_S:.0f}s")
            return
        ok = push(self.workdir)
        log(f"{'✓ 已同步' if ok else '· 未落地'}（快照 {t_snap:.2f}s，总 {time.time()-t0:.2f}s）")


def main() -> int:
    """用系统自带 inotifywait 监听（零新依赖；Pi 上无 watchdog 包，也不为此引入）。"""
    # ⚠ 输出目录必须在 state/ 之外：state/ 子目录被递归监听，输出落在里面会自激循环。
    workdir = Path(SNAP.ROOT) / ".reader-context-out"
    workdir.mkdir(parents=True, exist_ok=True)
    pusher = Pusher(workdir)

    targets, seen = [], set()
    for path in WATCH_PATHS:
        t = path if path.is_dir() else path.parent
        if t.exists() and str(t) not in seen:
            seen.add(str(t))
            targets.append(str(t))
    if not targets:
        log("没有可监听的路径，退出")
        return 1

    log(f"watcher 启动：{len(targets)} 个目录，debounce {DEBOUNCE_S}s → {WIN_DIR_NATIVE}")
    ensure_remote()
    pusher.touch("startup")                # 启动即推一份

    cmd = ["inotifywait", "-m", "-q", "-r",
           "-e", "close_write", "-e", "moved_to", "-e", "create", "-e", "delete",
           "--format", "%w%f"] + targets
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            p = line.strip()
            if not p or p.endswith((".tmp", ".part", "~")) or "/reader-context-out/" in p:
                continue          # 忽略中间态与自己的输出目录（防自激）
            pusher.touch(p)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
