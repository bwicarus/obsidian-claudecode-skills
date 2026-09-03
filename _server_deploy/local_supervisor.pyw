#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bwicarus 本地实例 —— 托盘守护进程(Windows)。

把本地 Flask 从「裸 dev server,关 PowerShell 窗口就宕」升级成大厂 ML 工具那套
「本地 server + 托盘 + --app 窗口」范式(Jupyter/Gradio/Ollama 同一路子),全程纯 Python:
  · 用 pythonw 后台跑,**关掉启动窗口也不宕**(detached)
  · Flask 当子进程 spawn + 健康检查 + **崩溃自动拉起**(按稳定存活时长退避)
  · **连续快失败熔断**:5 次秒退就停手并托盘气泡报警(端口占用/缺 SECRET_KEY/代码错时不刷爆日志)
  · **Job Object(KILL_ON_JOB_CLOSE)**:守护进程一旦消失(含被任务管理器强杀),Flask 子进程随之被带走
    → 不留残留占 5000 端口
  · 可选 **代码改动自动重启**(轮询 *.py mtime,治「改了端点要手动重启」)
  · 系统托盘菜单:打开 / 重启 / 打开日志 / ☑ 开机自启 / ☑ 代码变更自动重启 / 退出
  · 开机自启写 HKCU\\...\\Run(指向 pythonw + 本脚本,源码模式也有效)
  · 单实例锁(Windows 命名 mutex,Local\\ per-session);已在运行则弹提示后静默退出
  · 启动前预检依赖(pystray/PIL/requests)与 SECRET_KEY,缺则原生 MessageBox 报清楚,绝不静默闪退

启动:run_local.ps1 会用 `pythonw local_supervisor.pyw` 拉起它(detached);也可直接双击本文件。
不引入 Electron/Tauri/Rust/Node —— 见 references/client-exe-development.md「为何不做 exe」一节。
"""
from __future__ import annotations

import os
import sys
import time
import socket
import threading
import subprocess
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 5000
APP_URL = f"http://{HOST}:{PORT}/dashboard/"
HEALTH_URL = f"http://{HOST}:{PORT}/"
WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if WINDOWS else 0
_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_VALUE = "BwicarusLocal"
_MUTEX_NAME = "Local\\bwicarus-local-supervisor"   # per-session,单用户够用且不需提权
_ERROR_ALREADY_EXISTS = 183
_FAST_FAIL_SECS = 10          # 存活不到这么久即视为"起不来"
_FAST_FAIL_LIMIT = 5          # 连续快失败这么多次就熔断停手
_STABLE_SECS = 30             # 稳定存活超过这么久才把退避归零
_SYNC_TASK = "Obsidian Headless Sync"   # PC 登录自启的 obsidian-headless sync 计划任务
_SYNC_CHECK_TICKS = 150       # 每 ~5min(150×2s)看护一次 sync 任务

DEPLOY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT") or DEPLOY_DIR.parent)
ENV_FILE = PROJECT_ROOT / ".env.local"
DATA_DIR = Path(os.environ.get("WEBAPP_DATA") or (PROJECT_ROOT / "webapp-data"))
LOG_FILE = DATA_DIR / "local_supervisor.log"
FLASK_LOG = DATA_DIR / "local_flask.log"

try:
    import requests
except ImportError:
    requests = None


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _msgbox(title: str, text: str) -> None:
    """原生 MessageBox(pythonw 无控制台时唯一能让用户看到的报错途径)。"""
    if not WINDOWS:
        _log(f"{title}: {text}")
        return
    try:
        import ctypes
        ctypes.WinDLL("user32", use_last_error=True).MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        _log(f"{title}: {text}")


# ── .env.local 加载(跟 run_local.ps1 第 2 步一致)──────────────────────────────
def load_env() -> None:
    if not ENV_FILE.exists():
        _log(f"⚠ 缺 {ENV_FILE},请先跑一次 run_local.ps1 生成它")
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())   # PS 已设过为准,不覆盖


# ── Chrome --app 独立窗口(复刻 run_local.ps1 第 6 步)─────────────────────────
def _find_chrome() -> str | None:
    if not WINDOWS:
        return None
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LocalAppData", "")
    for p in (
        rf"{pf}\Google\Chrome\Application\chrome.exe",
        rf"{pf86}\Google\Chrome\Application\chrome.exe",
        rf"{lad}\Google\Chrome\Application\chrome.exe",
    ):
        if p and Path(p).exists():
            return p
    return None


def open_app_window() -> None:
    chrome = _find_chrome()
    try:
        if chrome:
            subprocess.Popen([chrome, f"--app={APP_URL}"], close_fds=True)
            _log(f"打开 Chrome --app 窗口 {APP_URL}")
        else:
            import webbrowser
            webbrowser.open(HEALTH_URL)
            _log("无 Chrome,退默认浏览器")
    except Exception as e:
        _log(f"开窗口失败:{e}")


# ── 单实例锁(Windows 命名 mutex;use_last_error 保证读到正确 last-error)─────────
_mutex_handle = None


def acquire_single_instance() -> bool:
    """True=本进程是唯一实例;False=已有实例在跑。"""
    global _mutex_handle
    if not WINDOWS:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        _mutex_handle = k32.CreateMutexW(None, True, _MUTEX_NAME)
        return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
    except Exception as e:
        _log(f"单实例锁异常(放行启动):{e}")
        return True


# ── Job Object:守护进程死则把 Flask 子进程一起带走(根除残留占端口)──────────────
_job_handle = None


def setup_job_object() -> None:
    """创建带 KILL_ON_JOB_CLOSE 的 job 并保留句柄(进程级生命周期);失败则降级(靠熔断兜底)。"""
    global _job_handle
    if not WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),       # ULONG_PTR
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IOC),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # ExtendedLimitInformation
        if not ok:
            return
        _job_handle = job
        _log("Job Object 就绪(守护退出将带走 Flask)")
    except Exception as e:
        _log(f"Job Object 创建失败(降级,靠熔断兜底):{e}")


def _assign_to_job(pid: int) -> None:
    if not (WINDOWS and _job_handle):
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not h:
            return
        try:
            k32.AssignProcessToJobObject(_job_handle, h)
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass


# ── 开机自启(源码模式:pythonw + 本脚本绝对路径)──────────────────────────────
def _pythonw_exe() -> str:
    exe = sys.executable or "pythonw.exe"
    cand = exe.replace("python.exe", "pythonw.exe")
    return cand if Path(cand).exists() else exe


def _autostart_cmd() -> str:
    return f'"{_pythonw_exe()}" "{Path(__file__).resolve()}"'


def autostart_enabled() -> bool:
    if not WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, _AUTOSTART_VALUE)
            return bool(v)
    except (FileNotFoundError, OSError):
        return False


def autostart_set(enable: bool) -> None:
    if not WINDOWS:
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
            if enable:
                winreg.SetValueEx(k, _AUTOSTART_VALUE, 0, winreg.REG_SZ, _autostart_cmd())
                _log("✓ 开机自启已开")
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_VALUE)
                    _log("开机自启已关")
                except FileNotFoundError:
                    pass
    except Exception as e:
        _log(f"写自启注册表失败:{e}")


# ── Obsidian 同步看护(确保登录自启的 headless sync 任务没被禁/在跑)──────────────
def ensure_obsidian_sync() -> None:
    """看护 PC 的「Obsidian Headless Sync」计划任务:被禁→启用,没跑→启动。

    用 Get-ScheduledTask 的 State 枚举(Disabled/Ready/Running,语言中立),
    避免解析中文 schtasks 输出。任务是单一 owner,不会双开。防住「任务被禁没人发现」。
    """
    if not WINDOWS:
        return
    try:
        st = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-ScheduledTask -TaskName '{_SYNC_TASK}' -ErrorAction SilentlyContinue).State"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=20)
        state = (st.stdout or "").strip()
        if not state:
            return  # 没这个任务(没装 headless sync)→ 不管
        if state == "Running":
            return
        cmd = ""
        if state == "Disabled":
            cmd += f"Enable-ScheduledTask -TaskName '{_SYNC_TASK}' | Out-Null; "
        cmd += f"Start-ScheduledTask -TaskName '{_SYNC_TASK}'"
        subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=20)
        _log(f"Obsidian sync 看护:state={state} → 已启用/启动")
    except Exception as e:
        _log(f"Obsidian sync 看护失败:{e}")


# ── Flask 子进程守护 ──────────────────────────────────────────────────────────
class Supervisor:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.stop_event = threading.Event()
        self.auto_reload = True          # 代码变更自动重启(治「改端点要手动重启」)
        self.icon = None                 # 托盘 icon 引用(给气泡通知用)
        self._restart_lock = threading.Lock()
        self._py_mtimes: dict[str, float] = {}
        self._flog = None                # FLASK_LOG 文件句柄(复用,不泄漏)
        self._last_up = 0.0              # 最近一次 spawn 的时刻
        self._fast_fail = 0              # 连续快失败计数
        self._halted = False             # 熔断:暂停自动重启,等用户手动
        self.keep_sync = True            # 看护 Obsidian headless sync 任务(默认开)
        self._sync_tick = 0              # sync 看护节流计数

    # —— 端口探测 ——
    @staticmethod
    def _port_in_use() -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            return s.connect_ex((HOST, PORT)) == 0
        except Exception:
            return False
        finally:
            s.close()

    # —— 起/停/杀 ——
    def _spawn(self) -> None:
        try:
            if self._flog:
                self._flog.close()
        except Exception:
            pass
        self._flog = open(FLASK_LOG, "a", encoding="utf-8", buffering=1)
        self._flog.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 Flask =====\n")
        self.proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=str(DEPLOY_DIR),
            env=os.environ.copy(),
            stdout=self._flog, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
        self._last_up = time.time()
        _assign_to_job(self.proc.pid)
        _log(f"Flask 已启动 pid={self.proc.pid}")

    def _kill(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            try:
                if WINDOWS:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   creationflags=CREATE_NO_WINDOW,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    p.terminate()
                p.wait(timeout=10)
            except Exception as e:
                _log(f"杀 Flask 失败:{e}")
                try:
                    p.kill()
                except Exception:
                    pass

    def _wait_healthy(self, timeout: float = 25.0) -> bool:
        if requests is None:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                return False  # 进程已退出,别白等
            try:
                requests.get(HEALTH_URL, timeout=2)
                return True   # 任何 HTTP 响应(含 302/401)都算活
            except Exception:
                time.sleep(0.5)
        return False

    def restart(self, reason: str = "", manual: bool = False) -> None:
        with self._restart_lock:
            if manual:
                self._halted = False
                self._fast_fail = 0
            _log(f"重启 Flask{(' — ' + reason) if reason else ''}")
            self._kill()
            if self._port_in_use():
                _log("⚠ 端口 5000 仍被占用(可能有残留/另一个实例),等 1s 再起")
                time.sleep(1.0)
            self._spawn()
            self._snapshot_mtimes()
            if self._wait_healthy():
                _log("✓ Flask 健康")
            else:
                _log("⚠ Flask 启动后健康检查未通过(看 local_flask.log)")

    def _notify(self, title: str, msg: str) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(msg, title)
                return
            except Exception:
                pass
        _msgbox(title, msg)

    # —— 代码改动检测 ——
    def _iter_py(self):
        try:
            for f in DEPLOY_DIR.glob("*.py"):
                yield f
        except Exception:
            pass

    def _snapshot_mtimes(self) -> None:
        self._py_mtimes = {str(f): f.stat().st_mtime for f in self._iter_py() if f.exists()}

    def _code_changed(self) -> str | None:
        for f in self._iter_py():
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if self._py_mtimes.get(str(f)) != m:
                return f.name
        return None

    # —— 守护主循环(后台线程)——
    def monitor_loop(self) -> None:
        backoff = 1.0
        self.restart("初次启动", manual=True)
        if self.keep_sync:
            threading.Thread(target=ensure_obsidian_sync, daemon=True).start()   # 开机即看护一次
        while not self.stop_event.is_set():
            time.sleep(2.0)
            if self.stop_event.is_set():
                break
            # 心跳(2026-09-03 实锤:守护自己卡死,日志停在 17:07,端口没人管):每轮写一次时间戳,
            # 外部看护(scripts/windows_server_watchdog.ps1)见它超过 3 分钟没动就杀掉重拉。
            try:
                (DATA_DIR / "local_supervisor.heartbeat").write_text(str(int(time.time())), encoding="utf-8")
            except Exception:
                pass
            # 周期看护 Obsidian sync 任务(没被禁/在跑),~5min 一次,防静默死亡
            self._sync_tick += 1
            if self.keep_sync and self._sync_tick % _SYNC_CHECK_TICKS == 0:
                threading.Thread(target=ensure_obsidian_sync, daemon=True).start()
            if self._halted:
                continue                       # 已熔断,等用户手动「重启服务」
            if self._restart_lock.locked():
                continue                       # 正在(手动)重启,本轮别插手,免得误判二次重启
            p = self.proc
            # 1) 崩溃 → 退避拉起 + 连续快失败熔断
            if p is not None and p.poll() is not None:
                uptime = time.time() - self._last_up
                self._fast_fail = self._fast_fail + 1 if uptime < _FAST_FAIL_SECS else 0
                if self._fast_fail >= _FAST_FAIL_LIMIT:
                    self._halted = True
                    msg = ("Flask 反复启动失败,已暂停自动重启。常见原因:端口 5000 被占用 / "
                           "缺 SECRET_KEY / 代码报错。请看 local_flask.log,修好后在托盘点「重启服务」。")
                    _log("熔断:" + msg)
                    self._notify("本地实例启动失败", msg)
                    continue
                _log(f"检测到 Flask 退出(code={p.returncode}),{backoff:.0f}s 后拉起"
                     f"(连续快失败 {self._fast_fail}/{_FAST_FAIL_LIMIT})")
                time.sleep(backoff)
                self.restart("崩溃自动拉起")
                backoff = min(backoff * 2, 30.0)
                continue
            # 2) 稳定存活足够久 → 退避/计数归零
            if time.time() - self._last_up > _STABLE_SECS:
                backoff = 1.0
                self._fast_fail = 0
            # 3) 代码变更自动重启(debounce 1.5s,避免连写多文件多次重启)
            if self.auto_reload:
                changed = self._code_changed()
                if changed:
                    time.sleep(1.5)
                    self.restart(f"代码变更 {changed}")

    def shutdown(self) -> None:
        self.stop_event.set()
        self._kill()
        try:
            if self._flog:
                self._flog.close()
        except Exception:
            pass
        _log("守护退出")


# ── 托盘 ──────────────────────────────────────────────────────────────────────
def _icon_image():
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, size - 4, size - 4), radius=12, fill=(34, 139, 87, 255))  # 本地实例=绿
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except Exception:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), "L", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - 2), "L",
           fill=(255, 255, 255, 255), font=font)
    return img


def run_tray(sup: Supervisor) -> None:
    import pystray
    from pystray import MenuItem as Item, Menu

    def _open(icon, item): open_app_window()
    def _restart(icon, item):
        threading.Thread(target=sup.restart, args=("托盘手动",), kwargs={"manual": True}, daemon=True).start()

    def _open_log(icon, item):
        try:
            if WINDOWS:
                os.startfile(str(FLASK_LOG))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(FLASK_LOG)])
        except Exception as e:
            _log(f"打开日志失败:{e}")

    def _toggle_autostart(icon, item):
        autostart_set(not autostart_enabled())
        icon.update_menu()

    def _toggle_reload(icon, item):
        sup.auto_reload = not sup.auto_reload
        if sup.auto_reload:
            sup._snapshot_mtimes()
        _log(f"代码变更自动重启:{'开' if sup.auto_reload else '关'}")
        icon.update_menu()

    def _toggle_sync(icon, item):
        sup.keep_sync = not sup.keep_sync
        if sup.keep_sync:
            threading.Thread(target=ensure_obsidian_sync, daemon=True).start()
        _log(f"看护 Obsidian 同步:{'开' if sup.keep_sync else '关'}")
        icon.update_menu()

    def _quit(icon, item):
        sup.shutdown()
        icon.stop()

    menu = Menu(
        Item("打开 webapp 窗口", _open, default=True),
        Item("重启服务", _restart),
        Item("打开日志", _open_log),
        Menu.SEPARATOR,
        Item("开机自启", _toggle_autostart, checked=lambda i: autostart_enabled()),
        Item("代码变更自动重启", _toggle_reload, checked=lambda i: sup.auto_reload),
        Item("保持 Obsidian 同步", _toggle_sync, checked=lambda i: sup.keep_sync),
        Menu.SEPARATOR,
        Item("退出", _quit),
    )
    icon = pystray.Icon("bwicarus-local", _icon_image(), "bwicarus 本地实例", menu)
    sup.icon = icon
    icon.run()  # 阻塞在主线程(pystray 在主线程装 Win32 消息泵)


# ── 预检 ──────────────────────────────────────────────────────────────────────
def _preflight() -> bool:
    """依赖 + SECRET_KEY 自检。绕过 run_local.ps1 直接双击 / HKCU 自启时,这是唯一的护栏。"""
    missing = []
    if requests is None:
        missing.append("requests")
    for mod in ("pystray", "PIL"):
        try:
            __import__(mod)
        except ImportError:
            missing.append("pillow" if mod == "PIL" else mod)
    if missing:
        _msgbox("缺少依赖", "本地守护需要:" + "、".join(missing) +
                "\n请先运行 run_local.ps1(会自动装),或手动:\n  pip install " + " ".join(missing))
        return False
    if not os.environ.get("SECRET_KEY"):
        _msgbox("缺少配置", f"未找到 SECRET_KEY。\n请先运行 run_local.ps1 生成 {ENV_FILE}。")
        return False
    return True


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    load_env()
    if not acquire_single_instance():
        _msgbox("已在运行", "bwicarus 本地实例已经在运行(托盘有绿色 L 图标)。")
        return 0
    if not _preflight():
        return 1
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    setup_job_object()
    _log(f"启动守护;项目={PROJECT_ROOT} 部署={DEPLOY_DIR} 数据={DATA_DIR}")

    sup = Supervisor()
    threading.Thread(target=sup.monitor_loop, daemon=True).start()

    # 等首个健康后开一次窗口(只开一次;之后崩溃重启不再弹窗,用户原窗口会自动重连)
    def _first_open():
        if requests is None:
            return
        for _ in range(60):
            try:
                requests.get(HEALTH_URL, timeout=2)
                open_app_window()
                return
            except Exception:
                time.sleep(0.5)
    threading.Thread(target=_first_open, daemon=True).start()

    try:
        run_tray(sup)
    except KeyboardInterrupt:
        pass
    finally:
        sup.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
